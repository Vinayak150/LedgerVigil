"""Reproducible demonstration: typed synthetic processor Payment -> ingested statement -> identity-based item matching
-> predicted settlement -> remaining blockers. Synthetic SANDBOX data only; never touches BOOKS or a provider.

Scenario and independently stated expectations come from fixtures/payments/typed-payment-matching-v1.json.

1. Fixture setup (owner + developer seed login): NEW tenants and SANDBOX ledgers. The invoice-like opening state is a
   clearly labelled MANUAL JOURNAL, not an Invoice document, and the payment is not applied to it. The Payment is a typed
   PAYMENT document created by lv.seed_processor_payment, which derives Dr PROCESSOR_CLEARING / Cr AR_CONTROL.
2. Identity: an identical repeat replays; a conflicting repeat is refused with no partial effects.
3. Ingestion (lv_ingest): statement files are staged as observations; this creates no Payment and no match.
4. Matching (lv_app, read-only snapshot): correct, other-processor, wrong-item, wrong-amount, cross-ledger and
   cross-tenant cases. Content hashes of every lv table are equal before and after the matching step.

Writes a new evidence directory and refuses to overwrite one. Accounting semantics remain UNREVIEWED.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid7
import argparse
import hashlib
import json

import psycopg
from psycopg import sql

from tools.dev_db import ROOT, connection
from tools.demo_seed import CHART
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ingestion import statement_format as fmt
from ledgervigil.ingestion.store import ingest_statement
from ledgervigil.kernel_adapter import AdapterError, preview_settlement
from ledgervigil.ledger.seed import seed_manual_journal, seed_processor_payment

FIXTURE = ROOT / 'fixtures/payments/typed-payment-matching-v1.json'
LV_TABLES = ('tenant', 'ledger', 'account', 'account_role_binding', 'party', 'fiscal_period', 'currency', 'service_principal', 'ledger_counter',
             'commit_batch', 'document', 'document_version', 'document_current', 'journal_entry', 'journal_line', 'ledger_event',
             'account_balance_current', 'party_balance_current', 'source_file', 'import_batch', 'source_record', 'staged_document',
             'staged_statement_item', 'processor_payment_identity', 'payment_current')


def fingerprint():
    with connection() as admin:
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(t))).fetchall()))).encode()).hexdigest()
                for t in LV_TABLES}


def new_ledger(tenant, fx, register_principal):
    ledger, party, chart = uuid7(), uuid7(), {}
    with connection('lv_migrator') as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
        if register_principal:
            owner.execute("INSERT INTO lv.tenant(tenant_id, name) VALUES (%s, 'SYNTHETIC typed payment demonstration')", (tenant,))
            owner.execute("INSERT INTO lv.service_principal(tenant_id, name, purpose, database_role) VALUES (%s, 'Local developer seed', 'DEVELOPER_SEED', 'lv_seed')", (tenant,))
        owner.execute("INSERT INTO lv.ledger(tenant_id, id, name, purpose, source_kind, currency, currency_exponent) "
                      "VALUES (%s, %s, 'Sandbox — not accounting records', 'SANDBOX', 'NATIVE', %s, %s)", (tenant, ledger, fx['ledger']['currency'], fx['ledger']['exponent']))
        for code, role, kind, contra, needs_party in CHART:
            aid = uuid7(); chart[role] = str(aid)
            owner.execute('INSERT INTO lv.account(tenant_id, ledger_id, id, code, name, type, is_contra, requires_party, currency, currency_exponent) '
                          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'INR', 2)", (tenant, ledger, aid, code, role, kind, contra, needs_party))
            owner.execute('INSERT INTO lv.account_role_binding VALUES (%s, %s, %s, %s, %s, %s, %s, true)', (tenant, ledger, role, aid, kind, contra, needs_party))
        owner.execute("INSERT INTO lv.party(tenant_id, ledger_id, id, name, external_ref) VALUES (%s, %s, %s, %s, 'fixture-customer-1')",
                      (tenant, ledger, party, fx['ledger']['customer']))
        owner.execute('INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, %s, %s)', (tenant, ledger, *fx['ledger']['period']))
    return {'tenant': tenant, 'ledger': ledger, 'party': str(party), 'chart': chart}


def opening(seed, cfg, fx):
    o = fx['opening_journal']
    lines = [{'account_id': cfg['chart'][role], 'side': side, 'amount': amount, **({'party_id': cfg['party']} if party else {})} for role, side, amount, party in o['lines']]
    return seed_manual_journal(seed, cfg['tenant'], cfg['ledger'], {'source_ref': o['source_ref'], 'effective_date': o['effective_date'],
                                                                    'description': o['label'], 'lines': lines})


def payment_body(cfg, spec, **overrides):
    return {**spec, 'party_id': cfg['party'], **overrides}


def statement_file(cfg, fx, fields):
    body = {'tenant': str(cfg['tenant']), 'ledger': str(cfg['ledger']), **fx['statement_defaults'], **fields}
    return (json.dumps({'format': fmt.FORMAT, 'version': fmt.VERSION, 'data_classification': 'SYNTHETIC', 'statement': body},
                       indent=2, ensure_ascii=False) + '\n').encode()


def settlement_request(fx, documents, **overrides):
    return {**fx['settlement_request_defaults'], 'items': [{'document_id': d, 'kind': 'PAYMENT'} for d in documents], **overrides}


def ledger_facts(cfg, document_id):
    with connection('lv_app', autocommit=True) as app, tenant_transaction(app, cfg['tenant']):
        doc = app.execute('SELECT d.id::text, d.doc_type::text, d.source_ref, v.version, v.id::text, v.financial_fingerprint, v.financial_fields, '
                          'v.origin::text, v.provenance::text FROM lv.document d JOIN lv.document_version v ON (v.tenant_id, v.ledger_id, v.document_id) = '
                          '(d.tenant_id, d.ledger_id, d.id) WHERE d.id = %s', (document_id,)).fetchone()
        identity = app.execute('SELECT processor_ref, processor_item_id, seq FROM lv.processor_payment_identity WHERE document_id = %s', (document_id,)).fetchone()
        projection = app.execute('SELECT amount::text, applied::text, unapplied::text, status FROM lv.payment_current WHERE document_id = %s', (document_id,)).fetchone()
        entry = app.execute('SELECT id::text, kind::text, posting_rule_id || \'/\' || posting_rule_version, source_document_version, provenance::text '
                            'FROM lv.journal_entry WHERE source_document_id = %s', (document_id,)).fetchone()
        lines = app.execute('SELECT line_no, account_id::text, side::text, amount::text, party_id::text FROM lv.journal_line WHERE document_id = %s ORDER BY line_no',
                            (document_id,)).fetchall()
        types = app.execute('SELECT doc_type::text, count(*) FROM lv.document WHERE ledger_id = %s GROUP BY 1 ORDER BY 1', (cfg['ledger'],)).fetchall()
    role = {v: k for k, v in cfg['chart'].items()}
    return {'document': {'id': doc[0], 'doc_type': doc[1], 'source_ref': doc[2], 'version': doc[3], 'version_id': doc[4],
                         'financial_fingerprint': doc[5], 'financial_fields': doc[6], 'origin': doc[7], 'provenance': doc[8]},
            'identity': {'processor_ref': identity[0], 'processor_item_id': identity[1], 'seq': identity[2]},
            'payment_current': dict(zip(('amount', 'applied', 'unapplied', 'status'), projection)),
            'posting_entry': {'id': entry[0], 'kind': entry[1], 'posting_rule': entry[2], 'document_version': entry[3], 'provenance': entry[4]},
            'posting_lines': [[n, role[a], s, amt, p] for n, a, s, amt, p in lines],
            'documents_by_type_in_ledger': [list(t) for t in types]}


def counts(cfg):
    with connection('lv_app', autocommit=True) as app, tenant_transaction(app, cfg['tenant']):
        return {t: app.execute(sql.SQL('SELECT count(*) FROM lv.{} WHERE ledger_id = %s').format(sql.Identifier(t)), (cfg['ledger'],)).fetchone()[0]
                for t in ('commit_batch', 'document', 'document_version', 'journal_entry', 'journal_line', 'ledger_event', 'processor_payment_identity', 'payment_current')}


def attempt(fn):
    try:
        return {'result': fn()}
    except AdapterError as error:
        return {'refused': {'code': error.code, 'source': error.source, 'detail': error.detail}}
    except psycopg.Error as error:
        return {'refused': {'sqlstate': error.sqlstate, 'class': type(error).__name__, 'message': error.diag.message_primary}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/payments' / datetime.now().strftime('demo-%Y%m%d-%H%M%S'))
    args = parser.parse_args()
    (args.output / 'inputs').mkdir(parents=True, exist_ok=False)
    fx = json.loads(FIXTURE.read_text()); exp = fx['expected']
    (args.output / 'inputs' / FIXTURE.name).write_bytes(FIXTURE.read_bytes())

    # 1. Fixture setup.
    tenant_a, tenant_b = uuid7(), uuid7()
    a = new_ledger(tenant_a, fx, True); b = new_ledger(tenant_a, fx, False); other_tenant = new_ledger(tenant_b, fx, True)
    with connection('lv_seed', autocommit=True) as seed:
        opening(seed, a, fx); opening(seed, b, fx)
        created = seed_processor_payment(seed, a['tenant'], a['ledger'], payment_body(a, fx['payment']))
        b_created = seed_processor_payment(seed, b['tenant'], b['ledger'], payment_body(b, fx['other_ledger_payment']))
        doc, b_doc = created['document_id'], b_created['document_id']
        # 2. Identity: identical repeat and conflicting repeat.
        before_retry = counts(a)
        repeat = attempt(lambda: seed_processor_payment(seed, a['tenant'], a['ledger'], payment_body(a, fx['payment'])))
        conflict = attempt(lambda: seed_processor_payment(seed, a['tenant'], a['ledger'], payment_body(a, fx['payment'], **fx['conflicting_payment'])))
        cross_tenant_seed = attempt(lambda: seed_processor_payment(seed, tenant_b, a['ledger'], payment_body(a, fx['payment'])))
        after_retry = counts(a)
    facts = ledger_facts(a, doc)
    (args.output / 'inputs' / 'payment-request-a.json').write_text(json.dumps(payment_body(a, fx['payment']), indent=2) + '\n')

    # 3. Ingestion as lv_ingest.
    payments_before_ingest = counts(a)
    ingestion = []
    with connection('lv_ingest', autocommit=True) as c:
        for name, fields in fx['statements'].items():
            cfg = b if name.startswith('b-') else a
            raw = statement_file(cfg, fx, fields); (args.output / 'inputs' / name).write_bytes(raw)
            result = ingest_statement(c, cfg['tenant'], cfg['ledger'], raw, declared_format=fmt.FORMAT, declared_version=fmt.VERSION)
            ingestion.append({'file': name, 'input_sha256': hashlib.sha256(raw).hexdigest(), **result})
    payments_after_ingest = counts(a)

    # 4. Matching, read-only.
    requests = {
        'a-correct': (a, settlement_request(fx, [doc])),
        'a-other-processor': (a, settlement_request(fx, [doc], processor='processor-2', payout='STL-3002')),
        'a-wrong-item': (a, settlement_request(fx, [doc], payout='STL-3003')),
        'a-wrong-amount': (a, settlement_request(fx, [doc], payout='STL-3004')),
        'a-requests-ledger-b-document': (a, settlement_request(fx, [b_doc])),
        'b-requests-ledger-a-document': (b, settlement_request(fx, [doc])),
        'b-statement-claims-ledger-a-item': (b, settlement_request(fx, [b_doc])),
        'tenant-b-reads-ledger-a': ({**a, 'tenant': tenant_b}, settlement_request(fx, [doc])),
    }
    (args.output / 'adapter-requests.json').write_text(json.dumps({k: v[1] for k, v in requests.items()}, indent=2) + '\n')
    before = fingerprint()
    with connection('lv_app', autocommit=True) as app:
        outcomes = {name: attempt(lambda cfg=cfg, req=req: preview_settlement(app, cfg['tenant'], cfg['ledger'], req)) for name, (cfg, req) in requests.items()}
    after = fingerprint()

    def ev(name):
        r = outcomes[name].get('result')
        return None if r is None else {c['id']: c['verdict'] for c in r['evidence_checks']}

    def refusal(name):
        return outcomes[name].get('refused', {}).get('code')
    correct = outcomes['a-correct']['result']
    item = next(c for c in correct['evidence_checks'] if c['id'] == 'E-ITEM-01')
    comparison = {
        'payment_postings_match_rule': [[l[1], l[2], l[3][:-2]] for l in facts['posting_lines']] == exp['payment_postings']
                                        and facts['posting_lines'][1][4] == a['party'] and facts['posting_lines'][0][4] is None,
        'payment_is_typed_synthetic_seed': (facts['document']['doc_type'], facts['document']['origin'], facts['document']['provenance'],
                                            facts['posting_entry']['kind'], facts['posting_entry']['posting_rule']) == ('PAYMENT', 'SEED', 'SYNTHETIC', 'DOCUMENT_POSTING', 'sp1-posting/1.0'),
        'opening_state_is_manual_journal_not_invoice': facts['documents_by_type_in_ledger'] == [['MANUAL_JOURNAL', 1], ['PAYMENT', 1]],
        'identical_repeat_replayed': repeat.get('result', {}).get('replayed') is True and repeat['result']['document_id'] == doc,
        'conflicting_identity_refused_without_effects': conflict.get('refused', {}).get('sqlstate') == '23505' and before_retry == after_retry,
        'cross_tenant_seed_refused': 'refused' in cross_tenant_seed,
        'ingestion_created_no_payment': payments_before_ingest == payments_after_ingest,
        'correct_evidence_matches': ev('a-correct') == exp['correct_evidence'],
        'correct_identity_link_is_the_typed_payment_version': item['values']['items'][0]['link']['document_version_id'] == facts['document']['version_id'],
        'predicted_postings_match': [[p['role'], p['side'], p['amount']] for p in correct['predicted_postings']] == exp['predicted_settlement_postings'],
        'balances_match': {x['role']: x['before'] for x in correct['balances']} == exp['balances_before']
                          and {x['role']: x['after'] for x in correct['balances']} == exp['balances_after'],
        'correct_blocked_by_expected_reasons': correct['outcome'] == 'BLOCKED' and sorted(correct['blocking_reasons']) == exp['correct_blocking_reasons'],
        'other_processor_refused_by_kernel': outcomes['a-other-processor'].get('refused') == {'code': 'PROCESSOR_MISMATCH', 'source': 'kernel', 'detail': ''},
        'wrong_item_unresolved': all(ev('a-wrong-item')[k] == v for k, v in exp['wrong_item'].items()),
        'wrong_amount_fails': all(ev('a-wrong-amount')[k] == v for k, v in exp['wrong_amount'].items()),
        'cross_ledger_documents_not_found': refusal('a-requests-ledger-b-document') == refusal('b-requests-ledger-a-document') == exp['cross_ledger_document'],
        'other_ledger_statement_unresolved': all(ev('b-statement-claims-ledger-a-item')[k] == v for k, v in exp['other_ledger_statement_claiming_item'].items()),
        'cross_tenant_ledger_not_found': refusal('tenant-b-reads-ledger-a') == exp['cross_tenant_ledger'],
        'all_results_blocked': all(o['result']['outcome'] == 'BLOCKED' for o in outcomes.values() if 'result' in o),
        'matching_changed_no_table': before == after,
    }
    summary = {
        'dataset_provenance': 'SYNTHETIC', 'accounting_semantics': 'UNREVIEWED', 'eligible_for_execution': False,
        'ledgers': {'a': [str(a['tenant']), str(a['ledger'])], 'b_same_tenant': [str(b['tenant']), str(b['ledger'])],
                    'other_tenant': [str(other_tenant['tenant']), str(other_tenant['ledger'])]},
        '1_typed_payment': {'created': created, 'facts': facts, 'opening_state_label': fx['opening_journal']['label']},
        '2_identity_retries': {'identical_repeat': repeat, 'conflicting_repeat': conflict, 'counts_before': before_retry, 'counts_after': after_retry,
                               'seed_into_ledger_a_under_other_tenant': cross_tenant_seed},
        '3_ingestion': {'files': ingestion, 'ledger_a_payment_counts_before': payments_before_ingest, 'after': payments_after_ingest},
        '4_matching': {name: (o['refused'] if 'refused' in o else {'evidence': ev(name), 'outcome': o['result']['outcome'],
                                                                   'blocking_reasons': o['result']['blocking_reasons']}) for name, o in outcomes.items()},
        'correct_match_detail': {'E-ITEM-01': item, 'source_items': correct['source']['items'], 'statement_observation': correct['statement_observation'],
                                 'predicted_postings': correct['predicted_postings'], 'balances': correct['balances'],
                                 'execution_readiness': correct['execution_readiness'], 'snapshot': correct['snapshot']},
        'comparison': comparison,
        'tables_hashed': len(before),
    }
    for name, value in (('summary.json', summary), ('table-hashes.json', {'before': before, 'after': after}),
                        *((f'adapter-result-{n}.json', o) for n, o in outcomes.items())):
        (args.output / name).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')
    print(json.dumps({'evidence': str(args.output), '4_matching': summary['4_matching'], 'comparison': comparison}, indent=2, ensure_ascii=False, default=str))
    raise SystemExit(0 if all(comparison.values()) else 1)


if __name__ == '__main__':
    main()
