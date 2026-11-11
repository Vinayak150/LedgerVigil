"""Proposal drafts and revision history (migration 0010) — through the HTTP application and real PostgreSQL.

Expected behaviour is taken from the owning contracts:
- Stage 2B Family 8: POST /proposals (`propose`; 201; 422 SCHEMA_INVALID / CAPABILITY_UNAVAILABLE / UNSUPPORTED_BY_PROFILE;
  "Proposal, revision 1 · Y"), GET /proposals · /{id} · /{id}/revisions (`read`; ?ledger_id&state&created_by&reviewable_by_me),
  PATCH /proposals/{id} (`propose`; If-Match; 409 not DRAFT; 412; "Editor recorded (blocks later approval by that user) · N
  (version-guarded)");
- Part 3 RecordSettlementOp / Proposal / EvidenceRef / Money; Part 2 (money strings, If-Match/428/412, pagination, 404 for
  other tenants); Part 4 codes; Part 5.1 (ADMINISTRATOR has no propose); Part 8.1 idempotency table; CT-04 (other-tenant ids
  in body and path are 404);
- Stage 2A §9.1 (validation order; evidence must exist in the ledger), §19.3 (RecordSettlement; full-item settlement),
  §11.2 (proposal, proposal_revision), §12.1 (FORCE RLS), §12.2 (segregation inputs: maker and editors).
Every identity, tenant, ledger, payment and statement is SYNTHETIC and created per test. Accounting remains UNREVIEWED.
"""
import hashlib
import json
import secrets
import threading
import time
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from tools.dev_db import connection
from tools.identity_admin import create_user, grant, owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.settings import AuthSettings
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ingestion.store import ingest_statement
from ledgervigil.ingestion import statement_format as fmt
from ledgervigil.ledger.seed import seed_manual_journal, seed_processor_payment
from period_close_support import admin_token
from test_posting import provision, register

BASE = '/api/v1'
COOKIE = '__Host-lv_session'
PROPOSAL_TABLES = {'lv.proposal', 'lv.proposal_revision', 'lv.proposal_reference', 'lv.api_idempotency',
                   'lv_identity.audit_event', 'lv_identity.audit_chain_head'}


# ------------------------------------------------------------------------------------------- world
def _payment(seed, cfg, item, amount, party=None):
    return seed_processor_payment(seed, cfg['tenant'], cfg['ledger'], {
        'processor_ref': 'processor-1', 'processor_item_id': item, 'party_id': party or cfg['party'], 'currency': cfg['currency'],
        'amount': amount, 'received_date': '2026-10-10', 'description': 'SYNTHETIC typed processor payment (proposal draft tests)'})['document_id']


def _statement(database, cfg, payout, items, gross, fee, net):
    raw = json.dumps({'format': fmt.FORMAT, 'version': fmt.VERSION, 'data_classification': 'SYNTHETIC', 'statement': {
        'tenant': str(cfg['tenant']), 'ledger': str(cfg['ledger']), 'processor': 'processor-1', 'payout': payout, 'date': '2026-10-14',
        'currency': cfg['currency'], 'items': [{'id': i, 'kind': 'PAYMENT', 'amount': a} for i, a in items],
        'gross': gross, 'fee': fee, 'net': net, 'memo': 'SYNTHETIC statement; not a real processor file'}}).encode()
    with connection(database=database) as db:head=db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()[0]
    writer=ingest_statement
    if head not in ('0021_native_execution','0022_approved_withdrawal','0023_configuration_boundary','0024_proof_export_authority','0025_export_jobs','0026_proof_packs','0027_account_lock_order','0028_workflow_lock_order','0029_account_patch','0030_override_independence','0031_semantic_revalidation','0032_detection_findings','0033_detection_overlap_dates','0034_detection_review_filters','0035_provider_connections','0036_identity_lifecycle','0037_restore_interlock','0038_identity_issuer','0039_mfa','0040_mfa_reset'):
        from legacy_ingestion_0020 import ingest_statement as writer
    with connection('lv_ingest', database, autocommit=True) as c:
        result = writer(c, cfg['tenant'], cfg['ledger'], raw, declared_format=fmt.FORMAT, declared_version=fmt.VERSION)
    assert result['status'] == 'STAGED', result
    return result


def _ledger(app, database, seed, tenant, purpose='SANDBOX', currency='INR'):
    cfg = provision(app, tenant, purpose=purpose, currency=currency)
    cfg['currency'] = currency
    return cfg


@pytest.fixture
def seed(database):
    with connection('lv_seed', database, autocommit=True) as c:
        yield c


