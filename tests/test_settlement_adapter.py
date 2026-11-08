"""Read-only accounting-kernel adapter against real PostgreSQL (synthetic SANDBOX data only).

Expected values are stated independently from Stage 2A §5.1/§7 OC-01 (settlement of a paid INR 50,000
invoice by a INR 49,000 payout with a INR 1,000 fee: Bank +49,000, Fee +1,000, Clearing back to 0,
Revenue and AR unchanged), not copied from kernel output. Accounting semantics remain UNREVIEWED.
Seeding of the invoice/payment journals below is fixture setup through the developer seed path; the
adapter itself never calls it.
"""
import ast
import hashlib
import json
import threading
from decimal import Decimal
from pathlib import Path
from uuid import uuid7

import psycopg
import pytest
from psycopg import sql

from tools.dev_db import connection
from period_close_support import admin_token, close_period, close_call, held_close   # 0009: audited close replaces the R-02 UPDATE
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.kernel_adapter import AdapterError, inspect_settlement, preview_settlement
from ledgervigil.kernel_adapter import loader as adapter_loader
from test_posting import line, post, provision, register

ROOT = Path(__file__).resolve().parents[1]
ALL_LV_TABLES = ('tenant', 'ledger', 'account', 'account_role_binding', 'party', 'fiscal_period', 'currency',
                 'service_principal', 'ledger_counter', 'commit_batch', 'document', 'document_version', 'document_current',
                 'journal_entry', 'journal_line', 'ledger_event', 'account_balance_current', 'party_balance_current')

# Independently stated OC-01 expectations (not derived from the kernel).
EXPECTED_POSTINGS = [('BANK_OPERATING', 'D', '49000.00'), ('PROCESSOR_FEE_EXPENSE', 'D', '1000.00'), ('PROCESSOR_CLEARING', 'C', '50000.00')]
EXPECTED_AFTER = {'BANK_OPERATING': '49000.00', 'AR_CONTROL': '0.00', 'PROCESSOR_CLEARING': '0.00', 'OPENING_BALANCE_EQUITY': '0.00',
                  'REVENUE_DEFAULT': '-50000.00', 'SALES_RETURNS': '0.00', 'PROCESSOR_FEE_EXPENSE': '1000.00'}
EXPECTED_BEFORE = {**EXPECTED_AFTER, 'BANK_OPERATING': '0.00', 'PROCESSOR_FEE_EXPENSE': '0.00', 'PROCESSOR_CLEARING': '50000.00'}


def settlement_request(**overrides):
    request = {'processor': 'processor-1', 'payout': 'STL-3001', 'date': '2026-10-14', 'currency': 'INR',
               'gross': '50000.00', 'fee': '1000.00', 'net': '49000.00',
               'items': [{'source_ref': 'synthetic-payment-1', 'kind': 'PAYMENT'}]}
    request.update(overrides)
    return request


def invoice_and_payment(seed, cfg):
    """Fixture: the first two OC-01 journals (sale, then processor payment against AR)."""
    for ref, lines in (('synthetic-invoice-1', [line(cfg, 'AR_CONTROL', 'D', '50000.00', cfg['party']), line(cfg, 'REVENUE_DEFAULT', 'C', '50000.00')]),
                       ('synthetic-payment-1', [line(cfg, 'PROCESSOR_CLEARING', 'D', '50000.00'), line(cfg, 'AR_CONTROL', 'C', '50000.00', cfg['party'])])):
        post(seed, cfg, {'source_ref': ref, 'effective_date': '2026-10-10', 'description': 'SYNTHETIC adapter fixture', 'lines': lines})


@pytest.fixture
def seed(database):
    with connection('lv_seed', database, autocommit=True) as c:
        yield c


@pytest.fixture
def ledger(app, seed, tenants, database):
    cfg = provision(app, tenants[0]); register(database, tenants[0]); invoice_and_payment(seed, cfg)
    return cfg


def fingerprint(database):
    """Content hash of every lv table, read as the database administrator (bypasses RLS)."""
    with connection(database=database) as admin:
        result = {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(t))).fetchall()))).encode()).hexdigest()
                  for t in ALL_LV_TABLES}
        return result


def by_role(result, key='after'):
    return {b['role']: b[key] for b in result['balances']}


