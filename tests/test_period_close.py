"""Authenticated, authorized, atomic period close (migration 0009) and the R-02 remediation — through the HTTP application
and real PostgreSQL.

Expected behaviour is taken from the contracts, not from the code:
- Stage 2B Family 4: `POST /periods/{id}/close`, permission `administer`, `If-Match` -> Period CLOSED, 200; 409 already
  closed or an earlier period still open; period row FOR UPDATE, status CAS, audit, outbox for the staleness job; Idem Y.
- Stage 2B Part 2 (ETag "v<n>", 428/412), Part 4 codes, Part 8.1 idempotency table row by row, Part 8.2 close vs. post.
- Stage 2A §8.1 (PERIOD_CLOSE commit: counter row lock, commit_batch, ledger_event in one transaction), §8.4 chains,
  §13.1/13.2 outbox + work item, I-12 / P-PER-01 (no entry dated in a CLOSED period).
All identities, tenants and ledgers are SYNTHETIC and created per test.
"""
import hashlib
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4, uuid7

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from tools.dev_db import connection, migrate
from tools.identity_admin import create_user, grant, owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.settings import AuthSettings
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ledger.replay import replay_events
from configuration_support import configuration_connection
from period_close_support import admin_token, close_call, held_close
from test_posting import provision, register, post, request as journal, events, wait_for_block

COOKIE = '__Host-lv_session'
ROUTE = '/api/v1/periods/{}/close'


# ------------------------------------------------------------------------------------------- world
@pytest.fixture
def world(database, app):
    """Tenant A: a provisioned SANDBOX ledger (Oct period) plus Nov; a BOOKS ledger; users of every role.
    Tenant B: its own administrator and SANDBOX ledger."""
    from test_processor_payments import new_tenant
    w = {'A': new_tenant(database), 'B': new_tenant(database), 'users': {}, 'passwords': {}}
    w['cfg'] = provision(app, w['A']); w['cfg']['actor'] = register(database, w['A'])
    w['cfgB'] = provision(app, w['B'])
    with configuration_connection(database) as setup, tenant_transaction(setup,w['A']):
        w['nov'] = setup.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, '2026-11-01', '2026-12-01') "
                               "RETURNING id", (w['A'], w['cfg']['ledger'])).fetchone()[0]
    w['books'] = provision(app, w['A'], purpose='BOOKS')
    with owner_transaction(database) as owner:
        for name, roles, tenant in (('admin', ['ADMINISTRATOR'], 'A'), ('controller', ['CONTROLLER'], 'A'), ('analyst', ['ANALYST'], 'A'),
                                    ('automation', ['AUTOMATION_DEVELOPER'], 'A'), ('adminB', ['ADMINISTRATOR'], 'B')):
            email = f'{name}-{uuid4().hex[:8]}@test.ledgervigil.invalid'
            password = secrets.token_urlsafe(18)
            uid = create_user(owner, email, f'Synthetic {name}', password)
            grant(owner, w[tenant], uid, roles)
            w['users'][name] = {'email': email, 'id': str(uid)}
            w['passwords'][name] = password
    return w


@pytest.fixture
def api(database):
    application = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database))
    yield application
    application.state.pool.close()


def client(application):
    return TestClient(application, base_url='https://testserver', raise_server_exceptions=False,
                      client=(f'10.7.{secrets.randbelow(250)}.{secrets.randbelow(250)}', 40000))


def signed_in(application, w, name):
    c = client(application)
    r = c.post('/api/v1/auth/login', json={'email': w['users'][name]['email'], 'password': w['passwords'][name]})
    assert r.status_code == 200, r.text
    c.headers['X-CSRF-Token'] = r.json()['csrf_token']
    from mfa_support import establish_mfa
    establish_mfa(application, c, w['passwords'][name], r.json())
    return c


def close(c, period, if_match='"v1"', key=None, **headers):
    h = {'Idempotency-Key': key or uuid4().hex, **headers}
    if if_match is not None:
        h['If-Match'] = if_match
    if key is False:
        del h['Idempotency-Key']
    return c.post(ROUTE.format(period), headers=h)


def everything(database):
    """Every row of every lv table plus the identity audit chain and stored responses (superuser view): a full fingerprint."""
    with connection(database=database) as admin:
        tables = [r[0] for r in admin.execute("SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                              "WHERE n.nspname IN ('lv', 'lv_identity') AND c.relkind = 'r' "
                                              "AND c.relname NOT IN ('session', 'login_attempt') ORDER BY 1").fetchall()]
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM {}').format(sql.SQL(t))).fetchall()))).encode()).hexdigest()
                for t in tables}


def period_row(database, period):
    with connection(database=database) as admin:
        return admin.execute('SELECT status::text, version, closed_seq, closed_by::text, close_provenance FROM lv.fiscal_period WHERE id = %s',
                             (period,)).fetchone()


def kinds(database, ledger):
    with connection(database=database) as admin:
        return [r[0] for r in admin.execute('SELECT kind::text FROM lv.commit_batch WHERE ledger_id = %s ORDER BY seq', (ledger,)).fetchall()]


# ------------------------------------------------------------------------------------------- authentication and authorization
def test_unauthenticated_request_is_401_and_changes_nothing(database, api, world):
    before = everything(database)
    r = close(client(api), world['cfg']['period'])
    assert r.status_code == 401 and r.json()['code'] == 'UNAUTHENTICATED'
    assert everything(database) == before


