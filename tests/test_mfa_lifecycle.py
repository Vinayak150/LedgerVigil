"""Reset cooling-off and BOOKS step-up gates using synthetic accounts and real PostgreSQL."""
import base64
import time

import pytest
from psycopg.types.json import Jsonb

from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.totp import at_step
from tools.dev_db import connection
from test_mfa import api, database, seed, world, key_file  # noqa: F401
from test_proposal_drafts import signed_in

PATH = '/api/v1/auth/mfa'


@pytest.fixture
def enrolled(api, world, key_file, monkeypatch):
    c = signed_in(api, world, 'admin')
    password = world['users']['admin']['password']
    assert c.post('/api/v1/auth/reauthenticate', json={'password': password}).status_code == 200
    r = c.post(PATH, json={'action': 'START'})
    assert r.status_code == 200
    secret = base64.b32decode(r.json()['setup_key'])
    clock = [int(time.time()) // 30]
    from ledgervigil.auth import mfa
    monkeypatch.setattr(mfa, 'time', type('Clock', (), {'time': staticmethod(lambda: clock[0] * 30)}))
    assert c.post(PATH, json={'action': 'CONFIRM', 'code': at_step(secret, clock[0])}).status_code == 200

    def action(name):
        clock[0] += 1
        return c.post(PATH, json={'action': name, 'code': at_step(secret, clock[0])})
    return c, action


def test_reset_cannot_skip_cooling_and_revokes_all_sessions(database, api, world, enrolled):
    c, action = enrolled
    assert action('RESET_REQUEST').status_code == 200
    assert c.get(PATH).json()['reset_after'] is not None
    r = action('RESET_COMPLETE')
    assert (r.status_code, r.json()['code']) == (409, 'COOLING_OFF')
    assert action('RESET_CANCEL').status_code == 200
    assert c.get(PATH).json()['reset_after'] is None
    assert action('RESET_REQUEST').status_code == 200
    # Test-only owner operation represents elapsed wall-clock time; no HTTP shortcut exists.
    with connection(database=database) as db:
        db.execute("UPDATE lv_identity.mfa_factor SET reset_after=statement_timestamp()-interval '1 second' WHERE user_id=%s",
                   (world['users']['admin']['id'],))
    assert action('RESET_COMPLETE').status_code == 200
    assert c.get('/api/v1/me').status_code == 401
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv_identity.mfa_factor').fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM lv_identity.session WHERE user_id=%s AND revoked_at IS NULL",
                          (world['users']['admin']['id'],)).fetchone() == (0,)


def test_books_privilege_refused_in_api_and_database_without_fresh_mfa(database, api, world, enrolled):
    c, action = enrolled
    # Synthetic BOOKS metadata only; no BOOKS posting or production gate is enabled.
    with connection(database=database) as db:
        db.execute("""INSERT INTO lv.ledger(tenant_id,name,purpose,source_kind,currency,currency_exponent)
            VALUES(%s,'SYNTHETIC MFA gate','BOOKS','NATIVE','INR',2)""", (world['A'],))
        db.execute("UPDATE lv_identity.session SET mfa_verified_at=statement_timestamp()-interval '16 minutes'")
    aid = world['l1']['chart']['BANK_OPERATING']
    r = c.patch('/api/v1/accounts/'+str(aid), json={'name': 'SYNTHETIC guarded edit'}, headers={'If-Match': '"v1"'})
    assert (r.status_code, r.json()['code']) == (403, 'MFA_REQUIRED')
    # Bypass HTTP entirely using the ordinary database login; it must still refuse.
    token = token_hash(c.cookies.get(api.state.auth_settings.cookie_name))
    with connection('lv_app', database) as db:
        result = db.execute('SELECT lv.edit_account(%s,%s,%s,%s,NULL,%s,%s)',
                            (token, aid, '"v1"', Jsonb({'name': 'SYNTHETIC guarded edit'}), 'test', 'mfa-raw')).fetchone()[0]
        assert result['status'] == 403
    assert action('CHALLENGE').status_code == 200
    assert c.get('/api/v1/me').json()['mfa_required'] is False
    assert c.patch('/api/v1/accounts/'+str(aid), json={'name': 'SYNTHETIC guarded edit'},
                   headers={'If-Match': '"v1"'}).status_code == 200
    assert action('RESET_REQUEST').status_code == 200
    assert c.patch('/api/v1/accounts/'+str(aid), json={'name': 'SYNTHETIC cooling edit'},
                   headers={'If-Match': '"v2"'}).status_code == 403
