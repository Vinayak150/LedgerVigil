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
    c = signed_in(api, world, 'admin', with_mfa=False)
    password = world['users']['admin']['password']
    assert c.post('/api/v1/auth/reauthenticate', json={'password': password}).status_code == 200
    r = c.post(PATH, json={'action': 'START'})
    assert r.status_code == 200
    secret = base64.b32decode(r.json()['setup_key'])
    clock = [int(time.time()) // 30]
    from ledgervigil.auth import mfa
    monkeypatch.setattr(mfa, 'time', type('Clock', (), {'time': staticmethod(lambda: clock[0] * 30)}))
    confirmed = c.post(PATH, json={'action': 'CONFIRM', 'code': at_step(secret, clock[0])})
    assert confirmed.status_code == 200
    c.synthetic_recovery_codes = confirmed.json()['recovery_codes']

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
    with connection(database=database) as db:
        version = db.execute('SELECT version FROM lv.account WHERE id=%s', (aid,)).fetchone()[0]
    match = f'"v{version}"'
    r = c.patch('/api/v1/accounts/'+str(aid), json={'name': 'SYNTHETIC guarded edit'}, headers={'If-Match': match})
    assert (r.status_code, r.json()['code']) == (403, 'MFA_REQUIRED')
    # Bypass HTTP entirely using the ordinary database login; it must still refuse.
    token = token_hash(c.cookies.get(api.state.auth_settings.cookie_name))
    with connection('lv_app', database) as db:
        result = db.execute('SELECT lv.edit_account(%s,%s,%s,%s,NULL,%s,%s)',
                            (token, aid, match, Jsonb({'name': 'SYNTHETIC guarded edit'}), 'test', 'mfa-raw')).fetchone()[0]
        assert result['status'] == 403
    assert action('CHALLENGE').status_code == 200
    assert c.get('/api/v1/me').json()['mfa_required'] is False
    assert c.patch('/api/v1/accounts/'+str(aid), json={'name': 'SYNTHETIC guarded edit'},
                   headers={'If-Match': match}).status_code == 200
    assert action('RESET_REQUEST').status_code == 200
    assert c.patch('/api/v1/accounts/'+str(aid), json={'name': 'SYNTHETIC cooling edit'},
                   headers={'If-Match': '"v2"'}).status_code == 403


@pytest.mark.parametrize('mfa_first', [True, False])
def test_mfa_and_session_switch_lock_orders(database, api, world, enrolled, mfa_first):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    import secrets

    c, _ = enrolled
    token = token_hash(c.cookies.get(api.state.auth_settings.cookie_name))
    pid = Queue()

    def switch():
        with connection('lv_authn', database) as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            pid.put(db.info.backend_pid)
            return db.execute('SELECT lv_identity.switch_tenant(%s,%s,%s,%s,%s)',
                              (token, world['A'], secrets.token_bytes(32), 'race', 'switch')).fetchone()[0]

    def prepare():
        with connection('lv_authn', database) as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            pid.put(db.info.backend_pid)
            return db.execute('SELECT lv_identity.mfa_prepare(%s,%s,%s,%s)',
                              (token, 'CHALLENGE', 'race', 'mfa')).fetchone()[0]

    def wait_for_transaction_lock(backend):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with connection(database=database) as observer:
                blocked = observer.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                                           (backend,)).fetchone()[0]
            if blocked:
                return
            time.sleep(.02)
        raise AssertionError('race did not reach the controlled lock wait')

    with ThreadPoolExecutor(1) as pool:
        with connection(database=database) as holder:
            holder.execute("SET LOCAL statement_timeout='5s'")
            if mfa_first:
                prepared = holder.execute('SELECT lv_identity.mfa_prepare(%s,%s,%s,%s)',
                                          (token, 'CHALLENGE', 'race-holder', 'mfa')).fetchone()[0]
                assert prepared['status'] == 'OK'
                future = pool.submit(switch)
            else:
                assert holder.execute('SELECT lv_identity.switch_tenant(%s,%s,%s,%s,%s)',
                                      (token, world['A'], secrets.token_bytes(32), 'race-holder', 'switch')).fetchone()[0]['valid']
                future = pool.submit(prepare)
            wait_for_transaction_lock(pid.get(timeout=3))
        result = future.result(timeout=6)
    assert (result.get('valid') if mfa_first else result['status'] == 'INVALID')


def test_reset_includes_session_replacement_committed_while_waiting(database, api, world, enrolled):
    from concurrent.futures import ThreadPoolExecutor
    import secrets

    first, action = enrolled
    second = signed_in(api, world, 'admin', with_mfa=False)
    token = token_hash(second.cookies.get(api.state.auth_settings.cookie_name))
    assert action('RESET_REQUEST').status_code == 200
    with connection(database=database) as db:
        db.execute("UPDATE lv_identity.mfa_factor SET reset_after=statement_timestamp()-interval '1 second'")
    with ThreadPoolExecutor(1) as pool:
        with connection(database=database) as holder:
            # Actual switch function holds the shared user mutex until commit.
            assert holder.execute('SELECT lv_identity.switch_tenant(%s,%s,%s,%s,%s)',
                                  (token, world['A'], secrets.token_bytes(32), 'race', 'switch')).fetchone()[0]['valid']
            future = pool.submit(action, 'RESET_COMPLETE')
            deadline = time.monotonic() + 4
            blocked = False
            while time.monotonic() < deadline:
                with connection(database=database) as observer:
                    blocked = observer.execute("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                        WHERE datname=current_database() AND wait_event_type='Lock'
                        AND strpos(query,'mfa_prepare')>0)""").fetchone()[0]
                if blocked:
                    break
                time.sleep(.02)
            assert blocked, 'reset did not reach controlled serialization point'
        assert future.result(timeout=6).status_code == 200
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv_identity.session WHERE revoked_at IS NULL').fetchone() == (0,)
        assert db.execute('SELECT count(*) FROM lv_identity.mfa_factor').fetchone() == (0,)



def test_reset_recovery_proofs_are_consumed(database, enrolled):
    c, _ = enrolled
    codes = c.synthetic_recovery_codes
    assert c.post(PATH, json={'action': 'RESET_REQUEST', 'code': codes[0]}).status_code == 200
    assert c.post(PATH, json={'action': 'RESET_CANCEL', 'code': codes[1]}).status_code == 200
    assert c.post(PATH, json={'action': 'RESET_REQUEST', 'code': codes[0]}).status_code == 401