def test_missing_wrong_and_cross_session_csrf_and_foreign_origin_are_refused_before_any_effect(database, api, world):
    admin = signed_in(api, world, 'admin')
    other = signed_in(api, world, 'controller')
    before = everything(database)
    for csrf in (None, 'x' * 64, other.headers['X-CSRF-Token']):
        c = signed_in(api, world, 'admin')
        if csrf is None:
            del c.headers['X-CSRF-Token']
        else:
            c.headers['X-CSRF-Token'] = csrf
        r = close(c, world['cfg']['period'])
        assert r.status_code == 403 and r.json()['code'] == 'PERMISSION_DENIED' and 'CSRF' in r.json()['detail']
    r = close(admin, world['cfg']['period'], Origin='https://evil.example')
    assert r.status_code == 403 and 'cross-origin' in r.json()['detail']
    assert period_row(database, world['cfg']['period'])[0] == 'OPEN'
    assert kinds(database, world['cfg']['ledger']) == []
    # Only the new login sessions differ; no lv table and no audit event changed.
    after = everything(database)
    assert {k for k in after if after[k] != before[k]} <= {'lv_identity.audit_event', 'lv_identity.audit_chain_head'}
    with connection(database=database) as a:
        assert not a.execute("SELECT 1 FROM lv_identity.audit_event WHERE event_type LIKE 'PERIOD%%' AND tenant_id = %s", (world['A'],)).fetchall()


@pytest.mark.parametrize('role', ['analyst', 'controller', 'automation'])
def test_roles_without_administer_are_refused(database, api, world, role):
    c = signed_in(api, world, role)
    r = close(c, world['cfg']['period'])
    assert r.status_code == 403 and r.json()['code'] == 'PERMISSION_DENIED' and r.json()['detail'] == 'requires administer'
    assert period_row(database, world['cfg']['period'])[:2] == ('OPEN', 1)
    assert 'administer' not in c.get('/api/v1/me').json()['permissions']
    with connection(database=database) as a:      # refused by the HTTP boundary itself: the close function was never reached
        assert a.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type LIKE 'PERIOD_CLOSE%%' AND user_id = %s",
                         (world['users'][role]['id'],)).fetchone() == (0,)


def test_administrator_closes_but_still_has_no_financial_approval_or_execution(api, world):
    c = signed_in(api, world, 'admin')
    perms = set(c.get('/api/v1/me').json()['permissions'])
    assert 'administer' in perms and perms.isdisjoint({'propose', 'review', 'execute'})


def test_other_tenant_period_is_404_and_audited_as_refused(database, api, world):
    c = signed_in(api, world, 'adminB')
    for target in (world['cfg']['period'], uuid7()):
        r = close(c, target)
        assert r.status_code == 404 and r.json()['code'] == 'NOT_FOUND'
    assert period_row(database, world['cfg']['period'])[:2] == ('OPEN', 1)
    with connection(database=database) as a:
        refused = a.execute("SELECT tenant_id::text, detail->>'reason' FROM lv_identity.audit_event WHERE event_type = 'PERIOD_CLOSE_REFUSED' "
                            "AND user_id = %s", (world['users']['adminB']['id'],)).fetchall()
    assert refused == [(str(world['B']), 'NOT_FOUND')] * 2    # attributed to B's own chain; A's tenant is never named
    assert kinds(database, world['cfg']['ledger']) == []


def test_spoofed_tenant_or_user_inputs_cannot_redirect_the_close(database, api, world):
    c = signed_in(api, world, 'adminB')
    r = c.post(ROUTE.format(world['cfg']['period']) + f'?tenant_id={world["A"]}',
               headers={'Idempotency-Key': uuid4().hex, 'If-Match': '"v1"', 'X-Tenant-Id': str(world['A']),
                        'X-User-Id': world['users']['admin']['id']})
    assert r.status_code == 404
    assert period_row(database, world['cfg']['period'])[0] == 'OPEN'


