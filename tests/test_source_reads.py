"""Source read APIs for settlement-draft selection controls — through the HTTP application and real PostgreSQL.

Owning contracts:
- Stage 2B Family 5: GET /ledgers/{id}/documents (`read`; ?type&party_id&status&date_from&date_to&q&as_of_seq -> page of
  DocumentSummary, provenance on every row) and GET /documents/{id} (current version, open amounts, links, postings summary);
- Stage 2B Family 3: GET /imports, /imports/{id} (ImportBatch with counts), /imports/{id}/records (?status=VALID|INVALID ->
  SourceRecord), /imports/{id}/staged-documents (StagedDocument);
- Part 2 (Money strings, cursor pagination 1-200 signed and bound to filters/order, unknown parameters 400, 404 for other
  tenants), Part 3 EvidenceRef (provenance and sha256 read-only), Part 4 codes, Part 5 (`read`), CT-04 (ids in path and filter);
- Stage 2A §5.2 (payment RECEIVED/SETTLED, unapplied = amount - applied), §11.2 (source_file/import_batch/source_record/
  staged_document), §12.1 (FORCE RLS), §19.2 (staging has no ledger effect).
Every tenant, ledger, payment, statement and user is SYNTHETIC and created per test. Accounting remains UNREVIEWED.
"""
import json
import secrets
import time
from uuid import uuid4

import pytest

from tools.dev_db import connection
from tools.identity_admin import owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ingestion import statement_format as fmt
from ledgervigil.ingestion.store import ingest_statement
from ledgervigil.ledger.seed import seed_processor_payment, seed_processor_settlement
from ledgervigil.reads import sources
from test_ledger_reads import _tamper, problem, walk
from test_period_close import everything
from test_proposal_drafts import (COOKIE, BASE, api, client, counters, create, fingerprint, seed, signed_in, world)  # noqa: F401

UNICODE = 'Café ₹ 日本語 — مرحبا 🚀 ﬁ‍'


# ------------------------------------------------------------------------------------------- helpers
def ingest(database, cfg, payout, items, gross, fee, net, memo='SYNTHETIC statement; not a real processor file', **statement):
    body = {'tenant': str(cfg['tenant']), 'ledger': str(cfg['ledger']), 'processor': 'processor-1', 'payout': payout, 'date': '2026-10-14',
            'currency': cfg['currency'], 'items': [{'id': i, 'kind': 'PAYMENT', 'amount': a} for i, a in items],
            'gross': gross, 'fee': fee, 'net': net, 'memo': memo}
    body.update(statement)
    raw = json.dumps({'format': fmt.FORMAT, 'version': fmt.VERSION, 'data_classification': 'SYNTHETIC', 'statement': body},
                     ensure_ascii=False).encode()
    with connection('lv_ingest', database, autocommit=True) as c:
        return ingest_statement(c, cfg['tenant'], cfg['ledger'], raw, declared_format=fmt.FORMAT, declared_version=fmt.VERSION)


def pay(seed, cfg, item, amount, description='SYNTHETIC typed processor payment (source read tests)', party=None):
    return seed_processor_payment(seed, cfg['tenant'], cfg['ledger'], {
        'processor_ref': 'processor-1', 'processor_item_id': item, 'party_id': party or cfg['party'], 'currency': cfg['currency'],
        'amount': amount, 'received_date': '2026-10-10', 'description': description})['document_id']


def batch_of(database, staged_id):
    with connection(database=database) as admin:
        return str(admin.execute('SELECT import_batch_id FROM lv.staged_document WHERE id = %s', (staged_id,)).fetchone()[0])


def version_id(database, document, version=1):
    with connection(database=database) as admin:
        return str(admin.execute('SELECT id FROM lv.document_version WHERE document_id = %s AND version = %s', (document, version)).fetchone()[0])


def m(amount, currency='INR'):
    return {'amount': amount, 'currency': currency}


def routes(w, database):
    imp = batch_of(database, w['s1'])
    return [f"{BASE}/ledgers/{w['l1']['ledger']}/documents", f"{BASE}/documents/{w['p1']}", f'{BASE}/imports',
            f'{BASE}/imports/{imp}', f'{BASE}/imports/{imp}/records', f'{BASE}/imports/{imp}/staged-documents']


def by_id(items):
    return {i['id']: i for i in items}


# ------------------------------------------------------------------------------------------- A. session, permission, revocation
def test_every_route_requires_a_verified_session_before_any_validation_or_304(database, api, world):
    a = signed_in(api, world, 'analyst')
    etags = {}
    for path in routes(world, database):
        r = a.get(path)
        assert r.status_code == 200, (path, r.text)
        etags[path] = r.headers['ETag']
        assert r.headers['Cache-Control'] == 'no-store'
    anonymous, forged = client(api), client(api)
    forged.cookies.set(COOKIE, secrets.token_urlsafe(32))
    for path in routes(world, database):
        for c in (anonymous, forged):
            problem(c.get(path), 401, 'UNAUTHENTICATED')
            problem(c.get(path, headers={'If-None-Match': etags[path]}), 401, 'UNAUTHENTICATED')
            problem(c.get(path, params={'limit': '0', 'bogus': '1', 'cursor': 'x'}), 401, 'UNAUTHENTICATED')
        assert a.get(path, headers={'If-None-Match': etags[path]}).status_code == 304