@pytest.fixture
def world(database, app, seed):
    """Tenant A: SANDBOX ledger L1 with two typed payments, a staged statement and a manual journal; a second SANDBOX ledger L2
    with its own payment; a BOOKS ledger. Tenant B: SANDBOX ledger with its own payment and statement. Users of every role."""
    from test_processor_payments import new_tenant
    w = {'A': new_tenant(database), 'B': new_tenant(database), 'users': {}}
    for t in ('A', 'B'):
        register(database, w[t])
    w['l1'] = _ledger(app, database, seed, w['A'])
    w['l2'] = _ledger(app, database, seed, w['A'])
    w['books'] = _ledger(app, database, seed, w['A'], purpose='BOOKS')
    w['lb'] = _ledger(app, database, seed, w['B'])
    w['p1'] = _payment(seed, w['l1'], 'PMT-2001', '50000.00')
    w['p2'] = _payment(seed, w['l1'], 'PMT-2002', '2500.50')
    st = _statement(database, w['l1'], 'STL-3001', [('PMT-2001', '50000.00'), ('PMT-2002', '2500.50')], '52500.50', '1000.00', '51500.50')
    w['s1'], w['r1'] = st['staged_document_id'], st['source_record_id']
    w['journal'] = seed_manual_journal(seed, w['A'], w['l1']['ledger'], {
        'source_ref': 'opening-1', 'effective_date': '2026-10-05', 'description': 'SYNTHETIC opening journal',
        'lines': [{'account_id': w['l1']['chart']['BANK_OPERATING'], 'side': 'D', 'amount': '10.00'},
                  {'account_id': w['l1']['chart']['OPENING_BALANCE_EQUITY'], 'side': 'C', 'amount': '10.00'}]})['document_id']
    w['q1'] = _payment(seed, w['l2'], 'PMT-9001', '50000.00')
    w['s2'] = _statement(database, w['l2'], 'STL-9001', [('PMT-9001', '50000.00')], '50000.00', '1000.00', '49000.00')['staged_document_id']
    w['pb'] = _payment(seed, w['lb'], 'PMT-2001', '50000.00')
    stb = _statement(database, w['lb'], 'STL-3001', [('PMT-2001', '50000.00')], '50000.00', '1000.00', '49000.00')
    w['sb'] = stb['staged_document_id']
    with connection(database=database) as admin:
        w['p1_version'] = str(admin.execute('SELECT id FROM lv.document_version WHERE document_id = %s AND version = 1', (w['p1'],)).fetchone()[0])
    with owner_transaction(database) as owner:
        for name, roles, tenant in (('analyst', ['ANALYST'], 'A'), ('automation', ['AUTOMATION_DEVELOPER'], 'A'),
                                    ('controller', ['CONTROLLER'], 'A'), ('admin', ['ADMINISTRATOR'], 'A'),
                                    ('analystB', ['ANALYST'], 'B')):
            email = f'{name}-{uuid4().hex[:8]}@test.ledgervigil.invalid'
            password = secrets.token_urlsafe(18)
            uid = create_user(owner, email, f'Synthetic {name}', password)
            grant(owner, w[tenant], uid, roles)
            owner.execute("UPDATE lv_identity.membership_role SET granted_at=statement_timestamp()-interval '48 hours' WHERE tenant_id=%s AND user_id=%s",(w[tenant],uid))
            w['users'][name] = {'email': email, 'password': password, 'id': str(uid)}
    return w


@pytest.fixture
def api(database):
    application = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database))
    yield application
    application.state.pool.close()


def client(application):
    return TestClient(application, base_url='https://testserver', raise_server_exceptions=False,
                      client=(f'10.11.{secrets.randbelow(250)}.{secrets.randbelow(250)}', 40000))


def signed_in(application, w, name):
    c = client(application)
    u = w['users'][name]
    r = c.post(f'{BASE}/auth/login', json={'email': u['email'], 'password': u['password']})
    assert r.status_code == 200, r.text
    c.headers['X-CSRF-Token'] = r.json()['csrf_token']
    return c


def money(amount, currency='INR'):
    return {'amount': amount, 'currency': currency}


def settlement(w, *items, statement=None, gross='52500.50', fee='1000.00', net='51500.50', payout='STL-3001'):
    items = items or ((w['p1'], '50000.00'), (w['p2'], '2500.50'))
    return {'type': 'RecordSettlement', 'schema_version': 1, 'processor_ref': 'processor-1', 'payout_id': payout,
            'payout_date': '2026-10-14', 'items': [{'document_id': str(d), 'kind': 'PAYMENT', 'amount': money(a)} for d, a in items],
            'gross': money(gross), 'fee': money(fee), 'net': money(net), 'statement_document_id': str(statement or w['s1'])}


def draft(w, ledger=None, **overrides):
    body = {'ledger_id': str(ledger or w['l1']['ledger']), 'target_adapter': 'NATIVE', 'operations': [settlement(w)],
            'evidence_refs': [{'kind': 'STAGED_DOCUMENT', 'id': str(w['s1'])}, {'kind': 'SOURCE_RECORD', 'id': str(w['r1'])}],
            'rationale': 'SYNTHETIC: record payout STL-3001'}
    body.update(overrides)
    return body


def create(c, body, key=None, **headers):
    h = {'Idempotency-Key': key or uuid4().hex, **headers}
    if key is False:
        del h['Idempotency-Key']
    return c.post(f'{BASE}/proposals', json=body, headers=h)


def edit(c, pid, body, if_match='"v1"'):
    return c.patch(f'{BASE}/proposals/{pid}', json=body, headers={} if if_match is None else {'If-Match': if_match})


def problem(r, status, code):
    assert r.status_code == status, (r.status_code, r.text)
    assert r.headers['content-type'].startswith('application/problem+json')
    body = r.json()
    assert body['code'] == code, body
    return body


def fingerprint(database, exclude=PROPOSAL_TABLES):
    """Every row of every lv and lv_identity table (superuser view), except the given tables and sessions/login attempts."""
    with connection(database=database) as admin:
        tables = [r[0] for r in admin.execute("SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                              "WHERE n.nspname IN ('lv', 'lv_identity') AND c.relkind = 'r' "
                                              "AND c.relname NOT IN ('session', 'login_attempt') ORDER BY 1").fetchall()]
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM {}').format(sql.SQL(t))).fetchall()))).encode()).hexdigest()
                for t in tables if t not in exclude}


def counters(database):
    with connection(database=database) as admin:
        return admin.execute('SELECT ledger_id, last_seq, last_entry_no, batch_head, event_head FROM lv.ledger_counter ORDER BY 1').fetchall()


def proposal_rows(database, w):
    with connection(database=database) as admin:
        return {t: admin.execute(sql.SQL('SELECT count(*) FROM lv.{} WHERE tenant_id IN (%s, %s)').format(sql.Identifier(t)),
                                 (w['A'], w['B'])).fetchone()[0]
                for t in ('proposal', 'proposal_revision', 'proposal_reference')}


def audit(database, w, kind_prefix='PROPOSAL'):
    with connection(database=database) as admin:
        return admin.execute("SELECT event_type, outcome, user_id::text, detail FROM lv_identity.audit_event WHERE event_type LIKE %s "
                             "AND tenant_id IN (%s, %s) ORDER BY occurred_at, seq", (kind_prefix + '%', w['A'], w['B'])).fetchall()