# ------------------------------------------------------------------------------------------- the successful close
def test_close_commits_period_event_counter_outbox_audit_and_response_together(database, api, world):
    cfg = world['cfg']
    seed_conn = connection('lv_seed', database, autocommit=True)
    try:
        post(seed_conn, cfg)                                                   # one posting in October first (seq 1)
    finally:
        seed_conn.close()
    with connection(database=database) as a:
        balances_before = a.execute('SELECT * FROM lv.account_balance_current WHERE ledger_id = %s ORDER BY account_id', (cfg['ledger'],)).fetchall()
        head_before = a.execute('SELECT last_seq, last_entry_no, batch_head, event_head FROM lv.ledger_counter WHERE ledger_id = %s',
                                (cfg['ledger'],)).fetchone()
    c = signed_in(api, world, 'admin')
    r = close(c, cfg['period'], key='close-oct-1', **{'X-Request-Id': 'req-close-oct-1'})
    assert r.status_code == 200, r.text
    assert r.headers['etag'] == '"v2"' and 'idempotent-replayed' not in r.headers
    body = r.json()
    assert (body['id'], body['ledger_id'], body['status'], body['version'], body['close_seq'], body['closed_by'], body['close_provenance']) == \
        (str(cfg['period']), str(cfg['ledger']), 'CLOSED', 2, '2', world['users']['admin']['id'], 'AUDITED_COMMIT')
    assert body['staleness_job']['kind'] == 'STALENESS_SCAN' and body['staleness_job']['state'] == 'QUEUED'
    assert c.get('/api/v1/me').status_code == 200          # the administrator's session is unaffected by the close
    with connection(database=database) as a:
        batch = a.execute("SELECT seq, kind::text, created_by, created_by_user::text, previous_hash FROM lv.commit_batch WHERE ledger_id = %s AND seq = 2",
                          (cfg['ledger'],)).fetchone()
        assert batch == (2, 'PERIOD_CLOSE', None, world['users']['admin']['id'], head_before[2])
        event = a.execute('SELECT canonical_content, previous_hash FROM lv.ledger_event WHERE ledger_id = %s AND seq = 2', (cfg['ledger'],)).fetchone()
        payload = json.loads(event[0])
        assert event[1] == head_before[3]
        assert {k: payload[k] for k in ('kind', 'period_id', 'start_date', 'end_date', 'period_version_before', 'period_version_after',
                                         'created_by_user', 'request_id')} == {
            'kind': 'PERIOD_CLOSE', 'period_id': str(cfg['period']), 'start_date': '2026-10-01', 'end_date': '2026-11-01',
            'period_version_before': '1', 'period_version_after': '2', 'created_by_user': world['users']['admin']['id'],
            'request_id': 'req-close-oct-1'}
        counter = a.execute('SELECT last_seq, last_entry_no FROM lv.ledger_counter WHERE ledger_id = %s', (cfg['ledger'],)).fetchone()
        assert counter == (2, head_before[1])                                   # sequence advanced, no entry number consumed
        assert a.execute('SELECT count(*) FROM lv.journal_entry WHERE ledger_id = %s', (cfg['ledger'],)).fetchone()[0] == 1
        assert a.execute('SELECT * FROM lv.account_balance_current WHERE ledger_id = %s ORDER BY account_id', (cfg['ledger'],)).fetchall() == balances_before
        work = a.execute("SELECT id::text, kind, subject_type, subject_id, trigger_seq, state FROM lv.work_item WHERE ledger_id = %s",
                         (cfg['ledger'],)).fetchall()
        assert work == [(body['staleness_job']['id'], 'STALENESS_SCAN', 'PERIOD', cfg['period'], 2, 'QUEUED')]
        assert payload['staleness_work_item_id'] == work[0][0]
        outbox = a.execute('SELECT topic, status, dedupe_key, dispatched_at FROM lv.outbox WHERE work_item_id = %s', (work[0][0],)).fetchall()
        assert outbox == [('maintenance', 'PENDING', f'STALENESS_SCAN:{cfg["ledger"]}:{cfg["period"]}:2', None)]
        audit = a.execute("SELECT outcome, user_id::text, tenant_id::text, session_id::text, request_id, source_key, detail FROM lv_identity.audit_event "
                          "WHERE event_type = 'PERIOD_CLOSED' AND tenant_id = %s", (world['A'],)).fetchall()
        assert len(audit) == 1 and audit[0][:3] == ('ALLOWED', world['users']['admin']['id'], str(world['A']))
        assert audit[0][3] == payload['session_id'] and audit[0][4] == 'req-close-oct-1' and audit[0][5].startswith('10.7.')
        assert audit[0][6] == {'ledger_id': str(cfg['ledger']), 'period_id': str(cfg['period']), 'seq': '2', 'start_date': '2026-10-01',
                               'end_date': '2026-11-01', 'period_version': '2', 'staleness_work_item_id': work[0][0]}
        stored = a.execute("SELECT response_status, response->'body' FROM lv.api_idempotency WHERE idempotency_key = 'close-oct-1' AND tenant_id = %s",
                           (world['A'],)).fetchone()
        assert stored == (200, body)
    with connection('lv_app', database, autocommit=True) as reader:
        replay = replay_events(events(reader, cfg))
    assert replay['closed_periods'] == [str(cfg['period'])] and replay['entries'] == 1


def test_period_list_exposes_versions_and_provenance_to_read_permission(database, api, world):
    cfg = world['cfg']
    analyst = signed_in(api, world, 'analyst')
    listed = analyst.get(f'/api/v1/ledgers/{cfg["ledger"]}/periods').json()
    assert [(p['start_date'], p['status'], p['version']) for p in listed['items']] == [('2026-10-01', 'OPEN', 1), ('2026-11-01', 'OPEN', 1)]
    assert close(signed_in(api, world, 'admin'), cfg['period']).status_code == 200
    listed = analyst.get(f'/api/v1/ledgers/{cfg["ledger"]}/periods').json()
    assert [(p['status'], p['version'], p['close_provenance']) for p in listed['items']] == [('CLOSED', 2, 'AUDITED_COMMIT'), ('OPEN', 1, None)]
    assert analyst.get(f'/api/v1/ledgers/{world["cfgB"]["ledger"]}/periods').status_code == 404
    assert client(api).get(f'/api/v1/ledgers/{cfg["ledger"]}/periods').status_code == 401


# ------------------------------------------------------------------------------------------- contract rules
def test_earlier_open_period_blocks_a_later_close(database, api, world):
    c = signed_in(api, world, 'admin')
    r = close(c, world['nov'])
    assert r.status_code == 409 and r.json()['code'] == 'INVALID_STATE_TRANSITION' and r.json()['state'] == 'OPEN'
    assert 'earlier period' in r.json()['detail'] and period_row(database, world['nov'])[:2] == ('OPEN', 1)
    assert close(c, world['cfg']['period']).status_code == 200
    assert close(c, world['nov']).status_code == 200
    assert kinds(database, world['cfg']['ledger']) == ['PERIOD_CLOSE', 'PERIOD_CLOSE']


def test_only_native_sandbox_ledgers_can_close(database, api, world):
    c = signed_in(api, world, 'admin')
    r = close(c, world['books']['period'])
    assert r.status_code == 422 and r.json()['code'] == 'CAPABILITY_UNAVAILABLE' and r.json()['class'] == 'UNSUPPORTED'
    assert period_row(database, world['books']['period'])[:2] == ('OPEN', 1) and kinds(database, world['books']['ledger']) == []
    with connection(database=database) as a:      # the refusal rolled back its counter row too: no partial side effect
        assert a.execute('SELECT count(*) FROM lv.ledger_counter WHERE ledger_id = %s', (world['books']['ledger'],)).fetchone() == (0,)


