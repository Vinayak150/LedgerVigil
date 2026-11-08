"""Synthetic processor-statement ingestion (Stage 2A §19.2 path A) and adapter evidence checks (§19.3).

Expected outcomes are stated from the requirements, independently of the implementation:
- path A writes only source_file, source_record, staged_document and import_batch, never financial state;
- source_file.sha256 is unique per ledger, and source_record (ledger, system, key, content hash) is unique;
- P-STL-01: gross, fee, net and the item list must equal the statement's;
- P-FEE-01 and DC-18: the recorded fee equals statement gross minus net;
- imported text is untrusted.
For the INR example (gross 50,000.00, fee 1,000.00, net 49,000.00) a correct statement agrees on currency,
payout identity and all three amounts. Item identity stays UNRESOLVED because typed Payment documents do
not exist. Duplicate settlement stays NOT_ESTABLISHED. The overall outcome is BLOCKED. All data is SYNTHETIC.
"""
import ast
import hashlib
import json
import threading
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tools.dev_db import connection
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ingestion import statement_format as fmt
from ledgervigil.ingestion.store import IngestionRefused, ingest_statement
from ledgervigil.kernel_adapter import preview_settlement
from ledgervigil.kernel_adapter import loader as adapter_loader
from test_posting import provision, register
from test_settlement_adapter import invoice_and_payment, settlement_request

ROOT = Path(__file__).resolve().parents[1]
FINANCIAL = ('ledger', 'account', 'account_role_binding', 'party', 'fiscal_period', 'currency', 'service_principal', 'ledger_counter',
             'commit_batch', 'document', 'document_version', 'document_current', 'journal_entry', 'journal_line', 'ledger_event',
             'account_balance_current', 'party_balance_current')
INGESTION = ('source_file', 'import_batch', 'source_record', 'staged_document', 'staged_statement_item')
CORRECT_VERDICTS = {'E-EVD-01': 'PASS', 'E-SRC-01': 'PASS', 'E-CUR-01': 'PASS', 'E-PAY-01': 'PASS', 'E-AMT-GROSS': 'PASS',
                    'E-AMT-FEE': 'PASS', 'E-AMT-NET': 'PASS', 'E-FEE-POST': 'PASS', 'E-ITEM-01': 'UNRESOLVED', 'E-DUP-01': 'NOT_ESTABLISHED'}


def statement(cfg, envelope=None, **fields):
    body = {'tenant': str(cfg['tenant']), 'ledger': str(cfg['ledger']), 'processor': 'processor-1', 'payout': 'STL-3001',
            'date': '2026-10-14', 'currency': 'INR', 'items': [{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '50000.00'}],
            'gross': '50000.00', 'fee': '1000.00', 'net': '49000.00', 'memo': 'SYNTHETIC fixture statement; not a real processor file'}
    body.update(fields)
    doc = {'format': fmt.FORMAT, 'version': fmt.VERSION, 'data_classification': 'SYNTHETIC', 'statement': body}
    doc.update(envelope or {})
    return json.dumps(doc, ensure_ascii=False).encode()


def ingest(database, cfg, raw, *, tenant=None, declared=(fmt.FORMAT, fmt.VERSION)):
    with connection(database=database) as db:head=db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()[0]
    writer=ingest_statement
    if head not in ('0021_native_execution','0022_approved_withdrawal','0023_configuration_boundary','0024_proof_export_authority','0025_export_jobs','0026_proof_packs','0027_account_lock_order','0028_workflow_lock_order','0029_account_patch','0030_override_independence','0031_semantic_revalidation','0032_detection_findings','0033_detection_overlap_dates'):
        from legacy_ingestion_0020 import ingest_statement as writer
    with connection('lv_ingest', database, autocommit=True) as c:
        return writer(c, tenant or cfg['tenant'], cfg['ledger'], raw, declared_format=declared[0], declared_version=declared[1])


def table_hashes(database, tables):
    with connection(database=database) as admin:
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(t))).fetchall()))).encode()).hexdigest()
                for t in tables}


def verdicts(result):
    return {c['id']: c['verdict'] for c in result['evidence_checks']}


@pytest.fixture
def seed(database):
    with connection('lv_seed', database, autocommit=True) as c:
        yield c


@pytest.fixture
def ledger(app, seed, tenants, database):
    cfg = provision(app, tenants[0]); register(database, tenants[0]); invoice_and_payment(seed, cfg)
    return cfg