def test_disabled_users_revoked_memberships_and_removed_roles_lose_access_on_the_next_request(database, api, world):
    analyst, controller, automation = (signed_in(api, world, n) for n in ('analyst', 'controller', 'automation'))
    paths = routes(world, database)
    for c in (analyst, controller, automation):
        assert all(c.get(p).status_code == 200 for p in paths)
    with owner_transaction(database) as conn:          # direct row changes; sessions deliberately NOT revoked
        conn.execute('UPDATE lv_identity.app_user SET disabled_at = now() WHERE id = %s', (world['users']['analyst']['id'],))
        conn.execute("UPDATE lv_identity.membership SET status = 'REVOKED', revoked_at = now() WHERE tenant_id = %s AND user_id = %s",
                     (world['A'], world['users']['controller']['id']))
        conn.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id = %s AND user_id = %s',
                     (world['A'], world['users']['automation']['id']))
    for p in paths:
        problem(analyst.get(p), 401, 'UNAUTHENTICATED')
        problem(controller.get(p), 401, 'UNAUTHENTICATED')
        assert problem(automation.get(p), 403, 'PERMISSION_DENIED')['detail'] == 'requires read'


def test_logged_out_sessions_cannot_read(api, world, database):
    a = signed_in(api, world, 'analyst')
    assert a.post(f'{BASE}/auth/logout').status_code in (200, 204)
    for p in routes(world, database):
        problem(a.get(p), 401, 'UNAUTHENTICATED')


# ------------------------------------------------------------------------------------------- B. tenant and ledger isolation
def test_other_tenant_ids_are_404_exactly_like_absent_ids_in_paths_and_filters(database, api, world):
    a = signed_in(api, world, 'analyst')
    foreign_import = batch_of(database, world['sb'])
    pairs = [(f"{BASE}/ledgers/{world['lb']['ledger']}/documents", f'{BASE}/ledgers/{uuid4()}/documents'),
             (f"{BASE}/documents/{world['pb']}", f'{BASE}/documents/{uuid4()}'),
             (f'{BASE}/imports/{foreign_import}', f'{BASE}/imports/{uuid4()}'),
             (f'{BASE}/imports/{foreign_import}/records', f'{BASE}/imports/{uuid4()}/records'),
             (f'{BASE}/imports/{foreign_import}/staged-documents', f'{BASE}/imports/{uuid4()}/staged-documents')]
    for foreign, absent in pairs:
        for params in ({}, {} if '/documents/' in foreign or foreign.count('/') == 4 else {'limit': '1'}):
            rf, ra = a.get(foreign, params=params), a.get(absent, params=params)
            bf, ba = problem(rf, 404, 'NOT_FOUND'), problem(ra, 404, 'NOT_FOUND')
            bf.pop('request_id'); ba.pop('request_id')
            assert bf == ba and 'ETag' not in rf.headers
    rf = a.get(f'{BASE}/imports', params={'ledger_id': str(world['lb']['ledger'])})
    ra = a.get(f'{BASE}/imports', params={'ledger_id': str(uuid4())})
    bf, ba = problem(rf, 404, 'NOT_FOUND'), problem(ra, 404, 'NOT_FOUND')
    bf.pop('request_id'); ba.pop('request_id')
    assert bf == ba
    # Another tenant's payment as a party filter value simply matches nothing in this ledger (a party id is not a resource path).
    b = signed_in(api, world, 'analystB')
    assert {i['ledger_id'] for i in walk(b, f'{BASE}/imports', 200)[0]} == {str(world['lb']['ledger'])}
    assert by_id(walk(b, f"{BASE}/ledgers/{world['lb']['ledger']}/documents", 200)[0]).keys() == {str(world['pb'])}


def test_lists_are_scoped_to_their_ledger_and_batch(database, api, world):
    a = signed_in(api, world, 'analyst')
    l1 = by_id(walk(a, f"{BASE}/ledgers/{world['l1']['ledger']}/documents", 200)[0])
    assert l1.keys() == {str(world['p1']), str(world['p2']), str(world['journal'])}
    assert by_id(walk(a, f"{BASE}/ledgers/{world['l2']['ledger']}/documents", 200)[0]).keys() == {str(world['q1'])}
    assert walk(a, f"{BASE}/ledgers/{world['books']['ledger']}/documents", 200)[0] == []
    every = walk(a, f'{BASE}/imports', 200)[0]
    assert {i['ledger_id'] for i in every} == {str(world['l1']['ledger']), str(world['l2']['ledger'])}
    only_l1 = walk(a, f'{BASE}/imports', 200, ledger_id=str(world['l1']['ledger']))[0]
    assert [i['id'] for i in only_l1] == [batch_of(database, world['s1'])]
    staged = walk(a, f"{BASE}/imports/{batch_of(database, world['s2'])}/staged-documents", 200)[0]
    assert [s['id'] for s in staged] == [str(world['s2'])] and staged[0]['ledger_id'] == str(world['l2']['ledger'])


def test_ledger_ids_in_filters_are_canonicalised_and_case_does_not_change_scope(database, api, world):
    a = signed_in(api, world, 'analyst')
    lower = a.get(f'{BASE}/imports', params={'ledger_id': str(world['l1']['ledger'])}).json()
    upper = a.get(f'{BASE}/imports', params={'ledger_id': str(world['l1']['ledger']).upper()}).json()
    assert lower == upper and len(lower['items']) == 1