def test_idempotency_and_version_contract_row_by_row(database, api, world):
    period = world['cfg']['period']
    c = signed_in(api, world, 'admin')
    missing_key = close(c, period, key=False)
    assert missing_key.status_code == 400 and missing_key.json()['code'] == 'IDEMPOTENCY_KEY_REQUIRED'
    bad_key = close(c, period, key='has space')
    assert bad_key.status_code == 422 and bad_key.json()['code'] == 'SCHEMA_INVALID'
    no_version = close(c, period, if_match=None, key='k-428')
    assert no_version.status_code == 428 and no_version.json()['code'] == 'VERSION_REQUIRED'
    again = close(c, period, if_match=None, key='k-428')                         # 4xx stored and replayed
    assert again.status_code == 428 and again.headers['idempotent-replayed'] == 'true'
    fixed_same_key = close(c, period, key='k-428')                               # "a fixed request needs a new key"
    assert fixed_same_key.status_code == 422 and fixed_same_key.json()['code'] == 'IDEMPOTENCY_KEY_REUSED'
    assert period_row(database, period)[:2] == ('OPEN', 1)
    first = close(c, period, key='k-1')
    assert first.status_code == 200
    replay = close(c, period, key='k-1')                                         # same key, same request: same response
    assert replay.status_code == 200 and replay.headers['idempotent-replayed'] == 'true'
    assert replay.json() == first.json() and replay.headers['etag'] == first.headers['etag']
    reused = close(c, period, if_match='"v2"', key='k-1')
    assert reused.status_code == 422 and reused.json()['code'] == 'IDEMPOTENCY_KEY_REUSED'
    stale = close(c, period, key='k-2')                                          # new key, old version
    assert stale.status_code == 412 and stale.json()['code'] == 'VERSION_MISMATCH' and stale.json()['current_version'] == 2
    closed = close(c, period, if_match='"v2"', key='k-3')                        # new key, current version, wrong state
    assert closed.status_code == 409 and closed.json()['code'] == 'INVALID_STATE_TRANSITION' and closed.json()['state'] == 'CLOSED'
    assert close(c, period, if_match='v2', key='k-3').json()['code'] == 'IDEMPOTENCY_KEY_REUSED'   # raw header value is the request
    assert close(c, period, if_match='"v2"', key='k-3').headers['idempotent-replayed'] == 'true'
    assert kinds(database, world['cfg']['ledger']) == ['PERIOD_CLOSE']           # exactly one close despite seven requests
    with connection(database=database) as a:
        assert a.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type = 'PERIOD_CLOSED' AND tenant_id = %s", (world['A'],)).fetchone() == (1,)
        assert a.execute("SELECT count(*) FROM lv.work_item WHERE tenant_id = %s", (world['A'],)).fetchone() == (1,)
        # Keys are scoped per principal: another administrator's identical key is a different request.
    other_admin = admin_token(database, world['A'])
    with connection('lv_app', database) as conn:
        assert close_call(conn, other_admin, world['nov'], key='k-1')['status'] == 200


def test_expired_key_is_treated_as_new_but_state_cas_still_prevents_a_duplicate(database, api, world):
    period = world['cfg']['period']
    c = signed_in(api, world, 'admin')
    assert close(c, period, key='k-exp').status_code == 200
    with connection(database=database) as a:          # TEST ONLY: age the stored response past its 24-hour retention
        a.execute('SET LOCAL session_replication_role = replica')
        a.execute("UPDATE lv.api_idempotency SET created_at = created_at - interval '25 hours', expires_at = expires_at - interval '25 hours' "
                  "WHERE idempotency_key = 'k-exp'")
    r = close(c, period, key='k-exp')
    assert r.status_code == 412 and 'idempotent-replayed' not in r.headers   # processed afresh; the version CAS refuses it
    assert kinds(database, world['cfg']['ledger']) == ['PERIOD_CLOSE']


def test_same_key_while_the_first_request_runs_is_409_request_in_progress(database, api, world):
    period = world['cfg']['period']
    c = signed_in(api, world, 'admin')
    token = c.cookies.get(COOKIE)
    with held_close(database, token, period, if_match='"v1"', key='k-run') as (_, first):
        assert first['status'] == 200
        r = close(c, period, key='k-run')
        assert r.status_code == 409 and r.json()['code'] == 'REQUEST_IN_PROGRESS'
        assert r.json()['retryable'] is True and r.headers['retry-after'] == '1'
    r = close(c, period, key='k-run')
    assert r.status_code == 200 and r.headers['idempotent-replayed'] == 'true' and r.json()['version'] == 2
    assert kinds(database, world['cfg']['ledger']) == ['PERIOD_CLOSE']


# ------------------------------------------------------------------------------------------- concurrency (deterministic)
def test_two_closes_of_one_period_serialize_and_exactly_one_wins(database, world):
    period = world['cfg']['period']
    a_token, b_token = admin_token(database, world['A']), admin_token(database, world['A'])
    ready, pid = threading.Event(), []

    def second():
        with connection('lv_app', database) as c:
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout = '6s'"); ready.set()
            return close_call(c, b_token, period, key='second')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with held_close(database, a_token, period, key='first') as (holder, first):
            assert first['status'] == 200
            future = pool.submit(second); assert ready.wait(3)
            wait_for_block(database, pid[0], holder.info.backend_pid)       # the second close is waiting on our lock
        result = future.result(timeout=8)
    assert result['status'] == 412 and result['code'] == 'VERSION_MISMATCH' and result['current_version'] == 2
    assert kinds(database, world['cfg']['ledger']) == ['PERIOD_CLOSE']


