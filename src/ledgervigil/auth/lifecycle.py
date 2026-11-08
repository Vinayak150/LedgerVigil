"""Stage 2B Family 1 completion and ADR-027 account lifecycle (migration 0036):

    POST /auth/email-verification            {email}                 202 always (or 429): resend a verification link
    POST /auth/email-verification/confirm    {token}                 200, or 400 TOKEN_INVALID
    POST /auth/password-reset                {email}                 202 always (or 429)
    POST /auth/password-reset/confirm        {token, new_password}   200 (every session of the user ends), or 400
    POST /auth/invitation/accept             {token, password}       200, or 400
    POST /auth/reauthenticate                {password}              200; required within 15 minutes before member changes
    POST /auth/sessions/revoke-all                                   204: sign out everywhere
    GET  /members                                                    read
    POST /members                            {email, display_name?, roles[]}  administer, Idempotency-Key, fresh auth
    PATCH /members/{user_id}                 {roles[]}               administer, If-Match, Idempotency-Key, fresh auth
    DELETE /members/{user_id}                                        administer, If-Match, Idempotency-Key, fresh auth
    POST /members/{user_id}/sessions/revoke                          administer, Idempotency-Key, fresh auth
    GET  /audit-events?before=&limit=                                audit.read; hash-chained, keyset-paged

Account enumeration: the public endpoints answer identically whether or not the address exists; messages are delivered after
the response (background task), so timing does not depend on existence either; throttling counts per address key and per
source alike. Tokens are 256-bit, single use, stored only as SHA-256, and travel in link fragments.
"""
import hashlib
import logging
import re
import secrets
from typing import Annotated
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import mail
from .boundary import check_origin, check_transport, csrf_matches, session_token, session_transaction, source_key
from .crypto import MAX_PASSWORD_LENGTH, hash_password, token_hash, verify_password
from .problems import ProblemError
from ledgervigil.reads.paging import check_query

log = logging.getLogger('ledgervigil.auth.lifecycle')
router = APIRouter(prefix='/api/v1')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
IDEMPOTENCY = re.compile(r'[!-~]{1,64}')
ROLES = ('ANALYST', 'CONTROLLER', 'AUTOMATION_DEVELOPER', 'ADMINISTRATOR')


class EmailRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3, max_length=320)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=43, max_length=43)


class TokenPasswordRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=43, max_length=43)
    new_password: str = Field(min_length=12, max_length=MAX_PASSWORD_LENGTH)


class InvitationAccept(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=43, max_length=43)
    password: str = Field(min_length=12, max_length=MAX_PASSWORD_LENGTH)


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class Invite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    roles: list[str] = Field(min_length=1, max_length=4)


class RolesChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    roles: list[str] = Field(min_length=1, max_length=4)


class MemberRole(BaseModel):
    role: str
    granted_at: str
    granted_by: str | None
    cooling_off_until: str | None


class Member(BaseModel):
    user_id: str
    email: str
    display_name: str
    status: str
    email_verified: bool
    disabled: bool
    roles: list[MemberRole]
    member_since: str
    invitation_pending: bool
    etag: str


class MemberList(BaseModel):
    items: list[Member]


class AuditEvent(BaseModel):
    seq: str
    occurred_at: str
    event_type: str
    outcome: str
    user_id: str | None
    session_id: str | None
    source_key: str | None
    request_id: str | None
    detail: dict
    canonical_content: str
    prev_hash: str
    hash: str


class AuditChain(BaseModel):
    last_seq: str
    head: str | None


class AuditPage(BaseModel):
    items: list[AuditEvent]
    next_before: str | None
    chain: AuditChain


class Accepted(BaseModel):
    status: str = 'ACCEPTED'
    detail: str


class Confirmed(BaseModel):
    status: str
    sessions_revoked: int = 0


class Reauthenticated(BaseModel):
    fresh_until: str


class SessionsRevoked(BaseModel):
    user_id: str
    sessions_revoked: int


PUBLIC = {'x-lv-permission': 'public'}


def _public(request):
    check_transport(request)
    check_origin(request)
    return source_key(request), request.state.request_id


def _new_token():
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def _token(value):
    if not TOKEN.fullmatch(value):
        raise ProblemError(400, 'TOKEN_INVALID', 'this link is not valid; request a new one')
    return token_hash(value)


