"""Stage 2B Family 1 (subset) and Family 2 read-only catalog: login, logout, switch-tenant, me, current tenant, permissions.

No route here writes financial data, seeds, ingests, changes periods, manages roles or executes anything.
"""
import logging

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .boundary import (PERMISSIONS, ROLE_PERMISSIONS, RequestContext, check_origin, check_transport, csrf_matches, require_permission,
                       require_session, session_token, source_key)
from .crypto import MAX_PASSWORD_LENGTH, csrf_token_for, new_session_token, token_hash, verify_password
from .problems import ProblemError

log = logging.getLogger('ledgervigil.auth')
router = APIRouter(prefix='/api/v1')
ABSOLUTE_SECONDS = 12 * 3600


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class SwitchTenantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tenant_id: UUID


class UserRef(BaseModel):
    id: str
    email: str
    display_name: str


class TenantRef(BaseModel):
    id: str


class Session(BaseModel):
    """The caller's session. The session token itself is only in the HttpOnly cookie and never in a body."""
    user: UserRef
    tenant: TenantRef | None
    tenant_selection_required: bool
    available_tenants: list[TenantRef]
    roles: list[str]
    permissions: list[str]
    idle_expires_at: str
    absolute_expires_at: str
    csrf_token: str = Field(description='Send as X-CSRF-Token on unsafe requests; derived from this session and rotated with it')


class Tenant(BaseModel):
    id: str
    name: str


class PermissionCatalog(BaseModel):
    permissions: list[str]
    roles: dict[str, list[str]]


def _session_body(info: dict, token: str) -> Session:
    return Session(user=UserRef(**info['user']), tenant=TenantRef(id=info['tenant_id']) if info['tenant_id'] else None,
                   tenant_selection_required=info['tenant_selection_required'],
                   available_tenants=[TenantRef(id=t) for t in info['available_tenant_ids']], roles=info['roles'],
                   permissions=sorted(info['permissions']), idle_expires_at=info['idle_expires_at'],
                   absolute_expires_at=info['absolute_expires_at'], csrf_token=csrf_token_for(token))


def _set_cookie(request: Request, response: Response, token: str):
    s = request.app.state.auth_settings
    response.set_cookie(s.cookie_name, token, max_age=ABSOLUTE_SECONDS, path='/', secure=s.cookie_secure, httponly=True, samesite='lax')


def _clear_cookie(request: Request, response: Response):
    s = request.app.state.auth_settings
    response.delete_cookie(s.cookie_name, path='/', secure=s.cookie_secure, httponly=True, samesite='lax')


def _attribution(request: Request):
    return source_key(request), request.state.request_id


@router.post('/auth/login', response_model=Session, operation_id='login', tags=['auth'],
             responses={401: {'description': 'UNAUTHENTICATED (same response for every refusal)'}, 429: {'description': 'RATE_LIMITED'}},
             openapi_extra={'x-lv-permission': 'public'})
def login(body: LoginRequest, request: Request, response: Response):
    check_transport(request)
    check_origin(request)
    if not request.headers.get('content-type', '').split(';')[0].strip() == 'application/json':
        raise ProblemError(422, 'SCHEMA_INVALID', 'Content-Type must be application/json')
    source, request_id = _attribution(request)
    previous = session_token(request)
    with request.app.state.issuer_pool.connection() as conn:
        with conn.transaction():
            prepared = conn.execute('SELECT lv_identity.login_prepare(%s, %s, %s)', (body.email, source, request_id)).fetchone()[0]
            if not prepared['throttled']:
                password_ok = verify_password(prepared['password_hash'], body.password)
                token = new_session_token()
                result = conn.execute('SELECT lv_identity.login_finish(%s, %s, %s, %s, %s, %s)',
                                      (prepared['attempt_id'], password_ok, token_hash(token),
                                       token_hash(previous) if previous else None, source, request_id)).fetchone()[0]
    if prepared['throttled']:
        log.info('login throttled request_id=%s', request_id)
        raise ProblemError(429, 'RATE_LIMITED', 'too many sign-in attempts; try again later',
                           headers={'Retry-After': str(prepared['retry_after_seconds'])})
    if not result['authenticated']:
        log.info('login refused request_id=%s', request_id)
        raise ProblemError(401, 'UNAUTHENTICATED', 'invalid email or password')
    log.info('login succeeded request_id=%s user_id=%s', request_id, result['session']['user']['id'])
    _set_cookie(request, response, token)
    return _session_body(result['session'], token)