def test_close_waits_for_a_pending_typed_payment_then_commits_after_it(database, world):
    from test_processor_payments import payment, opening
    cfg = world['cfg']
    token = admin_token(database, world['A'])
    ready, pid = threading.Event(), []

    def closing():
        with connection('lv_app', database) as c:
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout = '6s'"); ready.set()
            return close_call(c, token, cfg['period'])
    seed_conn = connection('lv_seed', database, autocommit=True)
    try:
        opening(seed_conn, cfg)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with tenant_transaction(seed_conn, cfg['tenant']):
                seed_conn.execute('SELECT lv.seed_processor_payment(%s, %s)', (cfg['ledger'], Jsonb(payment(cfg))))
                future = pool.submit(closing); assert ready.wait(3)
                wait_for_block(database, pid[0], seed_conn.info.backend_pid)
            assert future.result(timeout=8)['status'] == 200
    finally:
        seed_conn.close()
    assert kinds(database, cfg['ledger']) == ['SEED', 'SEED', 'PERIOD_CLOSE']   # the payment committed first, inside the open period


def test_close_and_a_posting_in_another_open_period_serialize_on_the_ledger_counter(database, world):
    """Gapless sequence (2A §8.1): a close must wait for an in-flight commit on the same ledger even when they touch
    different periods, then take the next sequence number."""
    cfg = world['cfg']
    token = admin_token(database, world['A'])
    november = journal(cfg, ref='november-pending'); november['effective_date'] = '2026-11-10'
    committed = journal(cfg, ref='november-committed'); committed['effective_date'] = '2026-11-09'
    ready, pid = threading.Event(), []

    def closing():
        with connection('lv_app', database) as c:
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout = '6s'"); ready.set()
            return close_call(c, token, cfg['period'])
    seed_conn = connection('lv_seed', database, autocommit=True)
    try:
        assert post(seed_conn, cfg, committed)['seq'] == '1'          # the counter row already exists (seq 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with tenant_transaction(seed_conn, cfg['tenant']):
                assert seed_conn.execute('SELECT lv.seed_manual_journal(%s, %s)', (cfg['ledger'], Jsonb(november))).fetchone()[0]['seq'] == '2'
                future = pool.submit(closing); assert ready.wait(3)
                wait_for_block(database, pid[0], seed_conn.info.backend_pid)
            result = future.result(timeout=8)
    finally:
        seed_conn.close()
    assert result['status'] == 200 and result['body']['close_seq'] == '3'
    assert kinds(database, cfg['ledger']) == ['SEED', 'SEED', 'PERIOD_CLOSE']


def test_close_waits_on_the_ledger_counter_lock_held_by_any_commit(database, world):
    """Lock protocol: every commit path holds ledger_counter FOR UPDATE for its whole transaction. A close must queue
    behind that lock (not merely behind a unique-key conflict), then take the next sequence."""
    cfg = world['cfg']
    token = admin_token(database, world['A'])
    seed_conn = connection('lv_seed', database, autocommit=True)
    try:
        assert post(seed_conn, cfg)['seq'] == '1'
    finally:
        seed_conn.close()
    ready, pid = threading.Event(), []

    def closing():
        with connection('lv_app', database) as c:
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout = '6s'"); ready.set()
            return close_call(c, token, cfg['period'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connection(database=database) as holder:        # stands in for a commit path between its counter lock and COMMIT
            holder.execute('SELECT 1 FROM lv.ledger_counter WHERE ledger_id = %s FOR UPDATE', (cfg['ledger'],))
            future = pool.submit(closing); assert ready.wait(3)
            wait_for_block(database, pid[0], holder.info.backend_pid)
            holder.rollback()
        result = future.result(timeout=8)
    assert result['status'] == 200 and result['body']['close_seq'] == '2'


def test_period_insert_cannot_slip_an_open_period_under_a_concurrent_close(database, app, world):
    cfg = world['cfg']
    token = admin_token(database, world['A'])
    ready, pid = threading.Event(), []

    def inserting():
        with configuration_connection(database) as c:
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout = '6s'"); ready.set()
            with pytest.raises(psycopg.errors.CheckViolation, match='earlier than a closed period'):
                with tenant_transaction(c, cfg['tenant']):
                    c.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, '2026-09-01', '2026-10-01')",
                              (cfg['tenant'], cfg['ledger']))
            return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with held_close(database, token, cfg['period']) as (holder, result):
            assert result['status'] == 200
            future = pool.submit(inserting); assert ready.wait(3)
            wait_for_block(database, pid[0], holder.info.backend_pid)
        assert future.result(timeout=8)


# ------------------------------------------------------------------------------------------- posting paths after close (I-12)
def test_every_posting_path_refuses_a_closed_period(database, api, world):
    from test_processor_payments import payment, opening, pay
    from test_processor_settlements import settlement, settle
    cfg = world['cfg']
    seed_conn = connection('lv_seed', database, autocommit=True)
    try:
        opening(seed_conn, cfg); doc = pay(seed_conn, cfg)['document_id']
        assert close(signed_in(api, world, 'admin'), cfg['period']).status_code == 200
        before = everything(database)
        with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
            post(seed_conn, cfg, journal(cfg, ref='after-close'))                              # manual journal
        with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
            pay(seed_conn, cfg, payment(cfg, processor_item_id='PMT-9001'))                    # typed payment
        with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
            settle(seed_conn, cfg, settlement([(doc, '50000.00')]))                             # typed settlement
        assert everything(database) == before
    finally:
        seed_conn.close()
    # (The read-only kernel preview's PERIOD_CLOSED refusal is test_settlement_adapter.py::test_closed_period_refused_by_the_kernel,
    #  which now closes through this audited path.)
    with connection(database=database) as owner:                                                # the trigger backstop (I-12)
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(cfg['tenant']),))
        with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
            owner.execute("INSERT INTO lv.journal_entry(tenant_id, ledger_id, entry_no, seq, kind, effective_date, period_id, currency, "
                          "currency_exponent, source_document_id, source_document_version, posting_rule_id, posting_rule_version, provenance, "
                          "created_by, description) SELECT tenant_id, ledger_id, 99, 1, 'MANUAL_ADJUSTMENT', '2026-10-20', %s, 'INR', 2, "
                          "source_document_id, 1, 'x', '1', 'SYNTHETIC', created_by, 'x' FROM lv.journal_entry LIMIT 1", (cfg['period'],))