def _issue(request, background, purpose, email):
    source, request_id = _public(request)
    token, digest = _new_token()
    with request.app.state.pool.connection() as conn:
        with conn.transaction():
            r = conn.execute('SELECT lv_identity.token_issue(%s,%s,%s,%s,%s)', (purpose, email, digest, source, request_id)).fetchone()[0]
    if r['throttled']:
        raise ProblemError(429, 'RATE_LIMITED', 'too many requests; try again later', headers={'Retry-After': str(r['retry_after_seconds'])})
    if r['deliver']:
        background.add_task(mail.deliver, purpose, r['email'], r['display_name'], token, int(r['expires_in_seconds']))
    del token
    return Accepted(detail='If this address belongs to an account that can use it, a message with a link is on its way.')


def _consume(request, purpose, token, password=None):
    source, request_id = _public(request)
    digest = _token(token)
    password_hash = hash_password(password) if password is not None else None
    with request.app.state.pool.connection() as conn:
        with conn.transaction():
            r = conn.execute('SELECT lv_identity.token_consume(%s,%s,%s,%s,%s)', (purpose, digest, password_hash, source, request_id)).fetchone()[0]
    if r.get('throttled'):
        raise ProblemError(429, 'RATE_LIMITED', 'too many attempts from this address; try again later', headers={'Retry-After': str(r['retry_after_seconds'])})
    if not r['ok']:
        raise ProblemError(400, 'TOKEN_INVALID', 'this link is not valid, was already used or has expired; request a new one')
    return Confirmed(status='CONFIRMED', sessions_revoked=r.get('sessions_revoked', 0))


@router.post('/auth/email-verification', response_model=Accepted, status_code=202, operation_id='requestEmailVerification', tags=['auth'],
             responses={429: {'description': 'RATE_LIMITED'}}, openapi_extra=PUBLIC)
def request_verification(body: EmailRequest, request: Request, background: BackgroundTasks):
    return _issue(request, background, 'VERIFY_EMAIL', body.email)


@router.post('/auth/email-verification/confirm', response_model=Confirmed, operation_id='confirmEmailVerification', tags=['auth'],
             responses={400: {'description': 'TOKEN_INVALID (same answer for every refusal)'}, 429: {'description': 'RATE_LIMITED'}}, openapi_extra=PUBLIC)
def confirm_verification(body: TokenRequest, request: Request):
    return _consume(request, 'VERIFY_EMAIL', body.token)


@router.post('/auth/password-reset', response_model=Accepted, status_code=202, operation_id='requestPasswordReset', tags=['auth'],
             responses={429: {'description': 'RATE_LIMITED'}}, openapi_extra=PUBLIC)
def request_reset(body: EmailRequest, request: Request, background: BackgroundTasks):
    return _issue(request, background, 'RESET_PASSWORD', body.email)


@router.post('/auth/password-reset/confirm', response_model=Confirmed, operation_id='confirmPasswordReset', tags=['auth'],
             responses={400: {'description': 'TOKEN_INVALID'}, 429: {'description': 'RATE_LIMITED'}}, openapi_extra=PUBLIC)
def confirm_reset(body: TokenPasswordRequest, request: Request):
    return _consume(request, 'RESET_PASSWORD', body.token, body.new_password)


@router.post('/auth/invitation/accept', response_model=Confirmed, operation_id='acceptInvitation', tags=['auth'],
             responses={400: {'description': 'TOKEN_INVALID'}, 429: {'description': 'RATE_LIMITED'}}, openapi_extra=PUBLIC)
def accept_invitation(body: InvitationAccept, request: Request):
    return _consume(request, 'INVITATION', body.token, body.password)


def _session_call(request):
    check_transport(request)
    token = session_token(request)
    if token is None or not csrf_matches(token, request.headers.get('x-csrf-token')):
        raise ProblemError(401 if token is None else 403, 'UNAUTHENTICATED' if token is None else 'PERMISSION_DENIED',
                           'no valid session' if token is None else 'missing or invalid CSRF token')
    check_origin(request)
    return token, source_key(request), request.state.request_id