@router.post('/auth/logout', status_code=204, operation_id='logout', tags=['auth'],
             responses={403: {'description': 'PERMISSION_DENIED (CSRF or origin)'}},
             openapi_extra={'x-lv-permission': 'any-session'})
def logout(request: Request):
    check_transport(request)
    check_origin(request)
    token = session_token(request)
    if token is None:
        response = Response(status_code=204)
        _clear_cookie(request, response)
        return response
    if not csrf_matches(token, request.headers.get('x-csrf-token')):
        raise ProblemError(403, 'PERMISSION_DENIED', 'missing or invalid CSRF token')
    check_origin(request)
    source, request_id = _attribution(request)
    with request.app.state.issuer_pool.connection() as conn:
        with conn.transaction():
            revoked = conn.execute('SELECT lv_identity.logout(%s, %s, %s)', (token_hash(token), source, request_id)).fetchone()[0]
    response = Response(status_code=204)
    _clear_cookie(request, response)
    log.info('logout request_id=%s revoked=%s', request_id, revoked)
    return response


@router.post('/auth/switch-tenant', response_model=Session, operation_id='switchTenant', tags=['auth'],
             responses={401: {'description': 'UNAUTHENTICATED'}, 403: {'description': 'PERMISSION_DENIED (CSRF or origin)'},
                        404: {'description': 'NOT_FOUND (not an active member of that tenant, or no such tenant)'}},
             openapi_extra={'x-lv-permission': 'membership'})
def switch_tenant(body: SwitchTenantRequest, request: Request, response: Response):
    check_transport(request)
    token = session_token(request)
    if token is None:
        raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
    if not csrf_matches(token, request.headers.get('x-csrf-token')):
        raise ProblemError(403, 'PERMISSION_DENIED', 'missing or invalid CSRF token')
    check_origin(request)
    source, request_id = _attribution(request)
    new_token = new_session_token()
    with request.app.state.issuer_pool.connection() as conn:
        with conn.transaction():
            result = conn.execute('SELECT lv_identity.switch_tenant(%s, %s, %s, %s, %s)',
                                  (token_hash(token), body.tenant_id, token_hash(new_token), source, request_id)).fetchone()[0]
    if not result['valid']:
        raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
    if not result['selected']:
        raise ProblemError(404, 'NOT_FOUND', 'tenant not found')
    _set_cookie(request, response, new_token)
    log.info('tenant selected request_id=%s user_id=%s', request_id, result['session']['user']['id'])
    return _session_body(result['session'], new_token)


@router.get('/me', response_model=Session, operation_id='getMe', tags=['auth'], responses={401: {'description': 'UNAUTHENTICATED'}},
            openapi_extra={'x-lv-permission': 'any-session'})
def me(ctx: Annotated[RequestContext, Depends(require_session(tenant=False))]):
    return _session_body(ctx.session, ctx.session_token)


@router.get('/tenants/current', response_model=Tenant, operation_id='getCurrentTenant', tags=['tenants'],
            responses={401: {'description': 'UNAUTHENTICATED'}, 403: {'description': 'PERMISSION_DENIED'}},
            openapi_extra={'x-lv-permission': 'read'})
def current_tenant(ctx: Annotated[RequestContext, Depends(require_permission('read'))]):
    # Read under row-level security: the transaction's tenant was bound by session_context, not by this code.
    row = ctx.conn.execute('SELECT tenant_id::text, name FROM lv.tenant').fetchall()
    if len(row) != 1 or row[0][0] != ctx.tenant_id:
        raise ProblemError(404, 'NOT_FOUND', 'tenant not found')
    return Tenant(id=row[0][0], name=row[0][1])


@router.get('/permissions', response_model=PermissionCatalog, operation_id='getPermissionCatalog', tags=['catalogs'],
            responses={401: {'description': 'UNAUTHENTICATED'}, 403: {'description': 'PERMISSION_DENIED'}},
            openapi_extra={'x-lv-permission': 'read'})
def permission_catalog(ctx: Annotated[RequestContext, Depends(require_permission('read'))]):
    return PermissionCatalog(permissions=list(PERMISSIONS), roles={r: list(p) for r, p in ROLE_PERMISSIONS.items()})