# ------------------------------------------------------------------------------------------- practical result
def test_oc01_prediction_matches_independent_expectations(app, ledger):
    result = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert [(p['role'], p['side'], p['amount']) for p in result['predicted_postings']] == EXPECTED_POSTINGS
    assert by_role(result, 'before') == EXPECTED_BEFORE and by_role(result) == EXPECTED_AFTER
    assert result['settlement_clearing_residual'] == '0.00'
    assert result['snapshot']['seq'] == '2' and result['snapshot']['isolation'] == 'repeatable read'
    assert result['snapshot']['read_only'] is True and result['snapshot']['read_role'] == 'lv_app'
    assert result['snapshot']['write_xid_assigned'] is False
    assert result['labels'] == {**result['labels'], 'dataset_provenance': 'SYNTHETIC', 'ledger_purpose': 'SANDBOX',
                                'accounting_semantics': 'UNREVIEWED', 'eligible_for_execution': False}
    assert result['kernel']['version'] == '0.1.0-review' and result['kernel']['rule_version'] == 'sp1-posting/1.0:settlement-slice'
    verdicts = {c['id']: c['verdict'] for c in result['checks']}
    assert verdicts == {'K-SETTLEMENT-POSTINGS': 'PASS', 'P-ACC-01': 'PASS', 'P-BAL-01': 'PASS', 'P-CLR-01': 'INSUFFICIENT_EVIDENCE',
                        'P-CLR-02': 'PASS', 'P-CUR-01': 'PASS', 'P-DUP-01': 'PASS', 'P-EVD-01': 'INSUFFICIENT_EVIDENCE',
                        'P-FEE-01': 'INSUFFICIENT_EVIDENCE', 'P-PER-01': 'PASS', 'P-REV-01': 'NOT_APPLICABLE', 'P-STL-01': 'INSUFFICIENT_EVIDENCE'}
    # Without ingested SOURCE evidence the kernel blocks; the adapter does not manufacture a pass.
    assert result['kernel']['outcome'] == 'BLOCKED'
    assert next(c for c in result['checks'] if c['id'] == 'P-DUP-01')['basis'].startswith('NOT_ESTABLISHED')
    assert result['source']['items'][0]['source_ref'] == 'synthetic-payment-1'


def test_prediction_equals_the_journal_the_existing_seed_path_would_record(app, seed, ledger):
    """Compare with existing synthetic journal evidence: posting the third demo journal as a separate
    fixture step must leave exactly the predicted balances in the database's own projection."""
    predicted = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    post(seed, ledger, {'source_ref': 'synthetic-settlement-1', 'effective_date': '2026-10-14', 'description': 'SYNTHETIC adapter fixture',
                        'lines': [line(ledger, role, side, amount) for role, side, amount in EXPECTED_POSTINGS]})
    with tenant_transaction(app, ledger['tenant']):
        rows = app.execute('SELECT account_id::text, sum(debit_total - credit_total) FROM lv.account_balance_current WHERE ledger_id = %s GROUP BY 1',
                           (ledger['ledger'],)).fetchall()
    actual = {account: balance for account, balance in rows}
    for b in predicted['balances']:
        assert actual.get(b['account_id'], Decimal(0)) == Decimal(b['after']), b['role']


def test_balanced_second_sale_is_detected_by_supported_checks(app, ledger):
    """OC-02: Dr Bank 49,000 / Dr Fee 1,000 / Cr Revenue 50,000 balances but double-counts revenue."""
    result = inspect_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request(),
                                [{'role': 'BANK_OPERATING', 'side': 'D', 'amount': '49000.00'},
                                 {'role': 'PROCESSOR_FEE_EXPENSE', 'side': 'D', 'amount': '1000.00'},
                                 {'role': 'REVENUE_DEFAULT', 'side': 'C', 'amount': '50000.00'}])
    verdicts = {c['id']: c['verdict'] for c in result['checks']}
    assert verdicts['P-BAL-01'] == 'PASS'
    assert verdicts['P-REV-01'] == 'FAIL' and verdicts['P-CLR-01'] == 'FAIL' and verdicts['K-SETTLEMENT-POSTINGS'] == 'FAIL'
    assert by_role(result)['REVENUE_DEFAULT'] == '-100000.00' and by_role(result)['PROCESSOR_CLEARING'] == '50000.00'
    assert result['settlement_clearing_residual'] == '50000.00' and result['kernel']['outcome'] == 'BLOCKED'


