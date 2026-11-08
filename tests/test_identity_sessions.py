"""Identity boundary (migration 0007): login, sessions, CSRF, tenant selection, permissions — through the HTTP app and real
PostgreSQL. Expected behaviour is stated from ADR-015, ADR-027 and Stage 2B Part 5 / Family 1, not from the code:
- generic 401 UNAUTHENTICATED for every refused login; 429 RATE_LIMITED with Retry-After; no account enumeration;
- session cookie HttpOnly, Secure, SameSite=Lax, rotated on login and privilege change; 30-min idle / 12-h absolute;
- unsafe methods need X-CSRF-Token bound to the session; tenant comes only from the session; switching = new session;
- 404 for a tenant the user is not an active member of; roles → permissions per 2B §5.1; administrator ≠ review/execute.
All identities are SYNTHETIC and created per test with random passwords.
"""
import hashlib
import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated
from uuid import uuid4, uuid7

import psycopg
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from psycopg import sql

from tools.dev_db import connection, migrate
from tools.identity_admin import create_tenant, create_user, grant, owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.boundary import PERMISSIONS, ROLE_PERMISSIONS, RequestContext, require_permission
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.pool import ConnectionPool, RuntimeRoleError
from ledgervigil.auth.settings import AuthSettings, SettingsError, load_settings

ROOT = Path(__file__).resolve().parents[1]
COOKIE = '__Host-lv_session'
# Stage 2B §5.1, restated independently of the implementation.
EXPECTED_MATRIX = {
    'ANALYST': {'read', 'propose', 'source.import'},
    'CONTROLLER': {'read', 'review', 'execute', 'regression', 'evidence.export', 'audit.read'},
    'AUTOMATION_DEVELOPER': {'read', 'propose', 'source.import', 'regression', 'evidence.export'},
    'ADMINISTRATOR': {'read', 'ledger.bootstrap', 'audit.read', 'administer'},
}
LV_TABLES = ('tenant', 'ledger', 'account', 'account_role_binding', 'party', 'fiscal_period', 'currency', 'service_principal', 'ledger_counter',
             'commit_batch', 'document', 'document_version', 'document_current', 'journal_entry', 'journal_line', 'ledger_event',
             'account_balance_current', 'party_balance_current', 'source_file', 'import_batch', 'source_record', 'staged_document',
             'staged_statement_item', 'processor_payment_identity', 'payment_current', 'processor_settlement_identity', 'settlement_current',
             'settlement_inclusion', 'settlement_inclusion_current')


# ------------------------------------------------------------------------------------------- fixtures
class Identities(dict):
    pass


@pytest.fixture
def ids(database):
    """Two synthetic tenants; one user per role in A; a two-tenant user; disabled, unverified and membership-less users."""
    world = Identities(passwords={}, users={})
    with owner_transaction(database) as conn:
        world['A'] = create_tenant(conn, 'SYNTHETIC tenant A')
        world['B'] = create_tenant(conn, 'SYNTHETIC tenant B')
        for name, roles, tenants, verified in (('analyst', ['ANALYST'], 'A', True), ('controller', ['CONTROLLER'], 'A', True),
                                               ('automation', ['AUTOMATION_DEVELOPER'], 'A', True), ('admin', ['ADMINISTRATOR'], 'A', True),
                                               ('multi', ['ANALYST'], 'AB', True), ('bonly', ['CONTROLLER'], 'B', True),
                                               ('unverified', ['ANALYST'], 'A', False), ('nomember', [], '', True)):
            email = f'{name}-{uuid4().hex[:8]}@test.ledgervigil.invalid'
            password = secrets.token_urlsafe(18)
            uid = create_user(conn, email, f'Synthetic {name}', password, verified)
            for t in tenants:
                grant(conn, world[t], uid, roles)
            world['users'][name] = {'email': email, 'id': str(uid)}
            world['passwords'][name] = password
    return world


_OPEN_APPS = []


@pytest.fixture(autouse=True)
def _close_pools():
    """Every application's pool is closed after its test, pass or fail, so no runtime connection outlives the test."""
    yield
    while _OPEN_APPS:
        _OPEN_APPS.pop().state.pool.close()


def make_app(database, settings=None):
    application = create_app(settings or AuthSettings(), connect=lambda: connection('lv_app', database))
    _OPEN_APPS.append(application)
    return application


@pytest.fixture
def app(database):
    application = make_app(database)
    yield application
    application.state.pool.close()


def client_for(application, source=None, base_url='https://testserver'):
    # raise_server_exceptions=False: a route failure must be observed as the HTTP 500 the client really receives.
    return TestClient(application, base_url=base_url, raise_server_exceptions=False, client=(source or f'10.{secrets.randbelow(250)}.{secrets.randbelow(250)}.{secrets.randbelow(250)}', 40000))


def cookie_client(application, token):
    """A fresh client presenting exactly this session cookie (sent as a header: the cookie jar would not attach it)."""
    c = client_for(application)
    c.headers['Cookie'] = f'{COOKIE}={token}'
    return c


def login(client, ids, name, password=None, **kw):
    return client.post('/api/v1/auth/login', json={'email': ids['users'][name]['email'], 'password': password or ids['passwords'][name]}, **kw)


def signed_in(application, ids, name):
    c = client_for(application)
    r = login(c, ids, name)
    assert r.status_code == 200, r.text
    return c, r.json()


def admin_sql(database, statement, args=()):
    with connection(database=database) as admin:
        admin.execute('SET LOCAL session_replication_role = replica')      # TEST ONLY: bypass guard triggers to simulate elapsed time
        admin.execute(statement, args)


def audit(database, event_type=None):
    with connection(database=database) as admin:
        q = 'SELECT event_type, outcome, user_id::text, tenant_id::text, detail FROM lv_identity.audit_event'
        rows = admin.execute(q + (' WHERE event_type = %s' if event_type else '') + ' ORDER BY occurred_at, seq', (event_type,) if event_type else ()).fetchall()
    return rows


def lv_hashes(database):
    with connection(database=database) as admin:
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(t))).fetchall()))).encode()).hexdigest()
                for t in LV_TABLES}


