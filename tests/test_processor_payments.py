"""Typed synthetic processor Payment documents (migration 0005) and identity-based statement-item matching.

Expected outcomes are stated from the owning requirements, independently of the implementation:
- Stage 2A §5.1 (sp1-posting/1.0): a processor payment posts Dr PROCESSOR_CLEARING / Cr AR_CONTROL for the customer,
  for the payment amount, applied or unapplied alike. §5.2: a new Payment is RECEIVED with unapplied = amount.
- §11.3 payment_current; §11.2 one document per (ledger, doc_type, source_ref) with immutable versions;
  §4.1 DOCUMENT_POSTING names the exact document version and posting rule (I-15, I-16).
- §19.2 path D / ADR-024/025: developer seeding only for NATIVE SANDBOX ledgers; the actor is the seed principal.
- Identity = (tenant, ledger, processor_ref, processor_item_id). An identical repeat replays; different content is
  refused with no effects. Identity is never inferred from amounts, dates or text.
- §19.3 / 2B RecordSettlementOp: settlement items are payment document ids sharing the payout's processor_ref.
For the INR example, payment PMT-2001 of 50,000.00 against statement STL-3001 (gross 50,000.00, fee 1,000.00, net
49,000.00) matches on identity, currency and amount. Duplicate settlement stays NOT_ESTABLISHED, execution stays
NOT_AVAILABLE, so the outcome is BLOCKED. All data is SYNTHETIC.
"""
import contextlib
import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4, uuid7

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tools.dev_db import connection, migrate, prepare_database
from period_close_support import admin_token, close_period, close_call, held_close   # 0009: audited close replaces the R-02 UPDATE
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.kernel_adapter import AdapterError, preview_settlement
from ledgervigil.kernel_adapter.calculation import calculate_settlement, validate_request
from ledgervigil.kernel_adapter.loader import load_snapshot
from ledgervigil.ledger.replay import replay_events
from ledgervigil.ledger.seed import seed_manual_journal, seed_processor_payment
from test_posting import events, line, provision, register
from test_statement_ingestion import CORRECT_VERDICTS, FINANCIAL as FINANCIAL_0004, INGESTION, ingest, statement, table_hashes, verdicts

PAYMENT_TABLES = ('processor_payment_identity', 'payment_current')
HISTORY = ('commit_batch', 'document', 'document_version', 'document_current', 'journal_entry', 'journal_line', 'ledger_event',
           'processor_payment_identity')
ALL_TABLES = FINANCIAL_0004 + INGESTION + PAYMENT_TABLES + ('tenant',)
OPENING_LABEL = ('SYNTHETIC invoice-like opening state recorded as a MANUAL JOURNAL; not an Invoice document or workflow, '
                 'and the payment is not applied to it')
# Independently stated expectations (OC-01 with a typed payment in place of the manual payment journal).
EXPECTED_POSTINGS = [('BANK_OPERATING', 'D', '49000.00'), ('PROCESSOR_FEE_EXPENSE', 'D', '1000.00'), ('PROCESSOR_CLEARING', 'C', '50000.00')]
EXPECTED_AFTER = {'BANK_OPERATING': '49000.00', 'AR_CONTROL': '0.00', 'PROCESSOR_CLEARING': '0.00', 'OPENING_BALANCE_EQUITY': '0.00',
                  'REVENUE_DEFAULT': '-50000.00', 'SALES_RETURNS': '0.00', 'PROCESSOR_FEE_EXPENSE': '1000.00'}
EXPECTED_BEFORE = {**EXPECTED_AFTER, 'BANK_OPERATING': '0.00', 'PROCESSOR_FEE_EXPENSE': '0.00', 'PROCESSOR_CLEARING': '50000.00'}
ALL_PASS = {'E-EVD-01': 'PASS', 'E-SRC-01': 'PASS', 'E-CUR-01': 'PASS', 'E-PAY-01': 'PASS', 'E-AMT-GROSS': 'PASS', 'E-AMT-FEE': 'PASS',
            'E-AMT-NET': 'PASS', 'E-FEE-POST': 'PASS', 'E-ITEM-01': 'PASS', 'E-DUP-01': 'PASS'}   # E-DUP-01 PASS since 0006 (preview scope)
EXPECTED_BLOCKING = {'P-CLR-01: INSUFFICIENT_EVIDENCE', 'P-EVD-01: INSUFFICIENT_EVIDENCE', 'P-FEE-01: INSUFFICIENT_EVIDENCE',
                     'P-STL-01: INSUFFICIENT_EVIDENCE', 'X-EXEC-01: NOT_AVAILABLE'}


def payment(cfg, **overrides):
    body = {'processor_ref': 'processor-1', 'processor_item_id': 'PMT-2001', 'party_id': cfg['party'], 'currency': 'INR',
            'amount': '50000.00', 'received_date': '2026-10-10', 'description': 'SYNTHETIC typed processor payment fixture'}
    body.update(overrides)
    return body


def pay(seed, cfg, body=None):
    return seed_processor_payment(seed, cfg['tenant'], cfg['ledger'], body or payment(cfg))


def opening(seed, cfg, amount='50000.00'):
    """Invoice-like opening state as a labelled MANUAL_JOURNAL (Dr AR customer / Cr revenue)."""
    return seed_manual_journal(seed, cfg['tenant'], cfg['ledger'], {
        'source_ref': 'synthetic-opening-1', 'effective_date': '2026-10-05', 'description': OPENING_LABEL,
        'lines': [line(cfg, 'AR_CONTROL', 'D', amount, cfg['party']), line(cfg, 'REVENUE_DEFAULT', 'C', amount)]})


def request(document_ids, **overrides):
    body = {'processor': 'processor-1', 'payout': 'STL-3001', 'date': '2026-10-14', 'currency': 'INR', 'gross': '50000.00',
            'fee': '1000.00', 'net': '49000.00', 'items': [{'document_id': d, 'kind': 'PAYMENT'} for d in document_ids]}
    body.update(overrides)
    return body