# ------------------------------------------------------------------------------------------- correct evidence
def test_correct_statement_is_preserved_staged_and_agrees(database, app, ledger):
    raw = statement(ledger)
    result = ingest(database, ledger, raw)
    assert result['status'] == 'STAGED' and result['failure_code'] is None
    assert result['sha256'] == hashlib.sha256(raw).hexdigest() and result['byte_size'] == str(len(raw))
    with connection(database=database) as admin:
        assert bytes(admin.execute('SELECT content FROM lv.source_file WHERE id = %s', (result['source_file_id'],)).fetchone()[0]) == raw
    outcome = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert verdicts(outcome) == CORRECT_VERDICTS
    assert outcome['outcome'] == 'BLOCKED'
    assert {'E-ITEM-01: UNRESOLVED', 'E-DUP-01: NOT_ESTABLISHED', 'P-DUP-01: PASS'} & set(outcome['blocking_reasons']) == {'E-ITEM-01: UNRESOLVED', 'E-DUP-01: NOT_ESTABLISHED'}
    for check in outcome['evidence_checks']:
        if check['id'] == 'E-DUP-01':   # 0006: derived from authoritative ledger state at the snapshot, not from the statement
            assert check['trace']['snapshot_seq'] == outcome['snapshot']['seq']
            continue
        assert check['trace']['source_file_sha256'] == result['sha256'] and check['trace']['staged_document_id'] == result['staged_document_id']
    obs = outcome['statement_observation']
    assert obs['label'].startswith('OBSERVATION') and (obs['gross'], obs['fee'], obs['net'], obs['currency']) == ('50000.00', '1000.00', '49000.00', 'INR')
    assert obs['items'] == [['PMT-2001', 'PAYMENT', '50000.00']]
    assert outcome['labels']['statement'] == 'STAGED_SYNTHETIC_OBSERVATION'
    # Ledger facts (balances from postings) are reported separately from the observation and are unchanged by it.
    assert {b['role']: b['before'] for b in outcome['balances']}['PROCESSOR_CLEARING'] == '50000.00'


def test_without_ingested_statement_evidence_is_insufficient(app, ledger):
    outcome = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert set(verdicts(outcome).values()) == {'INSUFFICIENT_EVIDENCE', 'NOT_ESTABLISHED'} and outcome['outcome'] == 'BLOCKED'


# ------------------------------------------------------------------------------------------- disagreement
@pytest.mark.parametrize('fields,expected', [
    pytest.param({'fee': '1200.00', 'net': '48800.00'}, {'E-AMT-GROSS': 'PASS', 'E-AMT-FEE': 'FAIL', 'E-AMT-NET': 'FAIL', 'E-FEE-POST': 'FAIL', 'E-ITEM-01': 'UNRESOLVED'}, id='wrong-fee-and-net'),
    pytest.param({'items': [{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '49500.00'}], 'gross': '49500.00', 'fee': '500.00'},
                 {'E-AMT-GROSS': 'FAIL', 'E-AMT-FEE': 'FAIL', 'E-AMT-NET': 'PASS', 'E-FEE-POST': 'FAIL', 'E-ITEM-01': 'FAIL'}, id='wrong-item-amount'),
    pytest.param({'items': [{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '25000.00'}, {'id': 'PMT-2002', 'kind': 'PAYMENT', 'amount': '25000.00'}]},
                 {'E-AMT-GROSS': 'PASS', 'E-AMT-FEE': 'PASS', 'E-AMT-NET': 'PASS', 'E-ITEM-01': 'FAIL'}, id='different-item-list'),
    pytest.param({'date': '2026-10-15'}, {'E-PAY-01': 'FAIL', 'E-AMT-GROSS': 'PASS'}, id='payout-date'),
])
def test_statement_disagreement_is_reported(database, app, ledger, fields, expected):
    assert ingest(database, ledger, statement(ledger, **fields))['status'] == 'STAGED'
    outcome = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert {k: verdicts(outcome)[k] for k in expected} == expected
    assert outcome['outcome'] == 'BLOCKED'


def test_statement_in_another_currency_is_rejected_at_ingestion(database, app, ledger):
    result = ingest(database, ledger, statement(ledger, currency='USD'))
    assert (result['status'], result['failure_code']) == ('REJECTED', 'CURRENCY_MISMATCH')
    assert verdicts(preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request()))['E-CUR-01'] == 'INSUFFICIENT_EVIDENCE'


# ------------------------------------------------------------------------------------------- validation failures
def _with_items(cfg, items, **kw):
    return statement(cfg, items=items, **kw)