# ------------------------------------------------------------------------------------------- end-to-end flow and read safety
def test_http_create_edit_read_flow_records_revisions_and_editors_and_changes_no_financial_state(database, api, world):
    analyst, automation = signed_in(api, world, 'analyst'), signed_in(api, world, 'automation')
    tb_path = f"{BASE}/ledgers/{world['l1']['ledger']}/trial-balance"
    tb_before = analyst.get(tb_path).json()
    before, seq_before = fingerprint(database), counters(database)

    r = create(analyst, draft(world))
    assert r.status_code == 201, r.text
    p = r.json()
    assert r.headers['ETag'] == '"v1"' and r.headers['Location'] == f"/api/v1/proposals/{p['id']}"
    assert (p['state'], p['revision'], p['version'], p['origin'], p['target_adapter']) == ('DRAFT', 1, 1, 'HUMAN', 'NATIVE')
    assert p['created_by'] == world['users']['analyst']['id'] and p['editors'] == [world['users']['analyst']['id']]
    assert (p['ledger_purpose'], p['accounting_status'], p['data_label']) == ('SANDBOX', 'UNREVIEWED', 'SANDBOX — not accounting records')
    assert (p['snapshot'], p['read_set'], p['latest_simulation_id'], p['submitted_by']) == (None, [], None, None)
    assert p['operations'] == [settlement(world)] and p['allowed_actions'] == ['EDIT','SUBMIT','WITHDRAW']
    assert p['evidence_refs'] == [{'kind': 'STAGED_DOCUMENT', 'id': str(world['s1']), 'provenance': 'SYNTHETIC'},
                                  {'kind': 'SOURCE_RECORD', 'id': str(world['r1']), 'sha256': p['evidence_refs'][1]['sha256']}]
    assert len(p['evidence_refs'][1]['sha256']) == 64

    got = analyst.get(f"{BASE}/proposals/{p['id']}")
    assert got.status_code == 200 and got.headers['ETag'] == '"v1"'
    assert {k: v for k, v in got.json().items()} == p

    # A different proposer edits: amounts with the exponent normalised, evidence replaced, rationale kept.
    changed = settlement(world, (world['p1'], '50000.00'), gross='50000.00', fee='750.5', net='49249.50')
    e = edit(automation, p['id'], {'operations': [changed], 'evidence_refs': [{'kind': 'DOCUMENT_VERSION', 'id': world['p1_version']}]})
    assert e.status_code == 200, e.text
    q = e.json()
    assert e.headers['ETag'] == '"v2"' and (q['revision'], q['version']) == (2, 2)
    assert q['operations'][0]['fee'] == money('750.50')                      # response scale = currency exponent
    assert q['rationale'] == p['rationale'] and q['created_by'] == p['created_by']
    assert q['editors'] == [world['users']['analyst']['id'], world['users']['automation']['id']]
    assert q['evidence_refs'] == [{'kind': 'DOCUMENT_VERSION', 'id': world['p1_version'], 'provenance': 'SYNTHETIC'}]

    revs = analyst.get(f"{BASE}/proposals/{p['id']}/revisions").json()
    assert [(i['revision'], i['change'], i['authored_by']) for i in revs['items']] == [
        (1, 'CREATED', world['users']['analyst']['id']), (2, 'EDITED', world['users']['automation']['id'])]
    assert revs['items'][0]['operations'] == p['operations'] and revs['items'][1]['operations'] == q['operations']
    assert revs['items'][0]['content_hash'] != revs['items'][1]['content_hash'] and revs['next_cursor'] is None

    listed = analyst.get(f'{BASE}/proposals').json()
    assert [i['id'] for i in listed['items']] == [p['id']]

    # Authoritative financial state: every row of every financial table, every counter, every ledger event: unchanged.
    assert fingerprint(database) == before and counters(database) == seq_before
    assert analyst.get(tb_path).json() == tb_before
    events = audit(database, world)
    assert [(e[0], e[1], e[2]) for e in events] == [('PROPOSAL_CREATED', 'ALLOWED', world['users']['analyst']['id']),
                                                    ('PROPOSAL_EDITED', 'ALLOWED', world['users']['automation']['id'])]
    assert [e[3]['content_hash'] for e in events] == [revs['items'][0]['content_hash'], revs['items'][1]['content_hash']]


def test_revision_history_is_immutable_and_the_runtime_role_cannot_write_proposal_tables(database, api, world):
    p = create(signed_in(api, world, 'analyst'), draft(world)).json()
    with connection('lv_app', database) as app_conn:
        with tenant_transaction(app_conn, world['A']):
            for statement in ("UPDATE lv.proposal SET version = version WHERE id = %s",
                              "UPDATE lv.proposal_revision SET rationale = 'x' WHERE proposal_id = %s",
                              "DELETE FROM lv.proposal_reference WHERE proposal_id = %s",
                              "INSERT INTO lv.proposal_revision SELECT * FROM lv.proposal_revision WHERE proposal_id = %s"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with app_conn.transaction():
                        app_conn.execute(statement, (p['id'],))
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(world['A']),))
        for statement, error in (("UPDATE lv.proposal_revision SET rationale = 'rewritten' WHERE proposal_id = %s", psycopg.errors.lookup('23514')),
                                 ("DELETE FROM lv.proposal_revision WHERE proposal_id = %s", psycopg.errors.lookup('23514')),
                                 ("UPDATE lv.proposal_reference SET pointer = '/x' WHERE proposal_id = %s", psycopg.errors.lookup('23514')),
                                 ("UPDATE lv.proposal SET created_by = created_by, ledger_id = ledger_id, version = version + 1 WHERE id = %s",
                                  psycopg.errors.lookup('23514')),
                                 ("UPDATE lv.proposal SET created_by = (SELECT id FROM lv_identity.app_user LIMIT 1), revision = revision + 1, "
                                  "version = version + 1 WHERE id = %s", psycopg.errors.lookup('23514')),
                                 ("DELETE FROM lv.proposal WHERE id = %s", psycopg.errors.lookup('23514'))):
            with pytest.raises(error):
                with owner.transaction():
                    owner.execute(statement, (p['id'],))