def counts(app, cfg):
    with tenant_transaction(app, cfg['tenant']):
        values = {t: app.execute(sql.SQL('SELECT count(*) FROM lv.{} WHERE ledger_id = %s').format(sql.Identifier(t)), (cfg['ledger'],)).fetchone()[0]
                  for t in HISTORY + ('payment_current', 'account_balance_current', 'party_balance_current')}
        values['counter'] = app.execute('SELECT last_seq, last_entry_no, batch_head, event_head FROM lv.ledger_counter WHERE ledger_id = %s',
                                        (cfg['ledger'],)).fetchall()
    return values


def by_role(result, key='after'):
    return {b['role']: b[key] for b in result['balances']}


def item_check(result):
    return next(c for c in result['evidence_checks'] if c['id'] == 'E-ITEM-01')


@contextlib.contextmanager
def disposable(revision=None):
    name = 'lv_migration_test_' + uuid4().hex
    with connection(autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        prepare_database(name); migrate(name, revision) if revision else migrate(name)
        yield name
    finally:
        with connection(autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def rows(database, table, only=None):
    with connection(database=database) as admin:
        found = {repr(r) for r in admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(table))).fetchall()}
    return found if only is None else found & only


def new_tenant(database):
    tenant = uuid7()
    with connection('lv_migrator', database) as c:
        c.execute('SET LOCAL ROLE lv_owner'); c.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
        c.execute("INSERT INTO lv.tenant(tenant_id, name) VALUES (%s, 'Synthetic test tenant')", (tenant,))
    return tenant


@pytest.fixture
def seed(database):
    with connection('lv_seed', database, autocommit=True) as c:
        yield c


@pytest.fixture
def cfg(app, tenants, database):
    config = provision(app, tenants[0]); register(database, tenants[0])
    return config


# ============================================================================== 1. atomic typed Payment creation
def test_payment_version_identity_projection_and_postings_are_created_together(database, app, seed, cfg):
    principal = None
    with connection(database=database) as admin:
        principal = str(admin.execute("SELECT id FROM lv.service_principal WHERE tenant_id = %s AND database_role = 'lv_seed'", (cfg['tenant'],)).fetchone()[0])
    result = pay(seed, cfg)
    assert result['seq'] == '1' and result['replayed'] is False
    doc = result['document_id']
    with tenant_transaction(app, cfg['tenant']):
        q = lambda text, *args: app.execute(text, args).fetchall()  # noqa: E731
        assert q('SELECT doc_type::text, source_ref, seq FROM lv.document WHERE id = %s', doc) == [('PAYMENT', 'processor-1/PMT-2001', 1)]
        [(version, origin, provenance, created_by, financial, original, reason)] = q(
            'SELECT version, origin::text, provenance::text, created_by::text, financial_fields, original_request, reason FROM lv.document_version WHERE document_id = %s', doc)
        assert (version, origin, provenance, created_by) == (1, 'SEED', 'SYNTHETIC', principal)
        assert financial == {'document_type': 'PAYMENT', 'method': 'PROCESSOR', 'processor_ref': 'processor-1', 'processor_item_id': 'PMT-2001',
                             'party_id': cfg['party'], 'amount': '50000.00', 'currency': 'INR', 'received_date': '2026-10-10'}
        assert original == payment(cfg) and 'Synthetic' in reason
        assert q('SELECT version FROM lv.document_current WHERE document_id = %s', doc) == [(1,)]
        assert q('SELECT processor_ref, processor_item_id, seq FROM lv.processor_payment_identity WHERE document_id = %s', doc) == [('processor-1', 'PMT-2001', 1)]
        assert q('SELECT version, party_id::text, currency, amount, applied, unapplied, status FROM lv.payment_current WHERE document_id = %s', doc) == \
            [(1, cfg['party'], 'INR', Decimal('50000.00'), Decimal(0), Decimal('50000.00'), 'RECEIVED')]
        [(entry_id, kind, rule, rule_version, doc_version, entry_prov, entry_by)] = q(
            'SELECT id::text, kind::text, posting_rule_id, posting_rule_version, source_document_version, provenance::text, created_by::text '
            'FROM lv.journal_entry WHERE source_document_id = %s', doc)
        assert (entry_id, kind, rule, rule_version, doc_version, entry_prov, entry_by) == (result['entry_id'], 'DOCUMENT_POSTING', 'sp1-posting', '1.0', 1, 'SYNTHETIC', principal)
        assert q('SELECT line_no, account_id::text, side::text, amount, party_id::text, document_id::text FROM lv.journal_line WHERE entry_id = %s ORDER BY line_no', entry_id) == [
            (1, cfg['chart']['PROCESSOR_CLEARING'], 'D', Decimal('50000.00'), None, doc),
            (2, cfg['chart']['AR_CONTROL'], 'C', Decimal('50000.00'), cfg['party'], doc)]
        assert sorted(q('SELECT account_id::text, debit_total, credit_total FROM lv.account_balance_current WHERE ledger_id = %s', cfg['ledger'])) == sorted([
            (cfg['chart']['PROCESSOR_CLEARING'], Decimal('50000.00'), Decimal(0)), (cfg['chart']['AR_CONTROL'], Decimal(0), Decimal('50000.00'))])
        assert q('SELECT ar_balance FROM lv.party_balance_current WHERE party_id = %s', cfg['party']) == [(Decimal('-50000.00'),)]
    # The hash-chained event replays and carries the derived postings separately from the document's financial fields.
    chain = events(app, cfg)
    replayed = replay_events(chain)
    assert len(chain) == 1 and replayed['head'] == chain[0]['hash'] == counts(app, cfg)['counter'][0][3]
    assert replayed['balances'] == {(cfg['chart']['PROCESSOR_CLEARING'], None): [Decimal('50000.0000'), Decimal(0)],
                                    (cfg['chart']['AR_CONTROL'], cfg['party']): [Decimal(0), Decimal('50000.0000')]}
    assert '"posting_rule":"sp1-posting/1.0"' in chain[0]['canonical_content'] and '"document_type":"PAYMENT"' in chain[0]['canonical_content']


