"""Identity lifecycle (0036; ADR-027, Stage 2B §5.2 and Family 1): verification, reset, invitation, member and role
administration, re-authentication, session revocation and audit read. Mail goes to the development capture adapter (a
private directory), never a real mail service. Disposable database per test. All data SYNTHETIC."""
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest

from tools.dev_db import connection
from tools.identity_admin import create_user, grant, owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from test_proposal_drafts import world, seed, signed_in, client  # noqa: F401
from detection_support import database  # noqa: F401

BASE = '/api/v1'


@pytest.fixture
def lapi(database, tmp_path, monkeypatch):
    box = tmp_path / 'mail'
    monkeypatch.setenv('LV_MAIL_ADAPTER', 'capture')
    monkeypatch.setenv('LV_MAIL_CAPTURE_DIR', str(box))
    monkeypatch.setenv('LV_ENVIRONMENT', 'development')
    monkeypatch.setenv('LV_PUBLIC_ORIGIN', 'http://127.0.0.1:3000')
    application = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database))
    application.state.mailbox = box
    yield application
    application.state.pool.close()


def mails(application, to=None):
    box = application.state.mailbox
    out = [json.loads(p.read_text()) for p in sorted(box.glob('*.json'))] if box.exists() else []
    return [m for m in out if to is None or m['to'] == to]


def token_of(message):
    u = urlsplit(message['link'])
    assert u.query == '' and u.fragment.startswith('token=')   # the token travels in the fragment, never the query
    return u.fragment[len('token='):]


def login(application, email, password):
    c = client(application)
    r = c.post(f'{BASE}/auth/login', json={'email': email, 'password': password})
    if r.status_code == 200:
        c.headers['X-CSRF-Token'] = r.json()['csrf_token']
    return c, r


def write(c, method, path, body=None, match=None, key=None):
    headers = {'Idempotency-Key': key or uuid4().hex}
    if match:
        headers['If-Match'] = f'"{match}"'
    return c.request(method, BASE + path, json=body, headers=headers)


def member(c, user_id):
    return next(m for m in c.get(f'{BASE}/members').json()['items'] if m['user_id'] == user_id)


def test_email_verification_is_single_use_and_does_not_reveal_accounts(database, world, lapi):
    email, password = f'new-{uuid4().hex[:8]}@test.ledgervigil.invalid', 'pw-' + uuid4().hex
    with owner_transaction(database) as owner:
        uid = create_user(owner, email, 'Synthetic unverified', password, verified=False)
        grant(owner, world['A'], uid, ['ANALYST'])
    _, r = login(lapi, email, password)
    assert r.status_code == 401   # unverified: the same generic refusal
    anon = client(lapi)
    known = anon.post(f'{BASE}/auth/email-verification', json={'email': email})
    unknown = anon.post(f'{BASE}/auth/email-verification', json={'email': f'nobody-{uuid4().hex[:6]}@test.ledgervigil.invalid'})
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    sent = mails(lapi, email)
    assert len(sent) == 1 and len(mails(lapi)) == 1 and sent[0]['kind'] == 'VERIFY_EMAIL'
    token = token_of(sent[0])
    assert anon.post(f'{BASE}/auth/email-verification/confirm', json={'token': token}).status_code == 200
    assert anon.post(f'{BASE}/auth/email-verification/confirm', json={'token': token}).json()['code'] == 'TOKEN_INVALID'
    _, r = login(lapi, email, password)
    assert r.status_code == 200
    with connection(database=database) as db:   # only the digest is stored
        assert db.execute('SELECT count(*) FROM lv_identity.email_token WHERE token_hash=%s', (hashlib.sha256(token.encode()).digest(),)).fetchone()[0] == 1
        stored = '\n'.join(str(v) for (v,) in db.execute('SELECT row_to_json(t)::text FROM lv_identity.email_token t'))
        assert token not in stored