# ------------------------------------------------------------------------------------------- R-02: direct-SQL bypass attempts
def test_runtime_roles_cannot_close_or_forge_a_close_by_direct_sql(database, app, world):
    cfg = world['cfg']
    with connection(database=database) as a:
        for role in ('lv_app', 'lv_seed', 'lv_ingest'):
            for privilege in ('UPDATE', 'DELETE', 'TRUNCATE'):
                assert a.execute("SELECT has_table_privilege(%s, 'lv.fiscal_period', %s)", (role, privilege)).fetchone() == (False,), (role, privilege)
            for table in ('commit_batch', 'ledger_event', 'ledger_counter', 'work_item', 'outbox', 'api_idempotency'):
                for privilege in ('INSERT', 'UPDATE', 'DELETE'):
                    assert a.execute('SELECT has_table_privilege(%s, %s, %s)', (role, 'lv.' + table, privilege)).fetchone() == (False,)
            assert a.execute("SELECT has_function_privilege(%s, 'lv.close_period(bytea, uuid, text, text, text, text)', 'EXECUTE')",
                             (role,)).fetchone() == (role == 'lv_app',)
    statements = ["UPDATE lv.fiscal_period SET status = 'CLOSED' WHERE id = %(p)s",
                  "UPDATE lv.fiscal_period SET version = version + 1 WHERE id = %(p)s",
                  "INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date, status) VALUES (%(t)s, %(l)s, '2027-01-01', '2027-02-01', 'CLOSED')",
                  "INSERT INTO lv.commit_batch(tenant_id, ledger_id, seq, kind, created_by_user, previous_hash, canonical_content, hash) "
                  "VALUES (%(t)s, %(l)s, 1, 'PERIOD_CLOSE', NULL, repeat('0', 64), '{}', repeat('0', 64))",
                  "SELECT * FROM lv.outbox", "SELECT * FROM lv.api_idempotency", "SELECT * FROM lv.work_item",
                  "SELECT lv.period_close_refusal(200, 'X', 'x', NULL)", "SELECT lv.period_json(p) FROM lv.fiscal_period p"]
    for statement in statements:
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)):
            with tenant_transaction(app, cfg['tenant']):
                app.execute(statement, {'p': cfg['period'], 't': cfg['tenant'], 'l': cfg['ledger']} if '%(' in statement else None)
    # The function itself demands a real session with administer in that session's tenant.
    analyst = admin_token(database, world['A'], roles=('ANALYST',))
    admin_b = admin_token(database, world['B'])
    admin = admin_token(database, world['A'])
    with connection('lv_app', database) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='session'):
            c.execute("SELECT lv.close_period(%s, %s, '\"v1\"', 'k', 'sql', 'sql')", (b'\x00' * 32, cfg['period']))
        c.rollback()
        assert close_call(c, analyst, cfg['period'])['status'] == 403; c.commit()
        assert close_call(c, admin_b, cfg['period'])['status'] == 404; c.commit()
        # A caller that bound ANOTHER tenant's context first cannot borrow an administrator session from tenant A.
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (str(world['B']),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='different session'):
            c.execute("SELECT lv.close_period(%s, %s, '\"v1\"', 'k2', 'sql', 'sql')", (token_hash(admin), cfg['period']))
        c.rollback()
    assert period_row(database, cfg['period'])[:2] == ('OPEN', 1)
    with connection(database=database) as a:
        denied = a.execute("SELECT detail->>'reason' FROM lv_identity.audit_event WHERE event_type = 'PERIOD_CLOSE_DENIED' AND tenant_id = %s",
                           (world['A'],)).fetchall()
    assert denied == [('PERMISSION_DENIED',)]


def test_even_the_owner_cannot_close_without_the_audited_commit(database, world):
    cfg = world['cfg']
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(cfg['tenant']),))
        for statement in ("UPDATE lv.fiscal_period SET status = 'CLOSED', close_provenance = 'LEGACY_UNAUDITED' WHERE id = %s",
                          "UPDATE lv.fiscal_period SET status = 'CLOSED', version = 2, closed_seq = NULL, closed_at = now(), "
                          "closed_by = (SELECT id FROM lv_identity.app_user LIMIT 1), close_provenance = 'AUDITED_COMMIT' WHERE id = %s"):
            with pytest.raises(psycopg.errors.CheckViolation):
                with owner.transaction():
                    owner.execute(statement, (cfg['period'],))
    assert period_row(database, cfg['period'])[:2] == ('OPEN', 1)


def test_close_function_is_pinned_and_resists_caller_temporary_objects(database, world):
    """R-01 regression retained for the new definer: caller temporary domains must never be resolved."""
    with connection(database=database) as a:
        config = a.execute("SELECT proconfig, prosecdef, pg_get_userbyid(proowner) FROM pg_proc WHERE oid = "
                           "'lv.close_period(bytea, uuid, text, text, text, text)'::regprocedure").fetchone()
    assert config == (['search_path=pg_catalog, pg_temp'], True, 'lv_owner')
    token = admin_token(database, world['A'])
    with connection(database=database, autocommit=True) as a:
        a.execute(sql.SQL('GRANT TEMPORARY ON DATABASE {} TO lv_app').format(sql.Identifier(database)))
    try:
        with connection('lv_app', database) as c:
            c.execute("""
              CREATE FUNCTION pg_temp.shadow_probe(v text) RETURNS boolean LANGUAGE plpgsql AS $$
              BEGIN RAISE EXCEPTION 'SHADOW EXECUTED AS %', current_user; END $$;
              CREATE DOMAIN pg_temp.uuid AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.jsonb AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.text AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.bigint AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.integer AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.timestamptz AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
            """)
            assert close_call(c, token, world['cfg']['period'])['status'] == 200
    finally:
        with connection(database=database, autocommit=True) as a:
            a.execute(sql.SQL('REVOKE TEMPORARY ON DATABASE {} FROM lv_app').format(sql.Identifier(database)))
    assert period_row(database, world['cfg']['period'])[0] == 'CLOSED'


