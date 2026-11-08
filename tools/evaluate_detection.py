"""Reproducible evaluation harness for the settlement reconciliation detector (docs/AI_DETECTION.md, ADR-017 / Stage 1 H-8).

    PYTHONPATH=src:.:tests python -m tools.evaluate_detection generate   # writes the frozen case file (deterministic)
    PYTHONPATH=src:.:tests python -m tools.evaluate_detection run OUT.json

Labels come ONLY from this deterministic generator (what it injected), never from detector output, and are SYNTHETIC
perturbations: they test that the detector does what its specification says on controlled cases. They do not establish
accounting correctness or effectiveness on real processor data (NOT YET ESTABLISHED). Two labels are kept per case:
`expected` (the findings the v1 specification requires) and `discrepancy_present` (whether the generator injected any
real statement/ledger difference, including ones v1 is not designed to see). There are no tunable parameters, so the dev
split was used only to build this harness; the held-out split is scored as-is. Runs in a disposable PostgreSQL database.
"""
import hashlib
import json
import random
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / 'evaluation/detection/cases-1.0.0.json'
GENERATOR = 'lv-detection-synthetic-cases/1.0.0'
SEEDS = {'dev': 20261106, 'held_out': 61102026}
PER_CLASS = {'dev': 2, 'held_out': 5}

# class -> (rationale, expected v1 abstention, expected v1 kinds, discrepancy actually injected)
CLASSES = {
    'C01_CLEAN_SETTLED': ('Statement and recorded settlement agree exactly.', None, [], False),
    'C02_SETTLEMENT_MISSING': ('Payout on the statement; its payments RECEIVED; no settlement recorded.', None, ['SETTLEMENT_NOT_RECORDED'], True),
    'C03_FEE_MISKEYED': ('Settlement recorded with the same items and gross but a different fee/net.', None, ['SETTLEMENT_AMOUNTS_DIFFER'], True),
    'C04_ITEM_OMITTED': ('Settlement recorded without one statement item (gross reduced accordingly).', None,
                         ['SETTLEMENT_AMOUNTS_DIFFER', 'SETTLEMENT_ITEMS_DIFFER'], True),
    'C05_PAYMENT_MISKEYED': ('One payment recorded at a different amount than the statement line; no settlement.', None,
                             ['SETTLEMENT_NOT_RECORDED', 'STATEMENT_ITEM_AMOUNT_DIFFERS'], True),
    'C06_UNKNOWN_ITEM': ('Statement lists an item with no payment of that identity; no settlement.', None,
                         ['SETTLEMENT_NOT_RECORDED', 'STATEMENT_ITEM_UNRESOLVED'], True),
    'C07_OTHER_NAMESPACE_TRAP': ('Item id exists only under another processor with the SAME amount; must not link.', None,
                                 ['SETTLEMENT_NOT_RECORDED', 'STATEMENT_ITEM_UNRESOLVED'], True),
    'C08_SETTLED_ELSEWHERE': ('One statement payment is already in another payout\'s settlement; this payout unsettled.', None,
                              ['SETTLEMENT_NOT_RECORDED', 'STATEMENT_ITEM_SETTLED_ELSEWHERE'], True),
    'C09_FORMAT_VARIANT_TRAP': ('Statement writes amounts with fewer decimals (300.0); settlement agrees exactly.', None, [], False),
    'C10_REFUND_LINE': ('Statement contains a refund line (unsupported).', 'REFUND_ITEMS_NOT_SUPPORTED', [], True),
    'C11_CONFLICTING_FILE': ('A second file claims the same payout with different content (ambiguous evidence).', 'AMBIGUOUS_STATEMENT', [], True),
    'C12_SWAPPED_AMOUNTS_TRAP': ('Two item amounts swapped between payments; totals and item set equal; settled.', None,
                                 ['STATEMENT_ITEM_AMOUNT_DIFFERS'], True),
    'C13_PAYOUT_DATE_BLIND_SPOT': ('Settlement recorded on a different payout date; amounts and items agree. v1 does not compare dates.',
                                   None, [], True),
}


# ------------------------------------------------------------------------------------------------------- generator
def _amount(rng):
    return f'{rng.randint(1, 900000) / 100:.2f}'