def test_failure_mid_commit_leaves_no_payment_fact(database, app, seed, cfg):
    """A deferred-validation failure after the function wrote every row must undo all of them."""
    with connection(database=database) as admin:
        admin.execute('GRANT UPDATE ON lv.payment_current TO lv_seed')
    try:
        with pytest.raises(psycopg.errors.CheckViolation, match='diverge'):
            with tenant_transaction(seed, cfg['tenant']):
                seed.execute('SELECT lv.seed_processor_payment(%s, %s)', (cfg['ledger'], Jsonb(payment(cfg))))
                seed.execute('UPDATE lv.payment_current SET amount = 40000, unapplied = 40000 WHERE ledger_id = %s', (cfg['ledger'],))
    finally:
        with connection(database=database) as admin:
            admin.execute('REVOKE UPDATE ON lv.payment_current FROM lv_seed')
    after = counts(app, cfg)
    assert all(after[t] == 0 for t in HISTORY + ('payment_current',)) and after['counter'] == []
    assert pay(seed, cfg)['seq'] == '1'


# ============================================================================== 2. exact money, derived postings
@pytest.mark.parametrize('bad', ['50000.001', '0.00', '0', '-50000.00', '5e4', '50,000.00', ' 50000.00', '50000.', '.5',
                                 '10000000000000000.00', '', 'NaN', 'Infinity', '٥٠٠٠٠'])
def test_inexact_or_invalid_amounts_are_refused_before_any_cast(app, seed, cfg, bad):
    with pytest.raises(psycopg.errors.CheckViolation):
        pay(seed, cfg, payment(cfg, amount=bad))
    assert counts(app, cfg)['commit_batch'] == 0


def test_json_number_amount_is_refused(app, seed, cfg):
    body = payment(cfg); body['amount'] = 50000
    with pytest.raises(psycopg.errors.CheckViolation):
        pay(seed, cfg, body)
    assert counts(app, cfg)['commit_batch'] == 0


@pytest.mark.parametrize('amount,canonical', [('9999999999999999.99', '9999999999999999.99'), ('0.01', '0.01'), ('50000', '50000.00'), ('50000.5', '50000.50')])
def test_exact_amounts_round_trip_and_postings_derive_from_them(app, seed, cfg, amount, canonical):
    doc = pay(seed, cfg, payment(cfg, amount=amount))['document_id']
    with tenant_transaction(app, cfg['tenant']):
        assert app.execute("SELECT financial_fields->>'amount', original_request->>'amount' FROM lv.document_version WHERE document_id = %s", (doc,)).fetchone() == (canonical, amount)
        stored = app.execute('SELECT amount FROM lv.payment_current WHERE document_id = %s', (doc,)).fetchone()[0]
        lines = app.execute('SELECT side::text, amount FROM lv.journal_line WHERE document_id = %s ORDER BY line_no', (doc,)).fetchall()
    assert stored == Decimal(canonical) and lines == [('D', Decimal(canonical)), ('C', Decimal(canonical))]


@pytest.mark.parametrize('change', [{'currency': 'USD'}, {'currency': 'inr'}, {'processor_ref': ''}, {'processor_ref': 'a/b'}, {'processor_ref': 'p' * 65},
                                    {'processor_item_id': 'PMT 2001'}, {'processor_item_id': 'x' * 64}, {'received_date': '2026-13-01'},
                                    {'received_date': '10/10/2026'}, {'party_id': 'not-a-uuid'}, {'description': 'd' * 513}])
def test_malformed_requests_are_refused_without_effects(app, seed, cfg, change):
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.InvalidDatetimeFormat, psycopg.errors.DatetimeFieldOverflow)):
        pay(seed, cfg, payment(cfg, **change))
    assert counts(app, cfg)['commit_batch'] == 0


@pytest.mark.parametrize('extra', ['created_by', 'account_id', 'lines', 'provenance', 'invoice_id', 'settlement_id', 'status'])
def test_caller_cannot_supply_actor_accounts_postings_or_links(app, seed, cfg, extra):
    with pytest.raises(psycopg.errors.CheckViolation):
        pay(seed, cfg, {**payment(cfg), extra: 'x'})
    body = payment(cfg); del body['description']
    with pytest.raises(psycopg.errors.CheckViolation):
        pay(seed, cfg, body)
    assert counts(app, cfg)['commit_batch'] == 0


@pytest.mark.parametrize('changes', [
    pytest.param([("clr.id, clr.requires_party, NULL, doc_id, 'D'", "clr.id, clr.requires_party, NULL, doc_id, 'C'"),
                  ("ar.id, ar.requires_party, customer, doc_id, 'C'", "ar.id, ar.requires_party, customer, doc_id, 'D'")], id='sides-reversed'),
    pytest.param([("b.role = 'PROCESSOR_CLEARING' FOR SHARE OF a", "b.role = 'BANK_OPERATING' FOR SHARE OF a")], id='debit-to-bank'),
])
def test_validator_refuses_a_seed_function_that_derives_wrong_postings(changes):
    """Independent derivation check: a (deliberately) faulty seed function whose entry still balances and whose
    projections agree with its own lines must still be refused at commit (disposable DB)."""
    with disposable() as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            config = provision(app_, tenant)
        register(name, tenant)
        with connection(database=name) as admin:
            source = admin.execute("SELECT pg_get_functiondef('lv.seed_processor_payment(uuid,jsonb)'::regprocedure)").fetchone()[0]
            for old, new in changes:                     # each variant still balances and keeps projections consistent with its lines
                assert source.count(old) == 1
                source = source.replace(old, new)
            admin.execute(source)
        with connection('lv_seed', name, autocommit=True) as s:
            with pytest.raises(psycopg.errors.CheckViolation, match='diverge'):
                pay(s, config)
        with connection(database=name) as admin:
            assert admin.execute('SELECT count(*) FROM lv.commit_batch').fetchone() == (0,)