def test_commit_time_validator_refuses_a_revision_without_its_audit_event(database, world):
    """Even the owner cannot commit a revision that skipped the audited write path. Uses a REAL session (never skips)."""
    token = admin_token(database, world['A'], roles=('ANALYST',))
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(world['A']),))
        sid, user = owner.execute('SELECT id, user_id FROM lv_identity.session WHERE token_hash = %s', (token_hash(token),)).fetchone()
        pid = owner.execute("INSERT INTO lv.proposal(tenant_id, ledger_id, target_adapter, revision, version, created_by) "
                            "VALUES (%s, %s, 'NATIVE', 1, 1, %s) RETURNING id", (world['A'], world['l1']['ledger'], user)).fetchone()[0]
        ops = json.dumps([{'type': 'RecordSettlement', 'items': []}])
        owner.execute("INSERT INTO lv.proposal_revision(tenant_id, ledger_id, proposal_id, revision, change, authored_by, session_id, target_adapter, "
                      "operations, evidence_refs, rationale, content_hash) VALUES (%s, %s, %s, 1, 'CREATED', %s, %s, 'NATIVE', %s::jsonb, '[]'::jsonb, NULL, "
                      "lv.proposal_content_hash('NATIVE', %s::jsonb, '[]'::jsonb, NULL))",
                      (world['A'], world['l1']['ledger'], pid, user, sid, ops, ops))
        owner.execute("INSERT INTO lv.proposal_reference(tenant_id, ledger_id, proposal_id, revision, ordinal, pointer, kind, staged_document_id) "
                      "VALUES (%s, %s, %s, 1, 1, '/operations/0/statement_document_id', 'SETTLEMENT_STATEMENT', %s)",
                      (world['A'], world['l1']['ledger'], pid, world['s1']))
        with pytest.raises(psycopg.errors.CheckViolation, match='audit event'):
            owner.commit()
    assert proposal_rows(database, world)['proposal'] == 0


# ------------------------------------------------------------------------------------------- authentication and authorization
def test_writes_need_a_session_csrf_and_propose_and_the_administrator_still_has_no_propose(database, api, world):
    p = create(signed_in(api, world, 'analyst'), draft(world)).json()
    rows, before = proposal_rows(database, world), fingerprint(database, exclude={'lv_identity.audit_event', 'lv_identity.audit_chain_head'})
    problem(create(client(api), draft(world)), 401, 'UNAUTHENTICATED')
    problem(edit(client(api), p['id'], {'rationale': 'x'}), 401, 'UNAUTHENTICATED')
    no_csrf = signed_in(api, world, 'analyst'); del no_csrf.headers['X-CSRF-Token']
    assert 'CSRF' in problem(create(no_csrf, draft(world)), 403, 'PERMISSION_DENIED')['detail']
    assert 'cross-origin' in problem(create(signed_in(api, world, 'analyst'), draft(world), Origin='https://evil.example'), 403,
                                     'PERMISSION_DENIED')['detail']
    for name in ('controller', 'admin'):
        c = signed_in(api, world, name)
        assert problem(create(c, draft(world)), 403, 'PERMISSION_DENIED')['detail'] == 'requires propose'
        bootstrap = draft(world, operations=[{'type': 'BootstrapLedger', 'schema_version': 1}])
        problem(create(c, bootstrap), 403, 'PERMISSION_DENIED')
        problem(edit(c, p['id'], {'rationale': 'x'}), 403, 'PERMISSION_DENIED')
        r = c.get(f"{BASE}/proposals/{p['id']}")                                        # read is allowed; no EDIT action
        assert r.status_code == 200 and r.json()['allowed_actions'] == []
    with connection(database=database) as admin:
        perms = {r[0] for r in admin.execute("SELECT rp.permission::text FROM lv_identity.role_permission rp WHERE rp.role = 'ADMINISTRATOR'")}
    assert 'propose' not in perms and 'review' not in perms and 'execute' not in perms
    assert proposal_rows(database, world) == rows
    assert fingerprint(database, exclude={'lv_identity.audit_event', 'lv_identity.audit_chain_head'}) == before


def test_disabled_user_revoked_membership_and_removed_roles_end_access_on_the_next_request(database, api, world):
    analyst, automation = signed_in(api, world, 'analyst'), signed_in(api, world, 'automation')
    p = create(analyst, draft(world)).json()
    rows = proposal_rows(database, world)
    with owner_transaction(database) as conn:
        conn.execute('UPDATE lv_identity.app_user SET disabled_at = now() WHERE id = %s', (world['users']['analyst']['id'],))
        conn.execute("UPDATE lv_identity.membership SET status = 'REVOKED', revoked_at = now() WHERE tenant_id = %s AND user_id = %s",
                     (world['A'], world['users']['automation']['id']))
    for c in (analyst, automation):
        problem(create(c, draft(world)), 401, 'UNAUTHENTICATED')
        problem(edit(c, p['id'], {'rationale': 'x'}), 401, 'UNAUTHENTICATED')
        problem(c.get(f"{BASE}/proposals/{p['id']}"), 401, 'UNAUTHENTICATED')
    remaining = signed_in(api, world, 'controller')
    with owner_transaction(database) as conn:
        conn.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id = %s AND user_id = %s', (world['A'], world['users']['controller']['id']))
    problem(remaining.get(f'{BASE}/proposals'), 403, 'PERMISSION_DENIED')
    assert proposal_rows(database, world) == rows


