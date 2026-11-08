"""Capture of the immutable simulation input bundle from ONE consistent snapshot, and its exact (de)serialisation.

Capture runs on its own pooled lv_app connection in a REPEATABLE READ, READ ONLY transaction bound to the verified session's
tenant (row-level security), so every fact below comes from one MVCC snapshot whose ledger sequence is recorded:

* the proposal revision being simulated (operations, evidence, content hash) and the proposal state;
* the ledger facts the existing adapter needs (`kernel_adapter.loader._read`, reused unchanged): role bindings and accounts,
  periods, every posted entry up to the snapshot sequence, the balance projection, typed payment versions with their identity
  and posting, the payout identity's existing settlement, active inclusions, and the payout identity's staged statement;
* pinned sources: the statement document the draft cites (by id) with its record and file hashes, and every cited evidence
  reference's current identity and hash;
* the server-computed read set: item payment DOCUMENTs, the payout PERIOD and all bound ACCOUNTs whose configuration
  and balance projection enter the adapter calculation, each with its version in the same snapshot;
* the installed versions (simulation.versions).

Replay never reads the database again: `ledger_snapshot()` rebuilds the adapter's snapshot from the bundle alone.
"""
import base64
from dataclasses import fields
from decimal import Decimal
from uuid import UUID

from psycopg import IsolationLevel

from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.kernel_adapter import loader
from ledgervigil.kernel_adapter.types import (ActiveInclusion, Binding, EvidenceConflict, LedgerSnapshot, PaymentIdentity, Period,
                                              PostedEntry, PostedLine, ProjectionRow, ReferencedDocument, SettlementRef,
                                              StatementEvidence, TypedPayment)

from . import versions

TIMESTAMP_EXCLUDED = 'EXCLUDED_FROM_BUNDLE'
MAX_ENTRIES = 20000          # synchronous capture budget for posted entries (Stage 2B R-08; larger ledgers are refused)
_after_capture_counter = None   # test seam only (None in production): runs inside the capture transaction


class CaptureRefused(Exception):
    def __init__(self, status, code, detail):
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


# ------------------------------------------------------------------------------------------- (de)serialisation
def _dec(value: Decimal) -> str:
    return format(value, 'f')