# ============================================================================== 3. customer, accounts, configuration
def test_missing_or_foreign_customer_is_refused(database, app, seed, cfg, tenants):
    other_ledger = provision(app, cfg['tenant']); other_tenant = provision(app, tenants[1])
    for party in (str(uuid7()), other_ledger['party'], other_tenant['party']):
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match='customer'):
            pay(seed, cfg, payment(cfg, party_id=party))
    assert counts(app, cfg)['commit_batch'] == 0


def test_wrong_ledger_tenant_or_unconfigured_ledger_is_refused(database, configuration, seed, cfg, tenants):
    from conftest import ledger as bare_ledger
    register(database, tenants[1])          # a valid principal in the other tenant still cannot see this ledger
    with pytest.raises(psycopg.errors.CheckViolation, match='native SANDBOX'):        # ledger of another tenant is invisible
        seed_processor_payment(seed, tenants[1], cfg['ledger'], payment(cfg))
    with tenant_transaction(configuration, cfg['tenant']):
        lid = bare_ledger(configuration, cfg['tenant'])
        configuration.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, '2026-10-01', '2026-11-01')", (cfg['tenant'], lid))
    with pytest.raises(psycopg.errors.CheckViolation, match='configuration'):
        seed_processor_payment(seed, cfg['tenant'], lid, payment(cfg))
    assert counts(configuration, cfg)['commit_batch'] == 0 and counts(configuration, {'tenant': cfg['tenant'], 'ledger': lid})['commit_batch'] == 0


def test_bound_posting_accounts_cannot_be_deactivated(configuration, cfg):
    """Configuration keeps role-bound accounts active (0001 FK + CHECK), so a wrong or inactive posting account cannot be chosen."""
    for role in ('PROCESSOR_CLEARING', 'AR_CONTROL'):
        with pytest.raises((psycopg.errors.ForeignKeyViolation, psycopg.errors.CheckViolation)):
            with tenant_transaction(configuration, cfg['tenant']):
                configuration.execute('UPDATE lv.account SET active = false WHERE id = %s', (cfg['chart'][role],))


def test_seed_refuses_inactive_posting_account_when_configuration_guard_is_absent():
    """Defence in depth: with the binding FK removed (disposable DB), an inactive clearing account is still refused."""
    with disposable() as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            config = provision(app_, tenant)
        register(name, tenant)
        with connection(database=name) as admin:
            fk = admin.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'lv.account_role_binding'::regclass AND contype = 'f'").fetchone()[0]
            admin.execute(sql.SQL('ALTER TABLE lv.account_role_binding DROP CONSTRAINT {}').format(sql.Identifier(fk)))
            admin.execute('UPDATE lv.account SET active = false WHERE id = %s', (config['chart']['PROCESSOR_CLEARING'],))
        with connection('lv_seed', name, autocommit=True) as s:
            with pytest.raises(psycopg.errors.CheckViolation, match='unavailable'):
                pay(s, config)
        with connection(database=name) as admin:
            assert admin.execute('SELECT count(*) FROM lv.commit_batch').fetchone() == (0,)


@pytest.mark.parametrize('mode', ['BOOKS', 'PROVIDER_CONNECTED'])
def test_books_and_provider_connected_ledgers_are_refused(database, configuration, seed, tenants, mode):
    config = provision(configuration, tenants[0], purpose='BOOKS' if mode == 'BOOKS' else 'SANDBOX'); register(database, tenants[0])
    if mode == 'PROVIDER_CONNECTED':
        with tenant_transaction(configuration, config['tenant']):
            configuration.execute("UPDATE lv.ledger SET source_kind = 'PROVIDER_CONNECTED' WHERE id = %s", (config['ledger'],))
    with pytest.raises(psycopg.errors.CheckViolation, match='native SANDBOX'):
        pay(seed, config)
    assert counts(configuration, config)['commit_batch'] == 0


# ============================================================================== 4. identity uniqueness and retries
def test_identical_repeat_replays_and_conflicting_content_is_refused(app, seed, cfg):
    first = pay(seed, cfg); before = counts(app, cfg)
    assert pay(seed, cfg) == {**first, 'replayed': True} and counts(app, cfg) == before
    for change in ({'amount': '49000.00'}, {'received_date': '2026-10-11'}, {'description': 'different text'}, {'amount': '50000'}):
        with pytest.raises(psycopg.errors.UniqueViolation, match='different content'):
            pay(seed, cfg, payment(cfg, **change))
        assert counts(app, cfg) == before
    # The same item id under another processor namespace is a different identity.
    second = pay(seed, cfg, payment(cfg, processor_ref='processor-2'))
    assert second['seq'] == '2' and second['document_id'] != first['document_id']


def test_identity_uniqueness_is_a_database_constraint(database):
    with connection(database=database) as admin:
        columns = admin.execute(
            "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.conrelid = 'lv.processor_payment_identity'::regclass AND c.contype = 'u' GROUP BY c.oid").fetchall()
    assert [r[0] for r in columns] == [['tenant_id', 'ledger_id', 'processor_ref', 'processor_item_id']]


def test_simultaneous_duplicates_converge_and_simultaneous_conflicts_leave_one_payment(database, app, cfg):
    barrier = threading.Barrier(2)

    def attempt(body):
        with connection('lv_seed', database, autocommit=True) as c:
            c.execute("SET lock_timeout = '6s'"); barrier.wait(timeout=3)
            try:
                return pay(c, cfg, body)
            except psycopg.errors.UniqueViolation as error:
                return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=8) for f in [pool.submit(attempt, payment(cfg)) for _ in range(2)]]
    assert {r['document_id'] for r in results} == {results[0]['document_id']} and sorted(r['replayed'] for r in results) == [False, True]
    barrier.reset()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=8) for f in [pool.submit(attempt, payment(cfg, processor_item_id='PMT-2002', amount=a)) for a in ('1.00', '2.00')]]
    assert sum(isinstance(r, psycopg.errors.UniqueViolation) for r in results) == 1
    after = counts(app, cfg)
    assert (after['document'], after['processor_payment_identity'], after['payment_current'], after['counter'][0][:2]) == (2, 2, 2, (2, 2))