# ------------------------------------------------------------------------------------------- tenant and ledger isolation
def test_other_tenants_proposals_and_body_references_are_404_like_absent_ones(database, api, world):
    a, b = signed_in(api, world, 'analyst'), signed_in(api, world, 'analystB')
    p = create(a, draft(world)).json()
    for path in (f"{BASE}/proposals/{p['id']}", f"{BASE}/proposals/{p['id']}/revisions"):
        foreign, absent = problem(b.get(path), 404, 'NOT_FOUND'), problem(b.get(path.replace(p['id'], str(uuid4()))), 404, 'NOT_FOUND')
        foreign.pop('request_id'); absent.pop('request_id')
        assert foreign == absent
    fe, ae = problem(edit(b, p['id'], {'rationale': 'x'}), 404, 'NOT_FOUND'), problem(edit(b, str(uuid4()), {'rationale': 'x'}), 404, 'NOT_FOUND')
    fe.pop('request_id'); ae.pop('request_id')
    assert fe == ae
    assert b.get(f'{BASE}/proposals').json()['items'] == []
    assert b.get(f'{BASE}/proposals', params={'ledger_id': str(world['l1']['ledger'])}).json()['items'] == []
    rows = proposal_rows(database, world)
    # Body references: another tenant's ledger, payment, statement and evidence; this tenant's OTHER ledger; an absent id.
    for body, pointer in ((draft(world), '/ledger_id'),):
        assert problem(create(b, body), 404, 'NOT_FOUND')['errors'] == [{'pointer': pointer, 'code': 'not_found'}]
    cases = [
        (settlement(world, (world['pb'], '50000.00'), gross='50000.00', net='49000.00'), '/operations/0/items/0/document_id'),
        (settlement(world, (world['q1'], '50000.00'), gross='50000.00', net='49000.00'), '/operations/0/items/0/document_id'),
        (settlement(world, (uuid4(), '50000.00'), gross='50000.00', net='49000.00'), '/operations/0/items/0/document_id'),
        (settlement(world, statement=world['sb']), '/operations/0/statement_document_id'),
        (settlement(world, statement=world['s2']), '/operations/0/statement_document_id'),
        (settlement(world, statement=world['p1']), '/operations/0/statement_document_id'),
    ]
    answers = []
    for op, pointer in cases:
        body = problem(create(a, draft(world, operations=[op])), 404, 'NOT_FOUND')
        assert body['errors'] == [{'pointer': pointer, 'code': 'not_found'}], body
        body.pop('request_id'); answers.append(body)
    assert all(x == answers[0] for x in answers[:3]) and answers[3] == answers[4] == answers[5]
    for ev in ({'kind': 'STAGED_DOCUMENT', 'id': str(world['sb'])}, {'kind': 'STAGED_DOCUMENT', 'id': str(world['s2'])},
               {'kind': 'DOCUMENT_VERSION', 'id': str(uuid4())},
               {'kind': 'SOURCE_RECORD', 'id': str(world['s1'])}):
        assert problem(create(a, draft(world, evidence_refs=[ev])), 404, 'NOT_FOUND')['errors'] == [{'pointer': '/evidence_refs/0/id', 'code': 'not_found'}]
    # Editing A's proposal to point into tenant B is refused the same way and leaves revision 1 as the head.
    problem(edit(a, p['id'], {'operations': [settlement(world, statement=world['sb'])]}), 404, 'NOT_FOUND')
    assert a.get(f"{BASE}/proposals/{p['id']}").json()['revision'] == 1
    assert proposal_rows(database, world) == rows


# ------------------------------------------------------------------------------------------- validation
def _op(w, **change):
    op = settlement(w)
    op.update(change)
    return op


BAD = {
    'json_number_amount': (lambda w: draft(w, operations=[_op(w, fee={'amount': 1000.0, 'currency': 'INR'})]), 422, 'MONEY_MUST_BE_STRING', '/operations/0/fee/amount'),
    'scale_over_exponent': (lambda w: draft(w, operations=[_op(w, fee=money('1000.001'))]), 422, 'MONEY_SCALE_EXCEEDED', '/operations/0/fee/amount'),
    'other_currency': (lambda w: draft(w, operations=[_op(w, net=money('51500.50', 'USD'))]), 422, 'CURRENCY_MISMATCH', '/operations/0/net/currency'),
    'negative_money': (lambda w: draft(w, operations=[_op(w, fee=money('-1.00'))]), 422, 'SCHEMA_INVALID', '/operations/0/fee/amount'),
    'unknown_field': (lambda w: draft(w, operations=[_op(w, memo='x')]), 422, 'SCHEMA_INVALID', '/operations/0/memo'),
    'missing_items': (lambda w: draft(w, operations=[{k: v for k, v in settlement(w).items() if k != 'items'}]), 422, 'SCHEMA_INVALID', '/operations/0/items'),
    'empty_items': (lambda w: draft(w, operations=[_op(w, items=[])]), 422, 'SCHEMA_INVALID', '/operations/0/items'),
    'schema_version_2': (lambda w: draft(w, operations=[_op(w, schema_version=2)]), 422, 'SCHEMA_INVALID', '/operations/0/schema_version'),
    'bad_date': (lambda w: draft(w, operations=[_op(w, payout_date='2026-02-30')]), 422, 'SCHEMA_INVALID', '/operations/0/payout_date'),
    'processor_with_space': (lambda w: draft(w, operations=[_op(w, processor_ref='processor 1')]), 422, 'SCHEMA_INVALID', '/operations/0/processor_ref'),
    'duplicate_item': (lambda w: draft(w, operations=[_op(w, items=[settlement(w)['items'][0]] * 2)]), 422, 'SCHEMA_INVALID', '/operations/0/items/1/document_id'),
    'non_canonical_uuid': (lambda w: draft(w, operations=[_op(w, statement_document_id=str(w['s1']).upper())]), 422, 'SCHEMA_INVALID', '/operations/0/statement_document_id'),
    'unknown_operation_type': (lambda w: draft(w, operations=[{'type': 'MoveMoney'}]), 422, 'SCHEMA_INVALID', '/operations/0/type'),
    'no_operations': (lambda w: draft(w, operations=[]), 422, 'SCHEMA_INVALID', '/operations'),
    'eleven_operations': (lambda w: draft(w, operations=[settlement(w)] * 11), 422, 'SCHEMA_INVALID', '/operations'),
    'read_only_field': (lambda w: draft(w, state='APPROVED'), 422, 'SCHEMA_INVALID', '/state'),
    'evidence_with_provenance': (lambda w: draft(w, evidence_refs=[{'kind': 'SOURCE_RECORD', 'id': str(w['r1']), 'provenance': 'SOURCE'}]), 422, 'SCHEMA_INVALID', '/evidence_refs/0'),
    'long_rationale': (lambda w: draft(w, rationale='x' * 4001), 422, 'SCHEMA_INVALID', '/rationale'),
    'refund_item': (lambda w: draft(w, operations=[_op(w, items=[{**settlement(w)['items'][0], 'kind': 'REFUND'}])]), 422, 'CAPABILITY_UNAVAILABLE', None),
    'apply_payment': (lambda w: draft(w, operations=[{'type': 'ApplyPayment', 'schema_version': 1}]), 422, 'CAPABILITY_UNAVAILABLE', None),
    'bootstrap_ledger': (lambda w: draft(w, operations=[{'type': 'BootstrapLedger', 'schema_version': 1}]), 422, 'CAPABILITY_UNAVAILABLE', None),
    'quickbooks': (lambda w: draft(w, target_adapter='QUICKBOOKS'), 422, 'CAPABILITY_UNAVAILABLE', None),
    'attachment_evidence': (lambda w: draft(w, evidence_refs=[{'kind': 'ATTACHMENT', 'id': str(uuid4())}]), 422, 'CAPABILITY_UNAVAILABLE', None),
    'books_ledger': (lambda w: draft(w, ledger=w['books']['ledger']), 422, 'CAPABILITY_UNAVAILABLE', None),
    'partial_item': (lambda w: draft(w, operations=[_op(w, items=[{'document_id': str(w['p1']), 'kind': 'PAYMENT', 'amount': money('49999.99')}])]),
                     422, 'UNSUPPORTED_BY_PROFILE', '/operations/0/items/0/amount'),
    'item_is_a_journal': (lambda w: draft(w, operations=[_op(w, items=[{'document_id': str(w['journal']), 'kind': 'PAYMENT', 'amount': money('10.00')}])]),
                          422, 'SCHEMA_INVALID', '/operations/0/items/0/document_id'),
}