REJECTIONS = [
    ('no-items', lambda c: _with_items(c, []), 'STATEMENT_ITEMS'),
    ('missing-items-field', lambda c: json.dumps({'format': fmt.FORMAT, 'version': '1', 'data_classification': 'SYNTHETIC',
                                                   'statement': {k: v for k, v in json.loads(statement(c))['statement'].items() if k != 'items'}}).encode(), 'STATEMENT_FIELDS'),
    ('repeated-item', lambda c: _with_items(c, [{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '25000.00'}] * 2), 'STATEMENT_ITEM_DUPLICATE'),
    ('item-total', lambda c: _with_items(c, [{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '49500.00'}]), 'ITEM_TOTAL_MISMATCH'),
    ('totals', lambda c: statement(c, net='48000.00'), 'STATEMENT_TOTALS'),
    ('excess-precision-gross', lambda c: statement(c, gross='50000.001'), 'MONEY_WIRE_SCALE'),
    ('trailing-scale-fee', lambda c: statement(c, fee='1000.0000', net='49000.0000'), 'MONEY_WIRE_SCALE'),
    ('excess-precision-item', lambda c: _with_items(c, [{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '50000.001'}]), 'MONEY_WIRE_SCALE'),
    ('exponent-notation', lambda c: statement(c, gross='5e4'), 'MONEY_WIRE_FORMAT'),
    ('json-number', lambda c: statement(c).replace(b'"fee": "1000.00"', b'"fee": 1000.00'), 'JSON_NUMBERS_NOT_ALLOWED'),
    ('malformed-json', lambda c: statement(c)[:-5], 'INVALID_JSON'),
    ('not-utf8', lambda c: statement(c).replace(b'SYNTHETIC fixture', b'SYNTHETIC \xff fixture'), 'INVALID_JSON'),
    ('duplicate-key', lambda c: statement(c).replace(b'"gross": "50000.00"', b'"gross": "50000.00", "gross": "1.00"'), 'DUPLICATE_JSON_KEY'),
    ('envelope-fields', lambda c: statement(c, envelope={'extra': 'x'}), 'ENVELOPE_FIELDS'),
    ('file-version', lambda c: statement(c, envelope={'version': '2'}), 'FORMAT_VERSION_MISMATCH'),
    ('not-synthetic', lambda c: statement(c, envelope={'data_classification': 'REAL'}), 'NOT_SYNTHETIC'),
    ('scope', lambda c: statement(c, ledger='00000000-0000-7000-8000-000000000000'), 'SCOPE_MISMATCH'),
    ('bad-date', lambda c: statement(c, date='2026-02-30'), 'DATE_INVALID'),
    ('bad-identifier', lambda c: statement(c, payout='STL 3001'), 'IDENTIFIER_INVALID'),
    ('refund-exceeds', lambda c: _with_items(c, [{'id': 'R-1', 'kind': 'REFUND', 'amount': '50000.00'}]), 'ITEM_TOTAL_MISMATCH'),
]


@pytest.mark.parametrize('make,code', [pytest.param(m, c, id=i) for i, m, c in REJECTIONS])
def test_invalid_files_are_recorded_with_a_specific_failure(database, app, ledger, make, code):
    raw = make(ledger)
    result = ingest(database, ledger, raw)
    assert (result['status'], result['failure_code'], result['staged_document_id']) == ('REJECTED', code, None)
    with connection(database=database) as admin:
        stored = admin.execute('SELECT content, sha256 FROM lv.source_file WHERE id = %s', (result['source_file_id'],)).fetchone()
    assert bytes(stored[0]) == raw and stored[1] == hashlib.sha256(raw).hexdigest()
    assert verdicts(preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request()))['E-EVD-01'] == 'INSUFFICIENT_EVIDENCE'


def test_oversized_file_is_recorded_by_hash_only_and_rejected(database, ledger):
    raw = statement(ledger) + b' ' * (fmt.MAX_FILE_BYTES + 1)
    result = ingest(database, ledger, raw)
    assert (result['status'], result['failure_code']) == ('REJECTED', 'FILE_TOO_LARGE')
    with connection(database=database) as admin:
        assert admin.execute('SELECT content IS NULL, byte_size, sha256 FROM lv.source_file WHERE id = %s', (result['source_file_id'],)).fetchone() \
            == (True, len(raw), hashlib.sha256(raw).hexdigest())


def test_declared_format_must_be_the_supported_version(database, ledger):
    result = ingest(database, ledger, statement(ledger, envelope={'version': '2'}), declared=(fmt.FORMAT, '2'))
    assert (result['status'], result['failure_code']) == ('REJECTED', 'UNSUPPORTED_DECLARED_FORMAT')