@router.post('/auth/reauthenticate', response_model=Reauthenticated, operation_id='reauthenticate', tags=['auth'],
             responses={401: {'description': 'UNAUTHENTICATED or wrong password'}, 429: {'description': 'RATE_LIMITED'}},
             openapi_extra={'x-lv-permission': 'any-session'})
def reauthenticate(body: PasswordRequest, request: Request):
    token, source, request_id = _session_call(request)
    with request.app.state.pool.connection() as conn:
        with conn.transaction():
            prepared = conn.execute('SELECT lv_identity.reauth_prepare(%s,%s,%s)', (token_hash(token), source, request_id)).fetchone()[0]
        if not prepared['valid']:
            raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
        if prepared['throttled']:
            raise ProblemError(429, 'RATE_LIMITED', 'too many attempts; try again later', headers={'Retry-After': str(prepared['retry_after_seconds'])})
        ok = verify_password(prepared['password_hash'], body.password)
        with conn.transaction():
            done = conn.execute('SELECT lv_identity.reauth_finish(%s,%s,%s,%s)', (token_hash(token), ok, source, request_id)).fetchone()[0]
    if not done['valid'] or not done.get('accepted'):
        raise ProblemError(401, 'UNAUTHENTICATED', 'the password was not accepted')
    return Reauthenticated(fresh_until=done['fresh_until'])


@router.post('/auth/sessions/revoke-all', status_code=204, operation_id='revokeAllMySessions', tags=['auth'],
             responses={401: {'description': 'UNAUTHENTICATED'}}, openapi_extra={'x-lv-permission': 'any-session'})
def revoke_all(request: Request):
    token, source, request_id = _session_call(request)
    with request.app.state.pool.connection() as conn:
        with conn.transaction():
            r = conn.execute('SELECT lv_identity.revoke_my_sessions(%s,%s,%s)', (token_hash(token), source, request_id)).fetchone()[0]
    if not r['valid']:
        raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
    response = Response(status_code=204)
    s = request.app.state.auth_settings
    response.delete_cookie(s.cookie_name, path='/', secure=s.cookie_secure, httponly=True, samesite='lax')
    return response


def _unauthenticated(exc):
    raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session') from exc


def _respond(result, model=None):
    headers = {'Idempotent-Replayed': str(bool(result.get('replayed'))).lower()}
    if result['status'] in (200, 201):
        body = result['body']
        if model:
            model.model_validate(body)
        if isinstance(body, dict) and body.get('etag'):
            headers['ETag'] = '"' + body['etag'] + '"'
        return JSONResponse(body, status_code=result['status'], headers=headers)
    raise ProblemError(result['status'], result['code'], result.get('detail'), headers=headers, errors=result.get('errors'))


@router.get('/members', response_model=MemberList, operation_id='listMembers', tags=['members'], openapi_extra={'x-lv-permission': 'read'})
def list_members(request: Request):
    check_query(request, ())
    try:
        with session_transaction(request, touch=True) as ctx:
            r = ctx.conn.execute('SELECT lv_identity.member_list(%s,%s,%s)', (token_hash(ctx.session_token), source_key(request), request.state.request_id)).fetchone()[0]
    except psycopg.errors.InsufficientPrivilege as e:
        _unauthenticated(e)
    return _respond(r, MemberList)


def _member_write(request, action, target, args, if_match, idempotency_key, background, invite_token=None):
    if idempotency_key is None or not IDEMPOTENCY.fullmatch(idempotency_key):
        raise ProblemError(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key must contain 1-64 visible ASCII characters')
    for role in args.get('roles', []):
        if role not in ROLES:
            raise ProblemError(422, 'SCHEMA_INVALID', 'unknown role', errors=[{'pointer': '/roles', 'code': 'enum'}])
    match = if_match.strip().strip('"') if if_match else None
    rhash = hashlib.sha256(repr((action, str(target), sorted(args.items()), match)).encode()).hexdigest()
    unusable = hash_password(secrets.token_urlsafe(32)) if action == 'INVITE' else None   # never usable: replaced on acceptance
    try:
        with session_transaction(request, touch=False) as ctx:
            r = ctx.conn.execute('SELECT lv_identity.member_api(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (
                token_hash(ctx.session_token), action, target, Jsonb(args), match, idempotency_key, rhash,
                invite_token[1] if invite_token else None, unusable, source_key(request), request.state.request_id)).fetchone()[0]
    except psycopg.errors.InsufficientPrivilege as e:
        _unauthenticated(e)
    if r.get('deliver') and not r.get('replayed'):
        kind = r['deliver']
        background.add_task(mail.deliver, kind, r['deliver_email'], r['body'].get('display_name'), invite_token[0] if kind == 'INVITATION' else None, 7 * 86400)
    return r