def _cents(text):
    whole, _, frac = text.partition('.')
    return int(whole) * 100 + int((frac + '00')[:2])


def _fmt(cents):
    return f'{cents // 100}.{cents % 100:02d}'


def generate():
    cases = []
    for split, seed in SEEDS.items():
        rng = random.Random(seed)
        prefix = {'dev': 'D', 'held_out': 'H'}[split]
        for cls, (rationale, abstention, kinds, injected) in CLASSES.items():
            for n in range(PER_CLASS[split]):
                cid = f'{prefix}{cls[:3]}{n:02d}'
                count = rng.randint(2, 4)
                payments = [{'item': f'{cid}-P{i}', 'processor': 'processor-1', 'amount': _amount(rng)} for i in range(count)]
                items = [[p['item'], 'PAYMENT', p['amount']] for p in payments]
                settlements, conflict, date = [], None, '2026-10-14'
                if cls == 'C05_PAYMENT_MISKEYED':
                    payments[0]['amount'] = _fmt(_cents(payments[0]['amount']) + rng.randint(1, 500))
                if cls == 'C06_UNKNOWN_ITEM':
                    items.append([f'{cid}-UNKNOWN', 'PAYMENT', _amount(rng)])
                if cls == 'C07_OTHER_NAMESPACE_TRAP':
                    amount = _amount(rng)
                    payments.append({'item': f'{cid}-NS', 'processor': 'processor-2', 'amount': amount})
                    items.append([f'{cid}-NS', 'PAYMENT', amount])
                if cls == 'C10_REFUND_LINE':
                    items.append([f'{cid}-RF', 'REFUND', '0.01'])
                if cls == 'C12_SWAPPED_AMOUNTS_TRAP':
                    while payments[0]['amount'] == payments[1]['amount']:
                        payments[1]['amount'] = _amount(rng)
                    items[0][2], items[1][2] = payments[1]['amount'], payments[0]['amount']
                if cls == 'C09_FORMAT_VARIANT_TRAP':
                    for p in payments:
                        p['amount'] = _fmt(_cents(p['amount']) // 10 * 10)        # whole tenths, so '123.40' can be written '123.4'
                    items = [[p['item'], 'PAYMENT', p['amount'][:-1]] for p in payments]
                gross_c = sum(_cents(a) * (1 if k == 'PAYMENT' else -1) for _, k, a in items)
                fee_c = min(rng.randint(0, 5000), gross_c)
                statement = {'payout': f'{cid}-PO', 'items': items, 'gross': _fmt(gross_c), 'fee': _fmt(fee_c), 'net': _fmt(gross_c - fee_c)}
                if cls == 'C09_FORMAT_VARIANT_TRAP':
                    statement['gross'] = statement['gross'][:-1] if statement['gross'].endswith('0') else statement['gross']
                ledger_items = [p['item'] for p in payments if p['processor'] == 'processor-1']
                ledger_total = sum(_cents(p['amount']) for p in payments if p['processor'] == 'processor-1')
                if cls in ('C01_CLEAN_SETTLED', 'C09_FORMAT_VARIANT_TRAP', 'C12_SWAPPED_AMOUNTS_TRAP', 'C13_PAYOUT_DATE_BLIND_SPOT'):
                    settlements.append({'payout': statement['payout'], 'items': ledger_items, 'gross': _fmt(ledger_total), 'fee': statement['fee'],
                                        'net': _fmt(ledger_total - fee_c), 'date': '2026-10-15' if cls == 'C13_PAYOUT_DATE_BLIND_SPOT' else date})
                if cls == 'C03_FEE_MISKEYED':
                    other_fee = fee_c + rng.randint(1, 99) if fee_c + 100 <= ledger_total else max(fee_c - rng.randint(1, 99), 0)
                    settlements.append({'payout': statement['payout'], 'items': ledger_items, 'gross': _fmt(ledger_total), 'fee': _fmt(other_fee),
                                        'net': _fmt(ledger_total - other_fee), 'date': date})
                if cls == 'C04_ITEM_OMITTED':
                    kept = payments[:-1]; total = sum(_cents(p['amount']) for p in kept); fee = min(fee_c, total)
                    settlements.append({'payout': statement['payout'], 'items': [p['item'] for p in kept], 'gross': _fmt(total), 'fee': _fmt(fee),
                                        'net': _fmt(total - fee), 'date': date})
                if cls == 'C08_SETTLED_ELSEWHERE':
                    p = payments[-1]
                    settlements.append({'payout': f'{cid}-OTHER', 'items': [p['item']], 'gross': p['amount'], 'fee': '0.00', 'net': p['amount'], 'date': date})
                if cls == 'C11_CONFLICTING_FILE':
                    conflict = {**statement, 'fee': _fmt(fee_c + 1) if fee_c + 1 <= gross_c else _fmt(fee_c - 1),
                                'net': _fmt(gross_c - (fee_c + 1 if fee_c + 1 <= gross_c else fee_c - 1))}
                cases.append({'id': cid, 'split': split, 'class': cls, 'rationale': rationale, 'payments': payments, 'statement': statement,
                              'settlements': settlements, 'conflict': conflict,
                              'expected': {'abstention': abstention, 'kinds': sorted(kinds)}, 'discrepancy_present': injected,
                              'label_source': f'{GENERATOR} (injected perturbation; SYNTHETIC; not an accounting review)'})
    body = {'generator': GENERATOR, 'seeds': SEEDS, 'per_class': PER_CLASS, 'classification': 'SYNTHETIC', 'cases': cases}
    CASES.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body, indent=1, sort_keys=True) + '\n'
    CASES.write_text(text)
    manifest = {'file': CASES.name, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'cases': len(cases), 'generator': GENERATOR}
    (CASES.parent / 'MANIFEST.json').write_text(json.dumps(manifest, indent=1) + '\n')
    return manifest