# ------------------------------------------------------------------------------------------- repeats and conflicts
def test_repeated_file_is_deterministic_and_writes_nothing(database, ledger):
    raw = statement(ledger)
    first = ingest(database, ledger, raw)
    before = table_hashes(database, INGESTION)
    again = ingest(database, ledger, raw)
    assert again['repeated_file'] and {**again, 'repeated_file': False} == first
    assert table_hashes(database, INGESTION) == before


def test_same_statement_in_different_bytes_is_a_duplicate_not_new_evidence(database, app, ledger):
    first = ingest(database, ledger, statement(ledger))
    reformatted = json.dumps(json.loads(statement(ledger)), indent=2, ensure_ascii=False).encode()
    second = ingest(database, ledger, reformatted)
    assert (second['status'], second['failure_code'], second['related_staged_document_id']) == ('DUPLICATE', 'DUPLICATE_STATEMENT', first['staged_document_id'])
    with tenant_transaction(app, ledger['tenant']):
        assert app.execute('SELECT count(*) FROM lv.staged_document WHERE ledger_id = %s', (ledger['ledger'],)).fetchone() == (1,)
    assert verdicts(preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request()))['E-EVD-01'] == 'PASS'


def test_conflicting_payout_identity_is_rejected_and_makes_evidence_ambiguous(database, app, ledger):
    first = ingest(database, ledger, statement(ledger))
    conflict = ingest(database, ledger, statement(ledger, fee='1200.00', net='48800.00'))
    assert (conflict['status'], conflict['failure_code'], conflict['related_staged_document_id']) == ('REJECTED', 'PAYOUT_IDENTITY_CONFLICT', first['staged_document_id'])
    outcome = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    check = next(c for c in outcome['evidence_checks'] if c['id'] == 'E-EVD-01')
    assert check['verdict'] == 'FAIL' and check['values']['conflicting_files'] == [conflict['sha256']]
    assert outcome['outcome'] == 'BLOCKED'


def test_concurrent_ingestion_of_the_same_file_converges(database, ledger):
    raw = statement(ledger); barrier = threading.Barrier(2); results = []
    def run():
        with connection('lv_ingest', database, autocommit=True) as c:
            barrier.wait(5)
            results.append(ingest_statement(c, ledger['tenant'], ledger['ledger'], raw, declared_format=fmt.FORMAT, declared_version=fmt.VERSION))
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(15)
    assert len(results) == 2 and len({r['import_batch_id'] for r in results}) == 1 and sorted(r['repeated_file'] for r in results) == [False, True]


# ------------------------------------------------------------------------------------------- scope
def test_cross_tenant_and_cross_ledger_isolation(database, app, seed, ledger, tenants):
    with pytest.raises(IngestionRefused, match='LEDGER_NOT_FOUND'):
        ingest(database, ledger, statement(ledger), tenant=tenants[1])
    staged = ingest(database, ledger, statement(ledger))
    other = provision(app, ledger['tenant']); invoice_and_payment(seed, other)          # same tenant, other ledger
    assert verdicts(preview_settlement(app, other['tenant'], other['ledger'], settlement_request()))['E-EVD-01'] == 'INSUFFICIENT_EVIDENCE'
    moved = ingest(database, other, statement(ledger))                                   # same bytes, other ledger
    assert (moved['status'], moved['failure_code']) == ('REJECTED', 'SCOPE_MISMATCH') and moved['source_file_id'] != staged['source_file_id']
    for role in ('lv_app', 'lv_ingest'):
        with connection(role, database) as c:
            with tenant_transaction(c, tenants[1]):
                for table in INGESTION:
                    assert c.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,), (role, table)
            for table in INGESTION:                                                      # no context: nothing visible
                assert c.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,), (role, table)


@pytest.mark.parametrize('purpose', ['BOOKS', 'PROVIDER_CONNECTED'])
def test_only_native_sandbox_ledgers_accept_synthetic_statements(database, app, tenants, purpose):
    cfg = provision(app, tenants[0], purpose='BOOKS' if purpose == 'BOOKS' else 'SANDBOX')
    if purpose == 'PROVIDER_CONNECTED':
        from configuration_support import configuration_connection
        with configuration_connection(database) as setup,tenant_transaction(setup, tenants[0]):
            setup.execute("UPDATE lv.ledger SET source_kind = 'PROVIDER_CONNECTED' WHERE id = %s", (cfg['ledger'],))
    with pytest.raises(psycopg.errors.CheckViolation, match='native SANDBOX'):
        ingest(database, cfg, statement(cfg))
    with tenant_transaction(app, tenants[0]):
        assert app.execute('SELECT count(*) FROM lv.source_file WHERE ledger_id = %s', (cfg['ledger'],)).fetchone() == (0,)