# ============================================================================== 5. periods, rollback, numbering
def test_closed_period_is_refused_and_consumes_no_numbers(database, app, seed, cfg):
    assert close_period(database, admin_token(database, cfg['tenant']), cfg['period'])['status'] == 200   # audited close (0009)
    with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
        pay(seed, cfg)
    with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
        pay(seed, cfg, payment(cfg, received_date='2026-12-01'))      # no period at all
    assert counts(app, cfg)['commit_batch'] == 1   # only the PERIOD_CLOSE commit; refused payments consumed no numbers
    assert q_kinds(app, cfg) == ['PERIOD_CLOSE']


def q_kinds(app, cfg):
    with tenant_transaction(app, cfg['tenant']):
        return [r[0] for r in app.execute('SELECT kind::text FROM lv.commit_batch WHERE ledger_id = %s ORDER BY seq', (cfg['ledger'],)).fetchall()]


def test_client_rollback_consumes_no_numbers(app, seed, cfg):
    with pytest.raises(RuntimeError):
        with tenant_transaction(seed, cfg['tenant']):
            seed.execute('SELECT lv.seed_processor_payment(%s, %s)', (cfg['ledger'], Jsonb(payment(cfg))))
            raise RuntimeError('client fails before commit')
    assert counts(app, cfg)['commit_batch'] == 0
    opening(seed, cfg)
    assert pay(seed, cfg)['seq'] == '2'
    assert counts(app, cfg)['counter'][0][:2] == (2, 2)
    assert replay_events(events(app, cfg))['entries'] == 2   # contiguous chain across manual-journal and typed-payment events


def test_payment_waits_for_concurrent_period_close_then_refuses(database, app, cfg):
    from test_posting import wait_for_block
    ready, pid = threading.Event(), []
    token = admin_token(database, cfg['tenant'])

    def seeding():
        with connection('lv_seed', database, autocommit=True) as c:
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout = '6s'"); ready.set()
            with pytest.raises(psycopg.errors.CheckViolation):
                pay(c, cfg)
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with held_close(database, token, cfg['period']) as (closer, result):
            assert result['status'] == 200
            future = pool.submit(seeding); assert ready.wait(3)
            wait_for_block(database, pid[0], closer.info.backend_pid)
        assert future.result(timeout=8)
    assert q_kinds(app, cfg) == ['PERIOD_CLOSE']   # the waiting payment wrote nothing


# ============================================================================== 6. immutable typed history, privileges
@pytest.mark.parametrize('table', ['document', 'document_version', 'journal_entry', 'journal_line', 'ledger_event', 'commit_batch', 'processor_payment_identity'])
def test_typed_payment_history_is_immutable_even_to_owner(database, seed, cfg, table):
    pay(seed, cfg)
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(cfg['tenant']),))
        for verb in ('UPDATE', 'DELETE'):
            with pytest.raises(psycopg.errors.CheckViolation):
                with owner.transaction():
                    statement_ = (sql.SQL('UPDATE lv.{} SET seq = seq WHERE ledger_id = %s').format(sql.Identifier(table))
                                  if verb == 'UPDATE' else sql.SQL('DELETE FROM lv.{} WHERE ledger_id = %s').format(sql.Identifier(table)))
                    owner.execute(statement_, (cfg['ledger'],))


def test_identity_cannot_be_attached_to_an_earlier_commit(database, app, seed, cfg):
    """Retro-fitting: even the owner cannot later attach a processor identity to an existing (e.g. manual-journal) document."""
    manual = opening(seed, cfg)
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("SELECT set_config('app.tenant_id', %s, true)", (str(cfg['tenant']),))
        with pytest.raises(psycopg.errors.CheckViolation, match='earlier commit'):
            with owner.transaction():
                owner.execute('INSERT INTO lv.processor_payment_identity VALUES (%s, %s, %s, %s, %s, 1)',
                              (cfg['tenant'], cfg['ledger'], manual['document_id'], 'processor-1', 'PMT-2001'))
    assert counts(app, cfg)['processor_payment_identity'] == 0


def test_restricted_roles(database, app, seed, cfg, tenants):
    pay(seed, cfg)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pay(app, cfg, payment(cfg, processor_item_id='PMT-2002'))
    with connection('lv_ingest', database, autocommit=True) as ingest_:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pay(ingest_, cfg, payment(cfg, processor_item_id='PMT-2002'))
    with connection(database=database) as admin:
        for table in PAYMENT_TABLES:
            for role in ('lv_app', 'lv_seed', 'lv_ingest'):
                for privilege in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                    assert admin.execute('SELECT has_table_privilege(%s, %s, %s)', (role, 'lv.' + table, privilege)).fetchone() == (False,), (role, table, privilege)
            assert admin.execute("SELECT has_table_privilege('lv_ingest', %s, 'SELECT')", ('lv.' + table,)).fetchone() == (False,)
            assert admin.execute("SELECT relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", ('lv.' + table,)).fetchone() == (True,)
        grants = dict(admin.execute("SELECT r.rolname, has_function_privilege(r.rolname, 'lv.seed_processor_payment(uuid,jsonb)', 'EXECUTE') "
                                    "FROM pg_roles r WHERE r.rolname IN ('lv_app', 'lv_seed', 'lv_ingest', 'public')").fetchall())
        assert grants == {'lv_app': False, 'lv_seed': True, 'lv_ingest': False}
        assert admin.execute("SELECT prosecdef, proconfig FROM pg_proc WHERE oid = 'lv.seed_processor_payment(uuid,jsonb)'::regprocedure").fetchone() == \
            (True, ['search_path=pg_catalog, pg_temp'])
    with tenant_transaction(app, tenants[1]):
        for table in PAYMENT_TABLES:
            assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,)
    for table in PAYMENT_TABLES:
        assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,)   # no tenant context


def test_unregistered_principal_is_refused(database, app, seed, tenants):
    config = provision(app, tenants[0])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pay(seed, config)
    assert counts(app, config)['commit_batch'] == 0