# ---------------------------------------------------------------------------------------------- leakage checks
def leakage(cases):
    by_split = {}
    for c in cases:
        keys = {('payout', c['statement']['payout'])} | {('item', p['processor'], p['item']) for p in c['payments']}
        keys |= {('scenario', hashlib.sha256(json.dumps([c['payments'], c['statement']], sort_keys=True).encode()).hexdigest())}
        by_split.setdefault(c['split'], set()).update(keys)
    shared = by_split['dev'] & by_split['held_out']
    ids = [c['id'] for c in cases]
    return {'shared_identifiers_or_scenarios_across_splits': sorted(map(str, shared)), 'duplicate_case_ids': sorted({i for i in ids if ids.count(i) > 1}),
            'passed': not shared and len(set(ids)) == len(ids)}


# --------------------------------------------------------------------------------------- naive comparison baseline
def naive_amount_matcher(case):
    """A deliberately simple alternative some reconciliations use: link each statement line to any unused payment of equal
    amount (any processor), then flag the payout if no settlement exists or its gross differs. No identity, no abstention."""
    pool = [dict(p) for p in case['payments']]
    flagged = False
    for item, kind, amount in case['statement']['items']:
        match = next((p for p in pool if _cents(p['amount']) == _cents(amount)), None)
        if match is None:
            flagged = True
        else:
            pool.remove(match)
    own = [s for s in case['settlements'] if s['payout'] == case['statement']['payout']]
    if not own or _cents(own[0]['gross']) != _cents(case['statement']['gross']):
        flagged = True
    return flagged


