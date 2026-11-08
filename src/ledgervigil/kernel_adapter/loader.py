"""Persistence loading for the read-only settlement adapter.

Reads one tenant-scoped native ledger through the restricted `lv_app` login inside a single
REPEATABLE READ, READ ONLY transaction, so every fact comes from one snapshot whose ledger sequence is
recorded. This default path never writes and rejects an assigned transaction id.
The private locked_submission mode requires a database-produced workflow/session/transaction proof;
it reads in the single locked submission write transaction and preserves the actual isolation metadata.
"""
from uuid import UUID

from psycopg import IsolationLevel

from ledgervigil.db.tenant import tenant_transaction

from .calculation import calculate_settlement, inspect_settlement_candidate, item_mode, validate_request
from .types import (ActiveInclusion, AdapterError, Binding, EvidenceConflict, LedgerSnapshot, PaymentIdentity, Period, PostedEntry,
                    PostedLine, ProjectionRow, ReferencedDocument, SettlementRef, StatementEvidence, TypedPayment)

ZERO_HEAD = '0' * 64
SNAPSHOT_ISOLATION = IsolationLevel.REPEATABLE_READ
REQUIRED_ISOLATION_NAME = 'repeatable read'
_after_counter_read = None   # test seam only: lets a test commit concurrently between reads


def load_snapshot(connection, tenant_id: UUID, ledger_id: UUID, source_refs=(), payout_identity=None, payment_ids=()) -> LedgerSnapshot:
    if not isinstance(ledger_id, UUID):
        raise TypeError('ledger_id must be a UUID')
    previous = (connection.isolation_level, connection.read_only)
    connection.isolation_level, connection.read_only = SNAPSHOT_ISOLATION, True
    try:
        with tenant_transaction(connection, tenant_id):
            return _read(connection, tenant_id, ledger_id, tuple(source_refs), payout_identity, tuple(payment_ids))
    finally:
        connection.isolation_level, connection.read_only = previous