def test_ingestion_has_zero_authority_over_payments(database, app, seed, cfg):
    raw = statement(cfg)
    assert ingest(database, cfg, raw)['status'] == 'STAGED'       # claims PMT-2001, but no Payment exists
    after = counts(app, cfg)
    assert (after['document'], after['processor_payment_identity'], after['payment_current'], after['commit_batch']) == (0, 0, 0, 0)
    with connection('lv_ingest', database) as c:
        with tenant_transaction(c, cfg['tenant']):
            for table in PAYMENT_TABLES:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with c.transaction():
                        c.execute(sql.SQL('SELECT 1 FROM lv.{}').format(sql.Identifier(table)))
    # A later typed Payment is created only by the seed path; the staged claim did not pre-empt or pre-match it.
    opening(seed, cfg); doc = pay(seed, cfg)['document_id']
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    assert item_check(result)['verdict'] == 'PASS'


# ============================================================================== 7. identity-based statement matching
@pytest.fixture
def matched(database, app, seed, cfg):
    opening(seed, cfg)
    doc = pay(seed, cfg)['document_id']
    staged = ingest(database, cfg, statement(cfg))
    assert staged['status'] == 'STAGED'
    return cfg, doc, staged


def test_typed_payment_matches_statement_by_identity_currency_and_amount(database, app, matched):
    cfg, doc, staged = matched
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    assert verdicts(result) == ALL_PASS
    assert result['outcome'] == 'BLOCKED' and set(result['blocking_reasons']) == EXPECTED_BLOCKING
    assert result['execution_readiness']['verdict'] == 'NOT_AVAILABLE' and result['labels']['eligible_for_execution'] is False
    assert [(p['role'], p['side'], p['amount']) for p in result['predicted_postings']] == EXPECTED_POSTINGS
    assert by_role(result, 'before') == EXPECTED_BEFORE and by_role(result) == EXPECTED_AFTER
    assert result['labels']['item_processor'].startswith('AUTHORITATIVE_PAYMENT_IDENTITY')
    assert result['labels']['item_amounts'].startswith('LOADED_FROM_TYPED_PAYMENT_VERSIONS')
    with tenant_transaction(app, cfg['tenant']):
        version_id, fingerprint = app.execute('SELECT id::text, financial_fingerprint FROM lv.document_version WHERE document_id = %s', (doc,)).fetchone()
    [source] = result['source']['items']
    assert (source['document_id'], source['version'], source['document_version_id'], source['financial_fingerprint'],
            source['processor_ref'], source['processor_item_id'], source['amount']) == (doc, '1', version_id, fingerprint, 'processor-1', 'PMT-2001', '50000.00')
    check = item_check(result)
    assert check['trace']['source_file_sha256'] == staged['sha256'] and check['trace']['source_record_id'] == staged['source_record_id']
    [item] = check['values']['items']
    assert (item['item_ref'], item['verdict'], item['link']['document_id'], item['link']['document_version_id'], item['link']['financial_fingerprint']) == \
        ('PMT-2001', 'PASS', doc, version_id, fingerprint)


def test_kernel_items_carry_persisted_identity_and_version(app, matched):
    cfg, doc, _ = matched
    from ledgervigil.kernel_adapter.calculation import translate
    snap = load_snapshot(app, cfg['tenant'], cfg['ledger'], (), ('processor-1', 'STL-3001'), [doc])
    kernel_snapshot, command, _ = translate(snap, request([doc]))
    assert [i.wire() for i in kernel_snapshot.items] == [{'id': 'PMT-2001', 'kind': 'PAYMENT', 'processor': 'processor-1',
                                                          'amount': {'amount': '50000.00', 'currency': 'INR'}, 'version': '1', 'settled': False, 'voided': False}]
    assert command.items == ('PMT-2001',)


def test_different_processor_namespace_is_refused_by_the_kernel(database, app, matched):
    cfg, doc, _ = matched
    assert ingest(database, cfg, statement(cfg, processor='processor-2', payout='STL-3002'))['status'] == 'STAGED'
    with pytest.raises(AdapterError) as refused:
        preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc], processor='processor-2', payout='STL-3002'))
    assert (refused.value.code, refused.value.source) == ('PROCESSOR_MISMATCH', 'kernel')


def test_item_id_from_another_namespace_with_equal_amount_stays_unresolved(database, app, seed, matched):
    """processor-2's statement lists PMT-2001 (which exists only under processor-1) for exactly the requested amount."""
    cfg, _, _ = matched
    other = pay(seed, cfg, payment(cfg, processor_ref='processor-2', processor_item_id='PMT-2002'))['document_id']
    assert ingest(database, cfg, statement(cfg, processor='processor-2', payout='STL-3002'))['status'] == 'STAGED'
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([other], processor='processor-2', payout='STL-3002', gross='50000.00'))
    check = item_check(result)
    assert check['verdict'] == 'UNRESOLVED' and verdicts(result)['E-AMT-GROSS'] == 'PASS'
    assert check['values']['items'][0]['other_namespaces'] == ['processor-1'] and 'link' not in check['values']['items'][0]
    assert check['values']['requested_not_matched_on_statement'] == [other]
    assert result['outcome'] == 'BLOCKED' and 'E-ITEM-01: UNRESOLVED' in result['blocking_reasons']


def test_wrong_item_identity_with_equal_amount_stays_unresolved(database, app, seed, cfg):
    opening(seed, cfg); doc = pay(seed, cfg)['document_id']
    assert ingest(database, cfg, statement(cfg, items=[{'id': 'PMT-9999', 'kind': 'PAYMENT', 'amount': '50000.00'}]))['status'] == 'STAGED'
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    check = item_check(result)
    assert (check['verdict'], verdicts(result)['E-AMT-GROSS']) == ('UNRESOLVED', 'PASS')
    assert check['values']['items'][0]['verdict'] == 'UNRESOLVED' and check['values']['requested_not_matched_on_statement'] == [doc]


