"""Reproducible demonstration: typed Payment → statement match → settlement preview → synthetic settlement commit →
duplicate refusal → unchanged result on identical retry. Synthetic SANDBOX data only; never touches BOOKS or a provider.

Scenario and independently stated expectations: fixtures/settlements/typed-settlement-v1.json (which reuses the ledger,
opening journal, payment and statement of fixtures/payments/typed-payment-matching-v1.json).

Steps and logins:
1. Setup (owner + lv_seed): a NEW tenant and SANDBOX ledger; a labelled invoice-like opening MANUAL JOURNAL (not an Invoice);
   the typed Payment processor-1/PMT-2001 of INR 50,000.00.
2. Statement (lv_ingest): STL-3001 (gross 50,000.00, fee 1,000.00, net 49,000.00) is staged as an observation.
3. Preview (lv_app, read-only): identity/currency/amount match, duplicate eligibility from authoritative state, predicted
   postings and balances. Every lv table is hashed before and after: the preview writes nothing.
4. Commit (lv_seed): lv.seed_processor_settlement records the settlement, its inclusion, the Payment's SETTLED version 2
   and the derived postings. This is developer fixture seeding, not an approved execution.
5. Duplicate refusal: preview of the same payout and of another payout reusing the payment (lv_app); inspection of the
   duplicate candidate; seeding the payment under another payout and a changed body for the same payout (lv_seed).
6. Identical retry (lv_seed): returns the stored result; every lv table is unchanged.

Writes a new evidence directory and refuses to overwrite one. Accounting semantics remain UNREVIEWED.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid7
import argparse
import hashlib
import json

from tools.dev_db import ROOT, connection
from tools.payment_matching_demo import LV_TABLES as PAYMENT_DEMO_TABLES, attempt, new_ledger, opening, payment_body, statement_file
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ingestion import statement_format as fmt
from ledgervigil.ingestion.store import ingest_statement
from ledgervigil.kernel_adapter import AdapterError, DuplicateSettlementRefused, inspect_settlement, preview_settlement
from ledgervigil.ledger.replay import replay_events
from ledgervigil.ledger.seed import seed_processor_payment, seed_processor_settlement
from psycopg import sql

FIXTURE = ROOT / 'fixtures/settlements/typed-settlement-v1.json'
LV_TABLES = PAYMENT_DEMO_TABLES + ('processor_settlement_identity', 'settlement_current', 'settlement_inclusion', 'settlement_inclusion_current')


def fingerprint():
    with connection() as admin:
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(t))).fetchall()))).encode()).hexdigest()
                for t in LV_TABLES}


def payment_facts(cfg, doc):
    with connection('lv_app', autocommit=True) as app, tenant_transaction(app, cfg['tenant']):
        versions = app.execute('SELECT version, previous_version, seq, ordinal, id::text, financial_fingerprint, financial_fields, origin::text, provenance::text '
                               'FROM lv.document_version WHERE document_id = %s ORDER BY version', (doc,)).fetchall()
        current = app.execute('SELECT version FROM lv.document_current WHERE document_id = %s', (doc,)).fetchone()[0]
        projection = app.execute('SELECT version, status, settlement_document_id::text, amount::text, last_seq FROM lv.payment_current WHERE document_id = %s', (doc,)).fetchone()
        entries = app.execute('SELECT id::text, seq, source_document_version FROM lv.journal_entry WHERE source_document_id = %s', (doc,)).fetchall()
    return {'versions': [dict(zip(('version', 'previous_version', 'seq', 'ordinal', 'version_id', 'financial_fingerprint', 'financial_fields', 'origin', 'provenance'), v))
                         for v in versions],
            'current_version': current,
            'payment_current': dict(zip(('version', 'status', 'settlement_document_id', 'amount', 'last_seq'), projection)),
            'posting_entries': [dict(zip(('entry_id', 'seq', 'source_document_version'), e)) for e in entries]}


def settlement_facts(cfg, result):
    role = {v: k for k, v in cfg['chart'].items()}
    with connection('lv_app', autocommit=True) as app, tenant_transaction(app, cfg['tenant']):
        doc = app.execute('SELECT d.doc_type::text, d.source_ref, v.version, v.id::text, v.financial_fingerprint, v.financial_fields, v.origin::text, v.provenance::text '
                          'FROM lv.document d JOIN lv.document_version v ON (v.tenant_id, v.ledger_id, v.document_id) = (d.tenant_id, d.ledger_id, d.id) WHERE d.id = %s',
                          (result['document_id'],)).fetchone()
        identity = app.execute('SELECT processor_ref, payout_id, seq FROM lv.processor_settlement_identity WHERE document_id = %s', (result['document_id'],)).fetchone()
        current = app.execute('SELECT version, gross::text, fee::text, net::text, item_count, status, last_seq FROM lv.settlement_current WHERE document_id = %s',
                              (result['document_id'],)).fetchone()
        inclusions = app.execute('SELECT line_no, payment_document_id::text, payment_version_before, payment_version_after, kind, amount::text, seq '
                                 'FROM lv.settlement_inclusion WHERE settlement_document_id = %s ORDER BY line_no', (result['document_id'],)).fetchall()
        active = app.execute('SELECT item_document_id::text, settlement_version, last_seq FROM lv.settlement_inclusion_current WHERE settlement_document_id = %s',
                             (result['document_id'],)).fetchall()
        lines = app.execute('SELECT line_no, account_id::text, side::text, amount FROM lv.journal_line WHERE entry_id = %s ORDER BY line_no', (result['entry_id'],)).fetchall()
        balances = app.execute('SELECT account_id::text, sum(debit_total - credit_total) FROM lv.account_balance_current WHERE ledger_id = %s GROUP BY 1',
                               (cfg['ledger'],)).fetchall()
        chain = [dict(zip(('seq', 'canonical_content', 'previous_hash', 'hash'), r)) for r in app.execute(
            'SELECT seq, canonical_content, previous_hash, hash FROM lv.ledger_event WHERE ledger_id = %s ORDER BY seq', (cfg['ledger'],))]
    by_role = {role[a]: b for a, b in balances}
    return {'document': dict(zip(('doc_type', 'source_ref', 'version', 'version_id', 'financial_fingerprint', 'financial_fields', 'origin', 'provenance'), doc)),
            'payout_identity': dict(zip(('processor_ref', 'payout_id', 'seq'), identity)),
            'settlement_current': dict(zip(('version', 'gross', 'fee', 'net', 'item_count', 'status', 'last_seq'), current)),
            'inclusions': [dict(zip(('line_no', 'payment_document_id', 'payment_version_before', 'payment_version_after', 'kind', 'amount', 'seq'), i)) for i in inclusions],
            'active_inclusions': [dict(zip(('item_document_id', 'settlement_version', 'last_seq'), i)) for i in active],
            'postings': [[n, role[a], s, f'{m:.2f}'] for n, a, s, m in lines],
            'balances_by_role': {r: f'{by_role.get(r, 0):.2f}' for r in sorted(role.values())},
            'event_chain': {'seqs': [e['seq'] for e in chain], 'replay': {k: str(v) for k, v in replay_events(chain).items() if k != 'balances'}}}


def refusal(fn):
    try:
        return {'unexpectedly_returned': fn()}
    except DuplicateSettlementRefused as error:
        return {'refused': {'code': error.code, 'source': error.source, 'evidence': error.evidence, 'snapshot': error.snapshot}}
    except AdapterError as error:   # any other refusal is recorded, not hidden
        return {'refused': {'code': error.code, 'source': error.source, 'detail': error.detail}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/settlements' / datetime.now().strftime('demo-%Y%m%d-%H%M%S'))
    args = parser.parse_args()
    (args.output / 'inputs').mkdir(parents=True, exist_ok=False)
    fx = json.loads(FIXTURE.read_text()); exp = fx['expected']
    pfx = json.loads((ROOT / fx['payment_fixture']).read_text())
    for path in (FIXTURE, ROOT / fx['payment_fixture']):
        (args.output / 'inputs' / path.name).write_bytes(path.read_bytes())

    # 1. Typed Payment.
    tenant = uuid7(); cfg = new_ledger(tenant, pfx, True)
    with connection('lv_seed', autocommit=True) as seed:
        opening(seed, cfg, pfx)
        payment = seed_processor_payment(seed, cfg['tenant'], cfg['ledger'], payment_body(cfg, pfx['payment']))
    doc = payment['document_id']
    payment_before = payment_facts(cfg, doc)

    # 2. Statement match input.
    raw = statement_file(cfg, pfx, pfx['statements']['a-STL-3001-correct.json'])
    (args.output / 'inputs' / 'statement-STL-3001.json').write_bytes(raw)
    with connection('lv_ingest', autocommit=True) as c:
        staged = ingest_statement(c, cfg['tenant'], cfg['ledger'], raw, declared_format=fmt.FORMAT, declared_version=fmt.VERSION)

    # 3. Settlement preview (read-only).
    sreq = fx['settlement_request']
    adapter_request = {'processor': sreq['processor_ref'], 'payout': sreq['payout_id'], 'date': sreq['payout_date'], 'currency': sreq['currency'],
                       'gross': sreq['gross'], 'fee': sreq['fee'], 'net': sreq['net'], 'items': [{'document_id': doc, 'kind': 'PAYMENT'}]}
    h0 = fingerprint()
    with connection('lv_app', autocommit=True) as app:
        preview = preview_settlement(app, cfg['tenant'], cfg['ledger'], adapter_request)
    h1 = fingerprint()

    # 4. Synthetic settlement commit (developer seed authority; not an execution).
    seed_request = {k: sreq[k] for k in ('processor_ref', 'payout_id', 'payout_date', 'currency', 'gross', 'fee', 'net')}
    seed_request['items'] = [{'document_id': doc, 'kind': 'PAYMENT', 'amount': a} for a in sreq['item_amounts']]
    seed_request['description'] = sreq['description']
    (args.output / 'inputs' / 'settlement-request.json').write_text(json.dumps(seed_request, indent=2) + '\n')
    with connection('lv_seed', autocommit=True) as seed:
        committed = seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], seed_request)
    facts = settlement_facts(cfg, committed)
    payment_after = payment_facts(cfg, doc)
    h2 = fingerprint()

    # 5. Duplicate refusal: read-only previews and inspection, then seed attempts.
    with connection('lv_app', autocommit=True) as app:
        dup_same = refusal(lambda: preview_settlement(app, cfg['tenant'], cfg['ledger'], adapter_request))
        dup_other = refusal(lambda: preview_settlement(app, cfg['tenant'], cfg['ledger'], {**adapter_request, 'payout': fx['reuse_payout_id']}))
        candidate = [{'role': r, 'side': s, 'amount': a} for r, s, a in exp['settlement_postings']]
        inspection = inspect_settlement(app, cfg['tenant'], cfg['ledger'], adapter_request, candidate)
    h3 = fingerprint()
    with connection('lv_seed', autocommit=True) as seed:
        reuse = attempt(lambda: seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], {**seed_request, 'payout_id': fx['reuse_payout_id']}))
        changed = attempt(lambda: seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], {**seed_request, **fx['changed_body']}))
    h4 = fingerprint()

    # 6. Identical retry.
    with connection('lv_seed', autocommit=True) as seed:
        retry = seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], seed_request)
    h5 = fingerprint()

    verdicts = lambda r: {c['id']: c['verdict'] for c in r['evidence_checks']}  # noqa: E731
    v1, v2 = payment_after['versions']
    comparison = {
        'statement_staged': staged['status'] == 'STAGED',
        'preview_evidence_matches': verdicts(preview) == exp['preview_evidence'],
        'preview_blocked_only_by_expected_reasons': preview['outcome'] == 'BLOCKED' and sorted(preview['blocking_reasons']) == exp['preview_blocking_reasons'],
        'preview_predicted_postings': [[p['role'], p['side'], p['amount']] for p in preview['predicted_postings']] == exp['settlement_postings'],
        'preview_wrote_nothing': h0 == h1,
        'commit_is_new': committed['replayed'] is False and committed['seq'] == '3',
        'committed_postings_equal_preview': [p[1:] for p in facts['postings']] == exp['settlement_postings'],
        'committed_balances_equal_preview': facts['balances_by_role'] == exp['balances_after'] == {b['role']: b['after'] for b in preview['balances']},
        'settlement_is_typed_synthetic_seed': (facts['document']['doc_type'], facts['document']['origin'], facts['document']['provenance']) == ('SETTLEMENT', 'SEED', 'SYNTHETIC'),
        'one_inclusion_from_v1_to_v2': [(i['payment_document_id'], i['payment_version_before'], i['payment_version_after']) for i in facts['inclusions']] == [(doc, 1, 2)],
        'payment_v1_preserved': payment_before['versions'][0] == v1 and 'status' not in v1['financial_fields'],
        'payment_v2_settled_with_link': v2['financial_fields'] == {**v1['financial_fields'], 'status': 'SETTLED', 'settlement_document_id': committed['document_id'],
                                                                   'settlement_version': '1'} and v2['previous_version'] == 1,
        'payment_current_advanced': payment_after['current_version'] == 2 and payment_after['payment_current']['status'] == 'SETTLED',
        'payment_posting_unchanged': payment_after['posting_entries'] == payment_before['posting_entries'] and payment_after['posting_entries'][0]['source_document_version'] == 1,
        'event_chain_replays': facts['event_chain']['seqs'] == [1, 2, 3],
        'duplicate_same_payout_refused': dup_same.get('refused', {}).get('code') == 'DUPLICATE_SETTLEMENT' and dup_same['refused']['evidence']['verdict'] == 'FAIL',
        'duplicate_other_payout_refused': dup_other.get('refused', {}).get('code') == 'ITEM_ALREADY_SETTLED' and dup_other['refused']['evidence']['verdict'] == 'FAIL',
        'duplicate_inspection_fails': {c['id']: c['verdict'] for c in inspection['checks']}['P-DUP-01'] == 'FAIL' and verdicts(inspection)['E-DUP-01'] == 'FAIL',
        'duplicate_checks_wrote_nothing': h2 == h3,
        'seed_reuse_refused': reuse.get('refused', {}).get('sqlstate') == '23505' and 'already included' in reuse['refused']['message'],
        'seed_changed_body_refused': changed.get('refused', {}).get('sqlstate') == '23505' and 'different content' in changed['refused']['message'],
        'refusals_left_no_effects': h3 == h4,
        'identical_retry_returns_stored_result': retry == {**committed, 'replayed': True},
        'identical_retry_changed_nothing': h4 == h5,
    }
    summary = {
        'dataset_provenance': 'SYNTHETIC', 'accounting_semantics': 'UNREVIEWED', 'eligible_for_execution': False,
        'ledger': [str(cfg['tenant']), str(cfg['ledger'])],
        '1_typed_payment': {'created': payment, 'facts_before_settlement': payment_before},
        '2_statement': {'staged': staged},
        '3_settlement_preview': {'evidence': verdicts(preview), 'E-ITEM-01': next(c for c in preview['evidence_checks'] if c['id'] == 'E-ITEM-01'),
                                 'E-DUP-01': next(c for c in preview['evidence_checks'] if c['id'] == 'E-DUP-01'),
                                 'P-DUP-01': next(c for c in preview['checks'] if c['id'] == 'P-DUP-01'),
                                 'predicted_postings': preview['predicted_postings'], 'balances': preview['balances'],
                                 'outcome': preview['outcome'], 'blocking_reasons': preview['blocking_reasons'], 'snapshot': preview['snapshot']},
        '4_settlement_commit': {'result': committed, 'facts': facts, 'payment_after': payment_after},
        '5_duplicate_refusal': {'preview_same_payout': dup_same, 'preview_reuse_under_other_payout': dup_other,
                                'inspection': {'checks': {c['id']: c['verdict'] for c in inspection['checks']}, 'evidence': verdicts(inspection),
                                               'outcome': inspection['outcome'], 'blocking_reasons': inspection['blocking_reasons']},
                                'seed_reuse_under_other_payout': reuse, 'seed_changed_body_same_payout': changed},
        '6_identical_retry': {'result': retry},
        'table_hash_checkpoints': {'tables': len(h0), 'before_preview==after_preview': h0 == h1, 'after_commit==after_duplicate_checks': h2 == h3,
                                   'after_checks==after_refused_seeds': h3 == h4, 'after_refused_seeds==after_identical_retry': h4 == h5},
        'comparison': comparison,
    }
    outputs = {'summary.json': summary, 'adapter-preview.json': preview, 'adapter-inspection-after-commit.json': inspection,
               'table-hashes.json': {'h0_before_preview': h0, 'h1_after_preview': h1, 'h2_after_commit': h2, 'h3_after_duplicate_checks': h3,
                                     'h4_after_refused_seeds': h4, 'h5_after_identical_retry': h5}}
    for name, value in outputs.items():
        (args.output / name).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')
    print(json.dumps({'evidence': str(args.output), 'comparison': comparison}, indent=2))
    raise SystemExit(0 if all(comparison.values()) else 1)


if __name__ == '__main__':
    main()
