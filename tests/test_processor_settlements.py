"""Typed synthetic processor Settlements (migration 0006): payout identity, inclusions, Payment versions, duplicate checks.

Expected outcomes are stated from the owning requirements, independently of the implementation:
- Stage 2A §5.1 / §19.3: Dr BANK net; Dr FEE fee (omitted when zero); Cr CLR gross; gross = Σ included payments; net = gross − fee;
  fee ≥ 0, net ≥ 0, gross > 0 (I-09). Lines are positive (I-02), so a zero net line is omitted as well.
- §5.2 / §19.3: included Payments become SETTLED; each payment is in at most one active settlement (I-08); same payout_id
  again → DUPLICATE_SETTLEMENT; an item already settled → ITEM_ALREADY_SETTLED.
- §19.1 (ADR-008): a status/link change is a new immutable version (previous + 1) with the pointer advanced by
  compare-and-swap in the same commit batch; §5.1 supersession: unchanged derived postings → no new entry.
- §19.2 path D: SANDBOX developer seeding only; actor from the seed principal; SYNTHETIC provenance.
For the INR example (payment PMT-2001 50,000.00; payout STL-3001 gross 50,000.00, fee 1,000.00, net 49,000.00) the
settlement posts Dr BANK 49,000.00 / Dr FEE 1,000.00 / Cr CLR 50,000.00 and leaves CLR at 0.00.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid7

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tools.dev_db import connection, migrate
from period_close_support import admin_token, close_period, close_call, held_close   # 0009: audited close replaces the R-02 UPDATE
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.kernel_adapter import AdapterError, inspect_settlement, preview_settlement
from ledgervigil.kernel_adapter.types import DuplicateSettlementRefused
from ledgervigil.ledger.replay import replay_events
from ledgervigil.ledger.seed import seed_processor_settlement
from test_posting import events, provision, register
from test_processor_payments import (ALL_TABLES as TABLES_0005, EXPECTED_AFTER, EXPECTED_BEFORE, EXPECTED_POSTINGS, disposable, item_check,
                                     new_tenant, opening, pay, payment, request, rows)
from test_statement_ingestion import ingest, statement, table_hashes, verdicts

SETTLEMENT_TABLES = ('processor_settlement_identity', 'settlement_current', 'settlement_inclusion', 'settlement_inclusion_current')
ALL_TABLES = TABLES_0005 + SETTLEMENT_TABLES
CANDIDATE = [{'role': r, 'side': s, 'amount': a} for r, s, a in EXPECTED_POSTINGS]


def settlement(docs, **overrides):
    body = {'processor_ref': 'processor-1', 'payout_id': 'STL-3001', 'payout_date': '2026-10-14', 'currency': 'INR',
            'gross': '50000.00', 'fee': '1000.00', 'net': '49000.00',
            'items': [{'document_id': d, 'kind': 'PAYMENT', 'amount': a} for d, a in docs],
            'description': 'SYNTHETIC typed processor settlement fixture'}
    body.update(overrides)
    return body


def settle(seed, cfg, body):
    return seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], body)


def q(app, cfg, text, *args):
    with tenant_transaction(app, cfg['tenant']):
        return app.execute(text, args).fetchall()


def state(database):
    return table_hashes(database, ALL_TABLES)


def owner(database, tenant):
    c = connection('lv_migrator', database)
    c.execute('SET LOCAL ROLE lv_owner'); c.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
    return c


@pytest.fixture
def seed(database):
    with connection('lv_seed', database, autocommit=True) as c:
        yield c


@pytest.fixture
def cfg(app, tenants, database):
    config = provision(app, tenants[0]); register(database, tenants[0])
    return config


@pytest.fixture
def paid(seed, cfg):
    """Invoice-like opening manual journal (labelled, not an Invoice) + typed Payment PMT-2001 of 50,000.00."""
    opening(seed, cfg)
    return cfg, pay(seed, cfg)['document_id']


@pytest.fixture
def settled(seed, paid):
    cfg, doc = paid
    body = settlement([(doc, '50000.00')])
    return cfg, doc, body, settle(seed, cfg, body)


# ============================================================================== 1. the settlement commit
def test_settlement_writes_document_identity_inclusions_projections_and_postings(database, app, settled):
    cfg, doc, body, result = settled
    assert result['seq'] == '3' and result['replayed'] is False
    sdoc = result['document_id']
    with connection(database=database) as admin:
        principal = str(admin.execute("SELECT id FROM lv.service_principal WHERE tenant_id = %s AND database_role = 'lv_seed'", (cfg['tenant'],)).fetchone()[0])
    assert q(app, cfg, 'SELECT doc_type::text, source_ref, seq FROM lv.document WHERE id = %s', sdoc) == [('SETTLEMENT', 'processor-1/STL-3001', 3)]
    [(version, previous, ordinal, origin, provenance, created_by, financial, original)] = q(app, cfg,
        'SELECT version, previous_version, ordinal, origin::text, provenance::text, created_by::text, financial_fields, original_request '
        'FROM lv.document_version WHERE document_id = %s', sdoc)
    assert (version, previous, ordinal, origin, provenance, created_by) == (1, None, 1, 'SEED', 'SYNTHETIC', principal)
    assert financial == {'document_type': 'SETTLEMENT', 'processor_ref': 'processor-1', 'payout_id': 'STL-3001', 'payout_date': '2026-10-14',
                         'currency': 'INR', 'gross': '50000.00', 'fee': '1000.00', 'net': '49000.00', 'bank_role': 'BANK_OPERATING',
                         'items': [{'line_no': '1', 'document_id': doc, 'kind': 'PAYMENT', 'amount': '50000.00', 'payment_version': '1'}]}
    assert original == body
    assert q(app, cfg, 'SELECT version FROM lv.document_current WHERE document_id = %s', sdoc) == [(1,)]
    assert q(app, cfg, 'SELECT processor_ref, payout_id, seq FROM lv.processor_settlement_identity WHERE document_id = %s', sdoc) == [('processor-1', 'STL-3001', 3)]
    assert q(app, cfg, 'SELECT version, payout_date::text, gross, fee, net, item_count, status, last_seq FROM lv.settlement_current WHERE document_id = %s', sdoc) == \
        [(1, '2026-10-14', Decimal('50000.00'), Decimal('1000.00'), Decimal('49000.00'), 1, 'RECORDED', 3)]
    [(entry_id, kind, rule, rule_version, src_version, effective)] = q(app, cfg,
        'SELECT id::text, kind::text, posting_rule_id, posting_rule_version, source_document_version, effective_date::text FROM lv.journal_entry WHERE source_document_id = %s', sdoc)
    assert (entry_id, kind, rule, rule_version, src_version, effective) == (result['entry_id'], 'DOCUMENT_POSTING', 'sp1-posting', '1.0', 1, '2026-10-14')
    chart = cfg['chart']
    assert q(app, cfg, 'SELECT line_no, account_id::text, side::text, amount, party_id, document_id::text FROM lv.journal_line WHERE entry_id = %s ORDER BY line_no', entry_id) == [
        (1, chart['BANK_OPERATING'], 'D', Decimal('49000.00'), None, sdoc), (2, chart['PROCESSOR_FEE_EXPENSE'], 'D', Decimal('1000.00'), None, sdoc),
        (3, chart['PROCESSOR_CLEARING'], 'C', Decimal('50000.00'), None, sdoc)]
    assert q(app, cfg, 'SELECT settlement_document_id::text, settlement_version, line_no, payment_document_id::text, payment_version_before, '
             'payment_version_after, kind, amount, seq FROM lv.settlement_inclusion') == [(sdoc, 1, 1, doc, 1, 2, 'PAYMENT', Decimal('50000.00'), 3)]
    assert q(app, cfg, 'SELECT item_document_id::text, settlement_document_id::text, settlement_version, last_seq FROM lv.settlement_inclusion_current') == [(doc, sdoc, 1, 3)]
    balances = {a: (d, c) for a, d, c in q(app, cfg, 'SELECT account_id::text, sum(debit_total), sum(credit_total) FROM lv.account_balance_current '
                                                    'WHERE ledger_id = %s GROUP BY 1', cfg['ledger'])}
    assert balances[chart['BANK_OPERATING']] == (Decimal('49000.00'), 0) and balances[chart['PROCESSOR_FEE_EXPENSE']] == (Decimal('1000.00'), 0)
    assert balances[chart['PROCESSOR_CLEARING']] == (Decimal('50000.00'), Decimal('50000.00'))
    assert balances[chart['AR_CONTROL']] == (Decimal('50000.00'), Decimal('50000.00'))
    chain = events(app, cfg)
    replayed = replay_events(chain)
    assert [e['seq'] for e in chain] == [1, 2, 3] and replayed['entries'] == 3 and replayed['head'] == chain[-1]['hash']
    assert '"document_type":"SETTLEMENT"' in chain[-1]['canonical_content'] and '"payment_versions":[' in chain[-1]['canonical_content']


def test_payment_gets_a_new_settled_version_and_keeps_its_original_facts(database, app, seed, paid):
    cfg, doc = paid
    with connection(database=database) as admin:
        v1_before = admin.execute('SELECT * FROM lv.document_version WHERE document_id = %s AND version = 1', (doc,)).fetchone()
        entry_before = admin.execute('SELECT * FROM lv.journal_entry WHERE source_document_id = %s', (doc,)).fetchall()
        lines_before = admin.execute('SELECT * FROM lv.journal_line WHERE document_id = %s ORDER BY line_no', (doc,)).fetchall()
    body = settlement([(doc, '50000.00')])
    sdoc = settle(seed, cfg, body)['document_id']
    with connection(database=database) as admin:
        assert admin.execute('SELECT * FROM lv.document_version WHERE document_id = %s AND version = 1', (doc,)).fetchone() == v1_before
        assert admin.execute('SELECT * FROM lv.journal_entry WHERE source_document_id = %s', (doc,)).fetchall() == entry_before
        assert admin.execute('SELECT * FROM lv.journal_line WHERE document_id = %s ORDER BY line_no', (doc,)).fetchall() == lines_before
    [(v1_fin, v1_info)] = q(app, cfg, 'SELECT financial_fields, informational_fields FROM lv.document_version WHERE document_id = %s AND version = 1', doc)
    [(previous, seq, ordinal, fin, info, original, reason, origin, provenance)] = q(app, cfg,
        'SELECT previous_version, seq, ordinal, financial_fields, informational_fields, original_request, reason, origin::text, provenance::text '
        'FROM lv.document_version WHERE document_id = %s AND version = 2', doc)
    assert (previous, seq, ordinal, origin, provenance) == (1, 3, 2, 'SEED', 'SYNTHETIC')
    assert fin == {**v1_fin, 'status': 'SETTLED', 'settlement_document_id': sdoc, 'settlement_version': '1'} and 'status' not in v1_fin
    assert info == v1_info and original == body and 'processor-1/STL-3001' in reason
    assert q(app, cfg, 'SELECT version FROM lv.document_current WHERE document_id = %s', doc) == [(2,)]
    assert q(app, cfg, 'SELECT version, status, settlement_document_id::text, amount, applied, unapplied, last_seq FROM lv.payment_current WHERE document_id = %s', doc) == \
        [(2, 'SETTLED', sdoc, Decimal('50000.00'), Decimal(0), Decimal('50000.00'), 3)]
    # Supersession with unchanged derived postings: no REVERSAL, no re-posting; the posting still names version 1.
    assert q(app, cfg, 'SELECT count(*), min(source_document_version) FROM lv.journal_entry WHERE source_document_id = %s', doc) == [(1, 1)]
    assert q(app, cfg, 'SELECT count(*) FROM lv.journal_entry WHERE ledger_id = %s', cfg['ledger']) == [(3,)]


def test_current_pointer_only_advances_by_compare_and_swap(database, settled):
    cfg, doc, _, result = settled
    for statement_, args in (('UPDATE lv.document_current SET version = 1 WHERE document_id = %s', (doc,)),
                             ('UPDATE lv.document_current SET version = 3 WHERE document_id = %s', (doc,)),
                             ('UPDATE lv.document_current SET version = 2 WHERE document_id = %s', (result['document_id'],)),
                             ('DELETE FROM lv.document_current WHERE document_id = %s', (doc,))):
        with owner(database, cfg['tenant']) as c:
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(statement_, args)


@pytest.mark.parametrize('fee,net,expected', [
    pytest.param('0.00', '50000.00', [('BANK_OPERATING', 'D', '50000.00'), ('PROCESSOR_CLEARING', 'C', '50000.00')], id='zero-fee'),
    pytest.param('0', '50000.00', [('BANK_OPERATING', 'D', '50000.00'), ('PROCESSOR_CLEARING', 'C', '50000.00')], id='zero-fee-integer-text'),
    pytest.param('50000.00', '0.00', [('PROCESSOR_FEE_EXPENSE', 'D', '50000.00'), ('PROCESSOR_CLEARING', 'C', '50000.00')], id='zero-net'),
    pytest.param('0.01', '49999.99', [('BANK_OPERATING', 'D', '49999.99'), ('PROCESSOR_FEE_EXPENSE', 'D', '0.01'), ('PROCESSOR_CLEARING', 'C', '50000.00')], id='smallest-fee'),
])
def test_zero_value_lines_are_omitted_and_entries_stay_balanced(app, seed, paid, fee, net, expected):
    cfg, doc = paid
    result = settle(seed, cfg, settlement([(doc, '50000.00')], fee=fee, net=net))
    role = {v: k for k, v in cfg['chart'].items()}
    lines = q(app, cfg, 'SELECT line_no, account_id::text, side::text, amount FROM lv.journal_line WHERE entry_id = %s ORDER BY line_no', result['entry_id'])
    assert [(role[a], s, f'{m:.2f}') for _, a, s, m in lines] == expected and [n for n, *_ in lines] == list(range(1, len(expected) + 1))
    assert all(m > 0 for *_, m in lines)
    assert sum(m for _, _, s, m in lines if s == 'D') == sum(m for _, _, s, m in lines if s == 'C')
    [(f, n)] = q(app, cfg, "SELECT financial_fields->>'fee', financial_fields->>'net' FROM lv.document_version WHERE document_id = %s", result['document_id'])
    assert (Decimal(f), Decimal(n)) == (Decimal(fee), Decimal(net)) and len(f.split('.')[1]) == 2


@pytest.mark.parametrize('changes', [
    pytest.param({'gross': '49000.00', 'net': '48000.00'}, id='gross-not-sum-of-items'),
    pytest.param({'net': '49000.01'}, id='net-not-gross-minus-fee'),
    pytest.param({'fee': '50000.01', 'net': '-0.01'}, id='fee-above-gross'),
    pytest.param({'fee': '-1000.00', 'net': '51000.00'}, id='negative-fee'),
    pytest.param({'fee': '1000.001', 'net': '48999.999'}, id='excess-scale'),
    pytest.param({'fee': '1e3', 'net': '49000.00'}, id='exponent-notation'),
    pytest.param({'fee': 'NaN'}, id='nan'),
    pytest.param({'gross': '0.00', 'fee': '0.00', 'net': '0.00'}, id='zero-gross'),
    pytest.param({'currency': 'USD'}, id='wrong-currency'),
    pytest.param({'payout_id': 'STL 3001'}, id='bad-payout-id'),
    pytest.param({'processor_ref': 'p' * 65}, id='long-processor'),
    pytest.param({'payout_date': '2026-13-01'}, id='bad-date'),
])
def test_invalid_settlement_amounts_or_fields_are_refused_without_effects(database, seed, paid, changes):
    cfg, doc = paid
    before = state(database)
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.DatetimeFieldOverflow, psycopg.errors.InvalidDatetimeFormat)):
        settle(seed, cfg, settlement([(doc, '50000.00')], **changes))
    assert state(database) == before


@pytest.mark.parametrize('mutate', [
    pytest.param(lambda b: b.update(fee=1000), id='json-number'),
    pytest.param(lambda b: b.update(settled='false'), id='caller-settled-flag'),
    pytest.param(lambda b: b.update(created_by='someone'), id='caller-actor'),
    pytest.param(lambda b: b['items'][0].update(status='RECEIVED'), id='item-status'),
    pytest.param(lambda b: b['items'][0].update(version='1'), id='item-version'),
    pytest.param(lambda b: b.update(items=[]), id='no-items'),
    pytest.param(lambda b: b.pop('description'), id='missing-key'),
])
def test_request_shape_is_strict(database, seed, paid, mutate):
    cfg, doc = paid
    body = settlement([(doc, '50000.00')]); mutate(body)
    before = state(database)
    with pytest.raises(psycopg.errors.CheckViolation):
        settle(seed, cfg, body)
    assert state(database) == before


# ============================================================================== 2. identity, retries, inclusion integrity
def test_identical_retry_replays_and_changed_body_is_refused(database, seed, settled):
    cfg, doc, body, first = settled
    before = state(database)
    assert settle(seed, cfg, body) == {**first, 'replayed': True}
    assert state(database) == before
    for change in ({'fee': '2000.00', 'net': '48000.00'}, {'payout_date': '2026-10-15'}, {'description': 'other text'}, {'gross': '50000.0'}):
        with pytest.raises(psycopg.errors.UniqueViolation, match='different content'):
            settle(seed, cfg, {**body, **change})
        assert state(database) == before


def test_payment_cannot_be_reused_under_another_payout(database, seed, settled):
    cfg, doc, body, _ = settled
    before = state(database)
    with pytest.raises(psycopg.errors.UniqueViolation, match='already included'):
        settle(seed, cfg, {**body, 'payout_id': 'STL-3002'})
    assert state(database) == before


def test_repeated_payment_within_one_request_is_refused(database, seed, paid):
    cfg, doc = paid
    before = state(database)
    with pytest.raises(psycopg.errors.CheckViolation, match='repeated'):
        settle(seed, cfg, settlement([(doc, '50000.00'), (doc, '50000.00')], gross='100000.00', fee='0.00', net='100000.00'))
    assert state(database) == before


def test_wrong_processor_ledger_tenant_kind_or_amount_is_refused(database, app, seed, paid, tenants):
    cfg, doc = paid
    other_processor = pay(seed, cfg, payment(cfg, processor_ref='processor-2', processor_item_id='PMT-2002'))['document_id']
    other_ledger = provision(app, cfg['tenant']); opening(seed, other_ledger)
    ledger_b_doc = pay(seed, other_ledger, payment(other_ledger, processor_item_id='PMT-3001'))['document_id']
    tenant_b = provision(app, tenants[1]); register(database, tenants[1]); opening(seed, tenant_b)
    tenant_b_doc = pay(seed, tenant_b, payment(tenant_b, processor_item_id='PMT-4001'))['document_id']
    manual_journal = q(app, cfg, "SELECT id::text FROM lv.document WHERE doc_type = 'MANUAL_JOURNAL' AND ledger_id = %s", cfg['ledger'])[0][0]
    before = state(database)
    cases = [
        (settlement([(other_processor, '50000.00')]), psycopg.errors.CheckViolation, 'another processor'),
        (settlement([(ledger_b_doc, '50000.00')]), psycopg.errors.ForeignKeyViolation, 'not found'),
        (settlement([(tenant_b_doc, '50000.00')]), psycopg.errors.ForeignKeyViolation, 'not found'),
        (settlement([(manual_journal, '50000.00')]), psycopg.errors.ForeignKeyViolation, 'not found'),
        (settlement([(str(uuid7()), '50000.00')]), psycopg.errors.ForeignKeyViolation, 'not found'),
        (settlement([(doc, '25000.00')], gross='25000.00', fee='0.00', net='25000.00'), psycopg.errors.CheckViolation, 'full amount'),
    ]
    for body, error, message in cases:
        with pytest.raises(error, match=message):
            settle(seed, cfg, body)
    refund = settlement([(doc, '50000.00')]); refund['items'][0]['kind'] = 'REFUND'
    with pytest.raises(psycopg.errors.FeatureNotSupported, match='refunds'):
        settle(seed, cfg, refund)
    other_kind = settlement([(doc, '50000.00')]); other_kind['items'][0]['kind'] = 'CHARGE'
    with pytest.raises(psycopg.errors.CheckViolation):
        settle(seed, cfg, other_kind)
    with pytest.raises(psycopg.errors.CheckViolation, match='native SANDBOX'):      # ledger A is invisible to tenant B's principal
        seed_processor_settlement(seed, tenants[1], cfg['ledger'], settlement([(doc, '50000.00')]))
    assert state(database) == before


def test_database_constraints_are_the_final_backstop(database):
    with connection(database=database) as admin:
        def keys(table, kind):
            return sorted(r[0] for r in admin.execute(
                "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum WHERE c.conrelid = %s::regclass AND c.contype = %s GROUP BY c.oid",
                ('lv.' + table, kind)).fetchall())
        assert ['tenant_id', 'ledger_id', 'processor_ref', 'payout_id'] in keys('processor_settlement_identity', 'u')
        assert keys('settlement_inclusion_current', 'p') == [['tenant_id', 'ledger_id', 'item_document_id']]        # I-08
        checks = ' '.join(r[0] for r in admin.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'lv.settlement_current'::regclass AND contype = 'c'"))
        assert all(part in checks for part in ('(net = (gross - fee))', '(fee >= (0)::numeric)', '(net >= (0)::numeric)', '(gross > (0)::numeric)'))   # I-09
        triggers = dict(admin.execute("SELECT tgrelid::regclass::text || '.' || tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal "
                                      "AND tgrelid::regclass::text IN ('lv.settlement_inclusion', 'lv.settlement_inclusion_current', 'lv.processor_settlement_identity', 'lv.document_current')").fetchall())
        for name in ('lv.settlement_inclusion.immutable', 'lv.settlement_inclusion_current.immutable', 'lv.processor_settlement_identity.immutable', 'lv.document_current.advance_only'):
            assert 'BEFORE DELETE OR UPDATE' in triggers[name], name


def test_inclusion_history_and_active_inclusion_cannot_be_changed_or_released(database, settled):
    cfg, doc, _, result = settled
    for table in SETTLEMENT_TABLES[0:1] + SETTLEMENT_TABLES[2:]:
        for verb in ('UPDATE', 'DELETE'):
            with owner(database, cfg['tenant']) as c:
                with pytest.raises(psycopg.errors.CheckViolation, match='immutable'):
                    c.execute(sql.SQL('UPDATE lv.{} SET tenant_id = tenant_id' if verb == 'UPDATE' else 'DELETE FROM lv.{}').format(sql.Identifier(table)))
    # The settlement's own document history is immutable too.
    for table in ('document', 'document_version', 'journal_entry', 'journal_line', 'ledger_event', 'commit_batch'):
        with owner(database, cfg['tenant']) as c:
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(sql.SQL('DELETE FROM lv.{} WHERE seq = 3').format(sql.Identifier(table)))


# ============================================================================== 3. concurrency, periods, partial failure
def _concurrently(database, cfg, bodies):
    barrier = threading.Barrier(len(bodies))

    def attempt(body):
        with connection('lv_seed', database, autocommit=True) as c:
            c.execute("SET lock_timeout = '6s'"); barrier.wait(timeout=3)
            try:
                return settle(c, cfg, body)
            except psycopg.errors.UniqueViolation as error:
                return error
    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return [f.result(timeout=10) for f in [pool.submit(attempt, b) for b in bodies]]


def test_concurrent_identical_payouts_converge(database, app, paid):
    cfg, doc = paid
    results = _concurrently(database, cfg, [settlement([(doc, '50000.00')])] * 2)
    assert len({r['document_id'] for r in results}) == 1 and sorted(r['replayed'] for r in results) == [False, True]
    assert q(app, cfg, 'SELECT count(*) FROM lv.settlement_current') == [(1,)]


def test_concurrent_payouts_competing_for_one_payment_include_it_once(database, app, paid):
    cfg, doc = paid
    results = _concurrently(database, cfg, [settlement([(doc, '50000.00')], payout_id=p) for p in ('STL-3001', 'STL-3002')])
    assert sum(isinstance(r, psycopg.errors.UniqueViolation) for r in results) == 1
    assert q(app, cfg, 'SELECT count(*) FROM lv.settlement_current') == [(1,)]
    assert q(app, cfg, 'SELECT count(*) FROM lv.document_version WHERE document_id = %s', doc) == [(2,)]
    assert q(app, cfg, 'SELECT count(*) FROM lv.settlement_inclusion') == [(1,)]


def test_concurrent_conflicting_content_for_one_payout_leaves_one_settlement(database, app, paid):
    cfg, doc = paid
    results = _concurrently(database, cfg, [settlement([(doc, '50000.00')], fee=f, net=n) for f, n in (('1000.00', '49000.00'), ('2000.00', '48000.00'))])
    assert sum(isinstance(r, psycopg.errors.UniqueViolation) for r in results) == 1
    assert q(app, cfg, 'SELECT count(*) FROM lv.settlement_current') == [(1,)]


def test_closed_period_is_refused_without_effects(database, app, seed, paid):
    cfg, doc = paid
    assert close_period(database, admin_token(database, cfg['tenant']), cfg['period'])['status'] == 200   # audited close (0009)
    before = state(database)
    with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
        settle(seed, cfg, settlement([(doc, '50000.00')]))
    with pytest.raises(psycopg.errors.CheckViolation, match='open period'):
        settle(seed, cfg, settlement([(doc, '50000.00')], payout_date='2026-12-01'))
    assert state(database) == before


def test_payment_from_a_closed_period_settles_in_an_open_one_without_changing_the_closed_period(database, configuration, seed, paid):
    cfg, doc = paid
    assert close_period(database, admin_token(database, cfg['tenant']), cfg['period'])['status'] == 200   # audited close (0009)
    with tenant_transaction(configuration, cfg['tenant']):
        configuration.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, '2026-11-01', '2026-12-01')", (cfg['tenant'], cfg['ledger']))
    october = q(configuration, cfg, "SELECT e.id, l.line_no, l.amount FROM lv.journal_entry e JOIN lv.journal_line l ON l.entry_id = e.id WHERE e.effective_date < '2026-11-01' ORDER BY 1, 2")
    result = settle(seed, cfg, settlement([(doc, '50000.00')], payout_date='2026-11-03'))
    assert q(configuration, cfg, 'SELECT effective_date::text FROM lv.journal_entry WHERE id = %s', result['entry_id']) == [('2026-11-03',)]
    assert q(configuration, cfg, "SELECT e.id, l.line_no, l.amount FROM lv.journal_entry e JOIN lv.journal_line l ON l.entry_id = e.id WHERE e.effective_date < '2026-11-01' ORDER BY 1, 2") == october


def test_settlement_waits_for_concurrent_close_then_refuses(database, app, paid):
    from test_posting import wait_for_block
    cfg, doc = paid
    ready, pid = threading.Event(), []
    token = admin_token(database, cfg['tenant'])

    def settling():
        with connection('lv_seed', database, autocommit=True) as c:
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout = '6s'"); ready.set()
            with pytest.raises(psycopg.errors.CheckViolation):
                settle(c, cfg, settlement([(doc, '50000.00')]))
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with held_close(database, token, cfg['period']) as (closer, result):
            assert result['status'] == 200
            future = pool.submit(settling); assert ready.wait(3)
            wait_for_block(database, pid[0], closer.info.backend_pid)
        assert future.result(timeout=8)
    assert q(app, cfg, 'SELECT count(*) FROM lv.settlement_current') == [(0,)]


def test_close_waits_for_pending_settlement_then_commits(database, app, seed, paid):
    from test_posting import wait_for_block
    cfg, doc = paid
    ready, pid = threading.Event(), []
    token = admin_token(database, cfg['tenant'])

    def closing():
        with connection('lv_app', database, autocommit=True) as c:
            pid.append(c.info.backend_pid)
            with c.transaction():
                c.execute("SET LOCAL lock_timeout = '6s'"); ready.set()
                assert close_call(c, token, cfg['period'])['status'] == 200
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_transaction(seed, cfg['tenant']):
            seed.execute('SELECT lv.seed_processor_settlement(%s, %s)', (cfg['ledger'], Jsonb(settlement([(doc, '50000.00')]))))
            future = pool.submit(closing); assert ready.wait(3)
            wait_for_block(database, pid[0], seed.info.backend_pid)
        assert future.result(timeout=8)
    assert q(app, cfg, 'SELECT count(*) FROM lv.settlement_current') == [(1,)]


def test_failure_after_intermediate_writes_leaves_nothing_and_consumes_no_number(database, app, seed, paid):
    cfg, doc = paid
    before = state(database)
    with connection(database=database) as admin:
        admin.execute('GRANT UPDATE ON lv.settlement_current TO lv_seed')
    try:
        with pytest.raises(psycopg.errors.CheckViolation, match='diverge'):
            with tenant_transaction(seed, cfg['tenant']):
                seed.execute('SELECT lv.seed_processor_settlement(%s, %s)', (cfg['ledger'], Jsonb(settlement([(doc, '50000.00')]))))
                seed.execute('UPDATE lv.settlement_current SET fee = fee + 1, net = net - 1 WHERE ledger_id = %s', (cfg['ledger'],))
    finally:
        with connection(database=database) as admin:
            admin.execute('REVOKE UPDATE ON lv.settlement_current FROM lv_seed')
    assert state(database) == before
    with pytest.raises(RuntimeError):
        with tenant_transaction(seed, cfg['tenant']):
            seed.execute('SELECT lv.seed_processor_settlement(%s, %s)', (cfg['ledger'], Jsonb(settlement([(doc, '50000.00')]))))
            raise RuntimeError('client fails before commit')
    assert state(database) == before
    assert settle(seed, cfg, settlement([(doc, '50000.00')]))['seq'] == '3'


@pytest.mark.parametrize('changes', [
    pytest.param([('IF fee_value > 0 THEN', 'IF false THEN'),
                  ("bank.id, bank.requires_party, NULL, doc_id, 'D', net_value", "bank.id, bank.requires_party, NULL, doc_id, 'D', gross_value")], id='bank-gets-gross'),
    pytest.param([('UPDATE lv.document_current SET version = pay.before_version + 1\n   WHERE', 'PERFORM 1 FROM lv.document_current\n   WHERE')], id='pointer-not-advanced'),
    pytest.param([("|| jsonb_build_object('status', 'SETTLED', 'settlement_document_id', doc_id::text, 'settlement_version', '1') AS f",
                   "|| jsonb_build_object('status', 'SETTLED') AS f")], id='version-without-link'),
])
def test_independent_validator_refuses_a_faulty_seed_function(changes):
    """Disposable DB: a deliberately faulty seed function whose entry still balances is refused at commit by the validator."""
    with disposable() as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            config = provision(app_, tenant)
        register(name, tenant)
        with connection('lv_seed', name, autocommit=True) as s:
            opening(s, config); doc = pay(s, config)['document_id']
        with connection(database=name) as admin:
            source = admin.execute("SELECT pg_get_functiondef('lv.seed_processor_settlement(uuid,jsonb)'::regprocedure)").fetchone()[0]
            for old, new in changes:
                assert source.count(old) == 1, old
                source = source.replace(old, new)
            admin.execute(source)
        with connection('lv_seed', name, autocommit=True) as s:
            with pytest.raises(psycopg.errors.CheckViolation, match='diverge'):
                settle(s, config, settlement([(doc, '50000.00')]))
        with connection(database=name) as admin:
            assert admin.execute('SELECT count(*) FROM lv.settlement_current').fetchone() == (0,)
            assert admin.execute('SELECT count(*) FROM lv.document_version WHERE document_id = %s', (doc,)).fetchone() == (1,)


# ============================================================================== 4. roles, ledgers, ingestion
def test_restricted_roles_and_tenant_isolation(database, app, seed, settled, tenants):
    cfg, doc, body, _ = settled
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        settle(app, cfg, {**body, 'payout_id': 'STL-9'})
    with connection('lv_ingest', database, autocommit=True) as ingest_:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            settle(ingest_, cfg, {**body, 'payout_id': 'STL-9'})
    with connection(database=database) as admin:
        for table in SETTLEMENT_TABLES:
            for role in ('lv_app', 'lv_seed', 'lv_ingest'):
                for privilege in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                    assert admin.execute('SELECT has_table_privilege(%s, %s, %s)', (role, 'lv.' + table, privilege)).fetchone() == (False,), (role, table, privilege)
            assert admin.execute("SELECT has_table_privilege('lv_ingest', %s, 'SELECT')", ('lv.' + table,)).fetchone() == (False,)
            assert admin.execute('SELECT relforcerowsecurity FROM pg_class WHERE oid = %s::regclass', ('lv.' + table,)).fetchone() == (True,)
        for role, expected in (('lv_app', False), ('lv_seed', True), ('lv_ingest', False)):
            assert admin.execute("SELECT has_function_privilege(%s, 'lv.seed_processor_settlement(uuid,jsonb)', 'EXECUTE')", (role,)).fetchone() == (expected,)
        assert admin.execute("SELECT prosecdef, proconfig FROM pg_proc WHERE oid = 'lv.seed_processor_settlement(uuid,jsonb)'::regprocedure").fetchone() == \
            (True, ['search_path=pg_catalog, pg_temp'])
    with tenant_transaction(app, tenants[1]):
        for table in SETTLEMENT_TABLES:
            assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,)
    for table in SETTLEMENT_TABLES:
        assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,)


@pytest.mark.parametrize('mode', ['BOOKS', 'PROVIDER_CONNECTED'])
def test_books_and_provider_connected_ledgers_are_refused(database, configuration, seed, tenants, mode):
    config = provision(configuration, tenants[0], purpose='BOOKS' if mode == 'BOOKS' else 'SANDBOX'); register(database, tenants[0])
    if mode == 'PROVIDER_CONNECTED':
        with tenant_transaction(configuration, config['tenant']):
            configuration.execute("UPDATE lv.ledger SET source_kind = 'PROVIDER_CONNECTED' WHERE id = %s", (config['ledger'],))
    with pytest.raises(psycopg.errors.CheckViolation, match='native SANDBOX'):
        settle(seed, config, settlement([(str(uuid7()), '50000.00')]))


def test_unregistered_principal_is_refused(app, seed, tenants):
    config = provision(app, tenants[0])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        settle(seed, config, settlement([(str(uuid7()), '50000.00')]))


def test_ingestion_retains_no_financial_write_authority(database, app, seed, paid):
    cfg, doc = paid
    before = {t: h for t, h in state(database).items() if t not in ('source_file', 'import_batch', 'source_record', 'staged_document', 'staged_statement_item')}
    assert ingest(database, cfg, statement(cfg))['status'] == 'STAGED'
    after = {t: h for t, h in state(database).items() if t not in ('source_file', 'import_batch', 'source_record', 'staged_document', 'staged_statement_item')}
    assert after == before
    with connection('lv_ingest', database) as c:
        with tenant_transaction(c, cfg['tenant']):
            for table in SETTLEMENT_TABLES + ('payment_current', 'document_current', 'journal_line'):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with c.transaction():
                        c.execute(sql.SQL('SELECT 1 FROM lv.{}').format(sql.Identifier(table)))


# ============================================================================== 5. read-only duplicate check in the adapter
@pytest.fixture
def matched(database, paid):
    cfg, doc = paid
    assert ingest(database, cfg, statement(cfg))['status'] == 'STAGED'
    return cfg, doc


def test_before_settlement_duplicate_eligibility_is_established_from_authoritative_state(app, matched):
    cfg, doc = matched
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    dup = next(c for c in result['evidence_checks'] if c['id'] == 'E-DUP-01')
    assert dup['verdict'] == 'PASS' and dup['trace']['snapshot_seq'] == result['snapshot']['seq'] == '2'
    assert dup['values']['recorded_settlement_for_payout'] is None and dup['values']['requested_payments_in_active_settlements'] == []
    assert dup['values']['scope'].startswith('PREVIEW only') and 'execution time' in dup['values']['scope']
    p_dup = next(c for c in result['checks'] if c['id'] == 'P-DUP-01')
    assert p_dup['verdict'] == 'PASS' and 'lv.processor_settlement_identity' in p_dup['basis']
    assert item_check(result)['verdict'] == 'PASS'
    assert set(result['blocking_reasons']) == {'P-CLR-01: INSUFFICIENT_EVIDENCE', 'P-EVD-01: INSUFFICIENT_EVIDENCE', 'P-FEE-01: INSUFFICIENT_EVIDENCE',
                                              'P-STL-01: INSUFFICIENT_EVIDENCE', 'X-EXEC-01: NOT_AVAILABLE'}
    assert result['outcome'] == 'BLOCKED' and result['labels']['duplicate_check_scope'].startswith('PREVIEW_SNAPSHOT')


def test_preview_equals_the_committed_settlement(app, seed, matched):
    cfg, doc = matched
    predicted = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    assert [(p['role'], p['side'], p['amount']) for p in predicted['predicted_postings']] == EXPECTED_POSTINGS
    assert {b['role']: b['before'] for b in predicted['balances']} == EXPECTED_BEFORE and {b['role']: b['after'] for b in predicted['balances']} == EXPECTED_AFTER
    result = settle(seed, cfg, settlement([(doc, '50000.00')]))
    role = {v: k for k, v in cfg['chart'].items()}
    committed = q(app, cfg, 'SELECT account_id::text, side::text, amount FROM lv.journal_line WHERE entry_id = %s ORDER BY line_no', result['entry_id'])
    assert [(role[a], s, f'{m:.2f}') for a, s, m in committed] == EXPECTED_POSTINGS
    actual = {role[a]: b for a, b in q(app, cfg, 'SELECT account_id::text, sum(debit_total - credit_total) FROM lv.account_balance_current WHERE ledger_id = %s GROUP BY 1', cfg['ledger'])}
    assert {r: f'{actual.get(r, Decimal(0)):.2f}' for r in EXPECTED_AFTER} == EXPECTED_AFTER


def test_after_settlement_the_same_payout_is_refused_as_a_duplicate(database, app, seed, matched):
    cfg, doc = matched
    sdoc = settle(seed, cfg, settlement([(doc, '50000.00')]))['document_id']
    before = state(database)
    with pytest.raises(DuplicateSettlementRefused) as refused:
        preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    error = refused.value
    assert (error.code, error.source) == ('DUPLICATE_SETTLEMENT', 'kernel') and error.snapshot['seq'] == '3'
    assert error.evidence['verdict'] == 'FAIL' and error.evidence['values']['recorded_settlement_for_payout']['settlement_document_id'] == sdoc
    assert error.evidence['values']['requested_payments_in_active_settlements'][0]['settlement_document_id'] == sdoc
    assert error.snapshot['write_xid_assigned'] is False and state(database) == before


def test_reusing_a_settled_payment_under_another_payout_is_refused(database, app, seed, matched):
    cfg, doc = matched
    sdoc = settle(seed, cfg, settlement([(doc, '50000.00')]))['document_id']
    with pytest.raises(DuplicateSettlementRefused) as refused:
        preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc], payout='STL-3002'))
    assert refused.value.code == 'ITEM_ALREADY_SETTLED' and refused.value.evidence['verdict'] == 'FAIL'
    values = refused.value.evidence['values']
    assert values['recorded_settlement_for_payout'] is None
    assert values['requested_payments_in_active_settlements'] == [{'payment_document_id': doc, 'settlement_document_id': sdoc, 'settlement_version': '1',
                                                                    'payout_identity': ['processor-1', 'STL-3001'], 'since_seq': '3'}]


def test_inspecting_a_duplicate_candidate_reports_failed_duplicate_checks(app, seed, matched):
    cfg, doc = matched
    settle(seed, cfg, settlement([(doc, '50000.00')]))
    result = inspect_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]), CANDIDATE)
    assert {c['id']: c['verdict'] for c in result['checks']}['P-DUP-01'] == 'FAIL'
    assert verdicts(result)['E-DUP-01'] == 'FAIL' and result['outcome'] == 'BLOCKED'
    assert {'P-DUP-01: FAIL', 'E-DUP-01: FAIL'} <= set(result['blocking_reasons'])
    [source] = result['source']['items']
    assert (source['version'], source['status'], source['posting_entry_document_version']) == ('2', 'SETTLED', '1')


def test_identical_seed_retry_is_not_a_new_eligible_settlement(database, app, seed, matched):
    cfg, doc = matched
    body = settlement([(doc, '50000.00')])
    first = settle(seed, cfg, body)
    before = state(database)
    assert settle(seed, cfg, body) == {**first, 'replayed': True} and state(database) == before
    with pytest.raises(DuplicateSettlementRefused):
        preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))


def test_an_unrelated_payment_under_a_new_payout_stays_eligible(database, app, seed, matched):
    cfg, doc = matched
    settle(seed, cfg, settlement([(doc, '50000.00')]))
    other = pay(seed, cfg, payment(cfg, processor_item_id='PMT-2002', amount='20000.00'))['document_id']
    assert ingest(database, cfg, statement(cfg, payout='STL-3002', items=[{'id': 'PMT-2002', 'kind': 'PAYMENT', 'amount': '20000.00'}],
                                           gross='20000.00', fee='400.00', net='19600.00'))['status'] == 'STAGED'
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], request([other], payout='STL-3002', gross='20000.00', fee='400.00', net='19600.00'))
    assert verdicts(result)['E-DUP-01'] == 'PASS' and item_check(result)['verdict'] == 'PASS'
    assert [(p['role'], p['side'], p['amount']) for p in result['predicted_postings']] == \
        [('BANK_OPERATING', 'D', '19600.00'), ('PROCESSOR_FEE_EXPENSE', 'D', '400.00'), ('PROCESSOR_CLEARING', 'C', '20000.00')]


def test_callers_cannot_assert_unsettled_or_not_duplicate(app, matched):
    cfg, doc = matched
    for body, code in ((request([doc], not_duplicate='true'), 'REQUEST_FIELDS'),
                       (request([], items=[{'document_id': doc, 'kind': 'PAYMENT', 'settled': 'false'}]), 'ITEM_FIELDS')):
        with pytest.raises(AdapterError) as refused:
            preview_settlement(app, cfg['tenant'], cfg['ledger'], body)
        assert refused.value.code == code


def test_duplicate_checks_are_read_only(database, app, seed, matched):
    cfg, doc = matched
    before_settlement = state(database)
    preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]))
    assert state(database) == before_settlement
    settle(seed, cfg, settlement([(doc, '50000.00')]))
    after_settlement = state(database)
    for payout in ('STL-3001', 'STL-3002'):
        with pytest.raises(DuplicateSettlementRefused):
            preview_settlement(app, cfg['tenant'], cfg['ledger'], request([doc], payout=payout))
    inspect_settlement(app, cfg['tenant'], cfg['ledger'], request([doc]), CANDIDATE)
    assert state(database) == after_settlement


def test_legacy_manual_journal_requests_keep_duplicate_settlement_not_established(app, seed, cfg):
    from test_settlement_adapter import invoice_and_payment, settlement_request
    invoice_and_payment(seed, cfg)
    result = preview_settlement(app, cfg['tenant'], cfg['ledger'], settlement_request())
    assert verdicts(result)['E-DUP-01'] == 'NOT_ESTABLISHED'
    assert next(c for c in result['checks'] if c['id'] == 'P-DUP-01')['basis'].startswith('NOT_ESTABLISHED')


# ============================================================================== 6. migration chain
def test_fresh_chain_reaches_0006_and_downgrade_restores_0005():
    with disposable('0005_processor_payments') as name:
        with connection(database=name) as admin:
            validator = admin.execute("SELECT pg_get_functiondef('lv.validate_seed_batch()'::regprocedure)").fetchone()[0]
            checks = admin.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'lv.payment_current'::regclass ORDER BY 1").fetchall()
        migrate(name, '0006_processor_settlements')
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0006_processor_settlements',)
            assert 'validate_settlement_seed' in admin.execute("SELECT pg_get_functiondef('lv.validate_seed_batch()'::regprocedure)").fetchone()[0]
        migrate(name, '0005_processor_payments', downgrade=True, disposable=True)
        with connection(database=name) as admin:
            assert admin.execute("SELECT pg_get_functiondef('lv.validate_seed_batch()'::regprocedure)").fetchone()[0] == validator
            assert admin.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'lv.payment_current'::regclass ORDER BY 1").fetchall() == checks
            assert admin.execute("SELECT to_regclass('lv.settlement_current'), to_regclass('lv.settlement_inclusion_current')").fetchone() == (None, None)
            assert admin.execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'advance_only'").fetchone() == (0,)
        migrate(name)


def test_downgrade_is_refused_while_settlements_exist():
    with disposable() as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            config = provision(app_, tenant)
        register(name, tenant)
        with connection('lv_seed', name, autocommit=True) as s:
            opening(s, config); doc = pay(s, config)['document_id']; settle(s, config, settlement([(doc, '50000.00')]))
        with pytest.raises(Exception, match='SETTLEMENT documents exist') as refused:
            migrate(name, '0005_processor_payments', downgrade=True, disposable=True)
        assert isinstance(refused.value.orig, psycopg.errors.ObjectNotInPrerequisiteState)
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0033_detection_overlap_dates',)
            assert admin.execute('SELECT count(*) FROM lv.settlement_inclusion_current').fetchone() == (1,)


def test_upgrade_from_populated_0005_preserves_records_then_settles():
    with disposable('0005_processor_payments') as name:
        tenant = new_tenant(name)
        with connection('lv_app', name, autocommit=True) as app_:
            config = provision(app_, tenant)
        register(name, tenant)
        with connection('lv_seed', name, autocommit=True) as s:
            opening(s, config); doc = pay(s, config)['document_id']
        assert ingest(name, config, statement(config))['status'] == 'STAGED'
        unchanged = tuple(t for t in TABLES_0005 if t != 'payment_current')
        before = table_hashes(name, unchanged)
        before_rows = {t: rows(name, t) for t in unchanged}
        with connection(database=name) as admin:
            payment_before = admin.execute('SELECT tenant_id, ledger_id, document_id, version, party_id, currency, currency_exponent, amount, applied, '
                                           'unapplied, status, last_seq FROM lv.payment_current').fetchall()
        # Scope: the 0005/0006 upgrade itself. 0009 adds period columns and is covered by test_period_close.py's
        # populated-0008 upgrade test, which compares the pre-existing columns.
        migrate(name, '0008_identity_force_rls')
        assert table_hashes(name, unchanged) == before
        with connection(database=name) as admin:
            assert admin.execute('SELECT tenant_id, ledger_id, document_id, version, party_id, currency, currency_exponent, amount, applied, '
                                 'unapplied, status, last_seq, settlement_document_id FROM lv.payment_current').fetchall() == [(*payment_before[0], None)]
        with connection('lv_app', name, autocommit=True) as app_:
            eligible = preview_settlement(app_, tenant, config['ledger'], request([doc]))
            assert verdicts(eligible)['E-DUP-01'] == 'PASS' and item_check(eligible)['verdict'] == 'PASS'
        with connection('lv_seed', name, autocommit=True) as s:
            result = settle(s, config, settlement([(doc, '50000.00')]))
        assert result['seq'] == '3'
        with connection('lv_app', name, autocommit=True) as app_:
            assert replay_events(events(app_, config))['entries'] == 3
            with pytest.raises(DuplicateSettlementRefused):
                preview_settlement(app_, tenant, config['ledger'], request([doc]))
        for table in ('commit_batch', 'document', 'document_version', 'journal_entry', 'journal_line', 'ledger_event', 'processor_payment_identity',
                      'source_file', 'import_batch', 'source_record', 'staged_document', 'staged_statement_item'):
            assert rows(name, table, before_rows[table]) == before_rows[table], table
