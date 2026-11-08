"""Detection runs, findings and reviewer dispositions (PROPOSED DET-1..DET-8; mounted only when LV_DETECTION_ENABLED=1).

Run: verified session -> lv.detection_begin (identity-first locks, idempotency, ledger counter FOR SHARE) -> deterministic
detector over database-built evidence sets -> lv.detection_record (independent database oracle; rejects any mismatch).
The run commits before a success response leaves this module. Nothing here writes a financial table.
"""
import re
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from ledgervigil.auth.boundary import RequestContext, require_read_only, session_transaction, source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.proposals import validation as v
from ledgervigil.proposals.routes import IDEMPOTENCY_KEY, NOT_FOUND, PROBLEM, READ_ERRORS, raw_body
from ledgervigil.reads.ledgers import CURSOR, LIMIT, codec
from ledgervigil.reads.paging import check_query, list_context, page, parse_limit
from ledgervigil.reads.representation import render
from ledgervigil.simulation.canonical import canonical, digest

from . import detector

router = APIRouter(prefix='/api/v1', tags=['detection'])
_before_record = None   # test seam: replaces the detector results before lv.detection_record (malformed-output tests)
UUID_TEXT = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
REASONS = ('VERIFIED_AGAINST_EVIDENCE', 'EXPECTED_TIMING', 'STATEMENT_ERROR', 'KNOWN_EXCEPTION', 'NOT_A_DISCREPANCY', 'INPUTS_CHANGED',
           'REVIEW_CORRECTION')
CONTRACT = 'DET-1..DET-8 PROPOSED (docs/AI_DETECTION.md); not part of the accepted Stage 2B contract; disabled by default'


class DetectionRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    ledger_id: str
    detector: str
    detector_version: str
    config: dict[str, Any]
    config_hash: str
    status: Literal['COMPLETED', 'REJECTED']
    rejection_code: str | None
    rejection_detail: dict[str, Any] | None
    snapshot_seq: int
    input_fingerprint: str
    scope: list[str]
    abstentions: list[dict[str, str]]
    findings: list[dict[str, Any]]
    finding_count: int
    new_finding_count: int
    requested_by: str
    created_at: str
    basis: Literal['DETERMINISTIC_COMPARISON']
    model: Literal['NONE']
    accounting_review_status: Literal['UNREVIEWED']


class DetectionFinding(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    id: str
    ledger_id: str
    detector: str
    detector_version: str
    kind: Literal[detector.KINDS]
    class_: Literal[detector.CLASSES] = Field(alias='class')
    staged_document_id: str
    finding_key: str
    basis: Literal['DETERMINISTIC_COMPARISON']
    verified: Literal[False]
    accounting_review_status: Literal['UNREVIEWED']
    observed: dict[str, Any]
    recorded: dict[str, Any]
    computed: dict[str, Any]
    interpretation: dict[str, Any]
    evidence: list[dict[str, Any]]
    input_fingerprint: str
    freshness: dict[str, str]
    first_run_id: str
    created_at: str
    review: dict[str, Any]
    dispositions: list[dict[str, Any]]


class FindingPage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ledger_id: str
    items: list[DetectionFinding]
    next_cursor: str | None


WRITE_ERRORS = {**NOT_FOUND, 400: {'description': 'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER', **PROBLEM},
                401: {'description': 'UNAUTHENTICATED', **PROBLEM}, 403: {'description': 'PERMISSION_DENIED (CSRF/Origin, permission)', **PROBLEM},
                409: {'description': 'REQUEST_IN_PROGRESS; INVALID_STATE_TRANSITION; FINDING_STALE', **PROBLEM},
                412: {'description': 'VERSION_MISMATCH', **PROBLEM}, 428: {'description': 'VERSION_REQUIRED', **PROBLEM},
                422: {'description': 'SCHEMA_INVALID; IDEMPOTENCY_KEY_REUSED; CAPABILITY_UNAVAILABLE; SCOPE_TOO_LARGE', **PROBLEM},
                502: {'description': 'DETECTOR_OUTPUT_REJECTED (stored as a REJECTED run; nothing recorded as a finding)', **PROBLEM}}
RUN_BODY = {'required': True, 'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': False, 'properties': {
    'staged_document_ids': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'items': {'type': 'string', 'format': 'uuid'},
                            'description': 'Optional scope; default every staged statement of the ledger (at most 200)'}}}}}}
DISPOSITION_BODY = {'required': True, 'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': False,
    'required': ['action', 'reason_code'], 'properties': {
        'action': {'enum': ['CONFIRMED', 'DISMISSED', 'REOPENED']}, 'reason_code': {'enum': list(REASONS)},
        'note': {'type': 'string', 'minLength': 1, 'maxLength': 2000, 'description': 'Untrusted reviewer text; stored, never interpreted'}}}}}}


def _unauthenticated(e):
    if 'authenticated session' in (e.diag.message_primary or '') or e.diag.message_primary == 'session unavailable':
        raise ProblemError(401, 'UNAUTHENTICATED', 'session is no longer valid') from None
    raise e