def test_incorrect_amount_on_a_linked_item_fails(database, app, seed, cfg):
    opening(seed, cfg); doc = pay(seed, cfg)['document_id']
    raw = statement(cfg, items=[{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '49500.00'}], gross='49500.00', fee='500.00')
    assert ingest(database, cfg, raw)['status'] == 'STAGED'
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    check = item_check(result)
    assert check['verdict'] == 'FAIL' and 'amount 49500.00 vs payment 50000.00' in check['values']['items'][0]['reason']
    assert check['values']['items'][0]['link']['document_id'] == doc
    assert {k: verdicts(result)[k] for k in ('E-AMT-GROSS', 'E-AMT-FEE', 'E-AMT-NET')} == {'E-AMT-GROSS': 'FAIL', 'E-AMT-FEE': 'FAIL', 'E-AMT-NET': 'PASS'}


def test_statement_item_linked_to_an_unrequested_payment_fails(database, app, seed, cfg):
    opening(seed, cfg, '75000.00')
    doc = pay(seed, cfg)['document_id']
    pay(seed, cfg, payment(cfg, processor_item_id='PMT-2002', amount='25000.00'))
    raw = statement(cfg, items=[{'id': 'PMT-2001', 'kind': 'PAYMENT', 'amount': '50000.00'}, {'id': 'PMT-2002', 'kind': 'PAYMENT', 'amount': '25000.00'}],
                    gross='75000.00', fee='1500.00', net='73500.00')
    assert ingest(database, cfg, raw)['status'] == 'STAGED'
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    check = item_check(result)
    assert check['verdict'] == 'FAIL'
    assert [i['verdict'] for i in check['values']['items']] == ['PASS', 'FAIL'] and 'not among the requested' in check['values']['items'][1]['reason']


def test_requested_payment_missing_from_a_linked_statement_fails(database, app, seed, cfg):
    opening(seed, cfg, '75000.00')
    pay(seed, cfg)
    other = pay(seed, cfg, payment(cfg, processor_item_id='PMT-2002', amount='25000.00'))['document_id']
    assert ingest(database, cfg, statement(cfg))['status'] == 'STAGED'          # lists only PMT-2001
    check = item_check(preview_settlement(app, cfg['tenant'], cfg['ledger'], request([other], gross='25000.00', fee='500.00', net='24500.00')))
    assert check['verdict'] == 'FAIL' and check['values']['requested_not_matched_on_statement'] == [other]


def test_ambiguous_identity_links_fail(app, matched):
    """The schema forbids two identities per (ledger, processor, item); the pure check must still fail closed if one appears."""
    cfg, doc, _ = matched
    snap = load_snapshot(app, cfg['tenant'], cfg['ledger'], (), ('processor-1', 'STL-3001'), [doc])
    doubled = dataclasses.replace(snap, statement_identities=snap.statement_identities * 2)
    check = item_check(calculate_settlement(doubled, request([doc])))
    assert check['verdict'] == 'FAIL' and check['values']['items'][0]['reason'].startswith('Ambiguous')


def test_inconsistent_typed_payment_facts_are_refused(app, matched):
    cfg, doc, _ = matched
    snap = load_snapshot(app, cfg['tenant'], cfg['ledger'], (), ('processor-1', 'STL-3001'), [doc])
    [p] = snap.payments
    for broken, code in ((dataclasses.replace(p, amount=Decimal('49999.00'), unapplied=Decimal('49999.00')), 'PAYMENT_POSTING_INCONSISTENT'),
                         (dataclasses.replace(p, posting_rule='other/9'), 'PAYMENT_POSTING_INCONSISTENT'),
                         (dataclasses.replace(p, party_id=str(uuid7())), 'PAYMENT_POSTING_INCONSISTENT'),
                         (dataclasses.replace(p, status='VOIDED'), 'UNSUPPORTED_PAYMENT_STATE')):
        with pytest.raises(AdapterError) as refused:
            calculate_settlement(dataclasses.replace(snap, payments=(broken,)), request([doc]))
        assert refused.value.code == code
    with pytest.raises(AdapterError) as refused:
        calculate_settlement(dataclasses.replace(snap, payments=(p, p)), request([doc]))
    assert refused.value.code == 'PAYMENT_POSTING_INCONSISTENT'


def test_cross_ledger_and_cross_tenant_references_do_not_resolve(database, app, seed, matched, tenants):
    cfg, doc, _ = matched
    other = provision(app, cfg['tenant'])
    opening(seed, other); other_doc = pay(seed, other, payment(other, processor_item_id='PMT-3001'))['document_id']
    for ledger_, document in ((cfg, other_doc), (other, doc)):
        with pytest.raises(AdapterError) as refused:
            preview_settlement(app, ledger_['tenant'], ledger_['ledger'], request([document]))
        assert refused.value.code == 'ITEM_NOT_FOUND'
    with pytest.raises(AdapterError) as refused:     # another tenant cannot see the ledger at all
        preview_settlement(app, tenants[1], cfg['ledger'], request([doc]))
    assert refused.value.code == 'LEDGER_NOT_FOUND'
    # Ledger B's statement claims PMT-2001, which exists only in ledger A: unresolved, not matched across ledgers.
    assert ingest(database, other, statement(other))['status'] == 'STAGED'
    check = item_check(preview_settlement(app, other['tenant'], other['ledger'], request([other_doc])))
    assert check['verdict'] == 'UNRESOLVED' and 'other_namespaces' not in check['values']['items'][0]


def test_missing_statement_is_insufficient_and_legacy_journals_stay_unrelabelled(database, app, seed, cfg):
    from test_settlement_adapter import invoice_and_payment, settlement_request
    invoice_and_payment(seed, cfg)                       # older manual-journal fixtures
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], settlement_request())
    assert item_check(result)['verdict'] == 'INSUFFICIENT_EVIDENCE'
    assert result['labels']['item_kind'].startswith('LEGACY_MANUAL_JOURNAL_FIXTURE')
    assert ingest(database, cfg, statement(cfg))['status'] == 'STAGED'
    assert item_check(preview_settlement(app, cfg['tenant'], cfg['ledger'], settlement_request()))['verdict'] == 'UNRESOLVED'
    with tenant_transaction(app, cfg['tenant']):
        assert app.execute('SELECT doc_type::text, count(*) FROM lv.document WHERE ledger_id = %s GROUP BY 1', (cfg['ledger'],)).fetchall() == [('MANUAL_JOURNAL', 2)]
        assert app.execute('SELECT count(*) FROM lv.processor_payment_identity').fetchone() == (0,)


