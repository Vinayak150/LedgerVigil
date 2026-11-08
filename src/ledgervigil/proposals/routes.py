"""Stage 2B Family 8 (draft subset): proposal drafts and their revision history.

    POST  /api/v1/proposals                    propose  Idempotency-Key required -> 201 Proposal (DRAFT, revision 1), ETag "v1"
    GET   /api/v1/proposals                    read     ?ledger_id&state&created_by&reviewable_by_me&limit&cursor -> page
    GET   /api/v1/proposals/{id}               read     -> Proposal, ETag "v<version>"
    GET   /api/v1/proposals/{id}/revisions     read     ?limit&cursor -> page of ProposalRevision (oldest first)
    PATCH /api/v1/proposals/{id}               propose  If-Match required -> 200 Proposal (new revision), ETag "v<version>"

Only DRAFT proposals exist, only on native SANDBOX ledgers, only with RecordSettlement operations. No route here submits,
simulates, approves, executes, or writes any authoritative financial table.

Writes: the HTTP boundary verifies the session, CSRF/Origin and `propose`; the body is validated (validation.py); then ONE call
to a SECURITY DEFINER function (lv.proposal_create / lv.proposal_edit, migration 0010) re-binds the session, re-checks
`propose`, resolves every reference inside the proposal's ledger, writes the proposal, its immutable revision and reference
rows, the audit event and (create) the stored idempotent response in the request's transaction. The route reports only after
COMMIT, so a commit-time refusal (the deferred revision validator) is a 500, never a reported success.
"""
import logging
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from ledgervigil.auth.boundary import RequestContext, require_read_only, session_transaction, source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.reads.ledgers import CURSOR, LIMIT, Money, codec
from ledgervigil.reads.paging import check_query, list_context, page, parse_limit
from ledgervigil.reads.representation import render

from . import validation as v

log = logging.getLogger('ledgervigil.proposals')
router = APIRouter(prefix='/api/v1', tags=['proposals'])
IDEMPOTENCY_KEY = re.compile(r'^[!-~]{1,64}$')
STATES = ('DRAFT', 'SUBMITTED', 'BLOCKED', 'READY_FOR_REVIEW', 'APPROVED', 'REJECTED', 'STALE', 'EXECUTING', 'EXECUTED',
          'EXECUTION_FAILED', 'NEEDS_ATTENTION', 'WITHDRAWN')
PROBLEM = {'content': {'application/problem+json': {}}}


# ------------------------------------------------------------------------------------------- schemas
class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SettlementItem(Strict):
    document_id: str = Field(description='A PAYMENT document of the same ledger')
    kind: str = Field(description='PAYMENT (REFUND is CAPABILITY_UNAVAILABLE: no native refund documents)')
    amount: Money = Field(description='The full payment amount (SP-1 settles items whole)')


class RecordSettlementOp(Strict):
    type: str = Field(description='RecordSettlement')
    schema_version: int = Field(description='1')
    processor_ref: str = Field(max_length=64)
    payout_id: str = Field(max_length=64)
    payout_date: str = Field(description='YYYY-MM-DD')
    items: list[SettlementItem] = Field(min_length=1, max_length=1000)
    gross: Money
    fee: Money
    net: Money
    statement_document_id: str = Field(description='A staged SETTLEMENT_STATEMENT of the same ledger (gap PG-2)')


class EvidenceRef(Strict):
    kind: str = Field(description='SOURCE_RECORD, DOCUMENT_VERSION or STAGED_DOCUMENT (ATTACHMENT not available)')
    id: str
    provenance: str | None = Field(default=None, description='Read-only; from the referenced record where it has one')
    sha256: str | None = Field(default=None, description='Read-only; content hash of a SOURCE_RECORD')


