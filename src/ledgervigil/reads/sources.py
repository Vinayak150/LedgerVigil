"""Stage 2B Family 5 and Family 3 (read subsets): native documents, import batches, source records and staged documents.

    GET /api/v1/ledgers/{ledger_id}/documents           read   page of DocumentSummary   ?type&status&party_id (+ refused filters)
    GET /api/v1/documents/{document_id}                 read   Document (current version, open amount, links, postings summary)
    GET /api/v1/imports                                 read   page of ImportBatch       ?ledger_id (additive, PC-S1)
    GET /api/v1/imports/{import_id}                     read   ImportBatch
    GET /api/v1/imports/{import_id}/records             read   page of SourceRecord      ?status=VALID|INVALID
    GET /api/v1/imports/{import_id}/staged-documents    read   page of StagedDocument

Purpose: let a user pick the typed payments, staged settlement statements and evidence a RecordSettlement draft cites, from
records that already exist, without pasting identifiers. Nothing here decides whether a record is *eligible* for a draft,
whether a statement *reconciles* with payments, or whether anything is *posted*: those rules are not decided by the owning
contracts for this data, so they are not invented (docs/SOURCE_READS.md, gaps SG-1..). The proposal API remains the only
place that accepts or refuses a reference.

Boundary and isolation (same as reads.ledgers):
* verified session -> tenant -> permission `read`, then the transaction is READ ONLY (auth.boundary.require_read_only);
* the tenant is only ever the session's; row-level security (FORCE) is the tenant filter, so another tenant's id is exactly
  as absent as a missing one (404 NOT_FOUND, identical body);
* every join between rows of these tables is on (tenant_id, ledger_id, id), so a reference never crosses ledgers;
* each list/detail representation is read by ONE SQL statement (one snapshot); where a second statement is used it reads only
  insert-only ingestion rows (immutable by trigger) of a batch already resolved;
* lists: only the listed query parameters (others 400), repeated parameters 422, limit 1-200, opaque HMAC-signed cursors
  bound to operation, tenant, ledger/batch, filters and order (any failure 400 CURSOR_MISMATCH);
* pages with large rows are additionally bounded by a content budget (STAGED_ITEM_BUDGET items, RECORD_CONTENT_BUDGET bytes);
  a page always holds at least one row and never more than `limit`, and the cursor continues exactly after the last row kept.

Money: amounts leave PostgreSQL as text and become the kernel's exact Money at the ledger's currency exponent (Decimal; a value
the kernel refuses is a 500, never a rounded figure). Wire format {"amount": "50000.00", "currency": "INR"}.
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ledger_kernel import money as kernel_money
from ledgervigil.auth.boundary import RequestContext, require_read_only
from ledgervigil.auth.problems import ProblemError
from ledgervigil.proposals.routes import EvidenceRef

from .ledgers import (ACCOUNTING_STATUS, CURSOR, LIMIT, NOT_FOUND, PROBLEM, READ_ERRORS, SANDBOX_LABEL, Money, _read_ledger,
                      codec)
from .paging import check_query, list_context, parse_limit
from .representation import render

log = logging.getLogger('ledgervigil.reads.sources')
router = APIRouter(prefix='/api/v1', tags=['sources'])

DOCUMENT_TYPES = ('PAYMENT', 'SETTLEMENT', 'MANUAL_JOURNAL')                 # lv.document_type, as it exists
NOT_IMPLEMENTED_TYPES = ('INVOICE', 'CREDIT_NOTE', 'REFUND')               # contracted document families with no native records
DOCUMENT_STATUSES = ('RECEIVED', 'SETTLED', 'RECORDED')                    # payment_current / settlement_current CHECKs
RECORD_STATUSES = ('VALID', 'INVALID')
STAGED_ITEM_BUDGET = 1000            # statement items per staged-documents page (one maximal statement always fits)
RECORD_CONTENT_BUDGET = 1048576      # source-record content bytes per records page (one maximal 256 KiB record always fits)
UTC = "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'"

UNAVAILABLE_FILTER = 'Contracted, NOT available: its semantics are not decided by the owning contracts. A value is refused with 422 ' \
                     'CAPABILITY_UNAVAILABLE (never ignored).'


# ------------------------------------------------------------------------------------------- schemas
class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PartyRef(Strict):
    id: str
    name: str


class DocumentSummary(Strict):
    id: str
    ledger_id: str
    type: str = Field(description='PAYMENT, SETTLEMENT or MANUAL_JOURNAL (lv.document_type as it exists)')
    source_ref: str = Field(description='Ledger-unique source reference: processor_ref/processor_item_id for a payment, '
                                        'processor_ref/payout_id for a settlement')
    version: int = Field(ge=1, description='Current document version')
    status: str | None = Field(description='Actual projection status: RECEIVED or SETTLED (payment), RECORDED (settlement); null for a '
                                           'manual journal, which has no status model')
    date: str | None = Field(description='The document\'s own accounting date: received_date (payment), payout_date (settlement), '
                                         'effective_date (manual journal)')
    amount: Money | None = Field(description='Payment amount, or settlement gross; null for a manual journal')
    party: PartyRef | None = Field(description='Customer of a payment; null otherwise')
    processor_ref: str | None
    processor_item_id: str | None = Field(description='Payment only: the processor\'s item id (statement items carry it as item_ref)')
    payout_id: str | None = Field(description='Settlement only')
    created_seq: int = Field(ge=1, description='Ledger sequence that created the document')
    version_seq: int = Field(ge=1, description='Ledger sequence that created the current version')
    provenance: str = Field(description='Provenance of the current version (SYNTHETIC in this milestone)')
    financial_fingerprint: str = Field(description='SHA-256 of the current version\'s canonical financial fields')
    evidence_ref: EvidenceRef = Field(description='Citable as proposal evidence: kind DOCUMENT_VERSION, the CURRENT version id')


class DocumentPage(Strict):
    ledger_id: str
    purpose: str
    data_label: str | None
    as_of_seq: int = Field(ge=0, description='Committed ledger sequence the page was read at (latest at request time)')
    items: list[DocumentSummary]
    next_cursor: str | None


class PaymentDetail(Strict):
    amount: Money
    applied: Money = Field(description='Payment applications are not implemented: always 0')
    unapplied: Money = Field(description='Open amount = amount - applied (2A §5.2)')
    received_date: str
    status: str
    settlement_document_id: str | None = Field(description='The settlement document that recorded this payment, when SETTLED')


class SettlementDocumentItem(Strict):
    line_no: int
    document_id: str = Field(description='Payment document id')
    kind: str
    amount: Money
    payment_version: int = Field(description='Payment version the settlement included')


class SettlementDetail(Strict):
    payout_date: str
    gross: Money
    fee: Money
    net: Money
    item_count: int
    status: str
    items: list[SettlementDocumentItem]


class DocumentPosting(Strict):
    entry_id: str
    entry_no: int
    seq: int
    kind: str
    effective_date: str
    posting_rule: str | None
    source_document_version: int | None


class Document(DocumentSummary):
    purpose: str
    data_label: str | None
    accounting_status: str = Field(description='UNREVIEWED: no credentialed accounting review exists (ADR-013)')
    current_version_id: str
    origin: str = Field(description='Commit kind that created the current version (SEED in this milestone)')
    reason: str
    description_untrusted: str | None = Field(description='Informational description from the source; untrusted text, display only')
    payment: PaymentDetail | None
    settlement: SettlementDetail | None
    postings: list[DocumentPosting] = Field(description='Journal entries whose source is this document, by sequence')


class SourceFileRef(Strict):
    id: str
    sha256: str = Field(description='SHA-256 of the received bytes')
    byte_size: int
    content_stored: bool = Field(description='False when the file exceeded the size bound and only its hash was kept')
    media_type: str
    declared_format: str
    declared_version: str
    data_classification: str
    received_at: str


class RecordCounts(Strict):
    VALID: int
    INVALID: int


class ImportBatch(Strict):
    id: str
    ledger_id: str
    purpose: str
    data_label: str | None
    status: str = Field(description='Actual stored outcome: STAGED, REJECTED or DUPLICATE (fixed at insert). The Stage 2B Part 7 '
                                    'batch lifecycle (RECEIVED, VALIDATED, RECONCILED ...) is not implemented (gap SG-3)')
    ledger_effect: str = Field(description='Always NONE: ingestion writes only ingestion tables (Family 3 "financial safety")')
    parser: str
    parser_version: str
    failure_code: str | None
    failure_detail: str | None
    related_staged_document_id: str | None = Field(description='DUPLICATE: the staged statement it repeats; REJECTED with '
                                                               'PAYOUT_IDENTITY_CONFLICT: the statement holding that identity')
    created_at: str
    source_file: SourceFileRef
    record_counts: RecordCounts
    staged_document_count: int


class ImportBatchPage(Strict):
    items: list[ImportBatch]
    next_cursor: str | None


class SourceRecord(Strict):
    id: str
    import_batch_id: str
    ledger_id: str
    record_index: int
    source_system: str
    source_key: str
    validation_status: str = Field(description='VALID (its batch was STAGED) or INVALID (its batch was REJECTED)')
    messages: list[dict] = Field(description='For INVALID: [{code, detail}] from the batch outcome; empty for VALID')
    content: str = Field(description='Raw canonical record text as received; untrusted, display only')
    byte_length: int
    evidence_ref: EvidenceRef = Field(description='Citable as proposal evidence: kind SOURCE_RECORD with its content SHA-256')


class SourceRecordPage(Strict):
    import_batch_id: str
    items: list[SourceRecord]
    next_cursor: str | None


class StagedStatementItem(Strict):
    line_no: int
    item_ref: str = Field(description='Processor item id as stated by the source (payments carry it as processor_item_id)')
    kind: str = Field(description='PAYMENT or REFUND as stated by the source')
    amount: Money


class StagedDocument(Strict):
    id: str
    import_batch_id: str
    ledger_id: str
    source_record_id: str
    type: str = Field(description='SETTLEMENT_STATEMENT')
    validation_status: str
    status: str = Field(description='Workflow status: VALID, RESERVED or POSTED; independent of immutable validation_status')
    version: int = Field(ge=1, description='Workflow CAS version; one before the first reservation')
    reserved_by_proposal_id: str | None
    provenance: str
    processor_ref: str
    payout_id: str
    payout_date: str
    gross: Money
    fee: Money
    net: Money
    item_count: int
    items: list[StagedStatementItem]
    financial_fingerprint: str
    description_untrusted: str
    duplicate_batch_count: int = Field(description='Later batches stored as DUPLICATE of this statement')
    conflicting_batch_count: int = Field(description='Later batches REJECTED with PAYOUT_IDENTITY_CONFLICT against this statement')
    evidence_ref: EvidenceRef = Field(description='Citable as proposal evidence: kind STAGED_DOCUMENT')
    source_record_evidence_ref: EvidenceRef = Field(description='Citable as proposal evidence: kind SOURCE_RECORD')


class StagedDocumentPage(Strict):
    import_batch_id: str
    items: list[StagedDocument]
    next_cursor: str | None


# ------------------------------------------------------------------------------------------- helpers
def _bad(pointer: str, code: str, detail: str):
    raise ProblemError(422, 'SCHEMA_INVALID', detail, errors=[{'pointer': pointer, 'code': code}])


def _uuid_param(raw: str | None, name: str) -> str | None:
    """Canonical lowercase UUID text, or 422. Bound into cursors in this canonical form."""
    if raw is None:
        return None
    try:
        if len(raw) > 64 or not raw.isascii():
            raise ValueError
        return str(UUID(raw))
    except ValueError:
        _bad(f'/query/{name}', 'uuid', f'{name} must be a UUID')


def _enum_param(raw: str | None, name: str, allowed: tuple[str, ...]) -> str | None:
    if raw is not None and raw not in allowed:
        _bad(f'/query/{name}', 'enum', f'{name} must be one of: ' + ', '.join(allowed))
    return raw


def _one(rows, what: str):
    if not rows:
        raise ProblemError(404, 'NOT_FOUND', f'{what} not found')
    if len(rows) > 1:      # ids are UUIDv7 and unique in practice; never pick one silently
        log.error('ambiguous %s id across ledgers', what)
        raise ProblemError(500, 'INTERNAL', f'{what} identifier is ambiguous')
    return rows[0]


def _currency(code: str, exponent: int) -> kernel_money.Currency:
    return kernel_money.Currency(code.strip(), exponent)


def _money(text: str, currency: kernel_money.Currency) -> dict:
    """Exact: a stored decimal that is not representable at the currency exponent is refused, never rounded."""
    try:
        return kernel_money.Money(Decimal(text), currency).wire()
    except (kernel_money.DomainError, InvalidOperation):
        log.error('stored amount is not contract Money')
        raise ProblemError(500, 'INTERNAL', 'a stored amount cannot be represented as contract Money') from None


def _label(purpose: str) -> str | None:
    return SANDBOX_LABEL if purpose == 'SANDBOX' else None


def _bounded(rows: list, limit: int, weight, budget: int) -> tuple[list, bool]:
    """Keep rows in order while count <= limit and the cumulative weight fits the budget; the first row is always kept.
    Returns (kept, more): more is True when any fetched row was not kept (rows were fetched with LIMIT limit + 1)."""
    kept, used = [], 0
    for row in rows[:limit]:
        w = weight(row)
        if kept and used + w > budget:
            break
        kept.append(row)
        used += w
    return kept, len(kept) < len(rows)


def _refuse_unavailable(name: str, raw: str | None, check=None):
    if raw is None:
        return
    if check is not None:
        check(raw)
    raise ProblemError(422, 'CAPABILITY_UNAVAILABLE', f'{name} is not available yet: its semantics are not decided by the owning '
                       'contracts (docs/SOURCE_READS.md). It is refused, never ignored.',
                       errors=[{'pointer': f'/query/{name}', 'code': 'unavailable'}])


def _date_check(name):
    def check(raw):
        from datetime import date
        try:
            if len(raw) != 10 or not raw.isascii():
                raise ValueError
            date.fromisoformat(raw)
        except ValueError:
            _bad(f'/query/{name}', 'date', f'{name} must be a calendar date YYYY-MM-DD')
    return check


def _seq_check(raw):
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18 or (len(raw) > 1 and raw[0] == '0'):
        _bad('/query/as_of_seq', 'integer', 'as_of_seq must be a non-negative integer')


# ------------------------------------------------------------------------------------------- documents
DOCUMENT_COLUMNS = """
 d.id::text, d.ledger_id::text, d.doc_type::text, d.source_ref, c.version, d.seq, v.seq, v.id::text, v.provenance::text,
 v.financial_fingerprint,
 CASE d.doc_type::text WHEN 'PAYMENT' THEN v.financial_fields->>'received_date' WHEN 'SETTLEMENT' THEN v.financial_fields->>'payout_date'
      ELSE v.financial_fields->>'effective_date' END,
 v.financial_fields->>'processor_ref', pi.processor_item_id, si.payout_id,
 coalesce(pc.status, sc.status), pc.amount::text, sc.gross::text, p.id::text, p.name,
 l.currency, l.currency_exponent, l.purpose::text"""
DOCUMENT_FROM = """
FROM lv.document d
JOIN lv.ledger l ON (l.tenant_id, l.id) = (d.tenant_id, d.ledger_id)
JOIN lv.document_current c ON (c.tenant_id, c.ledger_id, c.document_id) = (d.tenant_id, d.ledger_id, d.id)
JOIN lv.document_version v ON (v.tenant_id, v.ledger_id, v.document_id, v.version) = (c.tenant_id, c.ledger_id, c.document_id, c.version)
LEFT JOIN lv.payment_current pc ON (pc.tenant_id, pc.ledger_id, pc.document_id) = (d.tenant_id, d.ledger_id, d.id)
LEFT JOIN lv.processor_payment_identity pi ON (pi.tenant_id, pi.ledger_id, pi.document_id) = (d.tenant_id, d.ledger_id, d.id)
LEFT JOIN lv.party p ON (p.tenant_id, p.ledger_id, p.id) = (pc.tenant_id, pc.ledger_id, pc.party_id)
LEFT JOIN lv.settlement_current sc ON (sc.tenant_id, sc.ledger_id, sc.document_id) = (d.tenant_id, d.ledger_id, d.id)
LEFT JOIN lv.processor_settlement_identity si ON (si.tenant_id, si.ledger_id, si.document_id) = (d.tenant_id, d.ledger_id, d.id)"""
DOCUMENT_FIELDS = 21


def _summary(row) -> dict:
    (did, lid, kind, source_ref, version, created_seq, version_seq, version_id, provenance, fingerprint, when, processor_ref,
     item_id, payout_id, status, pay_amount, gross, party_id, party_name, cur, exponent, _purpose) = row[:DOCUMENT_FIELDS + 1]
    currency = _currency(cur, exponent)
    amount = pay_amount if kind == 'PAYMENT' else gross if kind == 'SETTLEMENT' else None
    if kind in ('PAYMENT', 'SETTLEMENT') and (amount is None or status is None):
        log.error('typed document without its current projection')
        raise ProblemError(500, 'INTERNAL', 'document projection is missing')
    return {'id': did, 'ledger_id': lid, 'type': kind, 'source_ref': source_ref, 'version': version, 'status': status, 'date': when,
            'amount': None if amount is None else _money(amount, currency),
            'party': {'id': party_id, 'name': party_name} if party_id else None,
            'processor_ref': processor_ref, 'processor_item_id': item_id, 'payout_id': payout_id,
            'created_seq': created_seq, 'version_seq': version_seq, 'provenance': provenance, 'financial_fingerprint': fingerprint,
            'evidence_ref': {'kind': 'DOCUMENT_VERSION', 'id': version_id, 'provenance': provenance, 'sha256': None}}


@router.get('/ledgers/{ledger_id}/documents', response_model=DocumentPage, operation_id='listLedgerDocuments',
            responses={**READ_ERRORS, **NOT_FOUND,
                       422: {'description': 'SCHEMA_INVALID (limit, repeated parameter, malformed id/type/status/party_id/date/'
                                            'as_of_seq); CAPABILITY_UNAVAILABLE (type INVOICE/CREDIT_NOTE/REFUND has no native records; '
                                            'date_from, date_to, q and as_of_seq semantics undecided)', **PROBLEM}},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'id descending (UUIDv7: newest first)'})
def list_documents(ledger_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                   limit: LIMIT = None, cursor: CURSOR = None,
                   type: Annotated[str | None, Query(max_length=32, description='PAYMENT, SETTLEMENT or MANUAL_JOURNAL. INVOICE, '
                       'CREDIT_NOTE and REFUND are contracted but have no native records: 422 CAPABILITY_UNAVAILABLE')] = None,
                   status: Annotated[str | None, Query(max_length=32, description='RECEIVED, SETTLED or RECORDED (exact match on '
                       'the actual projection status; manual journals have none and never match)')] = None,
                   party_id: Annotated[str | None, Query(max_length=64, description='Customer party id (payments)')] = None,
                   date_from: Annotated[str | None, Query(max_length=32, description=UNAVAILABLE_FILTER)] = None,
                   date_to: Annotated[str | None, Query(max_length=32, description=UNAVAILABLE_FILTER)] = None,
                   q: Annotated[str | None, Query(max_length=256, description=UNAVAILABLE_FILTER)] = None,
                   as_of_seq: Annotated[str | None, Query(max_length=32, description=UNAVAILABLE_FILTER + ' The page states the '
                       'sequence it was read at as as_of_seq.')] = None):
    check_query(request, ('limit', 'cursor', 'type', 'status', 'party_id', 'date_from', 'date_to', 'q', 'as_of_seq'))
    size = parse_limit(limit)
    if type in NOT_IMPLEMENTED_TYPES:
        raise ProblemError(422, 'CAPABILITY_UNAVAILABLE', f'{type} documents do not exist in the native ledger yet',
                           errors=[{'pointer': '/query/type', 'code': 'unavailable'}])
    kind = _enum_param(type, 'type', DOCUMENT_TYPES + NOT_IMPLEMENTED_TYPES)
    state = _enum_param(status, 'status', DOCUMENT_STATUSES)
    party = _uuid_param(party_id, 'party_id')
    _refuse_unavailable('date_from', date_from, _date_check('date_from'))
    _refuse_unavailable('date_to', date_to, _date_check('date_to'))
    _refuse_unavailable('q', q)
    _refuse_unavailable('as_of_seq', as_of_seq, _seq_check)
    ledger = _read_ledger(ctx, ledger_id)
    filters = {'type': kind, 'status': state, 'party_id': party}
    context = list_context('listLedgerDocuments', ctx.tenant_id, ledger_id, 'id:desc', {k: v for k, v in filters.items() if v})
    after = codec(request).decode(context, cursor, 1) if cursor is not None else None
    where, params = ['d.ledger_id = %s'], [ledger_id]
    if kind:
        where.append('d.doc_type::text = %s'); params.append(kind)
    if state:
        where.append('coalesce(pc.status, sc.status) = %s'); params.append(state)
    if party:
        where.append('pc.party_id = %s::uuid'); params.append(party)
    if after:
        where.append('d.id < %s::uuid'); params.append(after[0])
    rows = ctx.conn.execute(
        f'SELECT {DOCUMENT_COLUMNS}, (SELECT coalesce(max(k.last_seq), 0) FROM lv.ledger_counter k WHERE k.ledger_id = %s) '
        f'{DOCUMENT_FROM} WHERE {" AND ".join(where)} ORDER BY d.id DESC LIMIT %s', (ledger_id, *params, size + 1)).fetchall()
    if rows:
        seq = rows[0][-1]
    else:
        seq = ctx.conn.execute('SELECT coalesce(max(last_seq), 0) FROM lv.ledger_counter WHERE ledger_id = %s', (ledger_id,)).fetchone()[0]
    kept = rows[:size]
    items = [_summary(r) for r in kept]
    next_cursor = codec(request).encode(context, [items[-1]['id']]) if len(rows) > size else None
    return render(request, DocumentPage, {'ledger_id': str(ledger_id), 'purpose': ledger['purpose'], 'data_label': ledger['data_label'],
                                          'as_of_seq': seq, 'items': items, 'next_cursor': next_cursor})


@router.get('/documents/{document_id}', response_model=Document, operation_id='getDocument', responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-accounting-status': ACCOUNTING_STATUS})
def get_document(document_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    rows = ctx.conn.execute(
        f"""SELECT {DOCUMENT_COLUMNS}, v.origin::text, v.reason, v.informational_fields->>'description',
              pc.applied::text, pc.unapplied::text, pc.settlement_document_id::text,
              sc.fee::text, sc.net::text, sc.item_count, v.financial_fields->'items',
              (SELECT coalesce(jsonb_agg(jsonb_build_object('entry_id', e.id::text, 'entry_no', e.entry_no, 'seq', e.seq, 'kind', e.kind::text,
                        'effective_date', e.effective_date::text,
                        'posting_rule', CASE WHEN e.posting_rule_id IS NULL THEN NULL ELSE e.posting_rule_id || '/' || e.posting_rule_version END,
                        'source_document_version', e.source_document_version) ORDER BY e.seq, e.id), '[]'::jsonb)
                 FROM lv.journal_entry e WHERE (e.tenant_id, e.ledger_id, e.source_document_id) = (d.tenant_id, d.ledger_id, d.id))
            {DOCUMENT_FROM} WHERE d.id = %s""", (document_id,)).fetchall()
    row = _one(rows, 'document')
    body = _summary(row)
    purpose = row[DOCUMENT_FIELDS]
    (origin, reason, description, applied, unapplied, settled_by, fee, net, item_count, items, postings) = row[DOCUMENT_FIELDS + 1:]
    currency = _currency(row[19], row[20])
    payment = settlement = None
    if body['type'] == 'PAYMENT':
        payment = {'amount': body['amount'], 'applied': _money(applied, currency), 'unapplied': _money(unapplied, currency),
                   'received_date': body['date'], 'status': body['status'], 'settlement_document_id': settled_by}
    elif body['type'] == 'SETTLEMENT':
        try:
            lines = [{'line_no': int(i['line_no']), 'document_id': str(UUID(i['document_id'])), 'kind': i['kind'],
                      'amount': kernel_money.Money.parse(i['amount'], currency).wire(), 'payment_version': int(i['payment_version'])}
                     for i in items]
        except (KeyError, TypeError, ValueError, kernel_money.DomainError):
            log.error('settlement financial fields are not in the recorded shape')
            raise ProblemError(500, 'INTERNAL', 'settlement items cannot be represented') from None
        settlement = {'payout_date': body['date'], 'gross': body['amount'], 'fee': _money(fee, currency), 'net': _money(net, currency),
                      'item_count': item_count, 'status': body['status'], 'items': lines}
    body.update({'purpose': purpose, 'data_label': _label(purpose), 'accounting_status': ACCOUNTING_STATUS,
                 'current_version_id': body['evidence_ref']['id'], 'origin': origin, 'reason': reason, 'description_untrusted': description,
                 'payment': payment, 'settlement': settlement, 'postings': postings})
    return render(request, Document, body)


# ------------------------------------------------------------------------------------------- imports
IMPORT_COLUMNS = f"""
 b.id::text, b.ledger_id::text, l.purpose::text, b.status::text, b.parser, b.parser_version, b.failure_code, b.failure_detail,
 b.related_staged_document_id::text, to_char(b.created_at AT TIME ZONE 'UTC', {UTC}),
 f.id::text, f.sha256, f.byte_size, f.content IS NOT NULL, f.media_type, f.declared_format, f.declared_version, f.data_classification,
 to_char(f.received_at AT TIME ZONE 'UTC', {UTC}),
 (SELECT count(*) FROM lv.source_record r WHERE (r.tenant_id, r.ledger_id, r.import_batch_id) = (b.tenant_id, b.ledger_id, b.id)),
 (SELECT count(*) FROM lv.staged_document s WHERE (s.tenant_id, s.ledger_id, s.import_batch_id) = (b.tenant_id, b.ledger_id, b.id))"""
IMPORT_FROM = """
FROM lv.import_batch b
JOIN lv.source_file f ON (f.tenant_id, f.ledger_id, f.id) = (b.tenant_id, b.ledger_id, b.source_file_id)
JOIN lv.ledger l ON (l.tenant_id, l.id) = (b.tenant_id, b.ledger_id)"""


def _record_status(batch_status: str) -> str | None:
    """A source record is stored for STAGED and REJECTED batches only (a DUPLICATE stores none)."""
    return {'STAGED': 'VALID', 'REJECTED': 'INVALID'}.get(batch_status)


def _batch(row) -> dict:
    (bid, lid, purpose, status, parser, parser_version, failure_code, failure_detail, related, created, fid, sha, size, stored, media,
     declared_format, declared_version, classification, received, records, staged) = row
    counts = {'VALID': 0, 'INVALID': 0}
    if records:
        counts[_record_status(status)] = records
    return {'id': bid, 'ledger_id': lid, 'purpose': purpose, 'data_label': _label(purpose), 'status': status, 'ledger_effect': 'NONE',
            'parser': parser, 'parser_version': parser_version, 'failure_code': failure_code, 'failure_detail': failure_detail,
            'related_staged_document_id': related, 'created_at': created,
            'source_file': {'id': fid, 'sha256': sha, 'byte_size': size, 'content_stored': stored, 'media_type': media,
                            'declared_format': declared_format, 'declared_version': declared_version,
                            'data_classification': classification, 'received_at': received},
            'record_counts': counts, 'staged_document_count': staged}


def _read_batch(ctx: RequestContext, import_id: UUID) -> dict:
    return _batch(_one(ctx.conn.execute(f'SELECT {IMPORT_COLUMNS} {IMPORT_FROM} WHERE b.id = %s', (import_id,)).fetchall(), 'import'))


@router.get('/imports', response_model=ImportBatchPage, operation_id='listImports',
            responses={**READ_ERRORS, 404: {'description': 'NOT_FOUND (ledger_id filter names an absent or other-tenant ledger)', **PROBLEM}},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'id descending (UUIDv7: newest first)'})
def list_imports(request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                 limit: LIMIT = None, cursor: CURSOR = None,
                 ledger_id: Annotated[str | None, Query(max_length=64, description='Only batches of this ledger. Additive filter, not in '
                     'the Family 3 table (PC-S1); an absent or other-tenant ledger is 404')] = None):
    check_query(request, ('limit', 'cursor', 'ledger_id'))
    size = parse_limit(limit)
    ledger = _uuid_param(ledger_id, 'ledger_id')
    if ledger:
        _read_ledger(ctx, UUID(ledger))
    context = list_context('listImports', ctx.tenant_id, ledger, 'id:desc')
    after = codec(request).decode(context, cursor, 1) if cursor is not None else None
    where, params = [], []
    if ledger:
        where.append('b.ledger_id = %s::uuid'); params.append(ledger)
    if after:
        where.append('b.id < %s::uuid'); params.append(after[0])
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = ctx.conn.execute(f'SELECT {IMPORT_COLUMNS} {IMPORT_FROM} {clause} ORDER BY b.id DESC LIMIT %s', (*params, size + 1)).fetchall()
    items = [_batch(r) for r in rows[:size]]
    next_cursor = codec(request).encode(context, [items[-1]['id']]) if len(rows) > size else None
    return render(request, ImportBatchPage, {'items': items, 'next_cursor': next_cursor})


@router.get('/imports/{import_id}', response_model=ImportBatch, operation_id='getImport', responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read'})
def get_import(import_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    return render(request, ImportBatch, _read_batch(ctx, import_id))


@router.get('/imports/{import_id}/records', response_model=SourceRecordPage, operation_id='listImportRecords',
            responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'record_index, id',
                           'x-lv-page-budget': f'{RECORD_CONTENT_BUDGET} content bytes per page (at least one record)'})
def list_records(import_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                 limit: LIMIT = None, cursor: CURSOR = None,
                 status: Annotated[str | None, Query(max_length=16, description='VALID or INVALID')] = None):
    check_query(request, ('limit', 'cursor', 'status'))
    size = parse_limit(limit)
    wanted = _enum_param(status, 'status', RECORD_STATUSES)
    batch = _read_batch(ctx, import_id)
    context = list_context('listImportRecords', ctx.tenant_id, batch['ledger_id'], 'record_index,id',
                           {'import': batch['id'], **({'status': wanted} if wanted else {})})
    after = codec(request).decode(context, cursor, 2) if cursor is not None else None
    if after and (not after[0].isascii() or not after[0].isdigit() or len(after[0]) > 10):
        raise ProblemError(400, 'CURSOR_MISMATCH', 'cursor is not valid for this request; restart from the first page',
                           errors=[{'pointer': '/query/cursor', 'code': 'invalid'}])
    own = _record_status(batch['status'])
    if wanted is not None and wanted != own:
        rows = []                    # every record of a batch has the batch's validation outcome
    else:
        where, params = '', []
        if after:
            where, params = 'AND (r.record_index, r.id) > (%s::integer, %s::uuid)', after
        rows = ctx.conn.execute(
            'SELECT r.id::text, r.record_index, r.source_system, r.source_key, r.content_hash, octet_length(r.content) '
            'FROM lv.source_record r WHERE r.ledger_id = %s AND r.import_batch_id = %s ' + where +
            ' ORDER BY r.record_index, r.id LIMIT %s', (batch['ledger_id'], batch['id'], *params, size + 1)).fetchall()
    kept, more = _bounded(rows, size, lambda r: r[5], RECORD_CONTENT_BUDGET)
    content = {}
    if kept:
        content = dict(ctx.conn.execute('SELECT id::text, content FROM lv.source_record WHERE ledger_id = %s AND import_batch_id = %s '
                                        'AND id = ANY(%s::uuid[])', (batch['ledger_id'], batch['id'], [r[0] for r in kept])).fetchall())
    messages = [] if own == 'VALID' else [{'code': batch['failure_code'], 'detail': batch['failure_detail']}]
    items = [{'id': rid, 'import_batch_id': batch['id'], 'ledger_id': batch['ledger_id'], 'record_index': index, 'source_system': system,
              'source_key': key, 'validation_status': own, 'messages': messages, 'content': content[rid], 'byte_length': length,
              'evidence_ref': {'kind': 'SOURCE_RECORD', 'id': rid, 'provenance': None, 'sha256': sha}}
             for rid, index, system, key, sha, length in kept]
    next_cursor = codec(request).encode(context, [str(kept[-1][1]), kept[-1][0]]) if more else None
    return render(request, SourceRecordPage, {'import_batch_id': batch['id'], 'items': items, 'next_cursor': next_cursor})


@router.get('/imports/{import_id}/staged-documents', response_model=StagedDocumentPage, operation_id='listImportStagedDocuments',
            responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'id',
                           'x-lv-page-budget': f'{STAGED_ITEM_BUDGET} statement items per page (at least one document)'})
def list_staged_documents(import_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                          limit: LIMIT = None, cursor: CURSOR = None):
    check_query(request, ('limit', 'cursor'))
    size = parse_limit(limit)
    batch = _read_batch(ctx, import_id)
    context = list_context('listImportStagedDocuments', ctx.tenant_id, batch['ledger_id'], 'id', {'import': batch['id']})
    after = codec(request).decode(context, cursor, 1) if cursor is not None else None
    where, params = ('AND s.id > %s::uuid', after) if after else ('', [])
    rows = ctx.conn.execute(
        'SELECT s.id::text, s.source_record_id::text, s.doc_type::text, s.validation_status, s.provenance::text, s.processor_ref, '
        's.payout_id, s.payout_date::text, s.currency, s.currency_exponent, s.gross, s.fee, s.net, s.item_count, s.financial_fingerprint, '
        's.description_untrusted, r.content_hash, '
        "(SELECT count(*) FROM lv.import_batch o WHERE (o.tenant_id, o.ledger_id, o.related_staged_document_id) = (s.tenant_id, s.ledger_id, s.id) "
        "  AND o.status = 'DUPLICATE'), "
        "(SELECT count(*) FROM lv.import_batch o WHERE (o.tenant_id, o.ledger_id, o.related_staged_document_id) = (s.tenant_id, s.ledger_id, s.id) "
        "  AND o.status = 'REJECTED' AND o.failure_code = 'PAYOUT_IDENTITY_CONFLICT'), "
        "coalesce(w.status,'VALID'),coalesce(w.version,1),w.proposal_id::text "
        'FROM lv.staged_document s JOIN lv.source_record r ON (r.tenant_id, r.ledger_id, r.id, r.import_batch_id) = '
        '(s.tenant_id, s.ledger_id, s.source_record_id, s.import_batch_id) '
        'LEFT JOIN lv.staged_workflow w ON (w.tenant_id,w.ledger_id,w.staged_document_id)=(s.tenant_id,s.ledger_id,s.id) '
        f'WHERE s.ledger_id = %s AND s.import_batch_id = %s {where} ORDER BY s.id LIMIT %s',
        (batch['ledger_id'], batch['id'], *params, size + 1)).fetchall()
    kept, more = _bounded(rows, size, lambda r: r[13], STAGED_ITEM_BUDGET)
    lines: dict[str, list] = {r[0]: [] for r in kept}
    if kept:
        for sid, line_no, ref, kind, amount in ctx.conn.execute(
                'SELECT staged_document_id::text, line_no, item_ref, kind::text, amount FROM lv.staged_statement_item '
                'WHERE ledger_id = %s AND staged_document_id = ANY(%s::uuid[]) ORDER BY staged_document_id, line_no',
                (batch['ledger_id'], list(lines))).fetchall():
            lines[sid].append((line_no, ref, kind, amount))
    items = []
    try:
        for (sid, record_id, doc_type, validation, provenance, processor_ref, payout_id, payout_date, cur, exponent, gross, fee, net,
             item_count, fingerprint, description, record_sha, duplicates, conflicts, status, workflow_version, reserved_by) in kept:
            currency = _currency(cur, exponent)
            parse = lambda text: kernel_money.Money.parse(text, currency).wire()   # noqa: E731
            if len(lines[sid]) != item_count:
                raise ProblemError(500, 'INTERNAL', 'staged statement items are incomplete')
            items.append({'id': sid, 'import_batch_id': batch['id'], 'ledger_id': batch['ledger_id'], 'source_record_id': record_id,
                          'type': doc_type, 'validation_status': validation, 'status':status,'version':workflow_version,'reserved_by_proposal_id':reserved_by,'provenance': provenance, 'processor_ref': processor_ref,
                          'payout_id': payout_id, 'payout_date': payout_date, 'gross': parse(gross), 'fee': parse(fee), 'net': parse(net),
                          'item_count': item_count,
                          'items': [{'line_no': n, 'item_ref': ref, 'kind': kind, 'amount': parse(amount)} for n, ref, kind, amount in lines[sid]],
                          'financial_fingerprint': fingerprint, 'description_untrusted': description,
                          'duplicate_batch_count': duplicates, 'conflicting_batch_count': conflicts,
                          'evidence_ref': {'kind': 'STAGED_DOCUMENT', 'id': sid, 'provenance': provenance, 'sha256': None},
                          'source_record_evidence_ref': {'kind': 'SOURCE_RECORD', 'id': record_id, 'provenance': None, 'sha256': record_sha}})
    except kernel_money.DomainError:
        log.error('staged amount is not contract Money')
        raise ProblemError(500, 'INTERNAL', 'a staged amount cannot be represented as contract Money') from None
    next_cursor = codec(request).encode(context, [kept[-1][0]]) if more else None
    return render(request, StagedDocumentPage, {'import_batch_id': batch['id'], 'items': items, 'next_cursor': next_cursor})