# ------------------------------------------------------------------------------------------- failure injection
FAULT_POINTS = [('lv.commit_batch', 'AFTER INSERT'), ('lv.fiscal_period', 'AFTER UPDATE'), ('lv.work_item', 'AFTER INSERT'),
                ('lv.outbox', 'AFTER INSERT'), ('lv.ledger_event', 'AFTER INSERT'), ('lv.ledger_counter', 'AFTER UPDATE'),
                ('lv_identity.audit_event', 'AFTER INSERT'), ('lv.api_idempotency', 'AFTER INSERT')]


def inject(database, table, timing, deferred=False):
    with connection(database=database, autocommit=True) as a:
        # Precondition (setup, not the property under test): the definer's role can reach the fault function.
        assert a.execute("SELECT has_schema_privilege('lv_owner', 'public', 'USAGE')").fetchone() == (True,)
        a.execute("CREATE OR REPLACE FUNCTION public.lv_test_fault() RETURNS trigger LANGUAGE plpgsql AS "
                  "$$ BEGIN RAISE EXCEPTION 'INJECTED FAULT at %', TG_TABLE_NAME; END $$")
        a.execute('GRANT EXECUTE ON FUNCTION public.lv_test_fault() TO PUBLIC')
        kind = 'CONSTRAINT TRIGGER' if deferred else 'TRIGGER'
        tail = ' DEFERRABLE INITIALLY DEFERRED' if deferred else ''
        a.execute(f'CREATE {kind} lv_test_fault {timing} ON {table}{tail} FOR EACH ROW EXECUTE FUNCTION public.lv_test_fault()')


def remove(database, table):
    with connection(database=database, autocommit=True) as a:
        a.execute(f'DROP TRIGGER IF EXISTS lv_test_fault ON {table}')


@pytest.mark.parametrize('table,timing', FAULT_POINTS, ids=[t for t, _ in FAULT_POINTS])
def test_a_fault_at_any_write_rolls_back_the_whole_close_and_frees_the_key(database, api, world, table, timing):
    period = world['cfg']['period']
    c = signed_in(api, world, 'admin')
    before = everything(database)
    inject(database, table, timing)
    try:
        r = close(c, period, key='k-fault')
        assert r.status_code == 500 and r.json()['code'] == 'INTERNAL' and 'INJECTED' not in r.text
        assert everything(database) == before                                    # nothing at all was written
    finally:
        remove(database, table)
    retry = close(c, period, key='k-fault')                                       # Part 8.1: a 5xx stores nothing; the key is free
    assert retry.status_code == 200 and 'idempotent-replayed' not in retry.headers
    assert kinds(database, world['cfg']['ledger']) == ['PERIOD_CLOSE']


def test_a_commit_time_fault_is_a_500_never_a_reported_success(database, api, world):
    period = world['cfg']['period']
    c = signed_in(api, world, 'admin')
    before = everything(database)
    inject(database, 'lv.commit_batch', 'AFTER INSERT', deferred=True)
    try:
        r = close(c, period, key='k-commit')
        assert r.status_code == 500 and r.json()['code'] == 'INTERNAL'
        assert everything(database) == before
    finally:
        remove(database, 'lv.commit_batch')
    assert close(c, period, key='k-commit').status_code == 200


def test_a_fault_while_storing_a_refusal_stores_nothing(database, api, world):
    c = signed_in(api, world, 'admin')
    before = everything(database)
    inject(database, 'lv.api_idempotency', 'AFTER INSERT')
    try:
        assert close(c, world['nov'], key='k-409').status_code == 500
        assert everything(database) == before
    finally:
        remove(database, 'lv.api_idempotency')
    r = close(c, world['nov'], key='k-409')
    assert r.status_code == 409 and 'idempotent-replayed' not in r.headers


def test_commit_validator_refuses_a_period_close_batch_without_its_effects(database, world):
    """Backstop for a future writer that forgets an effect: even the schema owner cannot commit a PERIOD_CLOSE batch
    unless the period CAS, ledger event, work item, outbox row, audit event and stored response all match it."""
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner')
        owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(world['A']),))
        content = json.dumps({'kind': 'PERIOD_CLOSE', 'period_id': str(world['cfg']['period'])}, separators=(',', ':'), sort_keys=True)
        owner.execute("INSERT INTO lv.commit_batch(tenant_id, ledger_id, seq, kind, created_by_user, previous_seq, previous_hash, canonical_content, hash) "
                      "VALUES (%s, %s, 1, 'PERIOD_CLOSE', %s, NULL, repeat('0', 64), %s, encode(sha256(convert_to(%s || repeat('0', 64), 'UTF8')), 'hex'))",
                      (world['A'], world['cfg']['ledger'], world['users']['admin']['id'], content, content))
        with pytest.raises(psycopg.errors.CheckViolation, match='incomplete period-close commit'):
            owner.commit()
    assert period_row(database, world['cfg']['period'])[:2] == ('OPEN', 1) and kinds(database, world['cfg']['ledger']) == []