# ------------------------------------------------------------------------------------------- C. representations and decimals
def test_payment_document_detail_states_exact_money_status_version_provenance_and_evidence(database, api, world):
    a = signed_in(api, world, 'analyst')
    r = a.get(f"{BASE}/documents/{world['p2']}")
    assert r.status_code == 200 and r.headers['ETag'].startswith('"r')
    d = r.json()
    assert d['type'] == 'PAYMENT' and d['status'] == 'RECEIVED' and d['version'] == 1
    assert d['amount'] == m('2500.50') and d['payment'] == {
        'amount': m('2500.50'), 'applied': m('0.00'), 'unapplied': m('2500.50'), 'received_date': '2026-10-10', 'status': 'RECEIVED',
        'settlement_document_id': None}
    assert d['settlement'] is None and d['processor_ref'] == 'processor-1' and d['processor_item_id'] == 'PMT-2002'
    assert d['source_ref'] == 'processor-1/PMT-2002' and d['party'] == {'id': world['l1']['party'], 'name': 'Synthetic customer'}
    assert d['evidence_ref'] == {'kind': 'DOCUMENT_VERSION', 'id': version_id(database, world['p2']), 'provenance': 'SYNTHETIC', 'sha256': None}
    assert d['current_version_id'] == d['evidence_ref']['id'] and d['provenance'] == 'SYNTHETIC' and d['origin'] == 'SEED'
    assert d['purpose'] == 'SANDBOX' and d['data_label'] == 'SANDBOX — not accounting records' and d['accounting_status'] == 'UNREVIEWED'
    assert len(d['financial_fingerprint']) == 64 and len(d['postings']) == 1
    posting = d['postings'][0]
    assert posting['posting_rule'] == 'sp1-posting/1.0' and posting['source_document_version'] == 1 and posting['seq'] == d['created_seq']
    journal = a.get(f"{BASE}/documents/{world['journal']}").json()
    assert journal['type'] == 'MANUAL_JOURNAL' and journal['status'] is None and journal['amount'] is None and journal['party'] is None
    assert journal['date'] == '2026-10-05' and journal['payment'] is None and journal['settlement'] is None


def test_settled_payments_and_settlement_documents_show_actual_links_and_versions(database, api, world, seed):
    cfg = world['l2']
    result = seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], {
        'processor_ref': 'processor-1', 'payout_id': 'STL-9001', 'payout_date': '2026-10-14', 'currency': 'INR', 'gross': '50000.00',
        'fee': '1000.00', 'net': '49000.00', 'items': [{'document_id': str(world['q1']), 'kind': 'PAYMENT', 'amount': '50000.00'}],
        'description': 'SYNTHETIC typed processor settlement fixture'})
    settlement_id = result['document_id']
    a = signed_in(api, world, 'analyst')
    q1 = a.get(f"{BASE}/documents/{world['q1']}").json()
    assert q1['status'] == 'SETTLED' and q1['version'] == 2 and q1['payment']['settlement_document_id'] == settlement_id
    assert q1['evidence_ref']['id'] == version_id(database, world['q1'], 2) != version_id(database, world['q1'], 1)
    s = a.get(f'{BASE}/documents/{settlement_id}').json()
    assert s['type'] == 'SETTLEMENT' and s['status'] == 'RECORDED' and s['amount'] == m('50000.00') and s['payout_id'] == 'STL-9001'
    assert s['settlement'] == {'payout_date': '2026-10-14', 'gross': m('50000.00'), 'fee': m('1000.00'), 'net': m('49000.00'),
                               'item_count': 1, 'status': 'RECORDED',
                               'items': [{'line_no': 1, 'document_id': str(world['q1']), 'kind': 'PAYMENT', 'amount': m('50000.00'),
                                          'payment_version': 1}]}
    path = f"{BASE}/ledgers/{cfg['ledger']}/documents"
    assert [i['id'] for i in a.get(path, params={'status': 'SETTLED'}).json()['items']] == [str(world['q1'])]
    assert [i['id'] for i in a.get(path, params={'status': 'RECORDED'}).json()['items']] == [settlement_id]
    assert a.get(path, params={'status': 'RECEIVED'}).json()['items'] == []
    assert [i['id'] for i in a.get(path, params={'type': 'SETTLEMENT'}).json()['items']] == [settlement_id]
    page = a.get(path).json()
    assert page['as_of_seq'] == result_seq(database, cfg['ledger'])


def result_seq(database, ledger):
    with connection(database=database) as admin:
        return admin.execute('SELECT last_seq FROM lv.ledger_counter WHERE ledger_id = %s', (ledger,)).fetchone()[0]


@pytest.mark.parametrize('amount', ['0.01', '9999999999999999.99', '1234567890123456.78', '0.10'])
def test_payment_amounts_are_exact_strings_at_the_currency_exponent(database, api, world, seed, amount):
    doc = pay(seed, world['l2'], f'PMT-X{uuid4().hex[:6]}', amount)
    a = signed_in(api, world, 'analyst')
    detail = a.get(f'{BASE}/documents/{doc}').json()
    assert detail['amount'] == m(amount) and detail['payment']['unapplied'] == m(amount)
    listed = by_id(a.get(f"{BASE}/ledgers/{world['l2']['ledger']}/documents").json()['items'])[doc]
    assert listed['amount'] == m(amount)


