"""QuickBooks Online SANDBOX connection API (PROPOSED PROV-1..PROV-8; mounted only when LV_PROVIDER_SANDBOX_ENABLED=1).

Administrators connect, read and disconnect; any member with `read` sees connection state and the raw replica. The API
process never holds the client secret or a token: it seals the authorization code with the KEK public key and the integration
worker does every call to Intuit. No route here can create, change or delete anything at QuickBooks.
"""
import hashlib
import re
import secrets
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from psycopg.types.json import Jsonb

from ledgervigil.auth.boundary import RequestContext, require_read_only, session_transaction, source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.proposals import validation as v
from ledgervigil.proposals.routes import IDEMPOTENCY_KEY, NOT_FOUND, PROBLEM, READ_ERRORS, raw_body
from ledgervigil.reads.ledgers import CURSOR, LIMIT, codec
from ledgervigil.reads.paging import check_query, list_context, page, parse_limit
from ledgervigil.reads.representation import render
from ledgervigil.simulation.canonical import canonical, digest

from . import envelope, intuit
from .settings import ConfigurationError, api_settings

router = APIRouter(prefix='/api/v1', tags=['provider'])
CONTRACT = 'PROV-1..PROV-8 PROPOSED (docs/QUICKBOOKS_SANDBOX.md); QuickBooks SANDBOX, read-only; disabled by default'
STATE = re.compile(r'^[A-Za-z0-9_-]{43}$')
REALM = re.compile(r'^[0-9]{1,32}$')
CODE = re.compile(r'^[!-~]{8,512}$')
ERRORS = ('access_denied', 'invalid_scope', 'invalid_request', 'unauthorized_client', 'server_error', 'temporarily_unavailable', 'unsupported_response_type')
CAPABILITIES = {
    'environment': 'SANDBOX', 'scope': intuit.SCOPE, 'minor_version': intuit.MINOR_VERSION,
    'reads': {'entities': list(intuit.ENTITIES), 'status': 'DOCUMENTED_UNTESTED',
              'note': 'implemented and contract-tested against documented-shape mocks only; no live sandbox evidence recorded',
              'bound': {'page_size': intuit.PAGE_SIZE, 'max_pages_per_entity': intuit.MAX_PAGES_PER_ENTITY}},
    'writes': {'status': 'DISABLED', 'note': 'no QuickBooks write path exists; Stage 2C statuses (DOCUMENTED_UNTESTED / UNKNOWN / UNSUPPORTED) '
               'are unchanged and experiments X-1..X-10 have not been run'},
    'incremental_sync': 'NOT_IMPLEMENTED (change data capture, webhooks)', 'reconciliation': 'NOT_IMPLEMENTED (provider data never enters native ledgers)'}