@pytest.mark.parametrize('items,code', [
    ([{'document_id': 'not-a-uuid', 'kind': 'PAYMENT'}], 'ITEM_FIELDS'),
    ([{'document_id': '0190A3B2-0000-7000-8000-000000000000'.upper(), 'kind': 'PAYMENT'}], 'ITEM_FIELDS'),
    ([{'document_id': str(uuid7()), 'kind': 'REFUND'}], 'UNSUPPORTED_ITEM_KIND'),
    ([{'document_id': str(uuid7()), 'kind': 'PAYMENT', 'amount': '50000.00'}], 'ITEM_FIELDS'),
    ([{'document_id': str(uuid7()), 'kind': 'PAYMENT'}, {'source_ref': 'synthetic-payment-1', 'kind': 'PAYMENT'}], 'ITEM_FIELDS'),
])
def test_typed_request_shape_is_strict(items, code):
    with pytest.raises(AdapterError) as refused:
        validate_request(request([], items=items))
    assert refused.value.code == code
    same = str(uuid7())
    with pytest.raises(AdapterError) as refused:
        validate_request(request([same, same]))
    assert refused.value.code == 'DUPLICATE_ITEM'


def test_matching_is_read_only(database, app, matched):
    cfg, doc, _ = matched
    before = table_hashes(database, ALL_TABLES)
    results = [preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc])) for _ in range(2)]
    assert table_hashes(database, ALL_TABLES) == before
    assert all(r['snapshot']['write_xid_assigned'] is False and r['snapshot']['read_role'] == 'lv_app' for r in results)
    assert results[0]['kernel']['envelope_sha256'] == results[1]['kernel']['envelope_sha256']


# ============================================================================== 8. migration chain and upgrade
def test_fresh_chain_reaches_0005_and_downgrade_restores_0004_validator():
    with disposable('0004_statement_ingestion') as name:
        with connection(database=name) as admin:
            original = admin.execute("SELECT pg_get_functiondef('lv.validate_seed_batch()'::regprocedure)").fetchone()[0]
        migrate(name, '0005_processor_payments')
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0005_processor_payments',)
            assert admin.execute("SELECT pg_get_functiondef('lv.validate_seed_batch()'::regprocedure)").fetchone()[0] != original
        migrate(name, '0004_statement_ingestion', downgrade=True, disposable=True)
        with connection(database=name) as admin:
            assert admin.execute("SELECT pg_get_functiondef('lv.validate_seed_batch()'::regprocedure)").fetchone()[0] == original
            assert admin.execute("SELECT to_regclass('lv.payment_current'), to_regclass('lv.processor_payment_identity')").fetchone() == (None, None)
        migrate(name)


def test_downgrade_is_refused_while_typed_payments_exist():
    with disposable() as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            config = provision(app_, tenant)
        register(name, tenant)
        with connection('lv_seed', name, autocommit=True) as s:
            pay(s, config)
        with pytest.raises(Exception, match='PAYMENT documents exist') as refused:
            migrate(name, '0004_statement_ingestion', downgrade=True, disposable=True)
        assert isinstance(refused.value.orig, psycopg.errors.ObjectNotInPrerequisiteState)
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0039_mfa',)   # unchanged head
            assert admin.execute('SELECT count(*) FROM lv.payment_current').fetchone() == (1,)


def test_upgrade_from_populated_0004_preserves_records_then_matches():
    with disposable('0004_statement_ingestion') as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            config = provision(app_, tenant)
        register(name, tenant)
        from test_settlement_adapter import invoice_and_payment, settlement_request
        with connection('lv_seed', name, autocommit=True) as s:
            invoice_and_payment(s, config)
        assert ingest(name, config, statement(config))['status'] == 'STAGED'
        tables = FINANCIAL_0004 + INGESTION + ('tenant',)
        before = table_hashes(name, tables)
        before_rows = {t: rows(name, t) for t in tables}
        # Scope: the 0005/0006 upgrade itself. 0009 adds period columns and is covered by test_period_close.py's
        # populated-0008 upgrade test, which compares the pre-existing columns.
        migrate(name, '0008_identity_force_rls')
        assert table_hashes(name, tables) == before
        with connection('lv_app', name, autocommit=True) as app_:
            legacy_after = preview_settlement(app_, tenant, config['ledger'], settlement_request())
            assert verdicts(legacy_after) == CORRECT_VERDICTS     # 0004 result for these records: identity UNRESOLVED (legacy items)
            with tenant_transaction(app_, tenant):
                app_.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, '2026-11-01', '2026-12-01')", (tenant, config['ledger']))
        with connection('lv_seed', name, autocommit=True) as s:
            # A new item, so the earlier manual payment journal is not duplicated or relabelled.
            doc = pay(s, config, payment(config, processor_item_id='PMT-2101', received_date='2026-11-02'))['document_id']
        with connection('lv_app', name, autocommit=True) as app_:
            chain = events(app_, config)
            assert [e['seq'] for e in chain] == [1, 2, 3] and replay_events(chain)['entries'] == 3
            assert ingest(name, config, statement(config, payout='STL-3101', date='2026-11-05',
                                                  items=[{'id': 'PMT-2101', 'kind': 'PAYMENT', 'amount': '50000.00'}]))['status'] == 'STAGED'
            result = preview_settlement(app_, tenant, config['ledger'], request([doc], payout='STL-3101', date='2026-11-05'))
            assert item_check(result)['verdict'] == 'PASS' and result['outcome'] == 'BLOCKED'
        # Every history and evidence record that existed before the upgrade is still present, byte-identical, after new activity.
        for table in ('commit_batch', 'document', 'document_version', 'document_current', 'journal_entry', 'journal_line', 'ledger_event') + INGESTION:
            assert rows(name, table, before_rows[table]) == before_rows[table], table