# ------------------------------------------------------------------------------------------- history and replay
def test_replay_handles_close_events_and_detects_tampering(database, api, world):
    cfg = world['cfg']
    seed_conn = connection('lv_seed', database, autocommit=True)
    try:
        post(seed_conn, cfg)
        assert close(signed_in(api, world, 'admin'), cfg['period']).status_code == 200
        later = journal(cfg, ref='november-1'); later['effective_date'] = '2026-11-05'
        assert post(seed_conn, cfg, later)['seq'] == '3'
    finally:
        seed_conn.close()
    with connection('lv_app', database, autocommit=True) as reader:
        chain = events(reader, cfg)
    trailing = [dict(chain[0]), dict(chain[1])]                     # a chain that ENDS with the close event
    assert replay_events(trailing)['closed_periods'] == [str(cfg['period'])]
    trailing[1]['canonical_content'] = trailing[1]['canonical_content'].replace('2026-10-01', '2026-09-01')
    with pytest.raises(ValueError, match='chain mismatch'):
        replay_events(trailing)
    result = replay_events(chain)
    assert result['entries'] == 2 and result['closed_periods'] == [str(cfg['period'])]
    tampered = [dict(e) for e in chain]
    tampered[1]['canonical_content'] = tampered[1]['canonical_content'].replace('2026-10-01', '2026-09-01')
    with pytest.raises(ValueError):
        replay_events(tampered)
    with pytest.raises(ValueError):
        replay_events([chain[0], chain[2]])


# ------------------------------------------------------------------------------------------- migrations
def columns_hash(database, schema_tables):
    """Hash each table over the columns it had BEFORE 0009 (so added columns do not mask changes to existing data)."""
    out = {}
    with connection(database=database) as admin:
        for table, cols in schema_tables.items():
            rows = admin.execute(sql.SQL('SELECT {} FROM lv.{}').format(sql.SQL(', ').join(map(sql.Identifier, cols)), sql.Identifier(table))).fetchall()
            out[table] = hashlib.sha256('\n'.join(sorted(map(repr, rows))).encode()).hexdigest()
    return out


def test_upgrade_from_populated_0008_preserves_records_and_labels_legacy_closes():
    from test_processor_payments import disposable, new_tenant, opening, pay
    from test_processor_settlements import settle, settlement
    with disposable('0008_identity_force_rls') as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            cfg = provision(app_, tenant); legacy = provision(app_, tenant)
            with tenant_transaction(app_, tenant):                       # the pre-0009 unaudited path (R-02), still possible at 0008
                app_.execute("UPDATE lv.fiscal_period SET status = 'CLOSED' WHERE id = %s", (legacy['period'],))
                nov = app_.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, '2026-11-01', '2026-12-01') "
                                   "RETURNING id", (tenant, cfg['ledger'])).fetchone()[0]
        register(name, tenant)
        with connection('lv_seed', name, autocommit=True) as s:
            opening(s, cfg); doc = pay(s, cfg)['document_id']; settle(s, cfg, settlement([(doc, '50000.00')]))
        with connection(database=name) as admin:
            tables = [r[0] for r in admin.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                                  "WHERE n.nspname = 'lv' AND c.relkind = 'r' ORDER BY 1").fetchall()]
            cols = {t: [r[0] for r in admin.execute("SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
                                                    "AND NOT attisdropped ORDER BY attnum", ('lv.' + t,)).fetchall()] for t in tables}
        assert len(tables) == 29
        before = columns_hash(name, cols)
        migrate(name)
        assert columns_hash(name, cols) == before                       # every pre-existing row and column unchanged
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0040_mfa_reset',)
            rows = dict(admin.execute('SELECT id, close_provenance FROM lv.fiscal_period WHERE ledger_id IN (%s, %s)',
                                      (cfg['ledger'], legacy['ledger'])).fetchall())
        assert rows == {legacy['period']: 'LEGACY_UNAUDITED', cfg['period']: None, nov: None}
        token = admin_token(name, tenant)
        with connection('lv_app', name) as c:
            assert close_call(c, token, cfg['period'])['status'] == 200
        with connection('lv_app', name, autocommit=True) as reader:
            chain = events(reader, cfg)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with tenant_transaction(reader, tenant):
                    reader.execute("UPDATE lv.fiscal_period SET status = 'CLOSED' WHERE id = %s", (nov,))
        assert [json.loads(e['canonical_content'])['kind'] for e in chain] == ['SEED', 'SEED', 'SEED', 'PERIOD_CLOSE']
        assert replay_events(chain)['closed_periods'] == [str(cfg['period'])]


def test_fresh_chain_and_guarded_downgrade():
    from test_processor_payments import disposable, new_tenant
    with disposable() as name:
        migrate(name, '0008_identity_force_rls', downgrade=True, disposable=True)
        with connection(database=name) as admin:
            assert admin.execute("SELECT to_regclass('lv.outbox'), to_regclass('lv.work_item'), to_regclass('lv.api_idempotency')").fetchone() == (None,) * 3
            assert admin.execute("SELECT has_table_privilege('lv_app', 'lv.fiscal_period', 'UPDATE')").fetchone() == (True,)   # 0008 exactly
            assert admin.execute("SELECT count(*) FROM pg_attribute WHERE attrelid = 'lv.fiscal_period'::regclass AND attname = 'version'").fetchone() == (0,)
        migrate(name)
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            cfg = provision(app_, tenant)
        with connection('lv_app', name) as c:
            assert close_call(c, admin_token(name, tenant), cfg['period'])['status'] == 200
        with pytest.raises(Exception, match='audited period closes'):
            migrate(name, '0008_identity_force_rls', downgrade=True, disposable=True)
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0040_mfa_reset',)


def test_committed_openapi_documents_the_period_routes():
    generated = create_app(AuthSettings()).openapi()
    op = generated['paths']['/api/v1/periods/{period_id}/close']['post']
    assert op['x-lv-permission'] == 'administer' and op['operationId'] == 'closePeriod' and op['x-lv-jobs'] == ['STALENESS_SCAN']
    assert {'400', '401', '403', '404', '409', '412', '422', '428'} <= set(op['responses'])
    assert generated['paths']['/api/v1/ledgers/{ledger_id}/periods']['get']['x-lv-permission'] == 'read'
