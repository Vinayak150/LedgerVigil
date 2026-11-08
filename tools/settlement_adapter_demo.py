"""Reproducible read-only settlement demonstration (synthetic SANDBOX only; never touches BOOKS).

1. Fixture setup: create a NEW synthetic tenant and SANDBOX ledger (same chart as tools/demo_seed.py)
   and record only the first two journals of the INR 50,000 scenario: the sale and the processor
   payment. This setup uses the developer seed path, exactly as the existing demo does.
2. Calculation: as the restricted lv_app login, the adapter loads one consistent snapshot and asks the
   existing kernel to (a) simulate the INR 49,000 payout with INR 1,000 fee and (b) inspect the
   balanced-but-wrong "second sale" candidate. The adapter never calls the seed function.
3. Proof of no writes: content hashes of every lv table, taken by the database administrator, are equal
   before and after step 2.
4. Comparison: with independently stated OC-01 expectations, and with the existing synthetic journal
   evidence in evidence/posting/demo-*/ when it is present.

Writes a new evidence directory and refuses to overwrite one. Accounting semantics remain UNREVIEWED.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid7
import argparse
import hashlib
import json
from psycopg import sql
from tools.dev_db import ROOT, connection
from tools.demo_seed import CHART
from ledgervigil.ledger.seed import seed_manual_journal
from ledgervigil.kernel_adapter import inspect_settlement, preview_settlement

LV_TABLES = ('tenant', 'ledger', 'account', 'account_role_binding', 'party', 'fiscal_period', 'currency', 'service_principal',
             'ledger_counter', 'commit_batch', 'document', 'document_version', 'document_current', 'journal_entry', 'journal_line',
             'ledger_event', 'account_balance_current', 'party_balance_current')
EXPECTED = {  # Stage 2A §5.1 / §7 OC-01, stated independently of the kernel
    'postings': [['BANK_OPERATING', 'D', '49000.00'], ['PROCESSOR_FEE_EXPENSE', 'D', '1000.00'], ['PROCESSOR_CLEARING', 'C', '50000.00']],
    'after': {'BANK_OPERATING': '49000.00', 'PROCESSOR_FEE_EXPENSE': '1000.00', 'PROCESSOR_CLEARING': '0.00', 'AR_CONTROL': '0.00',
              'REVENUE_DEFAULT': '-50000.00', 'SALES_RETURNS': '0.00', 'OPENING_BALANCE_EQUITY': '0.00'},
}


def fingerprint():
    with connection() as admin:
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(t))).fetchall()))).encode()).hexdigest()
                for t in LV_TABLES}


def setup():
    tenant, ledger, party, chart = uuid7(), uuid7(), uuid7(), {}
    with connection('lv_migrator') as owner:
        owner.execute('SET LOCAL ROLE lv_owner')
        owner.execute("SELECT set_config('app.tenant_id',%s,true)", (str(tenant),))
        owner.execute("INSERT INTO lv.tenant(tenant_id,name) VALUES(%s,'SYNTHETIC settlement adapter demonstration')", (tenant,))
        owner.execute("INSERT INTO lv.ledger(tenant_id,id,name,purpose,source_kind,currency,currency_exponent) VALUES(%s,%s,'Sandbox — not accounting records','SANDBOX','NATIVE','INR',2)", (tenant, ledger))
        owner.execute("INSERT INTO lv.service_principal(tenant_id,name,purpose,database_role) VALUES(%s,'Local developer seed','DEVELOPER_SEED','lv_seed')", (tenant,))
        for code, role, kind, contra, needs_party in CHART:
            aid = uuid7(); chart[role] = str(aid)
            owner.execute("INSERT INTO lv.account(tenant_id,ledger_id,id,code,name,type,is_contra,requires_party,currency,currency_exponent) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'INR',2)", (tenant, ledger, aid, code, role, kind, contra, needs_party))
            owner.execute('INSERT INTO lv.account_role_binding VALUES(%s,%s,%s,%s,%s,%s,%s,true)', (tenant, ledger, role, aid, kind, contra, needs_party))
        owner.execute("INSERT INTO lv.party(tenant_id,ledger_id,id,name,external_ref) VALUES(%s,%s,%s,'Synthetic customer','fixture-customer-1')", (tenant, ledger, party))
        owner.execute("INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date) VALUES(%s,%s,'2026-10-01','2026-11-01')", (tenant, ledger))
    leg = lambda role, side, amount: {'account_id': chart[role], 'side': side, 'amount': amount, **({'party_id': str(party)} if role == 'AR_CONTROL' else {})}  # noqa: E731
    bodies = [{'source_ref': 'adapter-demo-v1-invoice', 'effective_date': '2026-10-10', 'description': 'SYNTHETIC sale; adapter demonstration; not accounting records',
               'lines': [leg('AR_CONTROL', 'D', '50000.00'), leg('REVENUE_DEFAULT', 'C', '50000.00')]},
              {'source_ref': 'adapter-demo-v1-payment', 'effective_date': '2026-10-10', 'description': 'SYNTHETIC processor payment; adapter demonstration; not accounting records',
               'lines': [leg('PROCESSOR_CLEARING', 'D', '50000.00'), leg('AR_CONTROL', 'C', '50000.00')]}]
    with connection('lv_seed', autocommit=True) as seed:
        for body in bodies:
            seed_manual_journal(seed, tenant, ledger, body)
    return tenant, ledger, bodies


def existing_evidence():
    found = sorted((ROOT / 'evidence/posting').glob('demo-*/result.json'))
    if not found:
        return None
    result = json.loads(found[-1].read_text()); requests = json.loads((found[-1].parent / 'requests.json').read_text())
    return {'path': str(found[-1].relative_to(ROOT)), 'sha256': hashlib.sha256(found[-1].read_bytes()).hexdigest(),
            'balances_by_role': {b['role']: b['signed_amount'] for b in result['balances']},
            'settlement_journal_lines': [[l['side'], l['amount']] for l in requests[2]['lines']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/adapter' / datetime.now().strftime('demo-%Y%m%d-%H%M%S'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    tenant, ledger, bodies = setup()
    request = {'processor': 'processor-1', 'payout': 'STL-3001', 'date': '2026-10-14', 'currency': 'INR',
               'gross': '50000.00', 'fee': '1000.00', 'net': '49000.00', 'items': [{'source_ref': 'adapter-demo-v1-payment', 'kind': 'PAYMENT'}]}
    wrong_sale = [{'role': 'BANK_OPERATING', 'side': 'D', 'amount': '49000.00'}, {'role': 'PROCESSOR_FEE_EXPENSE', 'side': 'D', 'amount': '1000.00'},
                  {'role': 'REVENUE_DEFAULT', 'side': 'C', 'amount': '50000.00'}]
    before = fingerprint()
    with connection('lv_app', autocommit=True) as app:
        valid = preview_settlement(app, tenant, ledger, request)
        wrong = inspect_settlement(app, tenant, ledger, request, wrong_sale)
    after = fingerprint()
    predicted = [[p['role'], p['side'], p['amount']] for p in valid['predicted_postings']]
    after_by_role = {b['role']: b['after'] for b in valid['balances']}
    evidence = existing_evidence()
    comparison = {
        'independent_expectations_OC01': {'postings_equal': predicted == EXPECTED['postings'], 'after_balances_equal': after_by_role == EXPECTED['after']},
        'existing_synthetic_journal_evidence': None if evidence is None else {
            'path': evidence['path'], 'sha256': evidence['sha256'],
            'settlement_lines_equal_by_side_and_amount': [[p[1], p[2]] for p in predicted] == evidence['settlement_journal_lines'],
            'after_balances_equal': all(Decimal(after_by_role[r]) == Decimal(v) for r, v in evidence['balances_by_role'].items()),
            'note': 'The recorded demo posted these three journals directly via the seed path; it is synthetic fixture data, not an approved execution.'},
        'no_financial_writes': {'all_lv_tables_unchanged': before == after, 'tables_hashed': len(before),
                                'adapter_write_xid_assigned': [valid['snapshot']['write_xid_assigned'], wrong['snapshot']['write_xid_assigned']]},
    }
    summary = {'dataset_provenance': 'SYNTHETIC', 'accounting_semantics': 'UNREVIEWED', 'eligible_for_execution': False,
               'tenant_id': str(tenant), 'ledger_id': str(ledger), 'snapshot_seq': valid['snapshot']['seq'],
               'valid_settlement': {'kernel_outcome': valid['kernel']['outcome'], 'predicted_postings': predicted, 'after': after_by_role,
                                    'checks': {c['id']: c['verdict'] for c in valid['checks']}},
               'balanced_wrong_sale': {'kernel_outcome': wrong['kernel']['outcome'], 'after': {b['role']: b['after'] for b in wrong['balances']},
                                       'checks': {c['id']: c['verdict'] for c in wrong['checks']}},
               'comparison': comparison}
    for name, value in (('fixture-requests.json', bodies), ('adapter-request.json', request), ('wrong-sale-candidate.json', wrong_sale),
                        ('valid-settlement-result.json', valid), ('wrong-sale-result.json', wrong),
                        ('table-fingerprints.json', {'before': before, 'after': after}), ('summary.json', summary)):
        (args.output / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'evidence': str(args.output), **summary}, indent=2, ensure_ascii=False))
    ok = all(comparison['independent_expectations_OC01'].values()) and comparison['no_financial_writes']['all_lv_tables_unchanged']
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