# ------------------------------------------------------------------------------------------- login
def test_login_creates_a_server_side_session_with_secure_cookie(database, app, ids):
    c = client_for(app)
    r = login(c, ids, 'analyst')
    assert r.status_code == 200
    cookie = r.headers['set-cookie']
    token = c.cookies.get(COOKIE)
    assert cookie.startswith(f'{COOKIE}=') and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
    assert 'Path=/' in cookie and 'Max-Age=43200' in cookie and 'Domain' not in cookie
    body = r.json()
    assert body['user']['email'] == ids['users']['analyst']['email'] and body['tenant'] == {'id': str(ids['A'])}
    assert body['roles'] == ['ANALYST'] and set(body['permissions']) == EXPECTED_MATRIX['ANALYST'] and not body['tenant_selection_required']
    assert token not in r.text and len(token) >= 43 and body['csrf_token'] != token
    with connection(database=database) as admin:
        rows = admin.execute('SELECT token_hash, tenant_id::text, idle_timeout, absolute_expires_at - created_at FROM lv_identity.session WHERE user_id = %s',
                             (ids['users']['analyst']['id'],)).fetchall()
        assert len(rows) == 1 and bytes(rows[0][0]) == hashlib.sha256(token.encode()).digest() and rows[0][1] == str(ids['A'])
        assert str(rows[0][2]) == '0:30:00' and str(rows[0][3]) == '12:00:00'
        # No usable bearer token, password or CSRF token is stored anywhere in the identity schema.
        dump = repr(admin.execute('SELECT * FROM lv_identity.session').fetchall()) + repr(admin.execute('SELECT * FROM lv_identity.app_user').fetchall()) \
            + repr(admin.execute('SELECT * FROM lv_identity.audit_event').fetchall()) + repr(admin.execute('SELECT * FROM lv_identity.login_attempt').fetchall())
    for secret in (token, ids['passwords']['analyst'], body['csrf_token']):
        assert secret not in dump
    assert [e[1] for e in audit(database, 'LOGIN_SUCCEEDED') if e[2] == ids['users']['analyst']['id']] == ['ALLOWED']


@pytest.mark.parametrize('case', ['wrong-password', 'unknown-account', 'disabled', 'unverified', 'no-membership', 'case-variant-wrong'])
def test_refused_logins_are_generic_and_audited(database, app, ids, case, monkeypatch):
    import ledgervigil.auth.routes as routes
    calls = []
    real = routes.verify_password
    monkeypatch.setattr(routes, 'verify_password', lambda h, p: calls.append(h is None) or real(h, p))
    if case == 'disabled':
        with owner_transaction(database) as conn:
            conn.execute('SELECT lv_identity.admin_disable_user(%s)', (ids['users']['controller']['id'],))
    c = client_for(app)
    email, password = {'wrong-password': (ids['users']['analyst']['email'], 'not-the-password-123'),
                       'unknown-account': (f'nobody-{uuid4().hex}@test.ledgervigil.invalid', 'whatever-password-1'),
                       'disabled': (ids['users']['controller']['email'], ids['passwords']['controller']),
                       'unverified': (ids['users']['unverified']['email'], ids['passwords']['unverified']),
                       'no-membership': (ids['users']['nomember']['email'], ids['passwords']['nomember']),
                       'case-variant-wrong': (ids['users']['analyst']['email'].upper(), 'not-the-password-123')}[case]
    r = c.post('/api/v1/auth/login', json={'email': email, 'password': password})
    assert r.status_code == 401 and 'set-cookie' not in r.headers and r.headers['content-type'] == 'application/problem+json'
    body = r.json()
    assert {k: v for k, v in body.items() if k != 'request_id'} == {
        'type': 'urn:ledgervigil:problem:unauthenticated', 'title': 'Authentication required', 'status': 401, 'code': 'UNAUTHENTICATED',
        'class': 'AUTHENTICATION', 'retryable': False, 'detail': 'invalid email or password'}
    assert len(calls) == 1             # exactly one Argon2id verification, also for an unknown account (no timing shortcut)
    reasons = {'wrong-password': 'BAD_PASSWORD', 'unknown-account': 'UNKNOWN_ACCOUNT', 'disabled': 'USER_DISABLED', 'unverified': 'EMAIL_UNVERIFIED',
               'no-membership': 'NO_ACTIVE_MEMBERSHIP', 'case-variant-wrong': 'BAD_PASSWORD'}
    assert audit(database, 'LOGIN_FAILED')[-1][4]['reason'] == reasons[case]


def test_login_email_is_case_insensitive(app, ids):
    c = client_for(app)
    assert c.post('/api/v1/auth/login', json={'email': '  ' + ids['users']['analyst']['email'].upper(), 'password': ids['passwords']['analyst']}).status_code == 200


@pytest.mark.parametrize('payload', [
    {'email': 'x@test.ledgervigil.invalid', 'password': 'SECRET-SHOULD-NOT-ECHO-1', 'tenant_id': '1'},
    {'email': 'x@test.ledgervigil.invalid', 'password': ['SECRET-SHOULD-NOT-ECHO-1']},
    {'email': 'SECRET-SHOULD-NOT-ECHO-1'},
    {'email': 'x@test.ledgervigil.invalid', 'password': 'SECRET-SHOULD-NOT-ECHO-1' * 100},
])
def test_invalid_login_bodies_are_rejected_without_echoing_values(app, payload):
    r = client_for(app).post('/api/v1/auth/login', json=payload)
    assert r.status_code == 422 and r.json()['code'] == 'SCHEMA_INVALID' and 'SECRET-SHOULD-NOT-ECHO' not in r.text


def test_login_requires_json_and_an_allowed_origin(app, ids):
    c = client_for(app)
    form = c.post('/api/v1/auth/login', data={'email': ids['users']['analyst']['email'], 'password': ids['passwords']['analyst']})
    assert form.status_code == 422 and COOKIE not in c.cookies
    evil = login(c, ids, 'analyst', headers={'Origin': 'https://evil.example'})
    assert evil.status_code == 403 and evil.json()['code'] == 'PERMISSION_DENIED' and COOKIE not in c.cookies
    assert login(c, ids, 'analyst', headers={'Origin': 'https://testserver'}).status_code == 200


# ------------------------------------------------------------------------------------------- throttling
def test_account_throttling_applies_before_password_checks(database, app, ids):
    c = client_for(app)
    for _ in range(5):
        assert login(c, ids, 'analyst', 'wrong-password-123').status_code == 401
    r = login(c, ids, 'analyst')                    # correct password, but the account limit is reached
    assert r.status_code == 429 and r.json()['code'] == 'RATE_LIMITED' and r.json()['retryable'] is True and 0 < int(r.headers['retry-after']) <= 900
    assert login(client_for(app), ids, 'analyst').status_code == 429     # per account, whatever the source
    assert login(c, ids, 'controller').status_code == 200                # other accounts from the same source are unaffected
    unknown = f'ghost-{uuid4().hex}@test.ledgervigil.invalid'
    for _ in range(5):
        c.post('/api/v1/auth/login', json={'email': unknown, 'password': 'wrong-password-123'})
    ghost = c.post('/api/v1/auth/login', json={'email': unknown, 'password': 'wrong-password-123'})
    assert ghost.status_code == 429 and set(ghost.json()) == set(r.json())   # unknown accounts are throttled identically
    assert audit(database, 'LOGIN_THROTTLED')