class Proposal(Strict):
    id: str
    ledger_id: str
    ledger_purpose: str
    state: str = Field(description='DRAFT, BLOCKED, READY_FOR_REVIEW, APPROVED, REJECTED, EXECUTED, STALE or WITHDRAWN in the native workflow')
    revision: int = Field(ge=1)
    version: int = Field(ge=1, description='Concurrency token; ETag is "v<version>". Equals revision while DRAFT')
    origin: str
    ai_draft_id: str | None
    target_adapter: str
    operations: list[RecordSettlementOp]
    evidence_refs: list[EvidenceRef]
    rationale: str | None
    snapshot: dict | None = Field(description='Pinned at submission; null for a draft')
    read_set: list[dict] = Field(description='Server computed and pinned at submission; empty for a draft')
    latest_simulation_id: str | None
    created_by: str = Field(description='Maker: the session user who created revision 1')
    submitted_by: str | None
    editors: list[str] = Field(description='Every user who authored a revision, maker first (future approval exclusion)')
    created_at: str
    updated_at: str
    accounting_status: str = Field(description='UNREVIEWED')
    data_label: str | None
    allowed_actions: list[str] = Field(description='Authorized caller actions; APPROVE/REJECT require an independent cooled reviewer and current native capture; EXECUTE is gated separately by the current native execution checks')


class ProposalPage(Strict):
    items: list[Proposal]
    next_cursor: str | None


class ProposalRevision(Strict):
    proposal_id: str
    revision: int
    change: str = Field(description='CREATED (revision 1) or EDITED')
    authored_by: str
    created_at: str
    target_adapter: str
    operations: list[RecordSettlementOp]
    evidence_refs: list[EvidenceRef]
    rationale: str | None
    content_hash: str = Field(description='SHA-256 of the canonical revision content')


class RevisionPage(Strict):
    proposal_id: str
    items: list[ProposalRevision]
    next_cursor: str | None


MONEY_REF = {'$ref': '#/components/schemas/Money'}
OP_REF = {'$ref': '#/components/schemas/RecordSettlementOp'}
EVIDENCE_IN = {'type': 'object', 'additionalProperties': False, 'required': ['kind', 'id'],
               'properties': {'kind': {'enum': ['SOURCE_RECORD', 'DOCUMENT_VERSION', 'STAGED_DOCUMENT', 'ATTACHMENT']},
                              'id': {'type': 'string', 'format': 'uuid'}}}
CREATE_BODY = {'required': True, 'content': {'application/json': {'schema': {
    'title': 'ProposalCreate', 'type': 'object', 'additionalProperties': False, 'required': ['ledger_id', 'target_adapter', 'operations'],
    'properties': {'ledger_id': {'type': 'string', 'format': 'uuid'}, 'target_adapter': {'enum': ['NATIVE', 'QUICKBOOKS']},
                   'operations': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': OP_REF},
                   'evidence_refs': {'type': 'array', 'maxItems': 100, 'items': EVIDENCE_IN},
                   'rationale': {'type': ['string', 'null'], 'maxLength': 4000}}}}}}
PATCH_BODY = {'required': True, 'content': {'application/json': {'schema': {
    'title': 'ProposalPatch', 'type': 'object', 'additionalProperties': False, 'minProperties': 1,
    'properties': {'operations': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': OP_REF},
                   'evidence_refs': {'type': 'array', 'maxItems': 100, 'items': EVIDENCE_IN},
                   'rationale': {'type': ['string', 'null'], 'maxLength': 4000}}}}}}
READ_ERRORS = {304: {'description': 'Not Modified (only after authentication)'},
               400: {'description': 'UNKNOWN_QUERY_PARAMETER; CURSOR_MISMATCH', **PROBLEM},
               401: {'description': 'UNAUTHENTICATED', **PROBLEM}, 403: {'description': 'PERMISSION_DENIED', **PROBLEM},
               422: {'description': 'SCHEMA_INVALID (filter, limit, repeated parameter, malformed id)', **PROBLEM}}
NOT_FOUND = {404: {'description': 'NOT_FOUND (absent or another tenant; indistinguishable)', **PROBLEM}}


