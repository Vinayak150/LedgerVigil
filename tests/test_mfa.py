"""Synthetic PostgreSQL and HTTP checks for issuer-owned authenticator state."""
import base64
from concurrent.futures import ThreadPoolExecutor
import secrets
import time

from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege
import pytest

from ledgervigil.app import create_app
from ledgervigil.auth.totp import at_step
from tools.dev_db import connection
from detection_support import database  # noqa: F401
from test_proposal_drafts import api, seed, signed_in, world  # noqa: F401

PATH = '/api/v1/auth/mfa'


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    key = tmp_path / 'synthetic-authenticator-key'
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    monkeypatch.setenv('LV_MFA_KEY_FILE', str(key))


def test_mfa_endpoints_are_issuer_only():
    with TestClient(create_app(service_mode='api')) as app, TestClient(create_app(service_mode='issuer')) as issuer:
        assert app.get(PATH).status_code == 404
        assert app.post(PATH, json={}).status_code == 404
        assert issuer.get(PATH).status_code == 401


def test_setup_challenge_and_single_use_recovery(api, world, key_file, monkeypatch):
    client = signed_in(api, world, 'analyst')
    password = world['users']['analyst']['password']

    def post(action, code=None):
        return client.post(PATH, json={'action': action, 'code': code})

    assert client.get(PATH).json() == {'configured': True, 'enrolled': False, 'verified_at': None}
    assert post('START').status_code == 403
    assert client.post('/api/v1/auth/reauthenticate', json={'password': password}).status_code == 200
    setup = post('START')
    assert setup.status_code == 200
    assert setup.headers['cache-control'] == 'no-store'
    secret = base64.b32decode(setup.json()['setup_key'])
    step = int(time.time()) // 30
    from ledgervigil.auth import mfa
    monkeypatch.setattr(mfa, 'time', type('Clock', (), {'time': staticmethod(lambda: step * 30)}))
    confirmation = post('CONFIRM', at_step(secret, step))
    assert confirmation.status_code == 200
    codes = confirmation.json()['recovery_codes']
    assert len(set(codes)) == 10
    assert client.get(PATH).json()['enrolled']
    assert post('CHALLENGE', at_step(secret, step)).status_code == 401
    step += 1
    assert post('CHALLENGE', at_step(secret, step)).status_code == 200
    assert post('START').status_code == 409
    with ThreadPoolExecutor(max_workers=4) as threads:
        results = list(threads.map(lambda _: post('RECOVERY', codes[0]).status_code, range(4)))
    assert sorted(results) == [200, 401, 401, 401]
    other_session = signed_in(api, world, 'analyst')
    assert other_session.get(PATH).json() == {'configured': True, 'enrolled': True, 'verified_at': None}


@pytest.mark.parametrize('role', ['lv_app', 'lv_worker', 'lv_seed', 'lv_ingest'])
def test_runtime_cannot_call_mfa_or_read_factor(database, role):
    with connection(role, database) as c:
        with pytest.raises(InsufficientPrivilege):
            c.execute("SELECT lv_identity.mfa_prepare(NULL,'STATUS','test','test')")
        c.rollback()
        with pytest.raises(InsufficientPrivilege):
            c.execute('SELECT * FROM lv_identity.mfa_factor')