def test_source_throttling_and_forwarded_headers_are_not_trusted(app, ids):
    source = '10.99.99.99'
    c = client_for(app, source)
    for i in range(30):
        c.post('/api/v1/auth/login', json={'email': f'probe{i}-{uuid4().hex}@test.ledgervigil.invalid', 'password': 'wrong-password-123'})
    blocked = login(c, ids, 'analyst', headers={'X-Forwarded-For': '203.0.113.7', 'X-Real-IP': '203.0.113.8'})
    assert blocked.status_code == 429
    assert login(client_for(app, '10.99.99.98'), ids, 'analyst').status_code == 200


def test_parallel_wrong_passwords_cannot_exceed_the_account_limit(database, app, ids):
    def attempt(i):
        return login(client_for(app), ids, 'automation', 'wrong-password-123').status_code
    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(attempt, range(10)))
    with connection(database=database) as admin:
        failed = admin.execute("SELECT count(*) FROM lv_identity.login_attempt a JOIN lv_identity.app_user u ON u.id = a.user_id "
                               "WHERE u.id = %s AND a.outcome = 'FAILED'", (ids['users']['automation']['id'],)).fetchone()[0]
    assert failed <= 5 and codes.count(429) >= 5 and set(codes) <= {401, 429}


# ------------------------------------------------------------------------------------------- session lifecycle and CSRF
def test_me_requires_a_valid_session(app, ids):
    c, body = signed_in(app, ids, 'analyst')
    me = c.get('/api/v1/me')
    assert me.status_code == 200 and me.json()['user']['id'] == ids['users']['analyst']['id'] and me.json()['csrf_token'] == body['csrf_token']
    anon = client_for(app).get('/api/v1/me')
    assert anon.status_code == 401 and anon.json()['code'] == 'UNAUTHENTICATED'
    forged = cookie_client(app, secrets.token_urlsafe(32))
    assert forged.get('/api/v1/me').status_code == 401


def test_csrf_is_required_bound_and_checked_before_any_effect(database, app, ids):
    c, body = signed_in(app, ids, 'analyst')
    other, other_body = signed_in(app, ids, 'controller')
    for headers in ({}, {'X-CSRF-Token': 'x' * 64}, {'X-CSRF-Token': other_body['csrf_token']}):
        r = c.post('/api/v1/auth/logout', headers=headers)
        assert r.status_code == 403 and r.json()['code'] == 'PERMISSION_DENIED'
        assert c.get('/api/v1/me').status_code == 200            # nothing happened
        r = c.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(ids['A'])}, headers=headers)
        assert r.status_code == 403
    assert c.post('/api/v1/auth/logout', headers={'X-CSRF-Token': body['csrf_token'], 'Origin': 'https://evil.example'}).status_code == 403
    assert c.get('/api/v1/me').status_code == 200


def test_logout_revokes_and_the_old_cookie_is_useless(database, app, ids):
    c, body = signed_in(app, ids, 'analyst')
    token = c.cookies.get(COOKIE)
    r = c.post('/api/v1/auth/logout', headers={'X-CSRF-Token': body['csrf_token']})
    assert r.status_code == 204 and f'{COOKIE}=""' in r.headers['set-cookie'] and 'Max-Age=0' in r.headers['set-cookie']
    replay = cookie_client(app, token)
    assert replay.get('/api/v1/me').status_code == 401
    assert replay.post('/api/v1/auth/logout', headers={'X-CSRF-Token': body['csrf_token']}).status_code == 204   # idempotent, no effect
    with connection(database=database) as admin:
        assert admin.execute('SELECT revoke_reason FROM lv_identity.session WHERE token_hash = %s', (token_hash(token),)).fetchone() == ('LOGOUT',)
    assert [e for e in audit(database, 'LOGOUT') if e[2] == ids['users']['analyst']['id']]


def test_login_rotates_any_presented_session(database, app, ids):
    c, _ = signed_in(app, ids, 'analyst')
    first = c.cookies.get(COOKIE)
    assert login(c, ids, 'analyst').status_code == 200
    second = c.cookies.get(COOKIE)
    assert first != second
    old = cookie_client(app, first)
    assert old.get('/api/v1/me').status_code == 401
    with connection(database=database) as admin:
        reason, replaced = admin.execute('SELECT revoke_reason, replaced_by FROM lv_identity.session WHERE token_hash = %s', (token_hash(first),)).fetchone()
        new_id = admin.execute('SELECT id FROM lv_identity.session WHERE token_hash = %s', (token_hash(second),)).fetchone()[0]
    assert (reason, replaced) == ('ROTATED_LOGIN', new_id)


def test_idle_and_absolute_expiry_are_enforced(database, app, ids):
    c, _ = signed_in(app, ids, 'analyst')
    token = c.cookies.get(COOKIE)
    admin_sql(database, "UPDATE lv_identity.session SET created_at = created_at - interval '31 minutes', last_seen_at = last_seen_at - interval '31 minutes', "
                        "absolute_expires_at = absolute_expires_at - interval '31 minutes' WHERE token_hash = %s", (token_hash(token),))
    assert c.get('/api/v1/me').status_code == 401
    with connection('lv_app', database) as app_conn:
        assert app_conn.execute('SELECT lv_identity.session_context(%s, false)', (token_hash(token),)).fetchone()[0] == {'valid': False, 'reason': 'IDLE_EXPIRED'}
    c2, _ = signed_in(app, ids, 'controller')
    token2 = c2.cookies.get(COOKIE)
    admin_sql(database, "UPDATE lv_identity.session SET created_at = now() - interval '12 hours 1 minute', last_seen_at = now() - interval '1 minute', "
                        "absolute_expires_at = now() - interval '1 minute' WHERE token_hash = %s", (token_hash(token2),))
    assert c2.get('/api/v1/me').status_code == 401
    with connection('lv_app', database) as app_conn:
        assert app_conn.execute('SELECT lv_identity.session_context(%s, false)', (token_hash(token2),)).fetchone()[0]['reason'] == 'ABSOLUTE_EXPIRED'
    # Activity slides the idle window.
    c3, _ = signed_in(app, ids, 'automation')
    token3 = c3.cookies.get(COOKIE)
    admin_sql(database, "UPDATE lv_identity.session SET created_at = created_at - interval '20 minutes', last_seen_at = last_seen_at - interval '20 minutes', "
                        "absolute_expires_at = absolute_expires_at - interval '20 minutes' WHERE token_hash = %s", (token_hash(token3),))
    assert c3.get('/api/v1/me').status_code == 200
    with connection(database=database) as admin:
        assert admin.execute('SELECT now() - last_seen_at < interval \'1 minute\' FROM lv_identity.session WHERE token_hash = %s', (token_hash(token3),)).fetchone() == (True,)