def _read(c, tenant_id, ledger_id, source_refs, payout_identity=None, payment_ids=(), *, locked_submission=False) -> LedgerSnapshot:
    role = c.execute('SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user').fetchone()
    if role != ('lv_app', False, False):
        raise AdapterError('WRONG_DATABASE_ROLE', detail=str(role[0]))
    isolation = c.execute('SHOW transaction_isolation').fetchone()[0]
    read_only = c.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
    if locked_submission:
        proof=c.execute("SELECT 1 FROM lv.proposal_transition WHERE tenant_id=%s AND ledger_id=%s AND to_state='SUBMITTED' "
                        "AND tx_id=pg_current_xact_id() AND session_id=nullif(current_setting('app.session_id',true),'')::uuid",
                        (tenant_id,ledger_id)).fetchone()
        if isolation!='read committed' or read_only or not proof:
            raise AdapterError('SUBMISSION_LOCK_PROOF_REQUIRED')
    elif isolation != REQUIRED_ISOLATION_NAME or not read_only:
        raise AdapterError('SNAPSHOT_NOT_ISOLATED', detail=isolation)
    p = {'t': tenant_id, 'l': ledger_id}
    ledger = c.execute('SELECT purpose::text, source_kind::text, currency, currency_exponent FROM lv.ledger '
                       'WHERE tenant_id = %(t)s AND id = %(l)s', p).fetchone()
    if ledger is None:
        raise AdapterError('LEDGER_NOT_FOUND')
    counter = c.execute('SELECT last_seq, last_entry_no, batch_head, event_head FROM lv.ledger_counter '
                        'WHERE tenant_id = %(t)s AND ledger_id = %(l)s', p).fetchone() or (0, 0, ZERO_HEAD, ZERO_HEAD)
    if _after_counter_read is not None:
        _after_counter_read()
    p['s'] = counter[0]
    max_batch = c.execute('SELECT coalesce(max(seq), 0) FROM lv.commit_batch WHERE tenant_id = %(t)s AND ledger_id = %(l)s', p).fetchone()[0]
    bindings = tuple(Binding(r[0], str(r[1]), r[2], r[3], r[4], r[5], r[6], r[7].strip(), r[8]) for r in c.execute(
        'SELECT b.role::text, a.id, a.code, a.type::text, a.is_contra, a.active, a.requires_party, a.currency, a.currency_exponent '
        'FROM lv.account_role_binding b JOIN lv.account a ON (a.tenant_id, a.ledger_id, a.id) = (b.tenant_id, b.ledger_id, b.account_id) '
        'WHERE b.tenant_id = %(t)s AND b.ledger_id = %(l)s ORDER BY b.role', p))
    periods = tuple(Period(r[0].isoformat(), r[1].isoformat(), r[2]) for r in c.execute(
        'SELECT start_date, end_date, status::text FROM lv.fiscal_period WHERE tenant_id = %(t)s AND ledger_id = %(l)s ORDER BY start_date', p))
    lines = {}
    for r in c.execute('SELECT entry_id, line_no, account_id, side, amount, currency, currency_exponent, party_id FROM lv.journal_line '
                       'WHERE tenant_id = %(t)s AND ledger_id = %(l)s AND seq <= %(s)s ORDER BY entry_id, line_no', p):
        lines.setdefault(r[0], []).append(PostedLine(r[1], str(r[2]), r[3], r[4], r[5].strip(), r[6], None if r[7] is None else str(r[7])))
    entries = tuple(PostedEntry(str(r[0]), r[1], r[2], str(r[3]), tuple(lines.get(r[0], ()))) for r in c.execute(
        'SELECT id, entry_no, seq, source_document_id FROM lv.journal_entry WHERE tenant_id = %(t)s AND ledger_id = %(l)s '
        'AND seq <= %(s)s ORDER BY entry_no', p))
    projection = tuple(ProjectionRow(str(r[0]), None if r[1] is None else str(r[1]), r[2], r[3], r[4]) for r in c.execute(
        'SELECT account_id, party_id, debit_total, credit_total, last_seq FROM lv.account_balance_current '
        'WHERE tenant_id = %(t)s AND ledger_id = %(l)s', p))
    documents = tuple(ReferencedDocument(r[0], str(r[1]), r[2]) for r in c.execute(
        'SELECT source_ref, id, doc_type::text FROM lv.document WHERE tenant_id = %(t)s AND ledger_id = %(l)s '
        'AND seq <= %(s)s AND source_ref = ANY(%(refs)s) ORDER BY source_ref', {**p, 'refs': list(source_refs)}))
    statement, conflicts = _statement(c, p, payout_identity) if payout_identity else (None, ())
    identities, payments = _payments(c, p, statement, payment_ids)
    payout_settlement, inclusions = _settlement_state(c, p, payout_identity, payment_ids)
    timestamp = c.execute('SELECT transaction_timestamp()::text').fetchone()[0]
    xid_assigned = c.execute('SELECT pg_current_xact_id_if_assigned() IS NOT NULL').fetchone()[0]
    if xid_assigned and not locked_submission:
        raise AdapterError('UNEXPECTED_WRITE')
    return LedgerSnapshot(str(tenant_id), str(ledger_id), ledger[0], ledger[1], ledger[2].strip(), ledger[3],
                          counter[0], counter[1], max_batch, counter[2], counter[3], bindings, periods, entries, projection,
                          documents, role[0], isolation, read_only, timestamp, xid_assigned, statement, conflicts, payments, identities,
                          payout_settlement, inclusions)