def _parse_run(raw):
    try:
        body = v.parse_body(raw)
    except v.Invalid as e:
        return None, v.request_hash(raw), e.problem
    errors = []
    if type(body) is not dict:
        errors.append({'pointer': '', 'code': 'object'})
    else:
        errors += [{'pointer': '/' + k.replace('~', '~0').replace('/', '~1'), 'code': 'additional_property'} for k in sorted(set(body) - {'staged_document_ids'})[:20]
                   if not any(0xD800 <= ord(c) <= 0xDFFF for c in k)]
        ids = body.get('staged_document_ids')
        if 'staged_document_ids' in body and (type(ids) is not list or not 1 <= len(ids) <= 200):
            errors.append({'pointer': '/staged_document_ids', 'code': 'array'})
        elif type(ids) is list:
            errors += [{'pointer': f'/staged_document_ids/{i}', 'code': 'uuid'} for i, x in enumerate(ids)
                       if type(x) is not str or not re.fullmatch(UUID_TEXT, x)][:20]
    if errors:
        return None, v.request_hash(raw, body, True), {'status': 422, 'code': 'SCHEMA_INVALID', 'errors': errors}
    return body.get('staged_document_ids'), v.request_hash(raw, body, True), None


@router.post('/ledgers/{ledger_id}/detection-runs', response_model=DetectionRun, status_code=201, operation_id='runSettlementDetection',
             responses=WRITE_ERRORS, openapi_extra={'x-lv-permission': 'propose', 'x-lv-idempotency': 'required', 'x-lv-contract-choices': CONTRACT,
                                                    'requestBody': RUN_BODY})
def run_detection(ledger_id: UUID, request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
                  idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None):
    try:
        with session_transaction(request, touch=False) as ctx:
            if 'propose' not in ctx.permissions:
                raise ProblemError(403, 'PERMISSION_DENIED', 'requires propose')
            if idempotency_key is None or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
                raise ProblemError(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key must contain 1-64 visible ASCII characters')
            scope, body_hash, problem = _parse_run(raw)
            if request.query_params:
                problem = {'status': 400, 'code': 'UNKNOWN_QUERY_PARAMETER', 'detail': 'detection runs accept no query parameters'}
            rhash = digest(canonical({'ledger_id': str(ledger_id), 'body_hash': body_hash,
                                      'query': [[k, x] for k, x in request.query_params.multi_items()]}))
            tok, src, req = token_hash(ctx.session_token), source_key(request), request.state.request_id
            result = ctx.conn.execute('SELECT lv.detection_begin(%s,%s,%s,%s,%s,%s,%s,%s)', (tok, ledger_id, idempotency_key, rhash,
                                      Jsonb(scope) if scope is not None else None, Jsonb(problem) if problem else None, src, req)).fetchone()[0]
            if result['status'] == 100:
                results = detector.run(ctx.conn, UUID(result['tenant_id']), ledger_id, result['scope'])
                if _before_record:
                    results = _before_record(results)
                result = ctx.conn.execute('SELECT lv.detection_record(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (tok, ledger_id, idempotency_key, rhash,
                                          detector.DETECTOR, detector.VERSION, Jsonb(detector.CONFIG), Jsonb(results), src, req)).fetchone()[0]
            if result['status'] == 201:
                DetectionRun.model_validate(result['body'])
        headers = {'Idempotent-Replayed': str(bool(result.get('replayed'))).lower()}
        if result['status'] == 201:
            return JSONResponse(result['body'], status_code=201, headers=headers)
        if result.get('retry_after'):
            headers['Retry-After'] = str(result['retry_after'])
        detail = result.get('detail')
        if result.get('run_id'):
            detail = f'{detail} (run {result["run_id"]})'
        raise ProblemError(result['status'], result['code'], detail, headers=headers, errors=result.get('errors'))
    except psycopg.errors.InsufficientPrivilege as e:
        _unauthenticated(e)


@router.get('/detection-runs/{run_id}', response_model=DetectionRun, operation_id='getDetectionRun', responses={**READ_ERRORS, **NOT_FOUND},
            openapi_extra={'x-lv-permission': 'read', 'x-lv-contract-choices': CONTRACT})
def get_run(run_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    row = ctx.conn.execute('SELECT lv.detection_run_json(r) FROM lv.detection_run r WHERE r.id = %s', (run_id,)).fetchone()
    if row is None:
        raise ProblemError(404, 'NOT_FOUND', 'detection run not found')
    return render(request, DetectionRun, row[0])


@router.get('/ledgers/{ledger_id}/detection-findings', response_model=FindingPage, operation_id='listDetectionFindings',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'id ascending (uuidv7, creation order)',
                                                                   'x-lv-contract-choices': CONTRACT})