def test_already_posted_settlement_is_blocked_only_by_the_clearing_balance(app, seed, ledger):
    """The payout was already recorded as a manual journal. No inclusion facts are persisted, so P-DUP-01
    cannot see it; the kernel still blocks because clearing would end in credit (P-CLR-02)."""
    post(seed, ledger, {'source_ref': 'synthetic-settlement-1', 'effective_date': '2026-10-14', 'description': 'SYNTHETIC adapter fixture',
                        'lines': [line(ledger, role, side, amount) for role, side, amount in EXPECTED_POSTINGS]})
    result = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    verdicts = {c['id']: c['verdict'] for c in result['checks']}
    assert verdicts['P-CLR-02'] == 'FAIL' and verdicts['P-DUP-01'] == 'PASS'
    assert by_role(result)['PROCESSOR_CLEARING'] == '-50000.00' and result['kernel']['outcome'] == 'BLOCKED'


# ------------------------------------------------------------------------------------------- no writes
def test_calculation_writes_nothing(database, app, ledger):
    before = fingerprint(database)
    preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    inspect_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request(), [{'role': 'BANK_OPERATING', 'side': 'D', 'amount': '1.00'}, {'role': 'REVENUE_DEFAULT', 'side': 'C', 'amount': '1.00'}])
    with pytest.raises(AdapterError):
        preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request(net='1.00'))
    assert fingerprint(database) == before
    assert app.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_adapter_transaction_is_read_only(app, ledger, monkeypatch):
    def attempt_write():
        app.execute("UPDATE lv.party SET name = 'changed' WHERE ledger_id = %s", (ledger['ledger'],))
    monkeypatch.setattr(adapter_loader, '_after_counter_read', attempt_write)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())


def test_only_the_restricted_application_login_may_load(database, ledger):
    with connection('lv_seed', database, autocommit=True) as seed_login:
        with pytest.raises(AdapterError, match='WRONG_DATABASE_ROLE'):
            preview_settlement(seed_login, ledger['tenant'], ledger['ledger'], settlement_request())


# ------------------------------------------------------------------------------------------- isolation
def test_other_tenant_and_other_ledger_are_invisible(app, seed, database, ledger, tenants):
    with pytest.raises(AdapterError, match='LEDGER_NOT_FOUND'):
        preview_settlement(app, tenants[1], ledger['ledger'], settlement_request())
    other = provision(app, ledger['tenant'])            # same tenant, separate ledger, complete chart, no journals
    with pytest.raises(AdapterError, match='ITEM_NOT_FOUND'):
        preview_settlement(app, ledger['tenant'], other['ledger'], settlement_request())
    foreign = provision(app, tenants[1]); register(database, tenants[1]); invoice_and_payment(seed, foreign)
    a = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    b = preview_settlement(app, tenants[1], foreign['ledger'], settlement_request())
    assert a['source']['ledger_id'] != b['source']['ledger_id']
    assert {p['account_id'] for p in a['predicted_postings']}.isdisjoint({p['account_id'] for p in b['predicted_postings']})


# ------------------------------------------------------------------------------------------- refusals
def test_incomplete_role_configuration_refused(database, configuration, app, tenants):
    from conftest import account, binding, ledger as make_ledger, period
    with tenant_transaction(configuration, tenants[0]):
        lid = make_ledger(configuration, tenants[0]); aid = account(configuration, tenants[0], lid); binding(configuration, tenants[0], lid, aid)
        period(configuration, tenants[0], lid, '2026-10-01', '2026-11-01')
    with pytest.raises(AdapterError, match='INCOMPLETE_CONFIGURATION'):
        preview_settlement(app, tenants[0], lid, settlement_request())