def _statement(c, p, payout_identity):
    """The staged statement for (processor, payout) in this ledger, with its file and record, read in the same
    snapshot as the ledger facts; plus any rejected attempts that claimed the same identity differently."""
    q = {**p, 'proc': payout_identity[0], 'payout': payout_identity[1]}
    row = c.execute(
        'SELECT s.id, s.import_batch_id, s.source_record_id, f.id, f.sha256, f.content, f.declared_format, f.declared_version, r.content, '
        'r.content_hash, s.processor_ref, s.payout_id, s.payout_date, s.currency, s.currency_exponent, s.gross, s.fee, s.net, '
        's.financial_fingerprint, s.provenance::text FROM lv.staged_document s '
        'JOIN lv.source_record r ON (r.tenant_id, r.ledger_id, r.id, r.import_batch_id) = (s.tenant_id, s.ledger_id, s.source_record_id, s.import_batch_id) '
        'JOIN lv.import_batch b ON (b.tenant_id, b.ledger_id, b.id) = (s.tenant_id, s.ledger_id, s.import_batch_id) '
        'JOIN lv.source_file f ON (f.tenant_id, f.ledger_id, f.id) = (b.tenant_id, b.ledger_id, b.source_file_id) '
        'WHERE s.tenant_id = %(t)s AND s.ledger_id = %(l)s AND s.processor_ref = %(proc)s AND s.payout_id = %(payout)s', q).fetchone()
    if row is None:
        return None, ()
    items = tuple(c.execute('SELECT line_no, item_ref, kind::text, amount FROM lv.staged_statement_item WHERE tenant_id = %(t)s '
                            'AND ledger_id = %(l)s AND staged_document_id = %(s)s ORDER BY line_no', {**p, 's': row[0]}).fetchall())
    conflicts = tuple(EvidenceConflict(str(r[0]), r[1], r[2]) for r in c.execute(
        "SELECT b.id, f.sha256, b.failure_code FROM lv.import_batch b JOIN lv.source_file f ON (f.tenant_id, f.ledger_id, f.id) = "
        "(b.tenant_id, b.ledger_id, b.source_file_id) WHERE b.tenant_id = %(t)s AND b.ledger_id = %(l)s AND b.status = 'REJECTED' "
        "AND b.related_staged_document_id = %(s)s ORDER BY b.id", {**p, 's': row[0]}))
    evidence = StatementEvidence(str(row[0]), str(row[1]), str(row[2]), str(row[3]), row[4], bytes(row[5]), row[6], row[7], row[8], row[9],
                                 row[10], row[11], row[12].isoformat(), row[13].strip(), row[14], row[15], row[16], row[17], items, row[18], row[19])
    return evidence, conflicts


def _payments(c, p, statement, payment_ids):
    """Typed Payments, read in the same snapshot.

    * `identities`: every persisted processor identity whose item id appears on the staged statement, in ANY processor
      namespace, so the evidence check can tell "linked in this namespace" from "exists only under another processor".
      Identity is looked up by the exact item id string only; amounts, dates and memo text are never used.
    * `payments`: the current version of each requested payment document and of each document linked above, joined to its
      identity, payment projection and DOCUMENT_POSTING entry. Only PAYMENT documents of this tenant and ledger qualify.
    """
    refs = sorted({item[1] for item in statement.items}) if statement else []
    identities = tuple(PaymentIdentity(r[0], r[1], str(r[2])) for r in c.execute(
        'SELECT processor_ref, processor_item_id, document_id FROM lv.processor_payment_identity WHERE tenant_id = %(t)s '
        'AND ledger_id = %(l)s AND seq <= %(s)s AND processor_item_id = ANY(%(refs)s) ORDER BY processor_ref, processor_item_id',
        {**p, 'refs': refs})) if refs else ()
    wanted = sorted(set(payment_ids) | {i.document_id for i in identities})
    if not wanted:
        return identities, ()
    payments = tuple(TypedPayment(str(r[0]), r[1], str(r[2]), r[3].strip(), r[4], r[5], str(r[6]), r[7].strip(), r[8], r[9], r[10], r[11],
                                  r[12], r[13], str(r[14]), r[15], r[16], None if r[17] is None else str(r[17]), r[18]) for r in c.execute(
        'SELECT d.id, v.version, v.id, v.financial_fingerprint, i.processor_ref, i.processor_item_id, pc.party_id, pc.currency, '
        'pc.currency_exponent, pc.amount, pc.applied, pc.unapplied, pc.status, i.seq, e.id, e.kind::text, '
        "e.posting_rule_id || '/' || e.posting_rule_version, pc.settlement_document_id, e.source_document_version "
        'FROM lv.document d '
        'JOIN lv.document_current dc ON (dc.tenant_id, dc.ledger_id, dc.document_id) = (d.tenant_id, d.ledger_id, d.id) '
        'JOIN lv.document_version v ON (v.tenant_id, v.ledger_id, v.document_id, v.version) = (dc.tenant_id, dc.ledger_id, dc.document_id, dc.version) '
        'JOIN lv.processor_payment_identity i ON (i.tenant_id, i.ledger_id, i.document_id) = (d.tenant_id, d.ledger_id, d.id) '
        'JOIN lv.payment_current pc ON (pc.tenant_id, pc.ledger_id, pc.document_id, pc.version) = (d.tenant_id, d.ledger_id, d.id, v.version) '
        # The posting entry is matched by document, not by current version: a version that leaves the derived postings
        # unchanged (e.g. RECEIVED -> SETTLED) writes no new entry, so the active entry keeps its original version (2A §5.1).
        'JOIN lv.journal_entry e ON (e.tenant_id, e.ledger_id, e.source_document_id) = (d.tenant_id, d.ledger_id, d.id) '
        "WHERE d.tenant_id = %(t)s AND d.ledger_id = %(l)s AND d.doc_type = 'PAYMENT' AND d.seq <= %(s)s AND e.seq <= %(s)s "
        'AND d.id = ANY(%(ids)s::uuid[]) ORDER BY d.id, e.entry_no', {**p, 'ids': wanted}))
    return identities, payments