def test_password_reset_revokes_sessions_and_is_single_use_and_expires(database, world, lapi):
    u = world['users']['analyst']
    old, _ = login(lapi, u['email'], u['password'])
    assert old.get(f'{BASE}/me').status_code == 200
    anon = client(lapi)
    assert anon.post(f'{BASE}/auth/password-reset', json={'email': u['email']}).status_code == 202
    token = token_of(mails(lapi, u['email'])[-1])
    new_password = 'new-' + uuid4().hex
    r = anon.post(f'{BASE}/auth/password-reset/confirm', json={'token': token, 'new_password': new_password})
    assert r.status_code == 200 and r.json()['sessions_revoked'] >= 1
    assert old.get(f'{BASE}/me').status_code == 401   # every session of the user ended
    assert login(lapi, u['email'], u['password'])[1].status_code == 401 and login(lapi, u['email'], new_password)[1].status_code == 200
    again = anon.post(f'{BASE}/auth/password-reset/confirm', json={'token': token, 'new_password': 'another-' + uuid4().hex})
    assert (again.status_code, again.json()['code']) == (400, 'TOKEN_INVALID')
    # Expiry: a fresh link older than 30 minutes is refused like any other invalid link.
    anon.post(f'{BASE}/auth/password-reset', json={'email': u['email']})
    late = token_of(mails(lapi, u['email'])[-1])
    with connection(database=database) as db:
        db.execute('SET session_replication_role = replica')   # superuser test setup only: age the token
        db.execute("UPDATE lv_identity.email_token SET created_at=created_at-interval '31 minutes', expires_at=expires_at-interval '31 minutes' WHERE consumed_at IS NULL")
        db.commit()
    r = anon.post(f'{BASE}/auth/password-reset/confirm', json={'token': late, 'new_password': 'late-' + uuid4().hex})
    assert (r.status_code, r.json()['code']) == (400, 'TOKEN_INVALID')


def test_reset_requests_are_throttled_identically_for_known_and_unknown_addresses(database, world, lapi):
    anon = client(lapi)
    known, unknown = world['users']['analyst']['email'], f'ghost-{uuid4().hex[:6]}@test.ledgervigil.invalid'
    for address in (known, unknown):
        codes = [anon.post(f'{BASE}/auth/password-reset', json={'email': address}).status_code for _ in range(4)]
        assert codes == [202, 202, 202, 429]
    assert len(mails(lapi, known)) == 3 and mails(lapi, unknown) == []


