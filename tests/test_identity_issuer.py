"""Verify the 0038 trust boundary using restricted PostgreSQL roles and real HTTP handlers."""
from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege

from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from tools.dev_db import connection, migrate
from test_identity_lifecycle import lapi, login, mails, token_of  # noqa: F401
from test_processor_payments import disposable
from test_proposal_drafts import client, world, seed  # noqa: F401
from test_identity_sessions import ids  # noqa: F401

PREFIX = '/api/v1'
RUNTIME = ('lv_app', 'lv_seed', 'lv_ingest', 'lv_worker')
ISSUER_FUNCTIONS = {
    'login_prepare', 'login_finish', 'reauth_prepare', 'reauth_finish', 'token_issue',
    'token_consume', 'member_api', 'member_list', 'switch_tenant', 'logout',
    'revoke_my_sessions', 'session_context',
}


def test_service_route_boundary_without_db():
    with TestClient(create_app(service_mode='api')) as api, TestClient(create_app(service_mode='issuer')) as issuer:
        assert api.post(PREFIX+'/auth/login', json={}).status_code == 404
        assert api.post(PREFIX+'/members', json={}).status_code == 404
        assert api.get(PREFIX+'/me').status_code == 401
        assert issuer.get(PREFIX+'/me').status_code == 404
        assert issuer.get(PREFIX+'/ledgers').status_code == 404
        assert issuer.post(PREFIX+'/auth/login', json={}).status_code == 422
        assert issuer.post(PREFIX+'/auth/password-reset/confirm', json={}).status_code == 422
        assert issuer.get(PREFIX+'/health').status_code == 404
        assert issuer.get('/health').status_code == 200


@pytest.mark.parametrize(('mode','setting'), [('issuer','LV_DATABASE_URL_FILE'),('api','LV_AUTHN_DATABASE_URL_FILE')])
def test_services_refuse_cross_mounted_credentials(monkeypatch, mode, setting):
    monkeypatch.setenv(setting, '/not-read')
    with pytest.raises(ValueError, match='must not receive'):
        create_app(service_mode=mode)


def test_runtime_grants_change_on_upgrade_and_restore_on_downgrade():
    with disposable('0037_restore_interlock') as db:
        def permissions():
            with connection(database=db) as admin:
                return {role:{r[0] for r in admin.execute("""
                    SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname='lv_identity' AND has_function_privilege(%s,p.oid,'EXECUTE')
                """, (role,))} for role in (*RUNTIME, 'lv_authn')}
        before = permissions()
        assert {'login_prepare','login_finish','reauth_finish','member_api','token_issue','token_consume'} <= before['lv_app']
        migrate(db, '0038_identity_issuer')
        after = permissions()
        assert after['lv_authn'] == ISSUER_FUNCTIONS
        assert after['lv_app'] == {'session_context', 'audit_page'}
        assert not any(after[r] for r in RUNTIME[1:])
        with connection('lv_authn', db) as issuer:
            with pytest.raises(InsufficientPrivilege):
                issuer.execute('SELECT count(*) FROM lv.journal_line')
        migrate(db, '0037_restore_interlock', downgrade=True, disposable=True)
        assert permissions() == before


@pytest.mark.parametrize('role', RUNTIME)
def test_direct_runtime_cannot_call_identity_verdicts(database, role):
    with connection(role, database, autocommit=True) as c:
        for statement in (
            'SET ROLE lv_authn',
            "SELECT lv_identity.login_finish(NULL,true,NULL,NULL,'synthetic','probe')",
            "SELECT lv_identity.reauth_finish(NULL,true,'synthetic','probe')",
            "SELECT lv_identity.token_issue('RESET_PASSWORD','synthetic@example.invalid',NULL,'synthetic','probe')",
            "SELECT lv_identity.token_consume('INVITATION',NULL,NULL,'synthetic','probe')",
            "SELECT lv_identity.member_api(NULL,'INVITE',NULL,'{}',NULL,NULL,NULL,NULL,NULL,'synthetic','probe')",
        ):
            with pytest.raises(InsufficientPrivilege):
                c.execute(statement)


def test_split_issuer_login_restore_and_logout(database, ids):
    settings = AuthSettings()
    api = create_app(settings, service_mode='api', connect=lambda: connection('lv_app', database))
    issuer = create_app(settings, service_mode='issuer', issuer_connect=lambda: connection('lv_authn', database))
    try:
        with TestClient(api, base_url='https://testserver') as a, TestClient(issuer, base_url='https://testserver') as i:
            user = ids['users']['analyst']
            sign_in = i.post(PREFIX+'/auth/login', json={'email': user['email'], 'password': ids['passwords']['analyst']})
            assert sign_in.status_code == 200
            a.cookies.update(i.cookies)
            assert a.get(PREFIX+'/me').status_code == 200
            assert i.post(PREFIX+'/auth/logout', headers={'X-CSRF-Token': sign_in.json()['csrf_token']}).status_code == 204
            assert a.get(PREFIX+'/me').status_code == 401
    finally:
        api.state.pool.close()


def test_four_reset_confirmations_accept_exactly_once(database, lapi, world):
    user = world['users']['analyst']
    old, ok = login(lapi, user['email'], user['password'])
    assert ok.status_code == 200
    assert client(lapi).post(PREFIX+'/auth/password-reset', json={'email': user['email']}).status_code == 202
    nonce = token_of(mails(lapi, user['email'])[-1])
    gate = threading.Barrier(4, timeout=10)
    def submit(_):
        gate.wait()
        return client(lapi).post(PREFIX+'/auth/password-reset/confirm', json={'token': nonce,'new_password':uuid4().hex}).status_code
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sorted(executor.map(submit, range(4))) == [200,400,400,400]
    assert old.get(PREFIX+'/me').status_code == 401


def test_populated_0029_upgrade_preserves_all_original_tables():
    """Exercise the installed schema revision using a new synthetic tenant world."""
    from tools.verify_recovery import fingerprints

    with disposable('0029_account_patch') as db:
        with connection('lv_app', db, autocommit=True) as app, connection('lv_seed', db, autocommit=True) as seeder:
            world.__wrapped__(db, app, seeder)
        with connection(database=db) as c:
            before = fingerprints(c)
        before.pop('lv_meta.alembic_version')
        assert len(before) == 65
        assert before['lv_identity.app_user']['rows'] > 0
        assert before['lv.journal_line']['rows'] > 0

        migrate(db, '0038_identity_issuer')
        with connection(database=db) as c:
            after = fingerprints(c)
            assert c.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0038_identity_issuer',)
        assert {name: after[name] for name in before} == before
        with connection('lv_app', db) as app:
            assert app.execute('SELECT lv.recovery_state()').fetchone() == ('BOUND',)