def test_staged_statement_amounts_are_normalised_exactly_to_the_currency_exponent(database, api, world):
    cfg = world['l2']
    result = ingest(database, cfg, 'STL-DEC', [('PMT-D1', '2500.5'), ('PMT-D2', '7')], '2507.5', '0.5', '2507')
    assert result['status'] == 'STAGED', result
    a = signed_in(api, world, 'analyst')
    s = a.get(f"{BASE}/imports/{result['import_batch_id']}/staged-documents").json()['items'][0]
    assert (s['gross'], s['fee'], s['net']) == (m('2507.50'), m('0.50'), m('2507.00'))
    assert [(i['line_no'], i['item_ref'], i['kind'], i['amount']) for i in s['items']] == [
        (1, 'PMT-D1', 'PAYMENT', m('2500.50')), (2, 'PMT-D2', 'PAYMENT', m('7.00'))]


def test_imports_records_and_staged_documents_carry_provenance_hashes_and_actual_statuses(database, api, world):
    a = signed_in(api, world, 'analyst')
    imp = batch_of(database, world['s1'])
    b = a.get(f'{BASE}/imports/{imp}').json()
    with connection(database=database) as admin:
        sha, size, record_hash = admin.execute(
            'SELECT f.sha256, f.byte_size, r.content_hash FROM lv.import_batch b JOIN lv.source_file f ON f.id = b.source_file_id '
            'JOIN lv.source_record r ON r.import_batch_id = b.id WHERE b.id = %s', (imp,)).fetchone()
    assert b['status'] == 'STAGED' and b['ledger_effect'] == 'NONE' and b['failure_code'] is None and b['related_staged_document_id'] is None
    assert b['source_file']['sha256'] == sha and b['source_file']['byte_size'] == size and b['source_file']['content_stored'] is True
    assert b['source_file']['data_classification'] == 'SYNTHETIC' and b['record_counts'] == {'VALID': 1, 'INVALID': 0}
    assert b['staged_document_count'] == 1 and b['data_label'] == 'SANDBOX — not accounting records'
    records = a.get(f'{BASE}/imports/{imp}/records').json()['items']
    assert len(records) == 1 and records[0]['id'] == str(world['r1']) and records[0]['validation_status'] == 'VALID'
    assert records[0]['messages'] == [] and records[0]['evidence_ref'] == {'kind': 'SOURCE_RECORD', 'id': str(world['r1']),
                                                                           'provenance': None, 'sha256': record_hash}
    import hashlib
    assert hashlib.sha256(records[0]['content'].encode()).hexdigest() == record_hash == records[0]['evidence_ref']['sha256']
    assert records[0]['byte_length'] == len(records[0]['content'].encode())
    s = a.get(f'{BASE}/imports/{imp}/staged-documents').json()['items'][0]
    assert s['id'] == str(world['s1']) and s['type'] == 'SETTLEMENT_STATEMENT' and s['validation_status'] == 'VALID'
    assert s['provenance'] == 'SYNTHETIC' and s['source_record_id'] == str(world['r1'])
    assert s['evidence_ref'] == {'kind': 'STAGED_DOCUMENT', 'id': str(world['s1']), 'provenance': 'SYNTHETIC', 'sha256': None}
    assert s['source_record_evidence_ref'] == records[0]['evidence_ref']
    assert (s['gross'], s['fee'], s['net'], s['item_count']) == (m('52500.50'), m('1000.00'), m('51500.50'), 2)
    assert [i['item_ref'] for i in s['items']] == ['PMT-2001', 'PMT-2002']
    assert (s['duplicate_batch_count'], s['conflicting_batch_count']) == (0, 0)


def test_rejected_duplicate_and_conflicting_batches_report_their_stored_outcomes(database, api, world):
    cfg = world['l1']
    bad = ingest(database, cfg, 'STL-BAD', [('PMT-B1', '10.00')], '11.00', '1.00', '10.00')
    assert bad['status'] == 'REJECTED' and bad['failure_code'] == 'ITEM_TOTAL_MISMATCH'
    dup = ingest(database, cfg, 'STL-3001', [('PMT-2001', '50000.00'), ('PMT-2002', '2500.50')], '52500.50', '1000.00', '51500.50',
                 memo='SYNTHETIC statement; same content, other bytes')
    assert dup['status'] == 'DUPLICATE'
    conflict = ingest(database, cfg, 'STL-3001', [('PMT-2001', '50000.00')], '50000.00', '1000.00', '49000.00')
    assert conflict['status'] == 'REJECTED' and conflict['failure_code'] == 'PAYOUT_IDENTITY_CONFLICT'
    a = signed_in(api, world, 'analyst')
    b = a.get(f"{BASE}/imports/{bad['import_batch_id']}").json()
    assert b['status'] == 'REJECTED' and b['failure_code'] == 'ITEM_TOTAL_MISMATCH' and b['record_counts'] == {'VALID': 0, 'INVALID': 1}
    assert b['staged_document_count'] == 0 and b['ledger_effect'] == 'NONE'
    recs = a.get(f"{BASE}/imports/{bad['import_batch_id']}/records").json()['items']
    assert [r['validation_status'] for r in recs] == ['INVALID']
    assert recs[0]['messages'] == [{'code': 'ITEM_TOTAL_MISMATCH', 'detail': 'payments minus refunds must equal gross'}]
    assert a.get(f"{BASE}/imports/{bad['import_batch_id']}/records", params={'status': 'VALID'}).json()['items'] == []
    assert len(a.get(f"{BASE}/imports/{bad['import_batch_id']}/records", params={'status': 'INVALID'}).json()['items']) == 1
    assert a.get(f"{BASE}/imports/{bad['import_batch_id']}/staged-documents").json()['items'] == []
    d = a.get(f"{BASE}/imports/{dup['import_batch_id']}").json()
    assert d['status'] == 'DUPLICATE' and d['related_staged_document_id'] == str(world['s1']) and d['record_counts'] == {'VALID': 0, 'INVALID': 0}
    s1 = a.get(f"{BASE}/imports/{batch_of(database, world['s1'])}/staged-documents").json()['items'][0]
    assert (s1['duplicate_batch_count'], s1['conflicting_batch_count']) == (1, 1)
    imp = batch_of(database, world['s1'])
    assert a.get(f'{BASE}/imports/{imp}/records', params={'status': 'INVALID'}).json()['items'] == []