def _encode(obj):
    """Dataclass tree -> canonical-safe JSON values. Decimal -> exact string; bytes -> base64; tuples -> lists."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, Decimal):
        return _dec(obj)
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode('ascii')
    if isinstance(obj, (tuple, list)):
        return [_encode(x) for x in obj]
    if hasattr(obj, '__dataclass_fields__'):
        return {f.name: _encode(getattr(obj, f.name)) for f in fields(obj)}
    raise TypeError(f'cannot encode {type(obj).__name__}')


DECIMAL_FIELDS = {PostedLine: {'amount'}, ProjectionRow: {'debit_total', 'credit_total'},
                  TypedPayment: {'amount', 'applied', 'unapplied'}, SettlementRef: {'gross', 'fee', 'net'}}


def _build(cls, data: dict):
    if set(data) != {f.name for f in fields(cls)}:
        raise ValueError(f'{cls.__name__}: fields differ')
    values = dict(data)
    for name in DECIMAL_FIELDS.get(cls, ()):
        values[name] = Decimal(values[name])
    return cls(**values)


def ledger_snapshot(facts: dict) -> LedgerSnapshot:
    """Rebuild the adapter snapshot exactly from captured facts (no database access)."""
    f = dict(facts)
    f['bindings'] = tuple(_build(Binding, b) for b in f['bindings'])
    f['periods'] = tuple(_build(Period, p) for p in f['periods'])
    f['entries'] = tuple(_build(PostedEntry, {**e, 'lines': tuple(_build(PostedLine, l) for l in e['lines'])}) for e in f['entries'])
    f['projection'] = tuple(_build(ProjectionRow, r) for r in f['projection'])
    f['documents'] = tuple(_build(ReferencedDocument, d) for d in f['documents'])
    if f['statement'] is not None:
        s = dict(f['statement'])
        s['source_file_content'] = base64.b64decode(s['source_file_content'], validate=True)
        s['items'] = tuple(tuple(i) for i in s['items'])
        f['statement'] = _build(StatementEvidence, s)
    f['statement_conflicts'] = tuple(_build(EvidenceConflict, c) for c in f['statement_conflicts'])
    f['payments'] = tuple(_build(TypedPayment, p) for p in f['payments'])
    f['statement_identities'] = tuple(_build(PaymentIdentity, i) for i in f['statement_identities'])
    f['payout_settlement'] = None if f['payout_settlement'] is None else _build(SettlementRef, f['payout_settlement'])
    f['active_inclusions'] = tuple(_build(ActiveInclusion, i) for i in f['active_inclusions'])
    return _build(LedgerSnapshot, f)


# ------------------------------------------------------------------------------------------- capture
def capture(pool, tenant_id: str, proposal_id: UUID, revision: int) -> dict:
    with pool.connection() as c:
        previous = (c.isolation_level, c.read_only)
        c.isolation_level, c.read_only = IsolationLevel.REPEATABLE_READ, True
        try:
            with tenant_transaction(c, UUID(str(tenant_id))):
                return _capture(c, str(tenant_id), proposal_id, revision)
        finally:
            c.isolation_level, c.read_only = previous


def _one(c, sql, params):
    rows = c.execute(sql, params).fetchall()
    return rows[0] if len(rows) == 1 else None


def _capture(c, tenant_id: str, proposal_id: UUID, revision: int, *, submitted=False) -> dict:
    expected = ('read committed', 'off') if submitted else ('repeatable read', 'on')
    if (c.execute('SHOW transaction_isolation').fetchone()[0], c.execute('SHOW transaction_read_only').fetchone()[0]) != expected:
        raise CaptureRefused(500, 'INTERNAL', 'capture is not isolated')
    head = _one(c, 'SELECT p.ledger_id, p.state::text, p.revision, l.purpose::text, l.source_kind::text, l.currency, l.currency_exponent, '
                   'l.profile_version FROM lv.proposal p JOIN lv.ledger l ON (l.tenant_id, l.id) = (p.tenant_id, p.ledger_id) WHERE p.id = %s',
                (proposal_id,))
    if head is None:
        raise CaptureRefused(404, 'NOT_FOUND', 'proposal not found')
    ledger_id, state, head_revision, purpose, source_kind, currency, exponent, profile = head
    rev = _one(c, 'SELECT operations, evidence_refs, content_hash, target_adapter FROM lv.proposal_revision WHERE proposal_id = %s '
                  'AND revision = %s', (proposal_id, revision))
    if rev is None:
        raise CaptureRefused(404, 'NOT_FOUND', 'proposal revision not found')
    if state != ('SUBMITTED' if submitted else 'DRAFT'):
        raise CaptureRefused(409, 'INVALID_STATE_TRANSITION', 'only DRAFT revisions are simulated here')
    operations, evidence_refs, content_hash, target_adapter = rev
    counter = c.execute('SELECT coalesce(max(last_seq), 0), coalesce(max(batch_head), %s), coalesce(max(event_head), %s) '
                        'FROM lv.ledger_counter WHERE ledger_id = %s', (loader.ZERO_HEAD, loader.ZERO_HEAD, ledger_id)).fetchone()
    if _after_capture_counter is not None:
        _after_capture_counter()
    bundle = {
        'bundle_format': versions.BUNDLE_FORMAT,
        'versions': versions.installed(),
        'tenant_id': tenant_id,
        'ledger': {'id': str(ledger_id), 'purpose': purpose, 'source': source_kind, 'currency': currency.strip(),
                   'currency_exponent': exponent, 'profile_version': profile},
        'proposal': {'id': str(proposal_id), 'revision': str(revision), 'head_revision_at_capture': str(head_revision), 'state_at_capture': state,
                     'content_hash': content_hash.strip(), 'target_adapter': target_adapter, 'operations': operations,
                     'evidence_refs': evidence_refs},
        'snapshot': {'ledger_id': str(ledger_id), 'seq': str(counter[0]), 'batch_head': counter[1], 'event_head': counter[2],
                     'profile_version': profile, 'posting_rule_version': versions.installed()['posting_rule'],
                     'policy_set_version': versions.installed()['policy_set'], 'provider_watermark': None, 'reconciliation': 'NOT_REQUIRED'},
        'read_set': [], 'pinned_sources': {}, 'facts': None,
    }
    op = operations[0] if len(operations) == 1 else None
    supported = (op is not None and isinstance(op, dict) and op.get('type') == 'RecordSettlement' and op.get('schema_version') == 1
                 and (purpose, source_kind) == ('SANDBOX', 'NATIVE'))
    if supported:
        n_entries = c.execute('SELECT count(*) FROM lv.journal_entry WHERE ledger_id = %s AND seq <= %s', (ledger_id, counter[0])).fetchone()[0]
        if n_entries > MAX_ENTRIES:
            raise CaptureRefused(422, 'CAPABILITY_UNAVAILABLE', f'ledgers with more than {MAX_ENTRIES} entries need asynchronous simulation, '
                                 'which is not built (Stage 2B: SIMULATION job)')
        payment_ids = sorted({i['document_id'] for i in op['items']})
        snap = loader._read(c, UUID(tenant_id), ledger_id, (), (op['processor_ref'], op['payout_id']), payment_ids, locked_submission=True) if submitted else loader._read(c, UUID(tenant_id), ledger_id, (), (op['processor_ref'], op['payout_id']), payment_ids)
        if str(snap.seq) != bundle['snapshot']['seq'] or snap.batch_head != counter[1]:
            raise CaptureRefused(500, 'INTERNAL', 'snapshot sequence changed inside one REPEATABLE READ transaction')
        # The transaction timestamp is not an input to any calculation (Stage 2A §10.2: no clock reads); it is excluded so that
        # identical committed state captures to an identical bundle.
        bundle['facts'] = {**_encode(snap), 'transaction_timestamp': TIMESTAMP_EXCLUDED}
        bundle['pinned_sources'] = _pinned_sources(c, ledger_id, op, evidence_refs)
        bundle['read_set'] = _read_set(c, ledger_id, snap, payment_ids, op.get('payout_date'))
    if not submitted and c.execute('SELECT pg_current_xact_id_if_assigned() IS NOT NULL').fetchone()[0]:
        raise CaptureRefused(500, 'INTERNAL', 'capture wrote')
    return bundle


def _pinned_sources(c, ledger_id, op, evidence_refs) -> dict:
    """Every lookup is scoped to the proposal's ledger (and, by RLS, its tenant)."""
    row = _one(c, 'SELECT s.id::text, s.doc_type::text, s.validation_status, s.provenance::text, s.processor_ref, s.payout_id, '
                  's.payout_date::text, s.currency, s.currency_exponent, s.gross, s.fee, s.net, s.item_count, s.financial_fingerprint, '
                  's.import_batch_id::text, r.id::text, r.content_hash, f.sha256 '
                  'FROM lv.staged_document s JOIN lv.source_record r ON (r.tenant_id, r.ledger_id, r.id, r.import_batch_id) = '
                  '(s.tenant_id, s.ledger_id, s.source_record_id, s.import_batch_id) '
                  'JOIN lv.import_batch b ON (b.tenant_id, b.ledger_id, b.id) = (s.tenant_id, s.ledger_id, s.import_batch_id) '
                  'JOIN lv.source_file f ON (f.tenant_id, f.ledger_id, f.id) = (b.tenant_id, b.ledger_id, b.source_file_id) '
                  'WHERE s.ledger_id = %s AND s.id = %s', (ledger_id, op['statement_document_id']))
    statement = None
    if row is not None:
        items = [[n, ref, kind, amount] for n, ref, kind, amount in c.execute(
            'SELECT line_no, item_ref, kind::text, amount FROM lv.staged_statement_item WHERE ledger_id = %s AND staged_document_id = %s '
            'ORDER BY line_no', (ledger_id, row[0])).fetchall()]
        statement = dict(zip(('id', 'type', 'validation_status', 'provenance', 'processor_ref', 'payout_id', 'payout_date', 'currency',
                              'currency_exponent', 'gross', 'fee', 'net', 'item_count', 'financial_fingerprint', 'import_batch_id',
                              'source_record_id', 'source_record_sha256', 'source_file_sha256'), row))
        statement['currency'] = statement['currency'].strip()
        statement['items'] = items
    evidence = []
    for ref in evidence_refs:
        kind, rid = ref['kind'], ref['id']
        if kind == 'SOURCE_RECORD':
            r = _one(c, 'SELECT content_hash FROM lv.source_record WHERE ledger_id = %s AND id = %s', (ledger_id, rid))
            evidence.append({'kind': kind, 'id': rid, 'present': r is not None, 'sha256': r[0] if r else None})
        elif kind == 'STAGED_DOCUMENT':
            r = _one(c, 'SELECT provenance::text, financial_fingerprint FROM lv.staged_document WHERE ledger_id = %s AND id = %s', (ledger_id, rid))
            evidence.append({'kind': kind, 'id': rid, 'present': r is not None, 'provenance': r[0] if r else None,
                             'financial_fingerprint': r[1] if r else None})
        elif kind == 'DOCUMENT_VERSION':
            r = _one(c, 'SELECT v.document_id::text, v.version, v.provenance::text, v.financial_fingerprint, dc.version FROM lv.document_version v '
                        'JOIN lv.document_current dc ON (dc.tenant_id, dc.ledger_id, dc.document_id) = (v.tenant_id, v.ledger_id, v.document_id) '
                        'WHERE v.ledger_id = %s AND v.id = %s', (ledger_id, rid))
            evidence.append({'kind': kind, 'id': rid, 'present': r is not None, 'document_id': r[0] if r else None,
                             'version': r[1] if r else None, 'provenance': r[2] if r else None,
                             'financial_fingerprint': r[3] if r else None, 'current_version': r[4] if r else None})
        else:
            evidence.append({'kind': kind, 'id': rid, 'present': False})
    return {'statement': statement, 'evidence': evidence}