@pytest.mark.parametrize('change,code', [
    ({'items': [{'source_ref': 'synthetic-payment-1', 'kind': 'REFUND'}]}, 'UNSUPPORTED_ITEM_KIND'),
    ({'gross': 50000}, 'UNSUPPORTED_INPUT_TYPE'),
    ({'fee': 1000.0}, 'UNSUPPORTED_INPUT_TYPE'),
    ({'net': True}, 'UNSUPPORTED_INPUT_TYPE'),
    ({'currency': 'USD'}, 'CURRENCY_MISMATCH'),
    ({'net': '49000.001', 'gross': '50000.001'}, 'MONEY_WIRE_SCALE'),
    ({'fee': '1000.0000'}, 'MONEY_WIRE_SCALE'),
    ({'net': 'NaN'}, 'MONEY_WIRE_FORMAT'),
    ({'net': '49000.01'}, 'SETTLEMENT_TOTALS'),
    ({'gross': '50001.00', 'net': '49001.00'}, 'ITEM_TOTAL_MISMATCH'),
    ({'items': [{'source_ref': 'synthetic-invoice-1', 'kind': 'PAYMENT'}]}, 'ITEM_NOT_A_CLEARING_RECEIPT'),
    ({'items': [{'source_ref': 'no-such-fixture', 'kind': 'PAYMENT'}]}, 'ITEM_NOT_FOUND'),
    ({'items': []}, 'ITEMS_INVALID'),
    ({'date': '2026-12-01'}, 'NO_PERIOD_FOR_DATE'),
    ({'processor': 'processor 1'}, 'IDENTIFIER_INVALID'),
])
def test_unsupported_or_invalid_inputs_are_refused_explicitly(app, ledger, change, code):
    with pytest.raises(AdapterError) as error:
        preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request(**change))
    assert error.value.code == code


def test_unknown_request_field_refused(app, ledger):
    with pytest.raises(AdapterError, match='REQUEST_FIELDS'):
        preview_settlement(app, ledger['tenant'], ledger['ledger'], {**settlement_request(), 'statement': {'gross': '50000.00'}})


def test_closed_period_refused_by_the_kernel(database, app, ledger):
    assert close_period(database, admin_token(database, ledger['tenant']), ledger['period'])['status'] == 200   # audited close (0009)
    with pytest.raises(AdapterError) as error:
        preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert (error.value.source, error.value.code) == ('kernel', 'PERIOD_CLOSED')


@pytest.mark.parametrize('purpose', ['BOOKS', 'PROVIDER_CONNECTED'])
def test_non_sandbox_or_provider_ledgers_unsupported(configuration, app, tenants, purpose):
    cfg = provision(configuration, tenants[0], purpose='BOOKS' if purpose == 'BOOKS' else 'SANDBOX')
    if purpose == 'PROVIDER_CONNECTED':
        with tenant_transaction(configuration, tenants[0]):
            configuration.execute("UPDATE lv.ledger SET source_kind = 'PROVIDER_CONNECTED' WHERE id = %s", (cfg['ledger'],))
    with pytest.raises(AdapterError, match='UNSUPPORTED_LEDGER'):
        preview_settlement(app, tenants[0], cfg['ledger'], settlement_request())


def test_postings_on_unbound_accounts_are_unsupported(configuration, app, seed, ledger):
    with tenant_transaction(configuration, ledger['tenant']):
        extra = configuration.execute("INSERT INTO lv.account(tenant_id,ledger_id,code,name,type,currency,currency_exponent) VALUES(%s,%s,'1200','Synthetic unbound','ASSET','INR',2) RETURNING id",
                            (ledger['tenant'], ledger['ledger'])).fetchone()[0]
    post(seed, ledger, {'source_ref': 'synthetic-unbound', 'effective_date': '2026-10-11', 'description': 'SYNTHETIC',
                        'lines': [{'account_id': str(extra), 'side': 'D', 'amount': '5.00'}, line(ledger, 'BANK_OPERATING', 'C', '5.00')]})
    with pytest.raises(AdapterError, match='UNSUPPORTED_UNBOUND_ACCOUNT_ACTIVITY'):
        preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())


def test_registered_exponent_drives_exactness(database, app, seed, tenants):
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("INSERT INTO lv.currency VALUES('JPY',0) ON CONFLICT DO NOTHING")
    cfg = provision(app, tenants[0], currency='JPY', exponent=0); register(database, tenants[0])
    post(seed, cfg, {'source_ref': 'synthetic-payment-1', 'effective_date': '2026-10-10', 'description': 'SYNTHETIC',
                     'lines': [line(cfg, 'PROCESSOR_CLEARING', 'D', '5000'), line(cfg, 'OPENING_BALANCE_EQUITY', 'C', '5000')]})
    ok = preview_settlement(app, tenants[0], cfg['ledger'], settlement_request(currency='JPY', gross='5000', fee='100', net='4900'))
    assert [(p['role'], p['amount']) for p in ok['predicted_postings']] == [('BANK_OPERATING', '4900'), ('PROCESSOR_FEE_EXPENSE', '100'), ('PROCESSOR_CLEARING', '5000')]
    with pytest.raises(AdapterError, match='MONEY_WIRE_SCALE'):
        preview_settlement(app, tenants[0], cfg['ledger'], settlement_request(currency='JPY', gross='5000.0', fee='100', net='4900.0'))