def test_unicode_source_text_round_trips_byte_for_byte_and_is_labelled_untrusted(database, api, world, seed):
    cfg = world['l2']
    result = ingest(database, cfg, 'STL-UNI', [('PMT-U1', '10.00')], '10.00', '0.00', '10.00', memo=UNICODE)
    assert result['status'] == 'STAGED', result
    doc = pay(seed, cfg, 'PMT-U1', '10.00', description=UNICODE)
    a = signed_in(api, world, 'analyst')
    r = a.get(f"{BASE}/imports/{result['import_batch_id']}/staged-documents")
    assert UNICODE.encode() in r.content and r.json()['items'][0]['description_untrusted'] == UNICODE
    record = a.get(f"{BASE}/imports/{result['import_batch_id']}/records").json()['items'][0]
    assert UNICODE in json.loads(record['content'])['memo']
    assert a.get(f'{BASE}/documents/{doc}').json()['description_untrusted'] == UNICODE


# ------------------------------------------------------------------------------------------- D. strict queries
@pytest.mark.parametrize('params,status,code', [
    ({'bogus': '1'}, 400, 'UNKNOWN_QUERY_PARAMETER'),
    ({'TYPE': 'PAYMENT'}, 400, 'UNKNOWN_QUERY_PARAMETER'),
    ({'type': 'payment'}, 422, 'SCHEMA_INVALID'),
    ({'type': 'ПЛАТЁЖ'}, 422, 'SCHEMA_INVALID'),
    ({'type': 'INVOICE'}, 422, 'CAPABILITY_UNAVAILABLE'),
    ({'type': 'REFUND'}, 422, 'CAPABILITY_UNAVAILABLE'),
    ({'status': 'OPEN'}, 422, 'SCHEMA_INVALID'),
    ({'status': 'received'}, 422, 'SCHEMA_INVALID'),
    ({'party_id': 'not-a-uuid'}, 422, 'SCHEMA_INVALID'),
    ({'party_id': '٣' * 36}, 422, 'SCHEMA_INVALID'),
    ({'date_from': '2026-10-01'}, 422, 'CAPABILITY_UNAVAILABLE'),
    ({'date_to': '2026-10-31'}, 422, 'CAPABILITY_UNAVAILABLE'),
    ({'date_from': '2026-13-01'}, 422, 'SCHEMA_INVALID'),
    ({'date_to': '２０２６-10-01'}, 422, 'SCHEMA_INVALID'),
    ({'q': 'PMT'}, 422, 'CAPABILITY_UNAVAILABLE'),
    ({'as_of_seq': '1'}, 422, 'CAPABILITY_UNAVAILABLE'),
    ({'as_of_seq': '-1'}, 422, 'SCHEMA_INVALID'),
    ({'as_of_seq': '١'}, 422, 'SCHEMA_INVALID'),
    ({'limit': '0'}, 422, 'SCHEMA_INVALID'),
    ({'limit': '201'}, 422, 'SCHEMA_INVALID'),
    ({'limit': '٥'}, 422, 'SCHEMA_INVALID'),
    ({'limit': '1.0'}, 422, 'SCHEMA_INVALID'),
    ({'cursor': 'not.valid'}, 400, 'CURSOR_MISMATCH'),
    ({'cursor': '日本.語'}, 400, 'CURSOR_MISMATCH'),
])
def test_document_list_queries_are_strict(api, world, params, status, code):
    a = signed_in(api, world, 'analyst')
    problem(a.get(f"{BASE}/ledgers/{world['l1']['ledger']}/documents", params=params), status, code)