WRITE = {'x-lv-permission': 'administer', 'x-lv-idempotency': 'required', 'x-lv-reauthentication': 'required within 15 minutes'}


@router.post('/members', response_model=Member, status_code=201, operation_id='inviteMember', tags=['members'], openapi_extra=WRITE,
             responses={403: {'description': 'PERMISSION_DENIED, REAUTHENTICATION_REQUIRED or SEGREGATION_OF_DUTIES'}, 409: {'description': 'MEMBER_EXISTS'}})
def invite_member(body: Invite, request: Request, background: BackgroundTasks,
                  idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None):
    args = {'email': body.email, 'roles': sorted(set(body.roles))}
    if body.display_name:
        args['display_name'] = body.display_name
    return _respond(_member_write(request, 'INVITE', None, args, None, idempotency_key, background, _new_token()), Member)


@router.patch('/members/{user_id}', response_model=Member, operation_id='changeMemberRoles', tags=['members'], openapi_extra={**WRITE, 'x-lv-version-guard': 'If-Match member etag'},
              responses={403: {'description': 'PERMISSION_DENIED, REAUTHENTICATION_REQUIRED or SEGREGATION_OF_DUTIES'}, 409: {'description': 'LAST_ADMINISTRATOR'},
                         412: {'description': 'PRECONDITION_FAILED'}, 428: {'description': 'PRECONDITION_REQUIRED'}})
def change_roles(user_id: UUID, body: RolesChange, request: Request, background: BackgroundTasks,
                 if_match: Annotated[str | None, Header(alias='If-Match', max_length=80)] = None,
                 idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None):
    return _respond(_member_write(request, 'SET_ROLES', user_id, {'roles': sorted(set(body.roles))}, if_match, idempotency_key, background), Member)


@router.delete('/members/{user_id}', response_model=Member, operation_id='removeMember', tags=['members'], openapi_extra={**WRITE, 'x-lv-version-guard': 'If-Match member etag'},
               responses={409: {'description': 'LAST_ADMINISTRATOR'}, 412: {'description': 'PRECONDITION_FAILED'}, 428: {'description': 'PRECONDITION_REQUIRED'}})
def remove_member(user_id: UUID, request: Request, background: BackgroundTasks,
                  if_match: Annotated[str | None, Header(alias='If-Match', max_length=80)] = None,
                  idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None):
    return _respond(_member_write(request, 'REMOVE', user_id, {}, if_match, idempotency_key, background), Member)


@router.post('/members/{user_id}/sessions/revoke', response_model=SessionsRevoked, operation_id='revokeMemberSessions', tags=['members'], openapi_extra=WRITE)
def revoke_member_sessions(user_id: UUID, request: Request, background: BackgroundTasks,
                           idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key', max_length=256)] = None):
    return _respond(_member_write(request, 'REVOKE_SESSIONS', user_id, {}, None, idempotency_key, background), SessionsRevoked)


@router.get('/audit-events', response_model=AuditPage, operation_id='listAuditEvents', tags=['audit'], openapi_extra={'x-lv-permission': 'audit.read'})
def audit_events(request: Request, before: Annotated[str | None, Query(pattern=r'^[1-9][0-9]{0,18}$')] = None,
                 limit: Annotated[int, Query(ge=1, le=200)] = 50):
    check_query(request, ('before', 'limit'))
    try:
        with session_transaction(request, touch=True) as ctx:
            r = ctx.conn.execute('SELECT lv_identity.audit_page(%s,%s,%s,%s,%s)', (token_hash(ctx.session_token), int(before) if before else None, limit,
                                                                                    source_key(request), request.state.request_id)).fetchone()[0]
    except psycopg.errors.InsufficientPrivilege as e:
        _unauthenticated(e)
    return _respond(r, AuditPage)