# ------------------------------------------------------------------------------------------- consistent snapshot
def _concurrent_commit(database, cfg, ref):
    def commit():
        done = {}
        def run():
            with connection('lv_seed', database, autocommit=True) as other:
                done['r'] = post(other, cfg, {'source_ref': ref, 'effective_date': '2026-10-12', 'description': 'SYNTHETIC concurrent',
                                              'lines': [line(cfg, 'BANK_OPERATING', 'D', '7.00'), line(cfg, 'OPENING_BALANCE_EQUITY', 'C', '7.00')]})
        t = threading.Thread(target=run); t.start(); t.join(10)
        assert done['r']['seq'] == '3'
    return commit


def test_snapshot_is_consistent_while_another_session_commits(database, app, ledger, monkeypatch):
    monkeypatch.setattr(adapter_loader, '_after_counter_read', _concurrent_commit(database, ledger, 'synthetic-concurrent'))
    result = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert result['snapshot']['seq'] == '2' and result['snapshot']['entries'] == '2'
    assert by_role(result, 'before') == EXPECTED_BEFORE           # the concurrent INR 7.00 is not half-visible
    monkeypatch.setattr(adapter_loader, '_after_counter_read', None)
    later = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert later['snapshot']['seq'] == '3' and by_role(later, 'before')['BANK_OPERATING'] == '7.00'


def test_weaker_isolation_interleaving_is_detected_not_silently_mixed(database, app, ledger, monkeypatch):
    """Fault experiment kept in the suite: with READ COMMITTED the same concurrent commit lands between
    the counter read and the later reads. The adapter's consistency checks must refuse the mixed state."""
    monkeypatch.setattr(adapter_loader, 'SNAPSHOT_ISOLATION', psycopg.IsolationLevel.READ_COMMITTED)
    monkeypatch.setattr(adapter_loader, 'REQUIRED_ISOLATION_NAME', 'read committed')
    monkeypatch.setattr(adapter_loader, '_after_counter_read', _concurrent_commit(database, ledger, 'synthetic-concurrent-rc'))
    with pytest.raises(AdapterError, match='SNAPSHOT_INCONSISTENT'):
        preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())


def test_loader_refuses_to_run_below_repeatable_read(app, ledger, monkeypatch):
    monkeypatch.setattr(adapter_loader, 'SNAPSHOT_ISOLATION', psycopg.IsolationLevel.READ_COMMITTED)
    with pytest.raises(AdapterError, match='SNAPSHOT_NOT_ISOLATED'):
        preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())


# ------------------------------------------------------------------------------------------- provenance and purity
def test_vendored_kernel_is_byte_identical_to_the_reviewed_package():
    provenance = json.loads((ROOT / 'docs/KERNEL_PROVENANCE.json').read_text())
    assert provenance['accounting_semantics'] == 'UNREVIEWED' and provenance['version'] == '0.1.0-review'
    for path, meta in provenance['files'].items():
        data = (ROOT / path).read_bytes()
        assert (len(data), hashlib.sha256(data).hexdigest()) == (meta['bytes'], meta['sha256']), path
    assert {p.name for p in (ROOT / 'src/ledger_kernel').glob('*.py')} == {Path(p).name for p in provenance['files']}


def test_calculation_code_has_no_database_or_network_access():
    forbidden = ('psycopg', 'sqlalchemy', 'socket', 'http', 'urllib', 'requests', 'httpx', 'fastapi', 'ledgervigil.db', 'tools')
    for path in [*(ROOT / 'src/ledger_kernel').glob('*.py'), ROOT / 'src/ledgervigil/kernel_adapter/calculation.py', ROOT / 'src/ledgervigil/kernel_adapter/types.py']:
        imported = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [m for m in imported if m.startswith(forbidden)], path.name
    assert 'seed_manual_journal' not in (ROOT / 'src/ledgervigil/kernel_adapter/loader.py').read_text()