# ------------------------------------------------------------------------------------------- privileges and integrity
def test_ingest_login_cannot_touch_financial_state(database, ledger):
    with connection(database=database) as admin:
        for table in FINANCIAL:
            for privilege in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                assert admin.execute('SELECT has_table_privilege(%s, %s, %s)', ('lv_ingest', 'lv.' + table, privilege)).fetchone() == (False,), (table, privilege)
        for table in INGESTION:
            assert admin.execute("SELECT has_table_privilege('lv_ingest', %s, 'INSERT'), has_table_privilege('lv_ingest', %s, 'UPDATE'), "
                                 "has_table_privilege('lv_ingest', %s, 'DELETE'), has_table_privilege('lv_app', %s, 'INSERT')",
                                 ('lv.' + table,) * 4).fetchone() == (True, False, False, False), table
        assert admin.execute("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'lv' "
                             "AND has_function_privilege('lv_ingest', p.oid, 'EXECUTE') ORDER BY p.proname").fetchall() == [('ingestion_ledger',)]
        assert admin.execute("SELECT has_database_privilege('lv_ingest', current_database(), 'TEMPORARY'), has_schema_privilege('lv_ingest', 'lv', 'CREATE'), "
                             "rolsuper, rolbypassrls, rolinherit FROM pg_roles WHERE rolname = 'lv_ingest'").fetchone() == (False, False, False, False, False)
    with connection('lv_ingest', database) as c:
        for statement_sql in ("UPDATE lv.ledger_counter SET last_seq = last_seq", "SELECT lv.seed_manual_journal(%s, '{}'::jsonb)",
                              "DELETE FROM lv.source_file", "UPDATE lv.staged_document SET fee = '0.00'"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with tenant_transaction(c, ledger['tenant']):
                    c.execute(statement_sql, (ledger['ledger'],) if '%s' in statement_sql else None)


def test_ingested_rows_are_immutable_and_hashes_are_enforced(database, ledger):
    ingest(database, ledger, statement(ledger))
    with connection('lv_migrator', database) as owner:
        owner.execute('SET ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, false)", (str(ledger['tenant']),)); owner.commit()
        for statement_sql in ("UPDATE lv.staged_document SET fee = '0.00'", 'DELETE FROM lv.source_record', "UPDATE lv.source_file SET received_by = 'x'"):
            with pytest.raises(psycopg.errors.CheckViolation, match='immutable'):
                with owner.transaction():
                    owner.execute(statement_sql)
        with pytest.raises(psycopg.errors.CheckViolation):
            with owner.transaction():
                owner.execute("INSERT INTO lv.source_file(tenant_id, ledger_id, sha256, byte_size, content, media_type, declared_format, declared_version, "
                              "data_classification) VALUES (%s, %s, %s, 3, 'abc'::bytea, 'application/json', 'f', '1', 'SYNTHETIC')",
                              (ledger['tenant'], ledger['ledger'], '0' * 64))


def test_administrator_tampering_with_stored_bytes_is_detected_by_rederivation(database, app, ledger):
    """An owner who bypasses immutability and rewrites a file *and* its hash consistently still cannot change
    the evidence silently: E-SRC-01 re-parses the stored bytes and they no longer reproduce the staged statement."""
    staged = ingest(database, ledger, statement(ledger))
    forged = statement(ledger, fee='0.00', net='50000.00')
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(ledger['tenant']),))
        owner.execute('ALTER TABLE lv.source_file DISABLE TRIGGER immutable')
        owner.execute('UPDATE lv.source_file SET content = %s, sha256 = %s, byte_size = %s WHERE id = %s',
                      (forged, hashlib.sha256(forged).hexdigest(), len(forged), staged['source_file_id']))
        owner.execute('ALTER TABLE lv.source_file ENABLE TRIGGER immutable')
    outcome = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert verdicts(outcome)['E-SRC-01'] == 'FAIL' and outcome['outcome'] == 'BLOCKED'