# ------------------------------------------------------------------------------------------- helpers
async def raw_body(request: Request) -> bytes | None:
    """The request body, read at most 1 MiB + 1 byte; None when larger. Judged only after authentication."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > v.MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b''.join(chunks)


def _actions(ctx: RequestContext, body: dict, *, review: bool = True) -> list[str]:
    if review and body['state']=='READY_FOR_REVIEW' and body['created_by']!=ctx.user_id and ctx.user_id not in body['editors'] and 'review' in ctx.permissions and ctx.conn.execute('SELECT lv.approval_available(%s,%s)',(body['id'],ctx.user_id)).fetchone()[0]:return ['APPROVE','REJECT']
    if review and body['state']=='APPROVED' and 'execute' in ctx.permissions and ctx.conn.execute('SELECT lv.execution_available(%s,%s)',(body['id'],ctx.user_id)).fetchone()[0]:return ['EXECUTE']
    if 'propose' not in ctx.permissions:return []
    maker=body['created_by']==ctx.user_id
    if body['state']=='DRAFT':return ['EDIT']+(['SUBMIT','WITHDRAW'] if maker else [])
    if not maker:return []
    if body['state']=='STALE':return ['RESIMULATE','WITHDRAW']
    if body['state']=='APPROVED':return ['REVALIDATE','WITHDRAW']
    return ['REVALIDATE','WITHDRAW'] if body['state'] in ('BLOCKED','READY_FOR_REVIEW') else []


def _respond(ctx, result: dict, status_ok: int):
    status = result['status']
    headers = {'Idempotent-Replayed': 'true'} if result.get('replayed') else {}
    if status == status_ok:
        body = dict(result['body'])
        body['allowed_actions'] = result['_caller_actions'] if '_caller_actions' in result else _actions(ctx, body, review=False)
        Proposal.model_validate(body)
        headers['ETag'] = f'"{result["etag"]}"'
        if status == 201:
            headers['Location'] = f'/api/v1/proposals/{body["id"]}'
        return JSONResponse(body, status_code=status, headers=headers)
    if result.get('retry_after'):
        headers['Retry-After'] = str(result['retry_after'])
    raise ProblemError(status, result['code'], result.get('detail'), headers=headers, errors=result.get('errors'),
                       extra={k: result[k] for k in ('current_version', 'state', 'reason') if k in result})


def _parse(raw: bytes | None):
    try:
        body = v.parse_body(raw)
        return body, v.request_hash(raw, body, True), None
    except v.Invalid as e:
        return None, v.request_hash(raw), e.problem


# ------------------------------------------------------------------------------------------- writes
@router.post('/proposals', response_model=Proposal, status_code=201, operation_id='createProposal',
             responses={400: {'description': 'IDEMPOTENCY_KEY_REQUIRED', **PROBLEM}, 401: {'description': 'UNAUTHENTICATED', **PROBLEM},
                        403: {'description': 'PERMISSION_DENIED (no propose; CSRF; Origin)', **PROBLEM},
                        404: {'description': 'NOT_FOUND: the ledger or a referenced record is absent, another tenant\'s or another '
                                             'ledger\'s (indistinguishable; errors[] gives the pointer)', **PROBLEM},
                        409: {'description': 'REQUEST_IN_PROGRESS (same Idempotency-Key still running; Retry-After)', **PROBLEM},
                        422: {'description': 'SCHEMA_INVALID, MONEY_MUST_BE_STRING, MONEY_SCALE_EXCEEDED, CURRENCY_MISMATCH, '
                                             'CAPABILITY_UNAVAILABLE, UNSUPPORTED_BY_PROFILE (PARTIAL_ITEM_SETTLEMENT), '
                                             'IDEMPOTENCY_KEY_REUSED', **PROBLEM}},
             openapi_extra={'x-lv-permission': 'propose', 'x-lv-idempotency': 'required', 'requestBody': CREATE_BODY})
def create_proposal(request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
                    idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=64, description=
                        'Required (Stage 2B Part 8.1). Absent: 400 IDEMPOTENCY_KEY_REQUIRED.')] = None):
    with session_transaction(request, permission='propose') as ctx:
        if idempotency_key is None:
            raise ProblemError(400, 'IDEMPOTENCY_KEY_REQUIRED', 'POST /proposals requires an Idempotency-Key header')
        if not IDEMPOTENCY_KEY.match(idempotency_key):
            raise ProblemError(422, 'SCHEMA_INVALID', 'Idempotency-Key must be 1-64 printable ASCII characters',
                               errors=[{'pointer': '/headers/Idempotency-Key', 'code': 'pattern'}])
        body, digest, problem = _parse(raw)
        draft = None
        if problem is None:
            try:
                ledger_id = v.check_create_shape(body)
                row = ctx.conn.execute('SELECT purpose::text, source_kind::text, currency, currency_exponent FROM lv.ledger WHERE id = %s',
                                       (ledger_id,)).fetchone()
                draft = v.check_create(body, v.check_ledger(row))
            except v.Invalid as e:
                problem = e.problem
        result = ctx.conn.execute('SELECT lv.proposal_create(%s, %s, %s, %s, %s, %s, %s)',
                                  (token_hash(ctx.session_token), idempotency_key, digest, Jsonb(draft or {}),
                                   Jsonb(problem) if problem else None, source_key(request), request.state.request_id)).fetchone()[0]
    # COMMITTED (or raised) here.
    log.info('proposal create request_id=%s user_id=%s status=%s replayed=%s', request.state.request_id, ctx.user_id,
             result['status'], bool(result.get('replayed')))
    return _respond(ctx, result, 201)


@router.patch('/proposals/{proposal_id}', response_model=Proposal, operation_id='editProposal',
              responses={401: {'description': 'UNAUTHENTICATED', **PROBLEM}, 403: {'description': 'PERMISSION_DENIED', **PROBLEM},
                         404: {'description': 'NOT_FOUND (proposal, or a referenced record outside its ledger)', **PROBLEM},
                         409: {'description': 'INVALID_STATE_TRANSITION (not DRAFT; body has state)', **PROBLEM},
                         412: {'description': 'VERSION_MISMATCH (body has current_version)', **PROBLEM},
                         422: {'description': 'SCHEMA_INVALID and the money codes; CAPABILITY_UNAVAILABLE; UNSUPPORTED_BY_PROFILE', **PROBLEM},
                         428: {'description': 'VERSION_REQUIRED (If-Match missing)', **PROBLEM}},
              openapi_extra={'x-lv-permission': 'propose', 'x-lv-idempotency': 'not used (version-guarded by If-Match)',
                             'requestBody': PATCH_BODY})
def edit_proposal(proposal_id: UUID, request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
                  if_match: Annotated[str | None, Header(alias='If-Match', max_length=64, description=
                      'Required: "v<version>". Absent: 428 VERSION_REQUIRED; different: 412 VERSION_MISMATCH.')] = None):
    with session_transaction(request, permission='propose') as ctx:
        body, _, problem = _parse(raw)
        patch = {}
        # The ledger (and so its currency) never changes for a proposal; the merge itself happens under the row lock in SQL.
        row = ctx.conn.execute('SELECT l.purpose::text, l.source_kind::text, l.currency, l.currency_exponent FROM lv.proposal p '
                               'JOIN lv.ledger l ON (l.tenant_id, l.id) = (p.tenant_id, p.ledger_id) WHERE p.id = %s',
                               (proposal_id,)).fetchone()
        if problem is None and row is not None:
            try:
                patch = v.check_patch(body, v.check_ledger(row))
            except v.Invalid as e:
                problem = e.problem
        result = ctx.conn.execute('SELECT lv.proposal_edit(%s, %s, %s, %s, %s, %s, %s)',
                                  (token_hash(ctx.session_token), proposal_id, if_match, Jsonb(patch),
                                   Jsonb(problem) if problem else None, source_key(request), request.state.request_id)).fetchone()[0]
    log.info('proposal edit request_id=%s user_id=%s status=%s', request.state.request_id, ctx.user_id, result['status'])
    return _respond(ctx, result, 200)


# ------------------------------------------------------------------------------------------- reads
@router.get('/proposals', response_model=ProposalPage, operation_id='listProposals', responses=READ_ERRORS,
            openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'id descending (newest first)'})
def list_proposals(request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                   ledger_id: Annotated[str | None, Query(description='Filter by ledger id')] = None,
                   state: Annotated[str | None, Query(description='Filter by state (only DRAFT exists yet)')] = None,
                   created_by: Annotated[str | None, Query(description='Filter by maker user id')] = None,
                   reviewable_by_me: Annotated[str | None, Query(description='true: READY_FOR_REVIEW proposals the caller may review '
                                                                              '(holds review; not maker or editor)')] = None,
                   limit: LIMIT = None, cursor: CURSOR = None):
    check_query(request, ('ledger_id', 'state', 'created_by', 'reviewable_by_me', 'limit', 'cursor'))
    size = parse_limit(limit)
    errors = []
    for name, value in (('ledger_id', ledger_id), ('created_by', created_by)):
        if value is not None and not v._uuid(value):
            errors.append({'pointer': f'/query/{name}', 'code': 'uuid'})
    if state is not None and state not in STATES:
        errors.append({'pointer': '/query/state', 'code': 'enum'})
    if reviewable_by_me not in (None, 'true', 'false'):
        errors.append({'pointer': '/query/reviewable_by_me', 'code': 'boolean'})
    if errors:
        raise ProblemError(422, 'SCHEMA_INVALID', 'invalid filter', errors=errors)
    filters = {'ledger_id': ledger_id, 'state': state, 'created_by': created_by, 'reviewable_by_me': reviewable_by_me == 'true'}
    context = list_context('listProposals', ctx.tenant_id, None, 'id:desc', filters)
    after = codec(request).decode(context, cursor, 1) if cursor is not None else None
    clauses, params = [], {'me': ctx.user_id, 'review': 'review' in ctx.permissions, 'n': size + 1}
    for name in ('ledger_id', 'state', 'created_by'):
        if filters[name] is not None:
            clauses.append(f'p.{name} = %({name})s')
            params[name] = filters[name]
    if filters['reviewable_by_me']:
        clauses.append('lv.approval_available(p.id,%(me)s::uuid)')
    if after:
        clauses.append('p.id < %(after)s::uuid')
        params['after'] = after[0]
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = ctx.conn.execute(f'SELECT lv.proposal_json(p) FROM lv.proposal p {where} ORDER BY p.id DESC LIMIT %(n)s', params).fetchall()
    items = [{**r[0], 'allowed_actions': _actions(ctx, r[0])} for r in rows]
    items, next_cursor = page(items, size, codec(request), context, lambda x: [x['id']])
    return render(request, ProposalPage, {'items': items, 'next_cursor': next_cursor})


def _read_proposal(ctx, proposal_id: UUID) -> dict:
    row = ctx.conn.execute('SELECT lv.proposal_json(p) FROM lv.proposal p WHERE p.id = %s', (proposal_id,)).fetchone()
    if row is None:
        raise ProblemError(404, 'NOT_FOUND', 'proposal not found')
    return row[0]


@router.get('/proposals/{proposal_id}', response_model=Proposal, operation_id='getProposal', responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read'})
def get_proposal(proposal_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    body = _read_proposal(ctx, proposal_id)
    body['allowed_actions'] = _actions(ctx, body)
    return render(request, Proposal, body, etag=f'"v{body["version"]}"')


@router.get('/proposals/{proposal_id}/revisions', response_model=RevisionPage, operation_id='listProposalRevisions',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'revision ascending'})
def list_revisions(proposal_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                   limit: LIMIT = None, cursor: CURSOR = None):
    check_query(request, ('limit', 'cursor'))
    size = parse_limit(limit)
    _read_proposal(ctx, proposal_id)
    context = list_context('listProposalRevisions', ctx.tenant_id, None, 'revision', {'proposal_id': str(proposal_id)})
    after = codec(request).decode(context, cursor, 2) if cursor is not None else None
    if after is not None and not after[0].isdigit():
        raise ProblemError(400, 'CURSOR_MISMATCH', 'cursor is not valid for this request; restart from the first page',
                           errors=[{'pointer': '/query/cursor', 'code': 'invalid'}])
    rows = ctx.conn.execute(
        "SELECT proposal_id::text, revision, change, authored_by::text, "
        "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), target_adapter, operations, evidence_refs, "
        "rationale, content_hash FROM lv.proposal_revision WHERE proposal_id = %s AND revision > %s ORDER BY revision LIMIT %s",
        (proposal_id, int(after[0]) if after else 0, size + 1)).fetchall()
    fields = ('proposal_id', 'revision', 'change', 'authored_by', 'created_at', 'target_adapter', 'operations', 'evidence_refs',
              'rationale', 'content_hash')
    items, next_cursor = page([dict(zip(fields, r)) for r in rows], size, codec(request), context,
                              lambda x: [str(x['revision']), x['proposal_id']])
    return render(request, RevisionPage, {'proposal_id': str(proposal_id), 'items': items, 'next_cursor': next_cursor})
