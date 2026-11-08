"""Detection 1.1.0 (0033): PAYMENT_CLAIMED_BY_OTHER_PAYOUT and PAYOUT_DATE_DIFFERS. PROPOSED DET-9 / DET-10 (docs/AI_DETECTION.md §8).

Expected outcomes are stated from the contract, not read from the implementation:
- Identity is (processor_ref, processor_item_id) in the statement's processor namespace (Stage 2A §19.3). Equal amounts, another
  processor, another ledger or another tenant never make two claims "the same payment".
- Ingestion keeps one staged statement per payout identity: the same content again is a DUPLICATE batch, different content is a
  REJECTED conflict that makes the statement AMBIGUOUS (store.py). So an overlap is only ever between two DIFFERENT payouts.
- Overlap needs both payouts unrecorded and the payment RECEIVED/unsettled; a recorded other payout is SETTLED_ELSEWHERE (1.0.0)
  or an excluded claim. Ambiguous or unsupported other statements never raise the finding on their own.
- Payout dates: StagedDocument.payout_date and RecordSettlement.payout_date have the same meaning (SOURCE_READS.md); calendar
  dates as recorded, no timezone/business-day/cutoff rule; ingestion time and posting dates are never compared.
- Detection and disposition write no financial, approval or execution record; old findings and reviewer history are kept;
  changed inputs make a finding STALE; a rerun on changed inputs creates a NEW finding (no carried confirmation). All SYNTHETIC.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from tools.dev_db import connection, migrate
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from ledgervigil.detection import detector, routes as detection_routes
from ledgervigil.ingestion import statement_format as fmt
from ledgervigil.ingestion.store import ingest_statement
from ledgervigil.ledger.seed import seed_processor_payment, seed_processor_settlement
from test_proposal_drafts import world, seed, signed_in  # noqa: F401
from test_posting import provision
from test_account_lock_order import blocked
from detection_support import BASE, database, dapi, run, finding, dispose, rows, protected  # noqa: F401

OVL, DATE, NR, SE, UR = 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'PAYOUT_DATE_DIFFERS', 'SETTLEMENT_NOT_RECORDED', 'STATEMENT_ITEM_SETTLED_ELSEWHERE', 'STATEMENT_ITEM_UNRESOLVED'


# ------------------------------------------------------------------------------------------------------------- helpers
def ledger(database, tenant):
    with connection('lv_app', database, autocommit=True) as c:
        cfg = provision(c, tenant)
    cfg['currency'] = 'INR'; return cfg


def pay(database, cfg, item, amount, processor='processor-1'):
    with connection('lv_seed', database, autocommit=True) as s:
        return seed_processor_payment(s, cfg['tenant'], cfg['ledger'], {
            'processor_ref': processor, 'processor_item_id': item, 'party_id': cfg['party'], 'currency': 'INR', 'amount': amount,
            'received_date': '2026-10-10', 'description': 'SYNTHETIC payment (detection 1.1.0 tests)'})['document_id']


def cents(a):
    w, _, f = a.partition('.'); return int(w) * 100 + int((f + '00')[:2])


def money(c):
    return f'{c // 100}.{c % 100:02d}'


def raw(cfg, payout, items, fee='0.00', date='2026-10-14', processor='processor-1', indent=None):
    gross = sum(cents(a) * (1 if k == 'PAYMENT' else -1) for _, k, a in items)
    body = {'format': fmt.FORMAT, 'version': fmt.VERSION, 'data_classification': 'SYNTHETIC', 'statement': {
        'tenant': str(cfg['tenant']), 'ledger': str(cfg['ledger']), 'processor': processor, 'payout': payout, 'date': date, 'currency': 'INR',
        'items': [{'id': i, 'kind': k, 'amount': a} for i, k, a in items], 'gross': money(gross), 'fee': fee, 'net': money(gross - cents(fee)),
        'memo': 'SYNTHETIC statement (detection 1.1.0 tests)'}}
    return json.dumps(body, indent=indent).encode()


def stage(database, cfg, payout, items, **kw):
    with connection('lv_ingest', database, autocommit=True) as c:
        return ingest_statement(c, cfg['tenant'], cfg['ledger'], raw(cfg, payout, items, **kw), declared_format=fmt.FORMAT, declared_version=fmt.VERSION)


def settle_args(cfg, payout, docs, fee='0.00', date='2026-10-14'):
    gross = sum(cents(a) for _, a in docs)
    return cfg['tenant'], cfg['ledger'], {'processor_ref': 'processor-1', 'payout_id': payout, 'payout_date': date, 'currency': 'INR',
                                          'gross': money(gross), 'fee': fee, 'net': money(gross - cents(fee)),
                                          'items': [{'document_id': str(d), 'kind': 'PAYMENT', 'amount': a} for d, a in docs],
                                          'description': 'SYNTHETIC settlement (detection 1.1.0 tests)'}


def settle(database, cfg, payout, docs, **kw):
    with connection('lv_seed', database, autocommit=True) as s:
        return seed_processor_settlement(s, *settle_args(cfg, payout, docs, **kw))


def detect(c, cfg, *staged):
    return run(c, cfg['ledger'], {'staged_document_ids': [str(s['staged_document_id']) for s in staged]} if staged else {})


def kinds(body, st):
    return sorted(f['kind'] for f in body['findings'] if f['staged_document_id'] == str(st['staged_document_id']))


def get(c, body, st, kind):
    fid = next(f['id'] for f in body['findings'] if f['staged_document_id'] == str(st['staged_document_id']) and f['kind'] == kind)
    r = finding(c, fid); assert r.status_code == 200, r.text
    return r.json()


def dead(database):
    with connection(database=database, autocommit=True) as m:
        return m.execute('SELECT deadlocks FROM pg_stat_database WHERE datname = %s', (database,)).fetchone()[0]


# ===================================================================================== 1. overlapping payment claims
def test_two_unrecorded_payouts_claiming_one_payment_cite_each_other(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst')
    p1 = pay(database, cfg, 'OV-P1', '100.00'); pay(database, cfg, 'OV-P2', '25.50'); pay(database, cfg, 'OV-P3', '7.25')
    sa = stage(database, cfg, 'OV-PA', [('OV-P1', 'PAYMENT', '100.00'), ('OV-P2', 'PAYMENT', '25.50')])
    sb = stage(database, cfg, 'OV-PB', [('OV-P1', 'PAYMENT', '100.00'), ('OV-P3', 'PAYMENT', '7.25')])
    before = protected(database)
    r = detect(a, cfg, sa, sb); assert r.status_code == 201, r.text; body = r.json()
    assert protected(database) == before
    assert body['detector_version'] == '1.1.0' and kinds(body, sa) == [OVL, NR] and kinds(body, sb) == [OVL, NR]
    fa = get(a, body, sa, OVL)
    assert fa['class'] == 'POTENTIAL_CONFLICT' and fa['verified'] is False and fa['interpretation']['status'] == 'NOT_AVAILABLE'
    [claim] = fa['computed']['claims']
    assert (claim['item_ref'], claim['payment_document_id'], claim['payment_amount'], claim['statement_amount']) == ('OV-P1', str(p1), '100.00', '100.00')
    [other] = claim['other_claims']
    assert other['staged_document_id'] == str(sb['staged_document_id']) and other['payout'] == ['processor-1', 'OV-PB'] and other['line_no'] == 1
    assert claim['excluded_claims'] == [] and 'not a conclusion' in fa['computed']['basis']
    cited = [e for e in fa['evidence'] if e['type'] == 'OTHER_PAYOUT_CLAIM']
    assert [(e['staged_document_id'], e['payout_id'], e['item_ref']) for e in cited] == [(str(sb['staged_document_id']), 'OV-PB', 'OV-P1')]
    fb = get(a, body, sb, OVL)
    assert fb['computed']['claims'][0]['other_claims'][0]['staged_document_id'] == str(sa['staged_document_id'])
    # The 1.0.0 rules are unchanged: each payout is still unrecorded with its own RECEIVED total.
    assert get(a, body, sa, NR)['computed']['received_total'] == '125.50' and get(a, body, sb, NR)['computed']['received_total'] == '107.25'


def test_repeated_import_and_duplicate_evidence_are_not_overlaps(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); pay(database, cfg, 'RP-P1', '40.00')
    s1 = stage(database, cfg, 'RP-PA', [('RP-P1', 'PAYMENT', '40.00')])
    again = stage(database, cfg, 'RP-PA', [('RP-P1', 'PAYMENT', '40.00')], indent=2)          # same payout, same content, other bytes
    assert again['status'] == 'DUPLICATE'
    body = detect(a, cfg).json()
    assert body['scope'] == [str(s1['staged_document_id'])] and kinds(body, s1) == [NR]      # one statement; nothing to overlap with
    conflict = stage(database, cfg, 'RP-PA', [('RP-P1', 'PAYMENT', '40.00')], fee='1.00')    # same payout, different content
    assert conflict['status'] == 'REJECTED' and conflict['failure_code'] == 'PAYOUT_IDENTITY_CONFLICT'
    body = detect(a, cfg, s1).json()
    assert body['abstentions'] == [{'staged_document_id': str(s1['staged_document_id']), 'code': 'AMBIGUOUS_STATEMENT'}] and body['findings'] == []


def test_identity_controls_processor_ledger_tenant_amount_and_unresolved(database, dapi, world):
    a = signed_in(dapi, world, 'analyst'); b = signed_in(dapi, world, 'analystB')
    cfg = ledger(database, world['A']); other_ledger = ledger(database, world['A']); tenant_b = ledger(database, world['B'])
    pay(database, cfg, 'ID-P1', '10.00'); pay(database, cfg, 'ID-P2', '10.00'); pay(database, cfg, 'ID-NS', '10.00', processor='processor-2')
    pay(database, other_ledger, 'ID-P1', '10.00'); pay(database, tenant_b, 'ID-P1', '10.00')
    sa = stage(database, cfg, 'ID-PA', [('ID-P1', 'PAYMENT', '10.00'), ('ID-NS', 'PAYMENT', '10.00'), ('ID-GHOST', 'PAYMENT', '3.00')])
    sb = stage(database, cfg, 'ID-PB', [('ID-P2', 'PAYMENT', '10.00'), ('ID-GHOST', 'PAYMENT', '3.00')])          # equal amounts, other ids
    sc = stage(database, cfg, 'ID-PC', [('ID-NS', 'PAYMENT', '10.00')], processor='processor-2')                 # same id, other processor
    so = stage(database, other_ledger, 'ID-PO', [('ID-P1', 'PAYMENT', '10.00')])                               # same id, other ledger
    st = stage(database, tenant_b, 'ID-PT', [('ID-P1', 'PAYMENT', '10.00')])                                   # same id, other tenant
    body = detect(a, cfg, sa, sb, sc).json()
    assert OVL not in kinds(body, sa) + kinds(body, sb) + kinds(body, sc), body['findings']
    assert UR in kinds(body, sa) and UR in kinds(body, sb)        # ID-GHOST is listed twice but never identified: an evidence gap only
    assert OVL not in kinds(detect(a, other_ledger, so).json(), so) and OVL not in kinds(detect(b, tenant_b, st).json(), st)


def test_excluded_claims_never_raise_the_finding_alone(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst')
    p = {i: pay(database, cfg, f'EX-P{i}', '20.00') for i in range(5)}
    target = stage(database, cfg, 'EX-T', [(f'EX-P{i}', 'PAYMENT', '20.00') for i in range(4)])
    amb = stage(database, cfg, 'EX-AMB', [('EX-P0', 'PAYMENT', '20.00')]); stage(database, cfg, 'EX-AMB', [('EX-P0', 'PAYMENT', '20.00')], fee='1.00')
    stage(database, cfg, 'EX-REF', [('EX-P1', 'PAYMENT', '20.00'), ('EX-RF', 'REFUND', '0.50')])
    stage(database, cfg, 'EX-REC', [('EX-P2', 'PAYMENT', '20.00'), ('EX-P4', 'PAYMENT', '20.00')])
    settle(database, cfg, 'EX-REC', [(p[4], '20.00')])                    # recorded other payout, without EX-P2
    good = stage(database, cfg, 'EX-OK', [('EX-P3', 'PAYMENT', '20.00')])
    body = detect(a, cfg, target).json(); f = get(a, body, target, OVL)
    [claim] = f['computed']['claims']                                     # only EX-P3 has a valid other claim
    assert claim['item_ref'] == 'EX-P3' and claim['other_claims'][0]['staged_document_id'] == str(good['staged_document_id'])
    assert amb['status'] == 'STAGED'
    # The excluded claims on EX-P0..EX-P2 are not listed under EX-P3, and do not raise their own claim entries.
    assert all(c['item_ref'] == 'EX-P3' for c in f['computed']['claims'])
    solo = ledger(database, world['A']); q = pay(database, solo, 'EX-Q', '5.00')
    t2 = stage(database, solo, 'EX-T2', [('EX-Q', 'PAYMENT', '5.00')]); stage(database, solo, 'EX-A2', [('EX-Q', 'PAYMENT', '5.00')])
    stage(database, solo, 'EX-A2', [('EX-Q', 'PAYMENT', '5.00')], fee='0.01')
    assert OVL not in kinds(detect(a, solo, t2).json(), t2)             # its only other claim is ambiguous: no overlap finding
    assert q


def test_settled_other_payout_is_settled_elsewhere_not_overlap(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst')
    p1 = pay(database, cfg, 'SE-P1', '60.00')
    sa = stage(database, cfg, 'SE-PA', [('SE-P1', 'PAYMENT', '60.00')]); sb = stage(database, cfg, 'SE-PB', [('SE-P1', 'PAYMENT', '60.00')])
    settle(database, cfg, 'SE-PA', [(p1, '60.00')])
    body = detect(a, cfg, sa, sb).json()
    assert kinds(body, sa) == [] and kinds(body, sb) == [NR, SE]


# ======================================================================================== 2. payout-date discrepancy
@pytest.mark.parametrize('stated, recorded, days', [('2026-10-14', '2026-10-15', -1), ('2026-10-31', '2026-10-30', 1), ('2026-10-02', '2026-10-30', -28)])   # recorded dates must fall in an OPEN period
def test_payout_date_difference_is_reported_with_exact_calendar_days(database, dapi, world, stated, recorded, days):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); p = pay(database, cfg, 'DT-P1', '80.00')
    settle(database, cfg, 'DT-PO', [(p, '80.00')], fee='1.00', date=recorded)
    st = stage(database, cfg, 'DT-PO', [('DT-P1', 'PAYMENT', '80.00')], fee='1.00', date=stated)
    body = detect(a, cfg, st).json(); assert kinds(body, st) == [DATE]
    c = get(a, body, st, DATE)['computed']
    assert (c['statement_payout_date'], c['ledger_payout_date'], c['calendar_days_statement_minus_ledger']) == (stated, recorded, days)
    assert 'no timezone, business-day or cutoff rule' in c['date_semantics']


def test_same_date_no_finding_and_date_with_amount_difference_reports_both(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst')
    p, q = pay(database, cfg, 'DS-P1', '30.00'), pay(database, cfg, 'DS-P2', '30.00')
    settle(database, cfg, 'DS-SAME', [(p, '30.00')]); same = stage(database, cfg, 'DS-SAME', [('DS-P1', 'PAYMENT', '30.00')])
    settle(database, cfg, 'DS-BOTH', [(q, '30.00')], fee='0.10', date='2026-10-13')
    both = stage(database, cfg, 'DS-BOTH', [('DS-P2', 'PAYMENT', '30.00')], fee='0.20')
    body = detect(a, cfg, same, both).json()
    assert kinds(body, same) == [] and kinds(body, both) == [DATE, 'SETTLEMENT_AMOUNTS_DIFFER']


def test_missing_statement_date_is_rejected_at_ingestion_so_nothing_is_compared(database, world):
    cfg = ledger(database, world['A'])
    body = json.loads(raw(cfg, 'DM-PO', [('DM-P1', 'PAYMENT', '1.00')])); del body['statement']['date']
    with connection('lv_ingest', database, autocommit=True) as c:
        r = ingest_statement(c, cfg['tenant'], cfg['ledger'], json.dumps(body).encode(), declared_format=fmt.FORMAT, declared_version=fmt.VERSION)
    assert r['status'] == 'REJECTED' and r.get('staged_document_id') is None


# ================================================================================== 3. staleness, history, versions
def test_overlap_finding_goes_stale_history_kept_and_confirmation_not_carried(database, dapi, world):
    cfg = ledger(database, world['A']); a, c = signed_in(dapi, world, 'analyst'), signed_in(dapi, world, 'controller')
    p1 = pay(database, cfg, 'ST-P1', '50.00')
    sa = stage(database, cfg, 'ST-PA', [('ST-P1', 'PAYMENT', '50.00')]); stage(database, cfg, 'ST-PB', [('ST-P1', 'PAYMENT', '50.00')])
    body = detect(a, cfg, sa).json(); f = get(a, body, sa, OVL)
    before = protected(database)
    r = dispose(c, f['id'], 'CONFIRMED', 'VERIFIED_AGAINST_EVIDENCE', '"v1"'); assert r.status_code == 200, r.text
    assert protected(database) == before
    settle(database, cfg, 'ST-PB', [(p1, '50.00')])                       # the other payout is recorded with the payment
    stale = finding(a, f['id']).json(); assert stale['freshness']['status'] == 'STALE' and stale['review']['status'] == 'CONFIRMED'
    r = dispose(c, f['id'], 'REOPENED', 'REVIEW_CORRECTION', '"v2"'); assert r.status_code == 200
    r = dispose(c, f['id'], 'CONFIRMED', 'VERIFIED_AGAINST_EVIDENCE', '"v3"'); assert r.status_code == 409 and r.json()['code'] == 'FINDING_STALE'
    body2 = detect(a, cfg, sa).json()
    assert kinds(body2, sa) == [NR, SE]                                   # now settled elsewhere, no overlap; old finding untouched
    kept = finding(a, f['id']).json(); assert [d['action'] for d in kept['dispositions']] == ['CONFIRMED', 'REOPENED']
    assert all(x['review']['status'] == 'OPEN' for x in (get(a, body2, sa, k) for k in (NR, SE)))   # nothing carried forward


def test_new_overlapping_file_makes_the_old_result_stale_and_rerun_finds_it(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); pay(database, cfg, 'NF-P1', '9.99')
    sa = stage(database, cfg, 'NF-PA', [('NF-P1', 'PAYMENT', '9.99')])
    body = detect(a, cfg, sa).json(); nr = get(a, body, sa, NR)
    stage(database, cfg, 'NF-PB', [('NF-P1', 'PAYMENT', '9.99')])
    assert finding(a, nr['id']).json()['freshness']['status'] == 'STALE'  # its v2 evidence set now includes the other claim
    body2 = detect(a, cfg, sa).json(); assert kinds(body2, sa) == [OVL, NR]
    assert get(a, body2, sa, NR)['id'] != nr['id']


def test_upgrade_keeps_1_0_0_findings_current_and_reruns_create_new_versions(database, world, monkeypatch):
    migrate(database, '0032_detection_findings', downgrade=True, disposable=True)     # fresh per-test database, no 1.1.0 runs yet
    cfg = ledger(database, world['A']); pay(database, cfg, 'UP-P1', '70.00'); sa = stage(database, cfg, 'UP-PA', [('UP-P1', 'PAYMENT', '70.00')])
    v1 = {k: v for k, v in detector.CONFIG.items() if k not in ('version', 'inputs', 'rules', 'date_comparison', 'overlap')}

    def run_v1(conn, tenant_id, ledger_id, scope):
        out = []
        for sid in scope:
            inputs, fp = conn.execute('SELECT i, lv.detection_fingerprint(i) FROM lv.detection_inputs(%s, %s, %s) i', (tenant_id, ledger_id, sid)).fetchone()
            out.append({'staged_document_id': sid, 'input_fingerprint': fp, 'integrity': 'VERIFIED', **detector.detect(inputs)})
        return out
    old = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database), detection=True)
    from legacy_issuer_support import use_historical_issuer
    use_historical_issuer(old,database,'0032_detection_findings')
    try:
        with monkeypatch.context() as m:
            m.setattr(detector, 'VERSION', '1.0.0'); m.setattr(detector, 'CONFIG', v1); m.setattr(detector, 'run', run_v1)
            body = detect(signed_in(old, world, 'analyst'), cfg, sa).json()
        assert body['detector_version'] == '1.0.0'; fid = body['findings'][0]['id']
    finally:
        old.state.pool.close()
    migrate(database)
    new = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database), detection=True)
    try:
        a = signed_in(new, world, 'analyst'); f = finding(a, fid).json()
        assert f['detector_version'] == '1.0.0' and f['freshness']['status'] == 'CURRENT'     # the upgrade alone makes nothing stale
        assert dispose(signed_in(new, world, 'controller'), fid, 'CONFIRMED', 'VERIFIED_AGAINST_EVIDENCE', '"v1"').status_code == 200
        body2 = detect(a, cfg, sa).json(); f2 = get(a, body2, sa, NR)
        assert f2['id'] != fid and f2['detector_version'] == '1.1.0' and f2['review']['status'] == 'OPEN'
        assert finding(a, fid).json()['review']['status'] == 'CONFIRMED'
    finally:
        new.state.pool.close()


# ================================================================================ 4. retries, concurrency, refusals
def test_idempotent_and_concurrent_runs_deduplicate_overlap_findings(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); pay(database, cfg, 'CC-P1', '11.00')
    sa = stage(database, cfg, 'CC-PA', [('CC-P1', 'PAYMENT', '11.00')]); sb = stage(database, cfg, 'CC-PB', [('CC-P1', 'PAYMENT', '11.00')])
    key = uuid4().hex
    first = run(a, cfg['ledger'], {}, key=key); replay = run(a, cfg['ledger'], {}, key=key)
    assert first.status_code == 201 and replay.status_code == 201 and replay.json() == first.json()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: detect(a, cfg, sa, sb), range(4)))
    assert all(r.status_code == 201 for r in results) and all(r.json()['new_finding_count'] == 0 for r in results)
    with connection(database=database) as db:
        assert db.execute("SELECT count(*) FROM lv.detection_finding WHERE ledger_id = %s AND kind = %s", (cfg['ledger'], OVL)).fetchone()[0] == 2


def test_refusals_permissions_and_tenants(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); pay(database, cfg, 'RF-P1', '3.00')
    sa = stage(database, cfg, 'RF-PA', [('RF-P1', 'PAYMENT', '3.00')]); stage(database, cfg, 'RF-PB', [('RF-P1', 'PAYMENT', '3.00')])
    f = get(a, detect(a, cfg, sa).json(), sa, OVL); before = protected(database)
    other, admin = signed_in(dapi, world, 'analystB'), signed_in(dapi, world, 'admin')
    assert finding(other, f['id']).status_code == 404
    assert dispose(other, f['id'], 'DISMISSED', 'NOT_A_DISCREPANCY', '"v1"').status_code == 404
    assert detect(other, cfg, sa).status_code == 404
    assert detect(admin, cfg, sa).status_code == 403 and finding(admin, f['id']).status_code == 200
    assert protected(database) == before


@pytest.mark.parametrize('first', ['detection', 'settlement'])
def test_concurrent_settlement_posting_in_both_lock_orders(database, dapi, world, monkeypatch, first):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); p1 = pay(database, cfg, 'LK-P1', '45.00')
    sa = stage(database, cfg, 'LK-PA', [('LK-P1', 'PAYMENT', '45.00')]); stage(database, cfg, 'LK-PB', [('LK-P1', 'PAYMENT', '45.00')])
    d0 = dead(database); held, release = threading.Event(), threading.Event()
    seed_c = connection('lv_seed', database); pid = seed_c.info.backend_pid
    if first == 'detection':
        def hold(results):
            held.set(); assert release.wait(20); return results
        monkeypatch.setattr(detection_routes, '_before_record', hold)
        with ThreadPoolExecutor(2) as pool:
            fut = pool.submit(detect, a, cfg, sa); assert held.wait(20)
            post = pool.submit(lambda: (seed_processor_settlement(seed_c, *settle_args(cfg, 'LK-PB', [(p1, '45.00')])), seed_c.commit()))
            blocked(database, pid); release.set()
            r = fut.result(timeout=30); post.result(timeout=30)
        assert r.status_code == 201 and kinds(r.json(), sa) == [OVL, NR]          # snapshot before the posting
        assert finding(a, get(a, r.json(), sa, OVL)['id']).json()['freshness']['status'] == 'STALE'
    else:
        tenant, ledger_id, request = settle_args(cfg, 'LK-PB', [(p1, '45.00')])      # the seed writer's own SQL, left uncommitted:
        seed_c.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))   # it holds the ledger and counter locks
        from psycopg.types.json import Jsonb
        seed_c.execute('SELECT lv.seed_processor_settlement(%s, %s)', (ledger_id, Jsonb(request)))
        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(detect, a, cfg, sa)
            end = time.monotonic() + 10
            while time.monotonic() < end:
                with connection(database=database) as m:
                    if m.execute("SELECT 1 FROM pg_stat_activity WHERE datname = %s AND query LIKE 'SELECT lv.detection_begin%%' AND wait_event_type = 'Lock'",
                                 (database,)).fetchone():
                        break
                time.sleep(.02)
            else:
                raise AssertionError('detection did not wait for the uncommitted posting')
            seed_c.commit(); r = fut.result(timeout=30)
        assert r.status_code == 201 and kinds(r.json(), sa) == [NR, SE]           # snapshot after the posting
    seed_c.close()
    assert dead(database) == d0


def test_statement_ingested_during_a_run_rejects_that_run_and_a_rerun_sees_it(database, dapi, world, monkeypatch):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); pay(database, cfg, 'IR-P1', '2.00')
    sa = stage(database, cfg, 'IR-PA', [('IR-P1', 'PAYMENT', '2.00')]); held, release = threading.Event(), threading.Event()

    def hold(results):
        held.set(); assert release.wait(20); return results
    monkeypatch.setattr(detection_routes, '_before_record', hold)
    with ThreadPoolExecutor(1) as pool:
        fut = pool.submit(detect, a, cfg, sa); assert held.wait(20)
        stage(database, cfg, 'IR-PB', [('IR-P1', 'PAYMENT', '2.00')])           # ingestion does not wait for detection
        release.set(); r = fut.result(timeout=30)
    assert r.status_code == 502 and r.json()['code'] == 'DETECTOR_OUTPUT_REJECTED'   # evidence changed under the run: nothing recorded
    assert rows(database, 'lv.detection_finding', world['A']) == 0
    monkeypatch.setattr(detection_routes, '_before_record', None)
    assert kinds(detect(a, cfg, sa).json(), sa) == [OVL, NR]


def test_missing_audit_rolls_back_an_overlap_run(database, dapi, world):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); pay(database, cfg, 'AU-P1', '4.00')
    sa = stage(database, cfg, 'AU-PA', [('AU-P1', 'PAYMENT', '4.00')]); stage(database, cfg, 'AU-PB', [('AU-P1', 'PAYMENT', '4.00')])
    sig = 'lv.detection_record(bytea,uuid,text,text,text,text,jsonb,jsonb,text,text)'
    with connection(database=database) as db:
        original = db.execute('SELECT pg_get_functiondef(%s::regprocedure)', (sig,)).fetchone()[0]
    start = original.index(" PERFORM lv_identity.audit_append('DETECTION_RUN_COMPLETED'"); end = original.index('));', start) + 3
    with connection(database=database) as db:
        db.execute(original[:start] + original[end:])
    try:
        before = protected(database)
        r = detect(a, cfg, sa); assert r.status_code == 500 and r.json()['code'] == 'INTERNAL'
        assert rows(database, 'lv.detection_run', world['A']) == 0 and rows(database, 'lv.detection_finding', world['A']) == 0
        assert protected(database) == before
    finally:
        with connection(database=database) as db:
            db.execute(original)


@pytest.mark.parametrize('tamper', ['drop_overlap', 'fabricate_overlap', 'wrong_days'])
def test_detector_output_that_disagrees_with_the_database_oracle_is_rejected(database, dapi, world, monkeypatch, tamper):
    cfg = ledger(database, world['A']); a = signed_in(dapi, world, 'analyst'); p = pay(database, cfg, 'TM-P1', '6.00')
    settle(database, cfg, 'TM-PD', [(p, '6.00')], date='2026-10-01'); sd = stage(database, cfg, 'TM-PD', [('TM-P1', 'PAYMENT', '6.00')])
    pay(database, cfg, 'TM-P2', '6.00'); s1 = stage(database, cfg, 'TM-P1X', [('TM-P2', 'PAYMENT', '6.00')]); stage(database, cfg, 'TM-P2X', [('TM-P2', 'PAYMENT', '6.00')])

    def edit(results):
        for r in results:
            fs = r['findings']
            if tamper == 'drop_overlap':
                r['findings'] = [f for f in fs if f['kind'] != OVL]
            elif tamper == 'fabricate_overlap' and not any(f['kind'] == OVL for f in fs) and fs:
                r['findings'] = sorted(fs + [{'kind': OVL, 'class': 'POTENTIAL_CONFLICT', 'computed': {'claims': []}}], key=lambda f: f['kind'])
            elif tamper == 'wrong_days':
                for f in fs:
                    if f['kind'] == DATE:
                        f['computed'] = {**f['computed'], 'calendar_days_statement_minus_ledger': 0}
        return results
    monkeypatch.setattr(detection_routes, '_before_record', edit)
    before = protected(database)
    r = detect(a, cfg, sd, s1); assert r.status_code == 502 and r.json()['code'] == 'DETECTOR_OUTPUT_REJECTED', r.text
    assert rows(database, 'lv.detection_finding', world['A']) == 0 and protected(database) == before