class Job(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    connection_id: str
    kind: Literal['EXCHANGE', 'SYNC', 'REVOKE']
    state: Literal['QUEUED', 'CLAIMED', 'DONE', 'DEAD', 'DENIED']
    attempts: int
    throttles: int
    available_at: str
    requested_by: str
    progress: dict[str, Any]
    result: dict[str, Any] | None
    last_error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class Connection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    provider: Literal['QUICKBOOKS_ONLINE']
    environment: Literal['SANDBOX']
    scope: Literal['com.intuit.quickbooks.accounting']
    state: Literal['AUTHORIZING', 'EXCHANGING', 'ACTIVE', 'DEGRADED', 'REVOKING', 'REVOKED', 'FAILED']
    version: int
    etag: str
    realm_id: str | None
    created_by: str
    created_at: str
    connected_at: str | None
    changed_at: str
    failure_code: str | None
    revoked_at: str | None
    revoke_outcome: Literal['PROVIDER_CONFIRMED', 'PROVIDER_UNCONFIRMED', 'NO_TOKENS', 'ABANDONED'] | None
    revocation_jobs: int
    revocation_auto_retry: bool
    jobs: list[Job]
    replica: dict[str, int]
    data_label: str
    writes: Literal['DISABLED']
    accounting_review_status: Literal['UNREVIEWED']


class ConnectionList(BaseModel):
    model_config = ConfigDict(extra='forbid')
    configuration: dict[str, Any]
    capabilities: dict[str, Any]
    items: list[Connection]


class ConnectStarted(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connection: Connection
    authorization_url: str | None
    authorization_expires_in_seconds: int


class ProviderObject(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    connection_id: str
    realm_id: str
    entity: str
    provider_id: str
    sync_token: str
    provider_last_updated: str | None
    object_sha256: str
    page_id: str
    provenance: Literal['PROVIDER_SANDBOX', 'RECORDED_MOCK']
    first_seen_at: str
    page: dict[str, Any]
    object_text: str | None = None   # exact provider JSON text (PostgreSQL jsonb form); amounts keep their decimal digits


class ObjectPage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connection_id: str
    entity: str | None
    data_label: str
    items: list[ProviderObject]
    next_cursor: str | None


WRITE_ERRORS = {**NOT_FOUND, 400: {'description': 'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER; OAUTH_STATE_INVALID; OAUTH_STATE_EXPIRED', **PROBLEM},
                401: {'description': 'UNAUTHENTICATED', **PROBLEM}, 403: {'description': 'PERMISSION_DENIED (CSRF/Origin, administer)', **PROBLEM},
                409: {'description': 'REQUEST_IN_PROGRESS; CONNECTION_EXISTS; OAUTH_STATE_USED; REALM_ALREADY_CONNECTED; SYNC_IN_PROGRESS; '
                                     'CONNECTION_DEGRADED; INVALID_STATE_TRANSITION', **PROBLEM},
                412: {'description': 'VERSION_MISMATCH', **PROBLEM}, 428: {'description': 'PRECONDITION_REQUIRED', **PROBLEM},
                422: {'description': 'SCHEMA_INVALID; IDEMPOTENCY_KEY_REUSED', **PROBLEM},
                503: {'description': 'PROVIDER_NOT_CONFIGURED (no private sandbox configuration on this server)', **PROBLEM}}
DISCONNECT_BODY = {'required': False, 'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': False, 'properties': {
    'abandon_revocation': {'type': 'boolean', 'description': 'only for a REVOKING connection whose revocation is pending: erase the local tokens '
                           'without Intuit confirming (outcome ABANDONED); the grant may still be live at Intuit'}}}}}}
EMPTY_BODY = {'required': False, 'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': False, 'properties': {}}}}}
CALLBACK_BODY = {'required': True, 'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': False, 'required': ['state'],
    'properties': {'state': {'type': 'string', 'pattern': STATE.pattern}, 'code': {'type': 'string', 'minLength': 8, 'maxLength': 512},
                   'realm_id': {'type': 'string', 'pattern': REALM.pattern}, 'error': {'enum': list(ERRORS)}},
    'description': 'Exactly the parameters Intuit appended to the redirect URI: state plus either (code, realm_id) or error'}}}}


def _unauthenticated(e):
    if 'authenticated session' in (e.diag.message_primary or '') or e.diag.message_primary == 'session unavailable':
        raise ProblemError(401, 'UNAUTHENTICATED', 'session is no longer valid') from None
    raise e


def _configuration():
    try:
        s = api_settings()
        return s, {'configured': True, 'redirect_uri': s.redirect_uri, 'missing': [], 'environment': 'SANDBOX'}
    except ConfigurationError as e:
        return None, {'configured': False, 'redirect_uri': None, 'missing': list(e.missing), 'environment': 'SANDBOX', 'reason': e.code}
    except (OSError, envelope.EnvelopeError, ValueError):
        return None, {'configured': False, 'redirect_uri': None, 'missing': [], 'environment': 'SANDBOX', 'reason': 'PROVIDER_CONFIG_UNREADABLE'}


def _require_administer(request):
    """L5: authenticate (session, CSRF/Origin) and require administer before revealing configuration state or sealing anything.
    Returns the session's tenant, which the sealed authorization code is bound to."""
    try:
        with session_transaction(request, touch=False) as ctx:
            if 'administer' not in ctx.permissions:
                raise ProblemError(403, 'PERMISSION_DENIED', 'requires administer')
            return ctx.tenant_id
    except psycopg.errors.InsufficientPrivilege as e:
        _unauthenticated(e)