def _read_set(c, ledger_id, snap, payment_ids, payout_date) -> list:
    """Stage 2A §9.1 read set: (entity type, id, version), computed here from the parameters, sorted."""
    entries = [{'entity_type': 'DOCUMENT', 'entity_id': p.document_id, 'version': p.version}
               for p in snap.payments if p.document_id in set(payment_ids)]
    period = _one(c, 'SELECT id::text, version FROM lv.fiscal_period WHERE ledger_id = %s AND start_date <= %s::date AND %s::date < end_date',
                  (ledger_id, payout_date, payout_date)) if payout_date else None
    if period is not None:
        entries.append({'entity_type': 'PERIOD', 'entity_id': period[0], 'version': period[1]})
    if versions.installed().get('account_read_set') == 'lv-account-dependencies/1':
        account_ids = sorted({b.account_id for b in snap.bindings})
        accounts = c.execute('SELECT id::text, version FROM lv.account WHERE ledger_id = %s AND id = ANY(%s::uuid[]) ORDER BY id',
                             (ledger_id, account_ids)).fetchall()
        if {a[0] for a in accounts} != set(account_ids):
            raise CaptureRefused(500, 'INTERNAL', 'account dependencies are incomplete')
        entries.extend({'entity_type': 'ACCOUNT', 'entity_id': aid, 'version': version} for aid, version in accounts)
    return sorted(entries, key=lambda e: (e['entity_type'], e['entity_id']))


def capture_submitted(ctx, proposal_id: UUID, revision: int) -> dict:
    """Single-connection capture after workflow_begin locked the full ledger/configuration domain.

    The transaction proof is produced only by the database begin function; a failed savepoint removes it and its locks.
    This path can write workflow rows, so it never claims the ordinary read-only capture guarantee.
    """
    proof = ctx.conn.execute("SELECT 1 FROM lv.proposal_transition WHERE tenant_id=%s AND proposal_id=%s AND revision=%s "
                             "AND to_state='SUBMITTED' AND tx_id=pg_current_xact_id() AND session_id=nullif(current_setting('app.session_id',true),'')::uuid",
                             (ctx.tenant_id,proposal_id,revision)).fetchone()
    if not proof:
        raise CaptureRefused(500,'INTERNAL','submission capture has no transaction lock proof')
    return _capture(ctx.conn,str(ctx.tenant_id),proposal_id,revision,submitted=True)