def test_session_rows_cannot_be_rewritten_or_deleted(database, app, ids):
    c, _ = signed_in(app, ids, 'analyst')
    token = c.cookies.get(COOKIE)
    for statement in ("UPDATE lv_identity.session SET tenant_id = NULL WHERE token_hash = %s",
                      "UPDATE lv_identity.session SET absolute_expires_at = absolute_expires_at + interval '1 hour' WHERE token_hash = %s",
                      "UPDATE lv_identity.session SET last_seen_at = last_seen_at - interval '1 minute' WHERE token_hash = %s",
                      "DELETE FROM lv_identity.session WHERE token_hash = %s"):
        with owner_transaction(database) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement, (token_hash(token),))


# ------------------------------------------------------------------------------------------- tenant selection and isolation
def test_multi_tenant_user_selects_a_tenant_and_each_switch_is_a_new_session(database, app, ids):
    c, body = signed_in(app, ids, 'multi')
    assert body['tenant'] is None and body['tenant_selection_required'] and {t['id'] for t in body['available_tenants']} == {str(ids['A']), str(ids['B'])}
    assert body['roles'] == [] and body['permissions'] == []
    denied = c.get('/api/v1/tenants/current')
    assert denied.status_code == 403 and 'select a tenant' in denied.json()['detail']
    first = c.cookies.get(COOKIE)
    r = c.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(ids['A'])}, headers={'X-CSRF-Token': body['csrf_token']})
    assert r.status_code == 200 and r.json()['tenant'] == {'id': str(ids['A'])} and r.json()['roles'] == ['ANALYST']
    second = c.cookies.get(COOKIE)
    assert second != first and r.json()['csrf_token'] != body['csrf_token'] and r.json()['absolute_expires_at'] == body['absolute_expires_at']
    assert c.get('/api/v1/tenants/current').json() == {'id': str(ids['A']), 'name': 'SYNTHETIC tenant A'}
    old = cookie_client(app, first)
    assert old.get('/api/v1/me').status_code == 401
    r2 = c.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(ids['B'])}, headers={'X-CSRF-Token': r.json()['csrf_token']})
    assert r2.status_code == 200 and c.get('/api/v1/tenants/current').json()['id'] == str(ids['B'])
    assert [e[3] for e in audit(database, 'TENANT_SELECTED') if e[2] == ids['users']['multi']['id']] == [str(ids['A']), str(ids['B'])]


def test_selecting_a_tenant_without_membership_is_404_and_changes_nothing(database, app, ids):
    c, body = signed_in(app, ids, 'analyst')
    token = c.cookies.get(COOKIE)
    for target in (ids['B'], uuid7()):
        r = c.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(target)}, headers={'X-CSRF-Token': body['csrf_token']})
        assert r.status_code == 404 and r.json()['code'] == 'NOT_FOUND' and c.cookies.get(COOKIE) == token
        assert c.get('/api/v1/tenants/current').json()['id'] == str(ids['A'])
    denied = audit(database, 'TENANT_SELECTION_DENIED')
    assert [d[4]['requested_tenant_id'] for d in denied[-2:]] == [str(ids['B']), denied[-1][4]['requested_tenant_id']] and denied[-1][3] is None


def test_spoofed_identity_and_tenant_inputs_are_ignored(app, ids):
    c, body = signed_in(app, ids, 'analyst')
    spoof = {'X-Tenant-Id': str(ids['B']), 'X-User-Id': ids['users']['bonly']['id'], 'X-Forwarded-User': ids['users']['bonly']['email'],
             'Authorization': 'Bearer ' + secrets.token_urlsafe(32)}
    r = c.get(f'/api/v1/tenants/current?tenant_id={ids["B"]}', headers=spoof)
    assert r.json()['id'] == str(ids['A'])
    me = c.get('/api/v1/me', headers=spoof)
    assert me.json()['user']['id'] == ids['users']['analyst']['id'] and me.json()['tenant']['id'] == str(ids['A'])
    extra = c.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(ids['A']), 'user_id': ids['users']['bonly']['id']},
                   headers={'X-CSRF-Token': body['csrf_token']})
    assert extra.status_code == 422
    # A bearer header without the session cookie is not a session.
    assert client_for(app).get('/api/v1/me', headers={'Authorization': 'Bearer anything'}).status_code == 401


def test_cross_tenant_reads_see_only_the_session_tenant(app, ids):
    ca, _ = signed_in(app, ids, 'analyst')
    cb, _ = signed_in(app, ids, 'bonly')
    assert ca.get('/api/v1/tenants/current').json()['id'] == str(ids['A'])
    assert cb.get('/api/v1/tenants/current').json()['id'] == str(ids['B'])


def test_disabled_users_and_revoked_memberships_lose_access(database, app, ids):
    c, _ = signed_in(app, ids, 'analyst')
    with owner_transaction(database) as conn:
        conn.execute('SELECT lv_identity.admin_disable_user(%s)', (ids['users']['analyst']['id'],))
    assert c.get('/api/v1/me').status_code == 401 and login(client_for(app), ids, 'analyst').status_code == 401
    cc, _ = signed_in(app, ids, 'controller')
    with owner_transaction(database) as conn:
        conn.execute('SELECT lv_identity.admin_revoke_membership(%s, %s)', (ids['A'], ids['users']['controller']['id']))
    assert cc.get('/api/v1/me').status_code == 401 and login(client_for(app), ids, 'controller').status_code == 401
    cm, body = signed_in(app, ids, 'multi')
    r = cm.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(ids['A'])}, headers={'X-CSRF-Token': body['csrf_token']})
    with owner_transaction(database) as conn:
        conn.execute('SELECT lv_identity.admin_revoke_membership(%s, %s)', (ids['A'], ids['users']['multi']['id']))
    assert cm.get('/api/v1/tenants/current').status_code == 401
    cm2, body2 = signed_in(app, ids, 'multi')
    assert body2['tenant'] == {'id': str(ids['B'])}       # one remaining membership: selected automatically
    assert cm2.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(ids['A'])}, headers={'X-CSRF-Token': body2['csrf_token']}).status_code == 404
    assert r.status_code == 200
    revoked = {e[0] for e in audit(database) if e[2] in (ids['users']['analyst']['id'], ids['users']['controller']['id'])}
    assert {'USER_DISABLED', 'MEMBERSHIP_REVOKED'} <= revoked