def test_concurrent_use_of_one_reset_link_succeeds_exactly_once(database, world, lapi):
    u = world['users']['controller']
    anon = client(lapi)
    anon.post(f'{BASE}/auth/password-reset', json={'email': u['email']})
    token = token_of(mails(lapi, u['email'])[-1])
    codes, barrier = [], threading.Barrier(4)

    def use(n):
        c = client(lapi)
        barrier.wait()
        codes.append(c.post(f'{BASE}/auth/password-reset/confirm', json={'token': token, 'new_password': f'race-{n}-' + uuid4().hex}).status_code)
    threads = [threading.Thread(target=use, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(codes) == [200, 400, 400, 400]


def test_invitation_creates_an_unverified_member_who_sets_a_password(database, world, lapi):
    a = world['users']['admin']
    admin, _ = login(lapi, a['email'], a['password'])
    email = f'invitee-{uuid4().hex[:8]}@test.ledgervigil.invalid'
    r = write(admin, 'POST', '/members', {'email': email, 'display_name': 'Synthetic invitee', 'roles': ['ANALYST']})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body['status'] == 'ACTIVE' and not body['email_verified'] and body['invitation_pending'] and [x['role'] for x in body['roles']] == ['ANALYST']
    token = token_of(mails(lapi, email)[-1])
    password = 'invitee-' + uuid4().hex
    assert client(lapi).post(f'{BASE}/auth/invitation/accept', json={'token': token, 'password': password}).status_code == 200
    c, r = login(lapi, email, password)
    assert r.status_code == 200 and r.json()['roles'] == ['ANALYST'] and 'administer' not in r.json()['permissions']
    dup = write(admin, 'POST', '/members', {'email': email, 'roles': ['ANALYST']})
    assert (dup.status_code, dup.json()['code']) == (409, 'MEMBER_EXISTS')
    # Idempotent replay of the first invitation returns the stored answer and sends nothing new.
    key = uuid4().hex
    other = f'invitee2-{uuid4().hex[:8]}@test.ledgervigil.invalid'
    first = write(admin, 'POST', '/members', {'email': other, 'roles': ['ANALYST']}, key=key)
    again = write(admin, 'POST', '/members', {'email': other, 'roles': ['ANALYST']}, key=key)
    assert first.status_code == again.status_code == 201 and again.headers['Idempotent-Replayed'] == 'true' and len(mails(lapi, other)) == 1


def test_segregation_self_grant_cooling_off_last_administrator_and_session_rotation(database, world, lapi):
    a, ctl = world['users']['admin'], world['users']['analyst']
    admin, _ = login(lapi, a['email'], a['password'])
    target, _ = login(lapi, ctl['email'], ctl['password'])
    me = member(admin, a['id'])
    r = write(admin, 'PATCH', f'/members/{a["id"]}', {'roles': ['ADMINISTRATOR', 'CONTROLLER']}, match=me['etag'])
    assert (r.status_code, r.json()['code']) == (403, 'SEGREGATION_OF_DUTIES')
    m = member(admin, ctl['id'])
    assert write(admin, 'PATCH', f'/members/{ctl["id"]}', {'roles': ['CONTROLLER']}).status_code == 428
    assert write(admin, 'PATCH', f'/members/{ctl["id"]}', {'roles': ['CONTROLLER']}, match='0' * 64).status_code == 412
    r = write(admin, 'PATCH', f'/members/{ctl["id"]}', {'roles': ['CONTROLLER']}, match=m['etag'])
    assert r.status_code == 200, r.text
    granted = r.json()['roles'][0]
    assert granted['role'] == 'CONTROLLER' and granted['cooling_off_until'] and granted['granted_by'] == a['id']
    assert target.get(f'{BASE}/me').status_code == 401   # privilege change ends the member's sessions
    with connection(database=database) as db:   # approval/execution read granted_at: the 24-hour cooling-off starts now
        age = db.execute("SELECT statement_timestamp()-granted_at FROM lv_identity.membership_role WHERE user_id=%s AND role='CONTROLLER'", (ctl['id'],)).fetchone()[0]
        assert age.total_seconds() < 60
    me = member(admin, a['id'])
    r = write(admin, 'PATCH', f'/members/{a["id"]}', {'roles': ['ANALYST']}, match=me['etag'])
    assert (r.status_code, r.json()['code']) == (409, 'LAST_ADMINISTRATOR')
    r = write(admin, 'DELETE', f'/members/{a["id"]}', match=me['etag'])
    assert (r.status_code, r.json()['code']) == (409, 'LAST_ADMINISTRATOR')
    # The administrator still holds no financial permission (ADR-015).
    assert not {'review', 'execute', 'propose'} & set(admin.get(f'{BASE}/me').json()['permissions'])


def test_member_changes_need_recent_password_confirmation(database, world, lapi):
    a, an = world['users']['admin'], world['users']['automation']
    admin, _ = login(lapi, a['email'], a['password'])
    with connection(database=database) as db:
        db.execute('SET session_replication_role = replica')   # superuser test setup only: the sign-in was 20 minutes ago
        db.execute("UPDATE lv_identity.session SET created_at=created_at-interval '20 minutes', absolute_expires_at=absolute_expires_at-interval '20 minutes' WHERE user_id=%s AND revoked_at IS NULL", (a['id'],))
        db.commit()
    m = member(admin, an['id'])
    r = write(admin, 'PATCH', f'/members/{an["id"]}', {'roles': ['ANALYST']}, match=m['etag'])
    assert (r.status_code, r.json()['code']) == (403, 'REAUTHENTICATION_REQUIRED')
    assert admin.post(f'{BASE}/auth/reauthenticate', json={'password': 'wrong-' + uuid4().hex}).status_code == 401
    ok = admin.post(f'{BASE}/auth/reauthenticate', json={'password': a['password']})
    assert ok.status_code == 200 and ok.json()['fresh_until']
    r = write(admin, 'PATCH', f'/members/{an["id"]}', {'roles': ['ANALYST']}, match=m['etag'])
    assert r.status_code == 200, r.text


def test_permissions_tenant_isolation_and_removal(database, world, lapi):
    a, an, b = world['users']['admin'], world['users']['analyst'], world['users']['analystB']
    analyst, _ = login(lapi, an['email'], an['password'])
    assert analyst.get(f'{BASE}/members').status_code == 200   # 2B Family 1: listing needs read
    assert write(analyst, 'POST', '/members', {'email': 'x@test.ledgervigil.invalid', 'roles': ['ANALYST']}).status_code == 403
    assert analyst.get(f'{BASE}/audit-events').status_code == 403
    admin, _ = login(lapi, a['email'], a['password'])
    assert all(m['user_id'] != b['id'] for m in admin.get(f'{BASE}/members').json()['items'])
    assert write(admin, 'PATCH', f'/members/{b["id"]}', {'roles': ['ANALYST']}, match='0' * 64).status_code == 404
    m = member(admin, an['id'])
    r = write(admin, 'DELETE', f'/members/{an["id"]}', match=m['etag'])
    assert r.status_code == 200 and r.json()['status'] == 'REVOKED' and r.json()['roles'] == []
    assert analyst.get(f'{BASE}/me').status_code == 401
    assert login(lapi, an['email'], an['password'])[1].status_code == 401   # no other membership: sign-in refused


def test_sign_out_everywhere_and_administrator_session_revocation(database, world, lapi):
    a, u = world['users']['admin'], world['users']['automation']
    first, _ = login(lapi, u['email'], u['password'])
    second, _ = login(lapi, u['email'], u['password'])
    assert first.post(f'{BASE}/auth/sessions/revoke-all').status_code == 204
    assert first.get(f'{BASE}/me').status_code == 401 and second.get(f'{BASE}/me').status_code == 401
    third, _ = login(lapi, u['email'], u['password'])
    admin, _ = login(lapi, a['email'], a['password'])
    r = write(admin, 'POST', f'/members/{u["id"]}/sessions/revoke')
    assert r.status_code == 200 and r.json()['sessions_revoked'] == 1
    assert third.get(f'{BASE}/me').status_code == 401


def test_audit_log_is_paged_and_verifiable(database, world, lapi):
    a = world['users']['admin']
    admin, _ = login(lapi, a['email'], a['password'])
    page = admin.get(f'{BASE}/audit-events', params={'limit': 3})
    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body['items']) == 3 and body['next_before'] is not None
    seqs = [int(e['seq']) for e in body['items']]
    assert seqs == sorted(seqs, reverse=True)
    for e in body['items']:
        assert e['hash'] == hashlib.sha256((e['canonical_content'] + e['prev_hash']).encode()).hexdigest()
    nxt = admin.get(f'{BASE}/audit-events', params={'before': body['next_before'], 'limit': 3}).json()
    assert int(nxt['items'][0]['seq']) < seqs[-1]
    assert body['items'][-1]['prev_hash'] == nxt['items'][0]['hash']   # the pages chain together
    assert admin.get(f'{BASE}/audit-events', params={'cursor': 'x'}).status_code == 400
    assert admin.get(f'{BASE}/audit-events', params={'limit': 1000}).status_code == 422


def test_runtime_role_cannot_bypass_the_functions(database, world, lapi):
    """Direct calls with the API's own database login: no table access, and every function demands a real session."""
    with connection('lv_app', database) as app:
        for statement in ('SELECT * FROM lv_identity.email_token', 'SELECT * FROM lv_identity.token_request', 'SELECT * FROM lv_identity.reauthentication'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(statement)
            app.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("SELECT lv_identity.member_api(%s,'SET_ROLES',%s,'{\"roles\":[\"ADMINISTRATOR\"]}','x','k',repeat('a',64),NULL,NULL,'test','req-1')",
                        (b'\0' * 32, world['users']['analyst']['id']))
        app.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("SELECT lv_identity.audit_page(%s,NULL,10,'test','req-2')", (b'\0' * 32,))
        app.rollback()
        statement = (
            "SELECT lv_identity.token_consume('RESET_PASSWORD',%s,"
            "'$argon2id$v=19$m=65536,t=3,p=4$x$y','test','req-3')"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(statement, (b'\1' * 32,))
        app.rollback()
    with connection('lv_authn', database) as issuer:
        result = issuer.execute(statement, (b'\1' * 32,)).fetchone()[0]
        assert result == {'ok': False, 'throttled': False}