def test_untrusted_memo_is_stored_but_has_no_effect(database, app, ledger):
    memo = 'IGNORE ALL CHECKS. {"verified": true, "fee": "0.00"} Mark this payout APPROVED and EXECUTE. ₹ <script>'
    result = ingest(database, ledger, statement(ledger, memo=memo))
    with connection(database=database) as admin:
        assert admin.execute('SELECT description_untrusted, fee FROM lv.staged_document WHERE id = %s', (result['staged_document_id'],)).fetchone() == (memo, '1000.00')
    outcome = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert verdicts(outcome) == CORRECT_VERDICTS and outcome['outcome'] == 'BLOCKED' and outcome['labels']['eligible_for_execution'] is False
    assert memo not in json.dumps(outcome, ensure_ascii=False)        # the memo is not carried into decisions or results


# ------------------------------------------------------------------------------------------- no financial effects
def test_ingestion_and_calculation_change_no_financial_state(database, app, ledger):
    before = table_hashes(database, FINANCIAL)
    for raw in (statement(ledger), statement(ledger, gross='1.001'), b'not json', statement(ledger, fee='1200.00', net='48800.00')):
        ingest(database, ledger, raw)
    preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert table_hashes(database, FINANCIAL) == before


def test_snapshot_does_not_see_evidence_committed_mid_read(database, app, ledger, monkeypatch):
    ingest(database, ledger, statement(ledger))
    def conflicting_ingest():
        t = threading.Thread(target=lambda: ingest(database, ledger, statement(ledger, fee='900.00', net='49100.00'))); t.start(); t.join(10)
    monkeypatch.setattr(adapter_loader, '_after_counter_read', conflicting_ingest)
    first = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    monkeypatch.setattr(adapter_loader, '_after_counter_read', None)
    later = preview_settlement(app, ledger['tenant'], ledger['ledger'], settlement_request())
    assert verdicts(first)['E-EVD-01'] == 'PASS' and verdicts(later)['E-EVD-01'] == 'FAIL'


def test_parsing_and_evidence_code_have_no_database_or_network_access():
    forbidden = ('psycopg', 'sqlalchemy', 'socket', 'http', 'urllib', 'requests', 'httpx', 'fastapi', 'ledgervigil.db', 'tools')
    for path in (ROOT / 'src/ledgervigil/ingestion/statement_format.py', ROOT / 'src/ledgervigil/kernel_adapter/evidence.py'):
        imported = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [m for m in imported if m.startswith(forbidden)], path.name
    for path in (ROOT / 'src/ledgervigil/ingestion').glob('*.py'):
        text = path.read_text()
        assert not any(t in text for t in ('lv.journal_', 'lv.commit_batch', 'lv.document ', 'lv.document(', 'lv.ledger_counter', 'seed_manual_journal')), path.name


# ------------------------------------------------------------------------------------------- migration
def test_upgrade_from_0003_preserves_records_then_ingests():
    from uuid import uuid4, uuid7
    from tools.dev_db import migrate, prepare_database
    name = 'lv_migration_test_' + uuid4().hex
    with connection(autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        prepare_database(name); migrate(name, '0003_pin_function_search_path')
        tenant = uuid7()
        with connection('lv_migrator', name) as c:
            c.execute('SET LOCAL ROLE lv_owner'); c.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
            c.execute("INSERT INTO lv.tenant(tenant_id, name) VALUES (%s, 'Synthetic upgrade tenant')", (tenant,))
        with connection('lv_app', name, autocommit=True) as app_:
            cfg = provision(app_, tenant)
        register(name, tenant)
        with connection('lv_seed', name, autocommit=True) as s:
            invoice_and_payment(s, cfg)
        tables = FINANCIAL + ('tenant',)
        before = table_hashes(name, tables)
        # Scope: the 0004+ chain up to 0008. 0009 adds period/commit columns; its populated upgrade is tested in
        # test_period_close.py by comparing the pre-existing columns.
        migrate(name, '0008_identity_force_rls')
        with connection(database=name) as c:
            assert c.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0008_identity_force_rls',)
        assert table_hashes(name, tables) == before
        assert ingest(name, cfg, statement(cfg))['status'] == 'STAGED'
        with connection('lv_app', name, autocommit=True) as app_:
            assert verdicts(preview_settlement(app_, tenant, cfg['ledger'], settlement_request())) == CORRECT_VERDICTS
        migrate(name, '0003_pin_function_search_path', downgrade=True, disposable=True)
        assert table_hashes(name, tables) == before
        with connection(database=name) as c:
            assert c.execute("SELECT to_regclass('lv.source_file')").fetchone() == (None,)
        migrate(name)
    finally:
        with connection(autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