@pytest.mark.parametrize('case', sorted(BAD))
def test_invalid_drafts_are_refused_with_the_contracted_code_and_write_nothing(database, api, world, case):
    make, status, code, pointer = BAD[case]
    c = signed_in(api, world, 'analyst')
    rows, before = proposal_rows(database, world), fingerprint(database)
    key, request_body = uuid4().hex, make(world)
    body = problem(create(c, request_body, key=key), status, code)
    if pointer is not None:
        assert pointer in [e['pointer'] for e in body['errors']], body
    if case == 'bootstrap_ledger':
        assert 'RG-4' in body['detail']
    assert proposal_rows(database, world) == rows and fingerprint(database) == before
    # Part 8.1: the 4xx outcome is stored and replayed for the same key and body.
    again = create(c, request_body, key=key)
    assert again.status_code == status and again.headers['Idempotent-Replayed'] == 'true' and again.json()['code'] == code


def test_malformed_bodies_and_oversized_requests_are_schema_invalid(database, api, world):
    c = signed_in(api, world, 'analyst')
    h = {'Idempotency-Key': uuid4().hex, 'Content-Type': 'application/json'}
    problem(c.post(f'{BASE}/proposals', content=b'{"ledger_id": 1, "ledger_id": 2}', headers=h), 422, 'SCHEMA_INVALID')
    problem(c.post(f'{BASE}/proposals', content=b'not json', headers={**h, 'Idempotency-Key': uuid4().hex}), 422, 'SCHEMA_INVALID')
    problem(c.post(f'{BASE}/proposals', content=b'{"x": NaN}', headers={**h, 'Idempotency-Key': uuid4().hex}), 422, 'SCHEMA_INVALID')
    big = json.dumps(draft(world, rationale=None, evidence_refs=[])).encode()[:-1] + b',"pad":"' + b'x' * 1048576 + b'"}'
    assert problem(c.post(f'{BASE}/proposals', content=big, headers={**h, 'Idempotency-Key': uuid4().hex}), 422, 'SCHEMA_INVALID')['errors'] == [
        {'pointer': '', 'code': 'size'}]
    p = create(c, draft(world)).json()
    for patch, code in (({}, 'SCHEMA_INVALID'), ({'ledger_id': str(world['l2']['ledger'])}, 'SCHEMA_INVALID'),
                        ({'operations': [_op(world, fee={'amount': 5, 'currency': 'INR'})]}, 'MONEY_MUST_BE_STRING')):
        problem(edit(c, p['id'], patch), 422, code)
    assert c.get(f"{BASE}/proposals/{p['id']}").json()['revision'] == 1


# ------------------------------------------------------------------------------------------- idempotency
def test_create_idempotency_follows_part_8_1(database, api, world):
    c = signed_in(api, world, 'analyst')
    key = uuid4().hex
    first = create(c, draft(world), key=key)
    again = create(c, draft(world), key=key)
    assert first.status_code == again.status_code == 201
    assert again.headers['Idempotent-Replayed'] == 'true' and 'Idempotent-Replayed' not in first.headers
    assert again.json() == first.json() and again.headers['ETag'] == '"v1"'
    assert proposal_rows(database, world)['proposal'] == 1
    problem(create(c, draft(world, rationale='different'), key=key), 422, 'IDEMPOTENCY_KEY_REUSED')
    problem(create(c, draft(world), key=False), 400, 'IDEMPOTENCY_KEY_REQUIRED')
    problem(create(c, draft(world), key='bad key with spaces'), 422, 'SCHEMA_INVALID')
    # The key is scoped to the principal: another proposer using the same key creates their own draft.
    other = create(signed_in(api, world, 'automation'), draft(world), key=key)
    assert other.status_code == 201 and other.json()['id'] != first.json()['id']
    assert proposal_rows(database, world)['proposal'] == 2


def test_a_duplicate_while_the_first_is_running_is_409_and_converges_to_one_draft(database, api, world):
    c = signed_in(api, world, 'analyst')
    key = uuid4().hex
    lock = ('lv-idem:' + str(world['A']) + ':' + world['users']['analyst']['id'] + ':POST:/api/v1/proposals:' + key)
    with connection(database=database) as holder:                       # stands in for an in-flight first request
        holder.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (lock,))
        r = create(c, draft(world), key=key)
        assert problem(r, 409, 'REQUEST_IN_PROGRESS')['retryable'] is True and r.headers['Retry-After'] == '1'
        assert proposal_rows(database, world)['proposal'] == 0
    # Real concurrency: eight identical requests at once.
    key2 = uuid4().hex
    barrier, results = threading.Barrier(8), []

    def go():
        barrier.wait()
        results.append(create(c, draft(world), key=key2))
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert {r.status_code for r in results} <= {201, 409}
    ids = {r.json()['id'] for r in results if r.status_code == 201}
    assert len(ids) == 1 and proposal_rows(database, world)['proposal'] == 1
    assert create(c, draft(world), key=key2).json()['id'] in ids