def _write(request, action, conn, idempotency_key, if_match, args, problem, body_hash):
    try:
        with session_transaction(request, touch=False) as ctx:
            if 'administer' not in ctx.permissions:
                raise ProblemError(403, 'PERMISSION_DENIED', 'requires administer')
            if idempotency_key is None or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
                raise ProblemError(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key must contain 1-64 visible ASCII characters')
            if request.query_params:
                problem = {'status': 400, 'code': 'UNKNOWN_QUERY_PARAMETER', 'detail': 'this endpoint accepts no query parameters'}
            rhash = digest(canonical({'action': action, 'connection_id': str(conn) if conn else None, 'body_hash': body_hash, 'if_match': if_match,
                                      'query': [[k, x] for k, x in request.query_params.multi_items()]}))
            result = ctx.conn.execute('SELECT lv.provider_api(%s,%s,%s,%s,%s,%s,%s,%s,%s)', (
                token_hash(ctx.session_token), action, conn, idempotency_key, rhash, Jsonb({**args, 'if_match': if_match}),
                Jsonb(problem) if problem else None, source_key(request), request.state.request_id)).fetchone()[0]
        return result
    except psycopg.errors.InsufficientPrivilege as e:
        _unauthenticated(e)


def _respond(result, model=None, wrap=None):
    headers = {'Idempotent-Replayed': str(bool(result.get('replayed'))).lower()}
    if result['status'] in (200, 201, 202):
        body = wrap(result['body']) if wrap else result['body']
        if model:
            model.model_validate(body)
        etag = result['body'].get('etag') if isinstance(result['body'], dict) else None
        if etag and result['body'].get('state'):
            headers['ETag'] = '"' + etag + '"'
        return JSONResponse(body, status_code=result['status'], headers=headers)
    if result.get('retry_after'):
        headers['Retry-After'] = str(result['retry_after'])
    raise ProblemError(result['status'], result['code'], result.get('detail'), headers=headers, errors=result.get('errors'),
                       extra={k: result[k] for k in ('current_version', 'connection_id') if k in result})


@router.get('/provider-connections', response_model=ConnectionList, operation_id='listProviderConnections', responses=READ_ERRORS,
            openapi_extra={'x-lv-permission': 'read', 'x-lv-contract-choices': CONTRACT})
def list_connections(request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    rows = ctx.conn.execute('SELECT lv.provider_connection_json(c) FROM lv.provider_connection c ORDER BY c.id DESC LIMIT 20').fetchall()
    _, configuration = _configuration()
    return render(request, ConnectionList, {'configuration': configuration, 'capabilities': CAPABILITIES, 'items': [r[0] for r in rows]})


@router.post('/provider-connections', response_model=ConnectStarted, status_code=201, operation_id='startProviderConnection', responses=WRITE_ERRORS,
             openapi_extra={'x-lv-permission': 'administer', 'x-lv-idempotency': 'required', 'x-lv-contract-choices': CONTRACT, 'requestBody': EMPTY_BODY,
                            'x-lv-replay': 'a replay returns the connection without authorization_url (the state value is never stored); start again'})
def start_connection(request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
                     idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None):
    problem, body_hash = None, v.request_hash(raw or b'')
    if raw:
        try:
            body = v.parse_body(raw)
            if body != {}:
                problem = {'status': 422, 'code': 'SCHEMA_INVALID', 'errors': [{'pointer': '', 'code': 'empty_object'}]}
        except v.Invalid as e:
            problem = e.problem
    _require_administer(request)
    settings, configuration = _configuration()
    if settings is None and problem is None:
        raise ProblemError(503, 'PROVIDER_NOT_CONFIGURED', 'the QuickBooks sandbox connection is not configured on this server',
                           extra={'reason': configuration.get('reason')})
    state = secrets.token_urlsafe(32)
    result = _write(request, 'CONNECT', None, idempotency_key, None, {'state_hash': hashlib.sha256(state.encode()).hexdigest()}, problem, body_hash)
    fresh = result['status'] == 201 and not result.get('replayed')
    return _respond(result, ConnectStarted, wrap=lambda body: {
        'connection': body, 'authorization_url': intuit.authorization_url(settings.client_id, settings.redirect_uri, state) if fresh else None,
        'authorization_expires_in_seconds': 600})


@router.post('/provider-connections/quickbooks/callback', response_model=Connection, status_code=202, operation_id='completeProviderAuthorization',
             responses={**WRITE_ERRORS, 200: {'description': 'Intuit returned an error; the attempt is FAILED', 'content': {'application/json': {}}}},
             openapi_extra={'x-lv-permission': 'administer', 'x-lv-idempotency': 'required', 'x-lv-contract-choices': CONTRACT, 'requestBody': CALLBACK_BODY,
                            'x-lv-security': 'state must belong to this session, user and tenant and be at most 10 minutes old; single use'})
def callback(request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
             idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None):
    problem, args, digest_body = None, {}, {}
    try:
        body = v.parse_body(raw)
    except v.Invalid as e:
        body, problem = None, e.problem
    if problem is None:
        errors = []
        if type(body) is not dict:
            errors.append({'pointer': '', 'code': 'object'})
        else:
            errors += [{'pointer': '/' + k[:64], 'code': 'additional_property'} for k in sorted(set(body) - {'state', 'code', 'realm_id', 'error'})[:20]
                       if k.isprintable()]
            if not isinstance(body.get('state'), str) or not STATE.fullmatch(body['state']):
                errors.append({'pointer': '/state', 'code': 'pattern'})
            if 'error' in body:
                if body['error'] not in ERRORS or 'code' in body:
                    errors.append({'pointer': '/error', 'code': 'enum'})
            elif not (isinstance(body.get('code'), str) and CODE.fullmatch(body['code']) and isinstance(body.get('realm_id'), str) and REALM.fullmatch(body['realm_id'])):
                errors.append({'pointer': '/code', 'code': 'code_and_realm_required'})
        if errors:
            problem = {'status': 422, 'code': 'SCHEMA_INVALID', 'errors': errors}
    tenant = _require_administer(request)
    settings, configuration = _configuration()
    if problem is None:
        state_hash = hashlib.sha256(body['state'].encode()).hexdigest()
        if 'error' in body:
            args = {'state_hash': state_hash, 'provider_error': body['error']}
            digest_body = {'state_hash': state_hash, 'error': body['error']}
        else:
            if settings is None:
                raise ProblemError(503, 'PROVIDER_NOT_CONFIGURED', 'the QuickBooks sandbox connection is not configured on this server')
            sealed = envelope.seal(settings.public_key, body['code'].encode(), envelope.aad('authorization-code', str(tenant), state_hash, body['realm_id']))
            args = {'state_hash': state_hash, 'realm_id': body['realm_id'], 'code_envelope': sealed.hex()}
            # the request hash covers the code only through its digest; the code itself is never stored in clear
            digest_body = {'state_hash': state_hash, 'realm_id': body['realm_id'], 'code_sha256': hashlib.sha256(body['code'].encode()).hexdigest()}
    result = _write(request, 'CALLBACK', None, idempotency_key, None, args, problem, digest(canonical(digest_body)) if digest_body else v.request_hash(raw or b''))
    return _respond(result, Connection)


@router.post('/provider-connections/{connection_id}/syncs', response_model=Job, status_code=202, operation_id='startProviderSync', responses=WRITE_ERRORS,
             openapi_extra={'x-lv-permission': 'administer', 'x-lv-idempotency': 'required', 'x-lv-version-guard': 'If-Match "v<connection version>" required',
                            'x-lv-contract-choices': CONTRACT, 'requestBody': EMPTY_BODY})
def start_sync(connection_id: UUID, request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
               idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None,
               if_match: Annotated[str | None, Header(alias='If-Match', max_length=64)] = None):
    problem = None
    if raw:
        try:
            if v.parse_body(raw) != {}:
                problem = {'status': 422, 'code': 'SCHEMA_INVALID', 'errors': [{'pointer': '', 'code': 'empty_object'}]}
        except v.Invalid as e:
            problem = e.problem
    return _respond(_write(request, 'SYNC', connection_id, idempotency_key, if_match, {}, problem, v.request_hash(raw or b'')), Job)


@router.post('/provider-connections/{connection_id}/disconnect', response_model=Connection, status_code=202, operation_id='disconnectProvider',
             responses={**WRITE_ERRORS, 200: {'description': 'an unfinished authorization was cancelled, or a pending revocation was abandoned', 'content': {'application/json': {}}}},
             openapi_extra={'x-lv-permission': 'administer', 'x-lv-idempotency': 'required', 'x-lv-version-guard': 'If-Match "v<connection version>" required',
                            'x-lv-contract-choices': CONTRACT, 'requestBody': DISCONNECT_BODY})
def disconnect(connection_id: UUID, request: Request, raw: Annotated[bytes | None, Depends(raw_body)],
               idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None,
               if_match: Annotated[str | None, Header(alias='If-Match', max_length=64)] = None):
    problem, args = None, {}
    if raw:
        try:
            body = v.parse_body(raw)
            if type(body) is not dict or set(body) - {'abandon_revocation'} or ('abandon_revocation' in body and type(body['abandon_revocation']) is not bool):
                problem = {'status': 422, 'code': 'SCHEMA_INVALID', 'errors': [{'pointer': '', 'code': 'abandon_revocation_boolean_only'}]}
            elif body.get('abandon_revocation'):
                args = {'abandon_revocation': 'true'}
        except v.Invalid as e:
            problem = e.problem
    return _respond(_write(request, 'DISCONNECT', connection_id, idempotency_key, if_match, args, problem, v.request_hash(raw or b'')), Connection)


@router.get('/provider-connections/{connection_id}', response_model=Connection, operation_id='getProviderConnection',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-contract-choices': CONTRACT})
def get_connection(connection_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    row = ctx.conn.execute('SELECT lv.provider_connection_json(c) FROM lv.provider_connection c WHERE c.id = %s', (connection_id,)).fetchone()
    if row is None:
        raise ProblemError(404, 'NOT_FOUND', 'connection not found')
    return render(request, Connection, row[0], etag='"' + row[0]['etag'] + '"')


ENTITY = Annotated[str | None, Query(pattern='^(' + '|'.join(intuit.ENTITIES) + ')$', description='Optional entity filter')]


@router.get('/provider-connections/{connection_id}/objects', response_model=ObjectPage, operation_id='listProviderObjects',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-order': 'id ascending (first seen)',
                                                                   'x-lv-contract-choices': CONTRACT})
def list_objects(connection_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))],
                 entity: ENTITY = None, limit: LIMIT = None, cursor: CURSOR = None):
    check_query(request, ('entity', 'limit', 'cursor'))
    if entity is not None and entity not in intuit.ENTITIES:
        raise ProblemError(422, 'SCHEMA_INVALID', 'unknown entity', errors=[{'pointer': '/query/entity', 'code': 'enum'}])
    if ctx.conn.execute('SELECT 1 FROM lv.provider_connection WHERE id = %s', (connection_id,)).fetchone() is None:
        raise ProblemError(404, 'NOT_FOUND', 'connection not found')
    size = parse_limit(limit)
    context = list_context('listProviderObjects', ctx.tenant_id, None, 'id.asc', {'connection': str(connection_id), 'entity': entity})
    after = codec(request).decode(context, cursor, 1) if cursor is not None else ['00000000-0000-0000-0000-000000000000']
    rows = ctx.conn.execute('SELECT lv.provider_object_json(o, false) FROM lv.provider_object o WHERE o.connection_id = %s AND (%s::text IS NULL OR o.entity = %s) '
                            'AND o.id > %s::uuid ORDER BY o.id LIMIT %s', (connection_id, entity, entity, after[0], size + 1)).fetchall()
    items, next_cursor = page([r[0] for r in rows], size, codec(request), context, lambda row: [row['id']])
    return render(request, ObjectPage, {'connection_id': str(connection_id), 'entity': entity, 'items': items, 'next_cursor': next_cursor,
                                        'data_label': 'PROVIDER SANDBOX DATA - not business evidence, not accounting records'})


@router.get('/provider-objects/{object_id}', response_model=ProviderObject, operation_id='getProviderObject',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-contract-choices': CONTRACT})
def get_object(object_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    row = ctx.conn.execute('SELECT lv.provider_object_json(o, true) FROM lv.provider_object o WHERE o.id = %s', (object_id,)).fetchone()
    if row is None:
        raise ProblemError(404, 'NOT_FOUND', 'provider object not found')
    return render(request, ProviderObject, row[0])