def _settlement_state(c, p, payout_identity, payment_ids):
    """Authoritative duplicate-settlement state in the same snapshot: the settlement already recorded for the requested
    payout identity (if any) and the active inclusions of the requested payments. Nothing is taken from the caller."""
    payout = None
    if payout_identity:
        row = c.execute(
            'SELECT i.document_id, s.version, i.processor_ref, i.payout_id, s.payout_date, s.gross, s.fee, s.net, s.status, i.seq '
            'FROM lv.processor_settlement_identity i JOIN lv.settlement_current s ON (s.tenant_id, s.ledger_id, s.document_id) = '
            '(i.tenant_id, i.ledger_id, i.document_id) WHERE i.tenant_id = %(t)s AND i.ledger_id = %(l)s AND i.seq <= %(s)s '
            'AND i.processor_ref = %(proc)s AND i.payout_id = %(payout)s', {**p, 'proc': payout_identity[0], 'payout': payout_identity[1]}).fetchone()
        if row is not None:
            payout = SettlementRef(str(row[0]), row[1], row[2], row[3], row[4].isoformat(), row[5], row[6], row[7], row[8], row[9])
    inclusions = tuple(ActiveInclusion(str(r[0]), str(r[1]), r[2], r[3], r[4], r[5]) for r in c.execute(
        'SELECT ic.item_document_id, ic.settlement_document_id, ic.settlement_version, i.processor_ref, i.payout_id, ic.last_seq '
        'FROM lv.settlement_inclusion_current ic JOIN lv.processor_settlement_identity i ON (i.tenant_id, i.ledger_id, i.document_id) = '
        '(ic.tenant_id, ic.ledger_id, ic.settlement_document_id) WHERE ic.tenant_id = %(t)s AND ic.ledger_id = %(l)s AND ic.last_seq <= %(s)s '
        'AND ic.item_document_id = ANY(%(ids)s::uuid[]) ORDER BY ic.item_document_id', {**p, 'ids': list(payment_ids)})) if payment_ids else ()
    return payout, inclusions


def _load_for(connection, tenant_id, ledger_id, request):
    typed = item_mode(request) == 'TYPED'
    return load_snapshot(connection, tenant_id, ledger_id, () if typed else [i['source_ref'] for i in request['items']],
                         (request['processor'], request['payout']), [i['document_id'] for i in request['items']] if typed else ())


def preview_settlement(connection, tenant_id: UUID, ledger_id: UUID, request: dict) -> dict:
    """Load one consistent snapshot, then run the kernel's typed settlement simulation (pure)."""
    validate_request(request)
    return calculate_settlement(_load_for(connection, tenant_id, ledger_id, request), request)


def inspect_settlement(connection, tenant_id: UUID, ledger_id: UUID, request: dict, candidate: list) -> dict:
    """Load one consistent snapshot, then run kernel diagnostics on candidate settlement lines (pure)."""
    validate_request(request)
    return inspect_settlement_candidate(_load_for(connection, tenant_id, ledger_id, request), request, candidate)