# ------------------------------------------------------------------------------------------- versions and competing edits
def test_if_match_is_required_and_stale_versions_are_412(database, api, world):
    c = signed_in(api, world, 'analyst')
    p = create(c, draft(world)).json()
    problem(edit(c, p['id'], {'rationale': 'x'}, if_match=None), 428, 'VERSION_REQUIRED')
    assert problem(edit(c, p['id'], {'rationale': 'x'}, if_match='"v9"'), 412, 'VERSION_MISMATCH')['current_version'] == 1
    assert problem(edit(c, p['id'], {'rationale': 'x'}, if_match='v1'), 412, 'VERSION_MISMATCH')['current_version'] == 1
    assert edit(c, p['id'], {'rationale': 'second'}).status_code == 200
    body = problem(edit(signed_in(api, world, 'automation'), p['id'], {'rationale': 'lost update'}, if_match='"v1"'), 412, 'VERSION_MISMATCH')
    assert body['current_version'] == 2
    assert c.get(f"{BASE}/proposals/{p['id']}").json()['rationale'] == 'second'
    assert [e[0] for e in audit(database, world)] == ['PROPOSAL_CREATED', 'PROPOSAL_EDIT_REFUSED', 'PROPOSAL_EDIT_REFUSED', 'PROPOSAL_EDIT_REFUSED',
                                               'PROPOSAL_EDITED', 'PROPOSAL_EDIT_REFUSED']


def test_competing_edits_serialize_on_the_row_lock_and_the_loser_gets_412(database, api, world):
    a = signed_in(api, world, 'analyst')
    p = create(a, draft(world)).json()
    token = admin_token(database, world['A'], roles=('AUTOMATION_DEVELOPER',))
    held = connection('lv_app', database)
    try:
        held.execute('SELECT lv_identity.session_context(%s, false)', (token_hash(token),))
        first = held.execute("SELECT lv.proposal_edit(%s, %s, '\"v1\"', %s::jsonb, NULL, 'test', 'held-edit')",
                             (token_hash(token), p['id'], json.dumps({'rationale': 'held winner'}))).fetchone()[0]
        assert first['status'] == 200
        pid = held.execute('SELECT pg_backend_pid()').fetchone()[0]
        out = {}
        t = threading.Thread(target=lambda: out.setdefault('r', edit(a, p['id'], {'rationale': 'concurrent'})))
        t.start()
        with connection(database=database, autocommit=True) as inspector:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                blocked = [r[0] for r in inspector.execute('SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))', (pid,))]
                if blocked:
                    break
                time.sleep(0.02)
            assert blocked, 'the HTTP edit did not wait for the row lock'
        held.commit()
        t.join(10)
    finally:
        held.close()
    assert problem(out['r'], 412, 'VERSION_MISMATCH')['current_version'] == 2
    final = a.get(f"{BASE}/proposals/{p['id']}").json()
    assert (final['revision'], final['rationale']) == (2, 'held winner')


# ------------------------------------------------------------------------------------------- rollback
@pytest.mark.parametrize('when', ['create', 'edit'])
def test_a_failure_after_the_rows_are_written_rolls_everything_back(database, api, world, when):
    c = signed_in(api, world, 'analyst')
    p = create(c, draft(world)).json() if when == 'edit' else None
    rows, before = proposal_rows(database, world), fingerprint(database, exclude=set())
    event = 'PROPOSAL_CREATED' if when == 'create' else 'PROPOSAL_EDITED'
    with connection(database=database, autocommit=True) as admin:
        admin.execute(f"""CREATE FUNCTION public.lv_test_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.event_type = '{event}' THEN RAISE EXCEPTION 'injected failure after the proposal rows'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER lv_test_fail BEFORE INSERT ON lv_identity.audit_event FOR EACH ROW EXECUTE FUNCTION public.lv_test_fail();""")
    try:
        key = uuid4().hex
        r = create(c, draft(world), key=key) if when == 'create' else edit(c, p['id'], {'rationale': 'never'})
        assert problem(r, 500, 'INTERNAL')['retryable'] is False
        assert proposal_rows(database, world) == rows and fingerprint(database, exclude=set()) == before   # incl. audit and idempotency
    finally:
        with connection(database=database, autocommit=True) as admin:
            admin.execute('DROP TRIGGER lv_test_fail ON lv_identity.audit_event; DROP FUNCTION public.lv_test_fail()')
    if when == 'create':
        assert create(c, draft(world), key=key).status_code == 201          # nothing was stored: the key is free
    else:
        assert edit(c, p['id'], {'rationale': 'now'}).json()['revision'] == 2