def test_every_request_rechecks_user_and_membership_state(database, app, ids):
    """Independent of the admin functions' session revocation: a session is re-validated against the CURRENT user and
    membership rows on every request, so a change made by any path ends access at once."""
    c, _ = signed_in(app, ids, 'analyst')
    cc, _ = signed_in(app, ids, 'controller')
    with owner_transaction(database) as conn:      # direct row changes; sessions deliberately left unrevoked
        conn.execute('UPDATE lv_identity.app_user SET disabled_at = now() WHERE id = %s', (ids['users']['analyst']['id'],))
        conn.execute("UPDATE lv_identity.membership SET status = 'REVOKED', revoked_at = now() WHERE tenant_id = %s AND user_id = %s",
                     (ids['A'], ids['users']['controller']['id']))
    with connection(database=database) as admin:
        assert admin.execute('SELECT count(*) FROM lv_identity.session WHERE user_id IN (%s, %s) AND revoked_at IS NULL',
                             (ids['users']['analyst']['id'], ids['users']['controller']['id'])).fetchone() == (2,)
    with connection('lv_app', database) as app_conn:
        assert app_conn.execute('SELECT lv_identity.session_context(%s, false)', (token_hash(c.cookies.get(COOKIE)),)).fetchone()[0]['reason'] == 'USER_UNAVAILABLE'
        assert app_conn.execute('SELECT lv_identity.session_context(%s, false)', (token_hash(cc.cookies.get(COOKIE)),)).fetchone()[0]['reason'] == 'MEMBERSHIP_INACTIVE'
    assert c.get('/api/v1/me').status_code == 401
    assert cc.get('/api/v1/tenants/current').status_code == 401


def test_role_change_ends_existing_sessions(database, app, ids):
    c, _ = signed_in(app, ids, 'analyst')
    with owner_transaction(database) as conn:
        conn.execute("SELECT lv_identity.admin_set_roles(%s, %s, ARRAY['CONTROLLER']::lv_identity.member_role[])", (ids['A'], ids['users']['analyst']['id']))
    assert c.get('/api/v1/me').status_code == 401
    _, body = signed_in(app, ids, 'analyst')
    assert body['roles'] == ['CONTROLLER'] and set(body['permissions']) == EXPECTED_MATRIX['CONTROLLER']


# ------------------------------------------------------------------------------------------- permissions
def permission_app(database):
    application = make_app(database)
    for verb in PERMISSIONS:
        def route(ctx: Annotated[RequestContext, Depends(require_permission(verb))], verb=verb):
            return {'verb': verb, 'tenant': ctx.conn.execute("SELECT current_setting('app.tenant_id')").fetchone()[0]}
        application.add_api_route(f'/test/permission/{verb}', route, methods=['GET'])
    return application


@pytest.mark.parametrize('role', ['analyst', 'controller', 'automation', 'admin'])
def test_roles_grant_exactly_the_accepted_permissions(database, ids, role):
    application = permission_app(database)
    c = client_for(application)
    assert login(c, ids, role).status_code == 200
    expected = EXPECTED_MATRIX[{'analyst': 'ANALYST', 'controller': 'CONTROLLER', 'automation': 'AUTOMATION_DEVELOPER', 'admin': 'ADMINISTRATOR'}[role]]
    granted = {verb for verb in PERMISSIONS if c.get(f'/test/permission/{verb}').status_code == 200}
    assert granted == expected
    for verb in set(PERMISSIONS) - expected:
        r = c.get(f'/test/permission/{verb}')
        assert r.status_code == 403 and r.json()['code'] == 'PERMISSION_DENIED'
    if role == 'admin':
        assert {'review', 'execute', 'propose'}.isdisjoint(granted)        # administrator has no financial approval or execution
    application.state.pool.close()


def test_permission_catalog_matches_contract_and_database(database, app, ids):
    with connection(database=database) as admin:
        rows = admin.execute('SELECT role::text, permission::text FROM lv_identity.role_permission').fetchall()
    db = {}
    for role, perm in rows:
        db.setdefault(role, set()).add(perm)
    assert db == EXPECTED_MATRIX == {r: set(p) for r, p in ROLE_PERMISSIONS.items()}
    c, _ = signed_in(app, ids, 'controller')
    catalog = c.get('/api/v1/permissions').json()
    assert {r: set(p) for r, p in catalog['roles'].items()} == EXPECTED_MATRIX and catalog['permissions'] == list(PERMISSIONS)
    assert client_for(app).get('/api/v1/permissions').status_code == 401


def test_unknown_permission_cannot_be_declared():
    with pytest.raises(ValueError):
        require_permission('approve')


# ------------------------------------------------------------------------------------------- connection reuse and privileges
def context_app(database, connect):
    application = create_app(AuthSettings(), connect=connect)
    application.state.pool = ConnectionPool(connect, max_size=1)
    _OPEN_APPS.append(application)

    def whoami(ctx: Annotated[RequestContext, Depends(require_permission('read'))]):
        row = ctx.conn.execute("SELECT current_setting('app.tenant_id'), current_setting('app.user_id'), pg_backend_pid(), "
                               "(SELECT count(*) FROM lv.tenant)").fetchone()
        return {'tenant': row[0], 'user': row[1], 'pid': row[2], 'visible_tenants': row[3]}

    def explode(ctx: Annotated[RequestContext, Depends(require_permission('read'))]):
        ctx.conn.execute("SELECT 1")
        raise RuntimeError('route failure after the tenant was bound')

    def leak(ctx: Annotated[RequestContext, Depends(require_permission('read'))]):
        ctx.conn.execute("SELECT set_config('app.tenant_id', %s, false)", (ctx.tenant_id,))      # deliberately session-level
        return {'pid': ctx.conn.execute('SELECT pg_backend_pid()').fetchone()[0]}

    def raw():
        with application.state.pool.connection() as conn:
            row = conn.execute("SELECT current_setting('app.tenant_id', true), current_setting('app.user_id', true), pg_backend_pid(), "
                               "(SELECT count(*) FROM lv.tenant)").fetchone()
            conn.rollback()
        return {'tenant': row[0], 'user': row[1], 'pid': row[2], 'visible_tenants': row[3]}
    application.add_api_route('/test/whoami', whoami, methods=['GET'])
    application.add_api_route('/test/explode', explode, methods=['GET'])
    application.add_api_route('/test/leak', leak, methods=['GET'])
    application.add_api_route('/test/raw', raw, methods=['GET'])
    return application