# ------------------------------------------------------------------------------------------------------- runner
def materialize_and_detect(cases):
    from psycopg import sql
    from tools.dev_db import connection, prepare_database, migrate
    from ledgervigil.db.tenant import tenant_transaction
    from ledgervigil.detection import detector
    from ledgervigil.ingestion import statement_format as fmt
    from ledgervigil.ingestion.store import ingest_statement
    from ledgervigil.ledger.seed import seed_processor_payment, seed_processor_settlement
    from test_posting import provision, register
    from test_processor_payments import new_tenant

    name = 'lv_migration_test_' + uuid4().hex
    with connection(autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    outcomes = []
    try:
        prepare_database(name); migrate(name)
        with connection('lv_app', name, autocommit=True) as app, connection('lv_seed', name, autocommit=True) as seed, \
                connection('lv_ingest', name, autocommit=True) as ingest:
            for split in ('dev', 'held_out'):
                tenant = new_tenant(name); register(name, tenant); cfg = provision(app, tenant); cfg['currency'] = 'INR'
                docs = {}
                for case in [c for c in cases if c['split'] == split]:
                    for p in case['payments']:
                        docs[(p['processor'], p['item'])] = seed_processor_payment(seed, tenant, cfg['ledger'], {
                            'processor_ref': p['processor'], 'processor_item_id': p['item'], 'party_id': cfg['party'], 'currency': 'INR',
                            'amount': p['amount'], 'received_date': '2026-10-10', 'description': 'SYNTHETIC evaluation payment'})['document_id']
                    for s in case['settlements']:
                        seed_processor_settlement(seed, tenant, cfg['ledger'], {
                            'processor_ref': 'processor-1', 'payout_id': s['payout'], 'payout_date': s['date'], 'currency': 'INR',
                            'gross': s['gross'], 'fee': s['fee'], 'net': s['net'], 'description': 'SYNTHETIC evaluation settlement',
                            'items': [{'document_id': str(docs[('processor-1', i)]), 'kind': 'PAYMENT',
                                       'amount': next(p['amount'] for p in case['payments'] if p['item'] == i)} for i in s['items']]})
                    staged = None
                    for body in [case['statement']] + ([case['conflict']] if case['conflict'] else []):
                        raw = json.dumps({'format': fmt.FORMAT, 'version': fmt.VERSION, 'data_classification': 'SYNTHETIC', 'statement': {
                            'tenant': str(tenant), 'ledger': str(cfg['ledger']), 'processor': 'processor-1', 'payout': body['payout'],
                            'date': '2026-10-14', 'currency': 'INR', 'items': [{'id': i, 'kind': k, 'amount': a} for i, k, a in body['items']],
                            'gross': body['gross'], 'fee': body['fee'], 'net': body['net'], 'memo': 'SYNTHETIC evaluation statement'}}).encode()
                        result = ingest_statement(ingest, tenant, cfg['ledger'], raw, declared_format=fmt.FORMAT, declared_version=fmt.VERSION)
                        staged = staged or result.get('staged_document_id')
                    with tenant_transaction(app, tenant):
                        started = time.perf_counter()
                        [result] = detector.run(app, tenant, cfg['ledger'], [str(staged)])
                        elapsed = time.perf_counter() - started
                        inputs = app.execute('SELECT lv.detection_inputs(%s,%s,%s)', (tenant, cfg['ledger'], staged)).fetchone()[0]
                        oracle = app.execute('SELECT lv.detection_expected(%s)', (json.dumps(inputs),)).fetchone()[0]
                    outcomes.append({'case': case, 'result': result, 'oracle_agrees': oracle == {k: result[k] for k in ('abstention', 'findings')},
                                     'seconds': elapsed})
    finally:
        with connection(autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
    return outcomes


def score(outcomes):
    kinds = ['SETTLEMENT_NOT_RECORDED', 'SETTLEMENT_AMOUNTS_DIFFER', 'SETTLEMENT_ITEMS_DIFFER', 'STATEMENT_ITEM_SETTLED_ELSEWHERE',
             'STATEMENT_ITEM_AMOUNT_DIFFERS', 'STATEMENT_ITEM_UNRESOLVED']
    report = {}
    for split in ('dev', 'held_out'):
        rows = [o for o in outcomes if o['case']['split'] == split]
        per_kind = {}
        for k in kinds:
            tp = sum(1 for o in rows if k in o['case']['expected']['kinds'] and k in [f['kind'] for f in o['result']['findings']])
            fp = sum(1 for o in rows if k not in o['case']['expected']['kinds'] and k in [f['kind'] for f in o['result']['findings']])
            fn = sum(1 for o in rows if k in o['case']['expected']['kinds'] and k not in [f['kind'] for f in o['result']['findings']])
            per_kind[k] = {'tp': tp, 'fp': fp, 'fn': fn, 'precision': None if tp + fp == 0 else round(tp / (tp + fp), 4),
                           'recall': None if tp + fn == 0 else round(tp / (tp + fn), 4)}
        scored = [o for o in rows if o['case']['expected']['abstention'] is None and o['result']['abstention'] is None]
        flags = lambda o: any(f['class'] == 'DISCREPANCY' for f in o['result']['findings'])
        def pr(pred):
            tp = sum(1 for o in scored if pred(o) and o['case']['discrepancy_present'])
            fp = sum(1 for o in scored if pred(o) and not o['case']['discrepancy_present'])
            fn = sum(1 for o in scored if not pred(o) and o['case']['discrepancy_present'])
            return {'tp': tp, 'fp': fp, 'fn': fn, 'precision': None if tp + fp == 0 else round(tp / (tp + fp), 4),
                    'recall': None if tp + fn == 0 else round(tp / (tp + fn), 4), 'cases': len(scored)}
        conformance = {}
        for o in rows:
            ok = (o['result']['abstention'] == o['case']['expected']['abstention']
                  and sorted(f['kind'] for f in o['result']['findings']) == o['case']['expected']['kinds'])
            c = conformance.setdefault(o['case']['class'], {'cases': 0, 'exact_match': 0})
            c['cases'] += 1; c['exact_match'] += ok
        seconds = sorted(o['seconds'] for o in rows)
        report[split] = {
            'cases': len(rows), 'specification_conformance_by_class': conformance, 'per_kind': per_kind,
            'discrepancy_detection_v1': pr(flags), 'discrepancy_detection_naive_amount_baseline': pr(naive_amount_matcher_outcome),
            'abstentions': {'expected': sum(1 for o in rows if o['case']['expected']['abstention']),
                            'correct': sum(1 for o in rows if o['case']['expected']['abstention'] and o['result']['abstention'] == o['case']['expected']['abstention']),
                            'unexpected': sum(1 for o in rows if o['result']['abstention'] and not o['case']['expected']['abstention'])},
            'database_oracle_agreement': f"{sum(o['oracle_agrees'] for o in rows)}/{len(rows)}",
            'latency_seconds_per_statement': {'measured': 'detector.run incl. evidence-set read and source re-parse, local PostgreSQL',
                                              'p50': round(seconds[len(seconds) // 2], 4), 'max': round(seconds[-1], 4)},
            'failure_cases': [{'case': o['case']['id'], 'class': o['case']['class'], 'expected': o['case']['expected'],
                               'got': {'abstention': o['result']['abstention'], 'kinds': sorted(f['kind'] for f in o['result']['findings'])},
                               'discrepancy_present': o['case']['discrepancy_present']}
                              for o in rows if (o['result']['abstention'] != o['case']['expected']['abstention']
                                                or sorted(f['kind'] for f in o['result']['findings']) != o['case']['expected']['kinds'])
                              or (o['case']['discrepancy_present'] and not flags(o) and not o['result']['abstention'])]}
    return report


def naive_amount_matcher_outcome(o):
    return naive_amount_matcher(o['case'])


def main(argv):
    if argv[:1] == ['generate']:
        print(json.dumps(generate())); return 0
    if argv[:1] == ['run'] and len(argv) == 2:
        text = CASES.read_text(); manifest = json.loads((CASES.parent / 'MANIFEST.json').read_text())
        if hashlib.sha256(text.encode()).hexdigest() != manifest['sha256']:
            raise SystemExit('case file does not match its manifest hash; refusing to score')
        cases = json.loads(text)['cases']
        outcomes = materialize_and_detect(cases)
        out = {'case_file': {'path': str(CASES.relative_to(ROOT)), 'sha256': manifest['sha256'], 'generator': GENERATOR},
               'leakage': leakage(cases), 'label_provenance': 'deterministic generator (injected perturbations), SYNTHETIC; no human or model labels',
               'effectiveness_on_real_data': 'NOT YET ESTABLISHED (no labelled real processor statements exist in this project)',
               'model': 'none; cost 0 (no external or local model call)', 'results': score(outcomes)}
        Path(argv[1]).write_text(json.dumps(out, indent=1) + '\n')
        print(json.dumps({s: {k: out['results'][s][k] for k in ('cases', 'discrepancy_detection_v1', 'discrepancy_detection_naive_amount_baseline',
                                                                  'database_oracle_agreement')} for s in out['results']}, indent=1))
        return 0
    print(__doc__); return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