# ------------------------------------------------------------------------------------------- lists, revisions, conditional reads
def test_lists_filter_paginate_and_bind_cursors_to_their_filters(database, api, world):
    a, auto = signed_in(api, world, 'analyst'), signed_in(api, world, 'automation')
    made = [create(a if n % 2 else auto, draft(world, rationale=f'n{n}')).json()['id'] for n in range(5)]
    made.append(create(a, draft(world, ledger=world['l2']['ledger'], operations=[settlement(world, (world['q1'], '50000.00'),
                       gross='50000.00', net='49000.00', statement=None)], evidence_refs=[])).status_code)
    assert made.pop() == 404                                            # L1's statement is not in L2: refused, nothing created
    seen, cursor = [], None
    while True:
        r = a.get(f'{BASE}/proposals', params={'limit': '2', **({'cursor': cursor} if cursor else {})}).json()
        seen += [i['id'] for i in r['items']]
        cursor = r['next_cursor']
        if not cursor:
            break
    assert seen == sorted(made, reverse=True)                           # newest first, each exactly once
    mine = a.get(f'{BASE}/proposals', params={'created_by': world['users']['analyst']['id']}).json()['items']
    assert {i['created_by'] for i in mine} == {world['users']['analyst']['id']} and len(mine) == 2
    assert len(a.get(f'{BASE}/proposals', params={'ledger_id': str(world['l1']['ledger']), 'state': 'DRAFT'}).json()['items']) == 5
    assert a.get(f'{BASE}/proposals', params={'state': 'APPROVED'}).json()['items'] == []
    assert signed_in(api, world, 'controller').get(f'{BASE}/proposals', params={'reviewable_by_me': 'true'}).json()['items'] == []
    first = a.get(f'{BASE}/proposals', params={'limit': '2', 'state': 'DRAFT'}).json()
    problem(a.get(f'{BASE}/proposals', params={'limit': '2', 'cursor': first['next_cursor']}), 400, 'CURSOR_MISMATCH')
    problem(signed_in(api, world, 'analystB').get(f'{BASE}/proposals', params={'limit': '2', 'state': 'DRAFT',
                                                                                'cursor': first['next_cursor']}), 400, 'CURSOR_MISMATCH')
    problem(a.get(f'{BASE}/proposals', params={'sort': 'id'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    for bad in ({'state': 'Draft'}, {'ledger_id': 'x'}, {'reviewable_by_me': 'yes'}, {'limit': '0'}):
        problem(a.get(f'{BASE}/proposals', params=bad), 422, 'SCHEMA_INVALID')
    # Revision history paginates in order.
    pid = made[0]
    for n in range(2, 5):
        assert edit(a, pid, {'rationale': f'r{n}'}, if_match=f'"v{n - 1}"').status_code == 200
    revs, cursor = [], None
    while True:
        r = a.get(f'{BASE}/proposals/{pid}/revisions', params={'limit': '3', **({'cursor': cursor} if cursor else {})}).json()
        revs += [(i['revision'], i['rationale']) for i in r['items']]
        cursor = r['next_cursor']
        if not cursor:
            break
    assert revs == [(1, 'n0'), (2, 'r2'), (3, 'r3'), (4, 'r4')]
    other_cursor = a.get(f'{BASE}/proposals/{pid}/revisions', params={'limit': '1'}).json()['next_cursor']
    problem(a.get(f'{BASE}/proposals/{made[1]}/revisions', params={'limit': '1', 'cursor': other_cursor}), 400, 'CURSOR_MISMATCH')


def test_conditional_reads_use_the_resource_version(database, api, world):
    c = signed_in(api, world, 'analyst')
    p = create(c, draft(world)).json()
    path = f"{BASE}/proposals/{p['id']}"
    r = c.get(path, headers={'If-None-Match': '"v1"'})
    assert r.status_code == 304 and r.headers['ETag'] == '"v1"'
    problem(client(api).get(path, headers={'If-None-Match': '"v1"'}), 401, 'UNAUTHENTICATED')
    problem(signed_in(api, world, 'analystB').get(path, headers={'If-None-Match': '*'}), 404, 'NOT_FOUND')
    edit(c, p['id'], {'rationale': 'changed'})
    r = c.get(path, headers={'If-None-Match': '"v1"'})
    assert r.status_code == 200 and r.headers['ETag'] == '"v2"'


def test_every_proposal_operation_is_documented_with_its_permission():
    spec = create_app(AuthSettings()).openapi()
    expected = {('/api/v1/proposals', 'post'): ('createProposal', 'propose'), ('/api/v1/proposals', 'get'): ('listProposals', 'read'),
                ('/api/v1/proposals/{proposal_id}', 'get'): ('getProposal', 'read'),
                ('/api/v1/proposals/{proposal_id}', 'patch'): ('editProposal', 'propose'),
                ('/api/v1/proposals/{proposal_id}/revisions', 'get'): ('listProposalRevisions', 'read')}
    for (path, method), (op_id, perm) in expected.items():
        op = spec['paths'][path][method]
        assert (op['operationId'], op['x-lv-permission']) == (op_id, perm)
    assert spec['paths']['/api/v1/proposals']['post']['x-lv-idempotency'] == 'required'
    body = spec['paths']['/api/v1/proposals']['post']['requestBody']['content']['application/json']['schema']
    assert body['properties']['operations']['items'] == {'$ref': '#/components/schemas/RecordSettlementOp'}
    assert set(spec['components']['schemas']['Proposal']['required']) >= {'id', 'ledger_id', 'state', 'revision', 'version', 'operations',
                                                                           'origin', 'created_by'}


def test_definer_entry_points_resist_caller_temporary_objects(database, api, world):
    """R-01 regression for lv.proposal_create / lv.proposal_edit: a caller-created temporary domain must never be resolved."""
    c = signed_in(api, world, 'analyst')
    p = create(c, draft(world)).json()
    token = admin_token(database, world['A'], roles=('ANALYST',))
    with connection(database=database, autocommit=True) as admin:
        admin.execute(sql.SQL('GRANT TEMPORARY ON DATABASE {} TO lv_app').format(sql.Identifier(database)))
    try:
        with connection('lv_app', database, autocommit=True) as conn:
            conn.execute("""
              CREATE FUNCTION pg_temp.shadow_probe(v text) RETURNS boolean LANGUAGE plpgsql AS $$
              BEGIN RAISE EXCEPTION 'SHADOW EXECUTED AS %', current_user; END $$;
              CREATE DOMAIN pg_temp.uuid AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.jsonb AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.bytea AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE FUNCTION pg_temp.sha256(bytea) RETURNS bytea LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_temp.shadow_probe('x'); RETURN NULL; END $$;
            """)
            with conn.transaction():
                conn.execute('SELECT lv_identity.session_context(%s, false)', (token_hash(token),))
                body = json.dumps(draft(world, rationale='temp-object probe'))
                created = conn.execute("SELECT lv.proposal_create(%s, %s, %s, %s::pg_catalog.jsonb, NULL, 'test', 'r01-create')",
                                       (token_hash(token), uuid4().hex, '0' * 64, body)).fetchone()[0]
                assert created['status'] == 201, created
                edited = conn.execute("SELECT lv.proposal_edit(%s, %s::pg_catalog.uuid, '\"v1\"', %s::pg_catalog.jsonb, NULL, 'test', 'r01-edit')",
                                      (token_hash(token), p['id'], json.dumps({'rationale': 'probe edit'}))).fetchone()[0]
                assert edited['status'] == 200, edited
    finally:
        with connection(database=database, autocommit=True) as admin:
            admin.execute(sql.SQL('REVOKE TEMPORARY ON DATABASE {} FROM lv_app').format(sql.Identifier(database)))
    assert c.get(f"{BASE}/proposals/{p['id']}").json()['rationale'] == 'probe edit'