def test_reused_connections_never_carry_a_previous_user_or_tenant(database, ids):
    application = context_app(database, lambda: connection('lv_app', database))
    a = client_for(application); assert login(a, ids, 'analyst').status_code == 200
    b = client_for(application); assert login(b, ids, 'bonly').status_code == 200
    first = a.get('/test/whoami').json()
    assert (first['tenant'], first['user'], first['visible_tenants']) == (str(ids['A']), ids['users']['analyst']['id'], 1)
    after = client_for(application).get('/test/raw').json()
    assert (after['tenant'], after['user'], after['visible_tenants'], after['pid']) == ('', '', 0, first['pid'])     # same connection, no context
    second = b.get('/test/whoami').json()
    assert (second['tenant'], second['user'], second['pid']) == (str(ids['B']), ids['users']['bonly']['id'], first['pid'])
    failed = client_for(application).get('/test/explode', headers={'Cookie': f'{COOKIE}={a.cookies.get(COOKIE)}'})
    assert failed.status_code == 500 and failed.json()['code'] == 'INTERNAL' and 'route failure' not in failed.text
    again = client_for(application).get('/test/raw').json()
    assert (again['tenant'], again['user'], again['pid']) == ('', '', first['pid'])
    leaked = a.get('/test/leak').json()
    fresh = client_for(application).get('/test/raw').json()
    # The dirty connection was closed, not reused: a brand-new backend, on which the setting has never existed (NULL).
    assert fresh['pid'] != leaked['pid'] and (fresh['tenant'], fresh['user'], fresh['visible_tenants']) == (None, None, 0)
    application.state.pool.close()


def test_runtime_service_must_be_the_restricted_role(database):
    for role in ('lv_migrator', 'postgres', 'lv_seed'):
        pool = ConnectionPool(lambda role=role: connection(role, database))
        try:
            with pytest.raises(RuntimeRoleError):
                with pool.connection():
                    pass
        finally:
            pool.close()