def list_findings(ledger_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                  limit: LIMIT = None, cursor: CURSOR = None):
    check_query(request, ('limit', 'cursor'))
    if ctx.conn.execute('SELECT 1 FROM lv.ledger WHERE id = %s', (ledger_id,)).fetchone() is None:
        raise ProblemError(404, 'NOT_FOUND', 'ledger not found')
    size = parse_limit(limit)
    context = list_context('listDetectionFindings', ctx.tenant_id, str(ledger_id), 'id.asc')
    after = codec(request).decode(context, cursor, 1) if cursor is not None else ['00000000-0000-0000-0000-000000000000']
    rows = ctx.conn.execute('SELECT lv.detection_finding_json(f) FROM lv.detection_finding f WHERE f.ledger_id = %s AND f.id > %s::uuid '
                            'ORDER BY f.id LIMIT %s', (ledger_id, after[0], size + 1)).fetchall()
    items, next_cursor = page([r[0] for r in rows], size, codec(request), context, lambda row: [row['id']])
    return render(request, FindingPage, {'ledger_id': str(ledger_id), 'items': items, 'next_cursor': next_cursor})


@router.get('/detection-findings/{finding_id}', response_model=DetectionFinding, operation_id='getDetectionFinding',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-contract-choices': CONTRACT})
def get_finding(finding_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    row = ctx.conn.execute('SELECT lv.detection_finding_json(f) FROM lv.detection_finding f WHERE f.id = %s', (finding_id,)).fetchone()
    if row is None:
        raise ProblemError(404, 'NOT_FOUND', 'finding not found')
    return render(request, DetectionFinding, row[0], etag='"' + row[0]['review']['etag'] + '"')


def _parse_disposition(raw):
    try:
        body = v.parse_body(raw)
    except v.Invalid as e:
        return None, v.request_hash(raw), e.problem
    errors = []
    if type(body) is not dict:
        errors.append({'pointer': '', 'code': 'object'})
    else:
        errors += [{'pointer': '/' + k.replace('~', '~0').replace('/', '~1'), 'code': 'additional_property'}
                   for k in sorted(set(body) - {'action', 'reason_code', 'note'})[:20] if not any(0xD800 <= ord(c) <= 0xDFFF for c in k)]
        if body.get('action') not in ('CONFIRMED', 'DISMISSED', 'REOPENED'):
            errors.append({'pointer': '/action', 'code': 'enum'})
        if body.get('reason_code') not in REASONS:
            errors.append({'pointer': '/reason_code', 'code': 'enum'})
        note = body.get('note')
        if 'note' in body:
            try:
                ok = type(note) is str and 1 <= len(note) <= 2000 and bool(note.strip()) and '\x00' not in note and bool(note.encode('utf-8'))
            except UnicodeError:
                ok = False
            if not ok:
                errors.append({'pointer': '/note', 'code': 'string'})
    if errors:
        return None, v.request_hash(raw, body, True), {'status': 422, 'code': 'SCHEMA_INVALID', 'errors': errors}
    return body, v.request_hash(raw, body, True), None


@router.post('/detection-findings/{finding_id}/dispositions', response_model=DetectionFinding, operation_id='disposeDetectionFinding',
             responses=WRITE_ERRORS, openapi_extra={'x-lv-permission': 'propose or review', 'x-lv-idempotency': 'required',
                                                    'x-lv-version-guard': 'If-Match "v<review version>" required', 'x-lv-contract-choices': CONTRACT,
                                                    'requestBody': DISPOSITION_BODY})
def dispose(finding_id: UUID, request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
            idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None,
            if_match: Annotated[str | None, Header(alias='If-Match', max_length=64)] = None):
    try:
        with session_transaction(request, touch=False) as ctx:
            if not ctx.permissions.intersection({'propose', 'review'}):
                raise ProblemError(403, 'PERMISSION_DENIED', 'requires propose or review')
            if idempotency_key is None or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
                raise ProblemError(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key must contain 1-64 visible ASCII characters')
            body, body_hash, problem = _parse_disposition(raw)
            if request.query_params:
                problem = {'status': 400, 'code': 'UNKNOWN_QUERY_PARAMETER', 'detail': 'dispositions accept no query parameters'}
            rhash = digest(canonical({'finding_id': str(finding_id), 'body_hash': body_hash, 'if_match': if_match,
                                      'query': [[k, x] for k, x in request.query_params.multi_items()]}))
            body = body or {}
            result = ctx.conn.execute('SELECT lv.detection_dispose(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (
                token_hash(ctx.session_token), finding_id, if_match, idempotency_key, rhash, body.get('action'), body.get('reason_code'),
                body.get('note'), Jsonb(problem) if problem else None, source_key(request), request.state.request_id)).fetchone()[0]
            if result['status'] == 200:
                DetectionFinding.model_validate(result['body'])
        headers = {'Idempotent-Replayed': str(bool(result.get('replayed'))).lower()}
        if result['status'] == 200:
            return JSONResponse(result['body'], headers={**headers, 'ETag': '"' + result['etag'] + '"'})
        if result.get('retry_after'):
            headers['Retry-After'] = str(result['retry_after'])
        raise ProblemError(result['status'], result['code'], result.get('detail'), headers=headers, errors=result.get('errors'),
                           extra={k: result[k] for k in ('current_version', 'state') if k in result})
    except psycopg.errors.InsufficientPrivilege as e:
        _unauthenticated(e)