def test_repeated_parameters_and_import_query_errors(database, api, world):
    a = signed_in(api, world, 'analyst')
    docs = f"{BASE}/ledgers/{world['l1']['ledger']}/documents"
    imp = batch_of(database, world['s1'])
    problem(a.get(docs + '?type=PAYMENT&type=PAYMENT'), 422, 'SCHEMA_INVALID')
    problem(a.get(f'{BASE}/imports?limit=1&limit=2'), 422, 'SCHEMA_INVALID')
    problem(a.get(f'{BASE}/imports', params={'ledger_id': 'x'}), 422, 'SCHEMA_INVALID')
    problem(a.get(f'{BASE}/imports', params={'status': 'STAGED'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    problem(a.get(f'{BASE}/imports/{imp}/records', params={'status': 'valid'}), 422, 'SCHEMA_INVALID')
    problem(a.get(f'{BASE}/imports/{imp}/records', params={'kind': 'x'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    problem(a.get(f'{BASE}/imports/{imp}/staged-documents', params={'status': 'VALID'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    problem(a.get(f'{BASE}/imports/{imp}', params={'limit': '1'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    problem(a.get(f"{BASE}/documents/{world['p1']}", params={'version': '1'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    for bad in ('not-a-uuid', '日本', '1' * 40, '%00'):
        problem(a.get(f'{BASE}/documents/{bad}'), 422, 'SCHEMA_INVALID')
        problem(a.get(f'{BASE}/imports/{bad}/records'), 422, 'SCHEMA_INVALID')
        problem(a.get(f'{BASE}/ledgers/{bad}/documents'), 422, 'SCHEMA_INVALID')


# ------------------------------------------------------------------------------------------- E. pagination and cursors
def test_document_pages_cover_every_row_once_newest_first_with_filters(database, configuration, api, world, seed):
    cfg = world['l2']
    made = [pay(seed, cfg, f'PMT-P{n:02d}', f'{n}.00') for n in range(1, 8)]
    with tenant_transaction(configuration, cfg['tenant']):
        other_party = str(configuration.execute("INSERT INTO lv.party(tenant_id, ledger_id, name) VALUES (%s, %s, 'Synthetic customer 2') RETURNING id",
                                      (cfg['tenant'], cfg['ledger'])).fetchone()[0])
    others = [pay(seed, cfg, f'PMT-O{n}', '3.00', party=other_party) for n in range(2)]
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/documents"
    full = a.get(path, params={'limit': '200'}).json()['items']
    for limit in (1, 2, 3, 200):
        items, pages = walk(a, path, limit)
        assert [i['id'] for i in items] == [i['id'] for i in full] and len(set(i['id'] for i in items)) == len(items)
        assert pages == -(-len(full) // limit)
    assert [i['id'] for i in full] == sorted((i['id'] for i in full), reverse=True)
    assert set(made) | set(others) | {str(world['q1'])} == {i['id'] for i in full}
    payments, _ = walk(a, path, 2, type='PAYMENT', party_id=cfg['party'])
    assert {i['id'] for i in payments} == set(made) | {str(world['q1'])}
    assert {i['id'] for i in walk(a, path, 1, party_id=other_party)[0]} == set(others)
    assert a.get(path, params={'party_id': str(uuid4())}).json()['items'] == []
    exact = a.get(path, params={'limit': '10'}).json()
    assert len(exact['items']) == 10 and exact['next_cursor'] is None


def test_cursors_are_bound_to_operation_ledger_filters_tenant_and_key(database, api, world, seed):
    for n in range(3):
        pay(seed, world['l1'], f'PMT-C{n}', '1.00')
        pay(seed, world['l2'], f'PMT-C{n}', '1.00')
    a = signed_in(api, world, 'analyst')
    l1, l2 = (f"{BASE}/ledgers/{world[k]['ledger']}/documents" for k in ('l1', 'l2'))
    plain = a.get(l1, params={'limit': '1'}).json()['next_cursor']
    typed = a.get(l1, params={'limit': '1', 'type': 'PAYMENT'}).json()['next_cursor']
    assert plain and typed and a.get(l1, params={'limit': '1', 'cursor': plain}).status_code == 200
    problem(a.get(l2, params={'limit': '1', 'cursor': plain}), 400, 'CURSOR_MISMATCH')
    problem(a.get(l1, params={'limit': '1', 'cursor': typed}), 400, 'CURSOR_MISMATCH')
    problem(a.get(l1, params={'limit': '1', 'type': 'PAYMENT', 'cursor': plain}), 400, 'CURSOR_MISMATCH')
    problem(a.get(l1, params={'limit': '1', 'type': 'PAYMENT', 'status': 'RECEIVED', 'cursor': typed}), 400, 'CURSOR_MISMATCH')
    problem(a.get(f'{BASE}/imports', params={'cursor': plain}), 400, 'CURSOR_MISMATCH')
    imports_cursor = a.get(f'{BASE}/imports', params={'limit': '1'}).json()['next_cursor']
    assert imports_cursor
    problem(a.get(l1, params={'cursor': imports_cursor}), 400, 'CURSOR_MISMATCH')
    problem(a.get(f'{BASE}/imports', params={'cursor': imports_cursor, 'ledger_id': str(world['l1']['ledger'])}), 400, 'CURSOR_MISMATCH')
    for bad in _tamper(plain):
        assert problem(a.get(l1, params={'limit': '1', 'cursor': bad}), 400, 'CURSOR_MISMATCH')['errors'] == [
            {'pointer': '/query/cursor', 'code': 'invalid'}]
    b = signed_in(api, world, 'analystB')
    problem(b.get(f'{BASE}/imports', params={'cursor': imports_cursor}), 400, 'CURSOR_MISMATCH')
    other = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database))
    try:
        problem(signed_in(other, world, 'analyst').get(l1, params={'limit': '1', 'cursor': plain}), 400, 'CURSOR_MISMATCH')
    finally:
        other.state.pool.close()


def test_import_pages_cover_every_batch_once_and_records_pages_are_bounded(database, api, world):
    cfg = world['l2']
    made = [ingest(database, cfg, f'STL-PG{n}', [(f'PMT-PG{n}', '5.00')], '5.00', '0.00', '5.00')['import_batch_id'] for n in range(5)]
    a = signed_in(api, world, 'analyst')
    every = [i['id'] for i in walk(a, f'{BASE}/imports', 200, ledger_id=str(cfg['ledger']))[0]]
    assert set(made) <= set(every) and every == sorted(every, reverse=True)
    for limit in (1, 2, 4):
        assert [i['id'] for i in walk(a, f'{BASE}/imports', limit, ledger_id=str(cfg['ledger']))[0]] == every
    for imp in made[:2]:
        assert len(walk(a, f'{BASE}/imports/{imp}/records', 1)[0]) == 1
        assert len(walk(a, f'{BASE}/imports/{imp}/staged-documents', 1)[0]) == 1


def test_content_budgets_keep_at_least_one_row_and_resume_exactly_after_the_last_kept_row():
    rows = [('a', 600), ('b', 600), ('c', 100), ('d', 2000), ('e', 1)]
    assert sources._bounded(rows, 4, lambda r: r[1], 1000) == ([('a', 600)], True)
    assert sources._bounded(rows[1:], 4, lambda r: r[1], 1000) == ([('b', 600), ('c', 100)], True)
    assert sources._bounded(rows[3:], 4, lambda r: r[1], 1000) == ([('d', 2000)], True)
    assert sources._bounded(rows[4:], 4, lambda r: r[1], 1000) == ([('e', 1)], False)
    assert sources._bounded(rows[:2], 1, lambda r: r[1], 10 ** 9) == ([('a', 600)], True)
    assert sources._bounded([], 3, lambda r: r[1], 1) == ([], False)


def test_record_and_staged_budgets_page_large_rows_through_http(database, api, world, monkeypatch):
    monkeypatch.setattr(sources, 'RECORD_CONTENT_BUDGET', 1)
    monkeypatch.setattr(sources, 'STAGED_ITEM_BUDGET', 1)
    a = signed_in(api, world, 'analyst')
    imp = batch_of(database, world['s1'])
    assert len(a.get(f'{BASE}/imports/{imp}/records').json()['items']) == 1
    staged = a.get(f'{BASE}/imports/{imp}/staged-documents').json()
    assert len(staged['items']) == 1 and staged['items'][0]['item_count'] == 2 and staged['next_cursor'] is None


# ------------------------------------------------------------------------------------------- F. read safety
def test_reads_change_no_financial_ingestion_proposal_or_audit_state(database, api, world):
    a = signed_in(api, world, 'analyst')
    before, ctrs = everything(database), counters(database)
    time.sleep(0.01)
    paths = routes(world, database) + [f"{BASE}/ledgers/{world['lb']['ledger']}/documents", f'{BASE}/documents/{uuid4()}']
    for path in paths:
        for params in ({}, {'limit': '1'}, {'type': 'PAYMENT'}, {'status': 'VALID'}, {'q': 'x'}, {'bogus': '1'}, {'cursor': 'x.y'}):
            r = a.get(path, params=params)
            r2 = a.get(path, params=params, headers={'If-None-Match': r.headers.get('ETag', '*')})
            assert r.status_code < 500 and r2.status_code < 500, (path, params, r.text)
    assert everything(database) == before and counters(database) == ctrs


def test_the_read_transaction_refuses_writes_even_if_route_code_attempts_one(database, api, world, monkeypatch):
    original = sources._read_batch
    attempted = []

    def writing(ctx, import_id):
        attempted.append(True)
        # Session touch is normally authorized for this login; capture the exact READ ONLY error.
        from ledgervigil.auth.crypto import token_hash
        try:ctx.conn.execute('SELECT lv_identity.session_context(%s,true)',(token_hash(ctx.session_token),))
        except Exception as exc:
            assert exc.sqlstate=='25006',type(exc).__name__
            attempted.append('READ_ONLY');raise
        return original(ctx, import_id)
    monkeypatch.setattr(sources, '_read_batch', writing)
    a = signed_in(api, world, 'analyst')
    before = everything(database)
    body = problem(a.get(f"{BASE}/imports/{batch_of(database, world['s1'])}"), 500, 'INTERNAL')
    assert attempted==[True,'READ_ONLY'] and 'tampered' not in json.dumps(body) and everything(database) == before


def test_a_typed_document_without_its_projection_fails_closed(database, api, world, monkeypatch):
    monkeypatch.setattr(sources, 'DOCUMENT_FROM', sources.DOCUMENT_FROM.replace(
        'LEFT JOIN lv.payment_current pc ON (pc.tenant_id, pc.ledger_id, pc.document_id) = (d.tenant_id, d.ledger_id, d.id)',
        'LEFT JOIN lv.payment_current pc ON false'))
    a = signed_in(api, world, 'analyst')
    problem(a.get(f"{BASE}/documents/{world['p1']}"), 500, 'INTERNAL')
    problem(a.get(f"{BASE}/ledgers/{world['l1']['ledger']}/documents"), 500, 'INTERNAL')


def test_every_operation_is_documented_with_read_permission_and_shared_money():
    spec = create_app(AuthSettings()).openapi()
    expected = {('/api/v1/ledgers/{ledger_id}/documents', 'listLedgerDocuments'), ('/api/v1/documents/{document_id}', 'getDocument'),
                ('/api/v1/imports', 'listImports'), ('/api/v1/imports/{import_id}', 'getImport'),
                ('/api/v1/imports/{import_id}/records', 'listImportRecords'),
                ('/api/v1/imports/{import_id}/staged-documents', 'listImportStagedDocuments')}
    for path, op in expected:
        operation = spec['paths'][path]['get']
        assert operation['operationId'] == op and operation['x-lv-permission'] == 'read'
        assert {'400', '401', '403', '422'} <= set(operation['responses'])
        assert set(spec['paths'][path]) == {'get'}
    schemas = spec['components']['schemas']
    for name in ('DocumentSummary', 'Document', 'ImportBatch', 'SourceRecord', 'StagedDocument'):
        assert schemas[name]['additionalProperties'] is False
    assert schemas['DocumentSummary']['properties']['evidence_ref'] == {'$ref': '#/components/schemas/EvidenceRef', 'description': schemas[
        'DocumentSummary']['properties']['evidence_ref']['description']}
    assert schemas['Money']['properties']['amount']['pattern'] == '^[0-9]{1,16}(\\.[0-9]{1,4})?$'


# ------------------------------------------------------------------------------------------- G. discovery -> draft, no financial effect
def test_http_discovery_of_payment_statement_and_evidence_creates_a_draft_without_financial_effect(database, api, world):
    """After fixture preparation: discover everything over HTTP, create a RecordSettlement draft with the returned ids only,
    and compare every financial, ingestion and identity table (except the proposal tables, idempotency rows and audit chain)."""
    a = signed_in(api, world, 'analyst')
    before, ctrs = fingerprint(database), counters(database)
    ledger = world['l1']['ledger']
    imports = a.get(f'{BASE}/imports', params={'ledger_id': str(ledger)}).json()['items']
    statement = a.get(f"{BASE}/imports/{imports[0]['id']}/staged-documents").json()['items'][0]
    record = a.get(f"{BASE}/imports/{imports[0]['id']}/records", params={'status': 'VALID'}).json()['items'][0]
    payments = {d['processor_item_id']: d for d in a.get(f'{BASE}/ledgers/{ledger}/documents',
                                                        params={'type': 'PAYMENT', 'status': 'RECEIVED'}).json()['items']}
    items = [{'document_id': payments[i['item_ref']]['id'], 'kind': 'PAYMENT', 'amount': i['amount']} for i in statement['items']]
    body = {'ledger_id': str(ledger), 'target_adapter': 'NATIVE', 'rationale': 'SYNTHETIC: drafted from discovered records',
            'operations': [{'type': 'RecordSettlement', 'schema_version': 1, 'processor_ref': statement['processor_ref'],
                            'payout_id': statement['payout_id'], 'payout_date': statement['payout_date'], 'items': items,
                            'gross': statement['gross'], 'fee': statement['fee'], 'net': statement['net'],
                            'statement_document_id': statement['id']}],
            'evidence_refs': [{'kind': e['kind'], 'id': e['id']} for e in
                              (statement['evidence_ref'], record['evidence_ref'], payments['PMT-2001']['evidence_ref'])]}
    r = create(a, body)
    assert r.status_code == 201, r.text
    proposal = r.json()
    assert proposal['state'] == 'DRAFT' and proposal['revision'] == 1
    assert {(e['kind'], e['id']) for e in proposal['evidence_refs']} == {
        ('STAGED_DOCUMENT', str(world['s1'])), ('SOURCE_RECORD', str(world['r1'])), ('DOCUMENT_VERSION', world['p1_version'])}
    assert fingerprint(database) == before and counters(database) == ctrs


def test_gap_SG7_the_existing_draft_api_still_accepts_a_settled_payment_as_an_item(database, api, world, seed):
    """Records current behaviour for the unresolved gap SG-7 (not a decision): the reads show SETTLED; the draft API does not refuse it."""
    cfg = world['l2']
    seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], {
        'processor_ref': 'processor-1', 'payout_id': 'STL-9001', 'payout_date': '2026-10-14', 'currency': 'INR', 'gross': '50000.00',
        'fee': '1000.00', 'net': '49000.00', 'items': [{'document_id': str(world['q1']), 'kind': 'PAYMENT', 'amount': '50000.00'}],
        'description': 'SYNTHETIC typed processor settlement fixture'})
    a = signed_in(api, world, 'analyst')
    assert a.get(f"{BASE}/documents/{world['q1']}").json()['status'] == 'SETTLED'
    body = {'ledger_id': str(cfg['ledger']), 'target_adapter': 'NATIVE', 'evidence_refs': [], 'rationale': 'SYNTHETIC SG-7 probe',
            'operations': [{'type': 'RecordSettlement', 'schema_version': 1, 'processor_ref': 'processor-1', 'payout_id': 'STL-9001',
                            'payout_date': '2026-10-14', 'items': [{'document_id': str(world['q1']), 'kind': 'PAYMENT', 'amount': m('50000.00')}],
                            'gross': m('50000.00'), 'fee': m('1000.00'), 'net': m('49000.00'), 'statement_document_id': str(world['s2'])}]}
    assert create(a, body).status_code == 201