def test_runtime_database_privileges_are_narrow(database):
    with connection(database=database) as admin:
        tables = [r[0] for r in admin.execute("SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                              "WHERE n.nspname = 'lv_identity' AND c.relkind = 'r'").fetchall()]
        assert len(tables) == 8
        for table in tables:
            for role in ('lv_app', 'lv_seed', 'lv_ingest'):
                for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'):
                    assert admin.execute('SELECT has_table_privilege(%s, %s, %s)', (role, table, privilege)).fetchone() == (False,), (role, table, privilege)
            assert admin.execute('SELECT relrowsecurity, pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s::regclass', (table,)).fetchone() == (True, 'lv_owner')
        functions = admin.execute("SELECT p.proname, p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), has_function_privilege('lv_app', p.oid, 'EXECUTE'), "
                                  "has_function_privilege('lv_seed', p.oid, 'EXECUTE'), has_function_privilege('lv_ingest', p.oid, 'EXECUTE') "
                                  "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'lv_identity'").fetchall()
        executable = {f[0] for f in functions if f[4]}
        assert executable == {'login_prepare', 'login_finish', 'session_context', 'logout', 'switch_tenant'}
        assert {f[0] for f in functions if f[1]} == executable                      # exactly the runtime entry points are SECURITY DEFINER
        for name, definer, config, owner, _, seed_exec, ingest_exec in functions:
            assert owner == 'lv_owner' and not seed_exec and not ingest_exec, name
            assert config is not None and 'search_path=pg_catalog, pg_temp' in config, name
        assert admin.execute("SELECT has_schema_privilege('lv_app', 'lv_identity', 'USAGE'), has_schema_privilege('lv_app', 'lv_identity', 'CREATE'), "
                             "has_schema_privilege('lv_seed', 'lv_identity', 'USAGE'), has_schema_privilege('lv_ingest', 'lv_identity', 'USAGE')").fetchone() \
            == (True, False, False, False)
    with connection('lv_app', database) as app_conn:
        for statement in ('SELECT * FROM lv_identity.app_user', 'SELECT * FROM lv_identity.session',
                          "SELECT lv_identity.admin_disable_user('00000000-0000-0000-0000-000000000000')",
                          "SELECT lv_identity.audit_append('X', 'Y', NULL, NULL, NULL, 's', 'r', '{}')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with app_conn.transaction():
                    app_conn.execute(statement)


def test_local_provisioning_uses_random_passwords_kept_out_of_output(database, app, tmp_path, capsys):
    from tools.identity_admin import ROOT as APP_ROOT, provision_demo
    for forbidden in (APP_ROOT / 'evidence' / 'creds.json', APP_ROOT / 'docs' / 'creds.json'):
        with pytest.raises(SystemExit, match='refusing'):
            provision_demo(forbidden, database)
    first = provision_demo(tmp_path / 'a.json', database)
    second = provision_demo(tmp_path / 'b.json', database)             # repeatable: a new random tag, new random passwords
    with pytest.raises(SystemExit, match='already exists'):
        provision_demo(tmp_path / 'a.json', database)
    creds_a, creds_b = (json.loads((tmp_path / n).read_text()) for n in ('a.json', 'b.json'))
    assert (tmp_path / 'a.json').stat().st_mode & 0o777 == 0o600 and len(creds_a) == 5
    assert set(creds_a).isdisjoint(creds_b) and set(creds_a.values()).isdisjoint(creds_b.values())
    assert all(len(p) >= 24 for p in creds_a.values())
    for result, creds in ((first, creds_a), (second, creds_b)):
        assert set(result['users']) == set(creds) and not any(p in json.dumps(result) for p in creds.values())
    admin_email = next(e for e in creds_a if e.startswith('administrator.'))
    c = client_for(app)
    r = c.post('/api/v1/auth/login', json={'email': admin_email, 'password': creds_a[admin_email]})
    assert r.status_code == 200 and r.json()['roles'] == ['ADMINISTRATOR']
    for guess in ('admin', 'password', 'ledgervigil', 'changeme', admin_email, 'administrator'):     # no default password exists
        assert c.post('/api/v1/auth/login', json={'email': admin_email, 'password': guess}).status_code in (401, 429)   # 429 once throttled
    with connection(database=database) as admin:
        events = admin.execute("SELECT detail FROM lv_identity.audit_event WHERE event_type = 'USER_PROVISIONED' AND user_id = %s",
                               (first['users'][admin_email]['user_id'],)).fetchall()
    assert events and events[0][0].get('email_verification') == 'MARKED_BY_LOCAL_PROVISIONING'


def test_authentication_does_not_grant_developer_seeding_or_financial_writes(database, ids):
    application = make_app(database)

    def try_seed(ctx: Annotated[RequestContext, Depends(require_permission('administer'))]):
        for statement in ("SELECT lv.seed_manual_journal(uuidv7(), '{}')", "SELECT lv.seed_processor_settlement(uuidv7(), '{}')",
                          "UPDATE lv.fiscal_period SET status = status WHERE false"):
            try:
                with ctx.conn.transaction():
                    ctx.conn.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                continue
            return {'denied': False, 'statement': statement}
        return {'denied': True}
    application.add_api_route('/test/try-seed', try_seed, methods=['GET'])
    c = client_for(application); assert login(c, ids, 'admin').status_code == 200
    result = c.get('/test/try-seed').json()
    # Seeding is refused to the runtime role, and since 0009 so is any direct period UPDATE (R-02 closed).
    assert result == {'denied': True}
    paths = set(create_app(AuthSettings()).openapi()['paths'])
    # 0009 adds exactly the Family 4 period routes; the read-API milestone adds exactly the four Family 4 GET read routes
    # (read-only transactions, tested in test_ledger_reads). Still no seed, ingestion-admin, execution or role-management route.
    assert paths == {'/api/v1/proposals/{proposal_id}/proof-packs','/api/v1/proof-packs/{pack_id}','/api/v1/proof-packs/{pack_id}/manifest','/api/v1/proof-packs/{pack_id}/verify','/api/v1/proof-packs/{pack_id}/exports','/api/v1/executions/{execution_id}/exports','/api/v1/execution-proof-exports/{job_id}','/api/v1/execution-proof-exports/{job_id}/download-link','/api/v1/execution-proof-exports/{job_id}/download','/api/v1/accounts/{account_id}','/api/v1/auth/login', '/api/v1/auth/logout', '/api/v1/auth/switch-tenant', '/api/v1/me', '/api/v1/tenants/current',
                     '/api/v1/permissions', '/api/v1/ledgers/{ledger_id}/periods', '/api/v1/periods/{period_id}/close',
                     '/api/v1/ledgers', '/api/v1/ledgers/{ledger_id}', '/api/v1/ledgers/{ledger_id}/accounts',
                     '/api/v1/ledgers/{ledger_id}/trial-balance',
                     # 0010 adds exactly the Family 8 draft routes (no submit, approve or execute route).
                     '/api/v1/proposals', '/api/v1/proposals/{proposal_id}', '/api/v1/proposals/{proposal_id}/revisions',
                     # The source-read milestone adds exactly six GET read routes (Family 5 documents, Family 3 imports); no upload.
                     '/api/v1/ledgers/{ledger_id}/documents', '/api/v1/documents/{document_id}', '/api/v1/imports',
                     '/api/v1/imports/{import_id}', '/api/v1/imports/{import_id}/records', '/api/v1/imports/{import_id}/staged-documents',
                     # The simulation foundation (0011) adds exactly two read routes; no creation, /submit or what-if route.
                     '/api/v1/simulations/{simulation_id}', '/api/v1/simulations/{simulation_id}/replay',
                     '/api/v1/proposals/{proposal_id}/submit','/api/v1/proposals/{proposal_id}/withdraw', '/api/v1/proposals/{proposal_id}/resimulate', '/api/v1/proposals/{proposal_id}/revalidate', '/api/v1/proposals/{proposal_id}/simulations', '/api/v1/source-plan-registrations', '/api/v1/source-plan-registrations/{registration_id}', '/api/v1/source-plan-registrations/{registration_id}/groups', '/api/v1/source-plan-registrations/{registration_id}/groups/{group_id}', '/api/v1/source-plan-registrations/{registration_id}/groups/{group_id}/rows', '/api/v1/proposals/{proposal_id}/comments', '/api/v1/proposals/{proposal_id}/execute', '/api/v1/executions/{execution_id}', '/api/v1/executions/{execution_id}/proof', '/api/v1/proposals/{proposal_id}/execution', '/api/v1/proposals/{proposal_id}/approve', '/api/v1/proposals/{proposal_id}/reject', '/api/v1/proposals/{proposal_id}/overrides', '/api/v1/proposals/{proposal_id}/reviews'}
    # Every GET route added for reads is read-only at the database level (auth.boundary.require_read_only).
    spec = create_app(AuthSettings()).openapi()
    assert {p for p, ops in spec['paths'].items() if 'get' in ops and ops['get'].get('x-lv-permission') == 'read'} >= {
        '/api/v1/ledgers', '/api/v1/ledgers/{ledger_id}', '/api/v1/ledgers/{ledger_id}/accounts', '/api/v1/ledgers/{ledger_id}/periods',
        '/api/v1/ledgers/{ledger_id}/trial-balance', '/api/v1/ledgers/{ledger_id}/documents', '/api/v1/documents/{document_id}',
        '/api/v1/imports', '/api/v1/imports/{import_id}', '/api/v1/imports/{import_id}/records', '/api/v1/imports/{import_id}/staged-documents'}
    application.state.pool.close()


def test_definer_functions_resist_caller_temporary_objects(database, ids):
    """R-01 regression for the identity entry points: a caller-created temporary domain must never be resolved."""
    with connection(database=database, autocommit=True) as admin:
        admin.execute(sql.SQL('GRANT TEMPORARY ON DATABASE {} TO lv_app').format(sql.Identifier(database)))
    try:
        with connection('lv_app', database, autocommit=True) as c:
            c.execute("""
              CREATE FUNCTION pg_temp.shadow_probe(v text) RETURNS boolean LANGUAGE plpgsql AS $$
              BEGIN RAISE EXCEPTION 'SHADOW EXECUTED AS %', current_user; END $$;
              CREATE DOMAIN pg_temp.uuid AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.jsonb AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.bytea AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.timestamptz AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE FUNCTION pg_temp.sha256(bytea) RETURNS bytea LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_temp.shadow_probe('x'); RETURN NULL; END $$;
            """)
            with c.transaction():
                prepared = c.execute('SELECT lv_identity.login_prepare(%s, %s, %s)', (ids['users']['analyst']['email'], '10.1.1.1', 'probe')).fetchone()[0]
            assert prepared['throttled'] is False and prepared['password_hash'].startswith('$argon2id$')
            with c.transaction():
                assert c.execute('SELECT lv_identity.session_context(%s, true)', (b'\x00' * 32,)).fetchone()[0] == {'valid': False, 'reason': 'UNKNOWN'}
    finally:
        with connection(database=database, autocommit=True) as admin:
            admin.execute(sql.SQL('REVOKE TEMPORARY ON DATABASE {} FROM lv_app').format(sql.Identifier(database)))


def test_authentication_requests_leave_financial_records_unchanged(database, app, ids):
    before = lv_hashes(database)
    c, body = signed_in(app, ids, 'multi')
    r = c.post('/api/v1/auth/switch-tenant', json={'tenant_id': str(ids['A'])}, headers={'X-CSRF-Token': body['csrf_token']})
    c.get('/api/v1/me'); c.get('/api/v1/tenants/current'); c.get('/api/v1/permissions')
    c.post('/api/v1/auth/logout', headers={'X-CSRF-Token': r.json()['csrf_token']})
    login(client_for(app), ids, 'analyst', 'wrong-password-123')
    assert lv_hashes(database) == before


# ------------------------------------------------------------------------------------------- audit, logs, settings, OpenAPI
def test_security_events_are_attributed_hash_chained_and_immutable(database, app, ids):
    c, body = signed_in(app, ids, 'analyst')
    c.post('/api/v1/auth/logout', headers={'X-CSRF-Token': body['csrf_token'], 'X-Request-Id': 'req-logout-1'})
    with connection(database=database) as admin:
        rows = admin.execute('SELECT chain_key, seq, canonical_content, prev_hash, hash, request_id, source_key, session_id FROM lv_identity.audit_event '
                             'ORDER BY chain_key, seq').fetchall()
        heads = dict(admin.execute('SELECT chain_key, head FROM lv_identity.audit_chain_head').fetchall())
    by_chain = {}
    for chain, seq, content, prev, digest, *_ in rows:
        by_chain.setdefault(chain, []).append((seq, content, prev, digest))
    for chain, events in by_chain.items():
        previous = '0' * 64
        for expected_seq, (seq, content, prev, digest) in enumerate(events, 1):
            assert seq == expected_seq and prev == previous and digest == hashlib.sha256((content + prev).encode()).hexdigest()
            previous = digest
        assert heads[chain] == previous
    logout_row = next(r for r in rows if r[5] == 'req-logout-1')
    assert logout_row[6] and logout_row[7] is not None
    with owner_transaction(database) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute('DELETE FROM lv_identity.audit_event')


def test_logs_and_errors_contain_no_secrets(database, app, ids, caplog):
    caplog.set_level(logging.DEBUG)
    c, body = signed_in(app, ids, 'analyst')
    token = c.cookies.get(COOKIE)
    c.get('/api/v1/me')
    login(client_for(app), ids, 'analyst', 'wrong-password-SECRET-xyz')
    c.post('/api/v1/auth/logout', headers={'X-CSRF-Token': body['csrf_token']})
    text = '\n'.join(r.getMessage() for r in caplog.records)
    for secret in (token, ids['passwords']['analyst'], 'wrong-password-SECRET-xyz', body['csrf_token']):
        assert secret not in text
    assert 'login succeeded' in text and 'login refused' in text


def test_local_http_mode_is_explicit_and_loopback_only(database, ids):
    with pytest.raises(SettingsError):
        AuthSettings(cookie_mode='local-http', environment='production')
    with pytest.raises(SettingsError):
        load_settings({'LV_COOKIE_MODE': 'local-http'})
    assert load_settings({}).cookie_mode == 'secure' and load_settings({}).cookie_name == COOKIE
    local = make_app(database, AuthSettings(cookie_mode='local-http', environment='development'))
    c = client_for(local, base_url='http://127.0.0.1')
    r = login(c, ids, 'analyst')
    assert r.status_code == 200 and r.headers['set-cookie'].startswith('lv_session_local=') and 'Secure' not in r.headers['set-cookie']
    assert 'HttpOnly' in r.headers['set-cookie'] and c.get('/api/v1/me').status_code == 200
    exposed = client_for(local, base_url='http://lan-host.example')
    assert login(exposed, ids, 'controller').status_code == 403
    local.state.pool.close()


def test_committed_openapi_is_the_generated_document():
    generated = create_app(AuthSettings()).openapi()
    committed = json.loads((ROOT / 'api/openapi.json').read_text())
    assert committed == generated
    for path, operations in generated['paths'].items():
        for method, op in operations.items():
            assert op.get('x-lv-permission') and op.get('operationId'), (path, method)


# ------------------------------------------------------------------------------------------- migrations
def test_fresh_chain_reaches_0007_and_downgrade_removes_the_identity_schema():
    from test_processor_payments import disposable
    with disposable('0006_processor_settlements') as name:
        migrate(name)
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0033_detection_overlap_dates',)
        migrate(name, '0006_processor_settlements', downgrade=True, disposable=True)
        with connection(database=name) as admin:
            assert admin.execute("SELECT to_regnamespace('lv_identity')").fetchone() == (None,)
        migrate(name)
        with owner_transaction(name) as conn:
            create_user(conn, 'keep@test.ledgervigil.invalid', 'Synthetic', secrets.token_urlsafe(18))
        with pytest.raises(Exception, match='identity records exist'):
            migrate(name, '0006_processor_settlements', downgrade=True, disposable=True)


def test_upgrade_from_populated_0006_preserves_records_then_authenticates():
    from test_processor_payments import disposable, new_tenant, opening, pay
    from test_processor_settlements import settle, settlement
    from test_posting import provision, register
    with disposable('0006_processor_settlements') as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            cfg = provision(app_, tenant)
        register(name, tenant)
        with connection('lv_seed', name, autocommit=True) as s:
            opening(s, cfg); doc = pay(s, cfg)['document_id']; settle(s, cfg, settlement([(doc, '50000.00')]))
        before = lv_hashes(name)
        migrate(name, '0008_identity_force_rls')   # scope: the identity upgrade; 0009's populated upgrade is in test_period_close.py
        assert lv_hashes(name) == before
        password = secrets.token_urlsafe(18)
        with owner_transaction(name) as conn:
            uid = create_user(conn, 'upgrade@test.ledgervigil.invalid', 'Synthetic', password)
            grant(conn, tenant, uid, ['CONTROLLER'])
        application = make_app(name)
        c = client_for(application)
        r = c.post('/api/v1/auth/login', json={'email': 'upgrade@test.ledgervigil.invalid', 'password': password})
        assert r.status_code == 200 and c.get('/api/v1/tenants/current').json()['id'] == str(tenant)
        application.state.pool.close()
        assert lv_hashes(name) == before
