"""The reusable request boundary for future routes: verified session → verified tenant → required permission.

`require_permission('review')` (for example) yields a RequestContext whose `conn` is inside ONE database transaction
that began with lv_identity.session_context(token hash). That function binds app.tenant_id / app.user_id from the
session row, so every query in the route runs under row-level security for the session's tenant. Nothing the client
sends (headers, query, body, frontend state) can choose the tenant or the user.
"""
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi import Request

from .crypto import csrf_matches, token_hash
from .problems import ProblemError
from .settings import LOOPBACK_HOSTS, LOCAL_HTTP

UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
PERMISSIONS = ('read', 'propose', 'review', 'execute', 'source.import', 'ledger.bootstrap', 'regression', 'evidence.export',
               'audit.read', 'administer')
# Stage 2B §5.1 (2A §12.2). Must equal lv_identity.role_permission (tested).
ROLE_PERMISSIONS = {
    'ANALYST': ('read', 'propose', 'source.import'),
    'CONTROLLER': ('read', 'review', 'execute', 'regression', 'evidence.export', 'audit.read'),
    'AUTOMATION_DEVELOPER': ('read', 'propose', 'source.import', 'regression', 'evidence.export'),
    'ADMINISTRATOR': ('read', 'ledger.bootstrap', 'audit.read', 'administer'),
}
SOURCE_RE = re.compile(r'[^A-Za-z0-9.:_\[\]-]')


@dataclass
class RequestContext:
    conn: object
    session: dict
    session_token: str = field(repr=False)

    @property
    def user_id(self) -> str:
        return self.session['user']['id']

    @property
    def tenant_id(self) -> str | None:
        return self.session['tenant_id']

    @property
    def permissions(self) -> frozenset:
        return frozenset(self.session['permissions'])


def source_key(request: Request) -> str:
    """The client address used for rate limits and audit. Locally this is the direct peer (no proxy headers are trusted:
    tools/serve_api runs uvicorn with proxy_headers=False). In the deployment layout uvicorn trusts X-Forwarded-For only from
    LV_FORWARDED_ALLOW_IPS, and the Caddy proxy overwrites that header with the real peer address (deploy/proxy/Caddyfile)."""
    host = request.client.host if request.client else 'unknown'
    return SOURCE_RE.sub('_', host)[:64] or 'unknown'


def check_transport(request: Request):
    settings = request.app.state.auth_settings
    if settings.cookie_mode == LOCAL_HTTP and request.url.hostname not in {h.strip('[]') for h in LOOPBACK_HOSTS}:
        raise ProblemError(403, 'PERMISSION_DENIED', 'local-http mode only serves loopback hosts')


def check_origin(request: Request):
    """Unsafe requests from a browser must come from an allowed origin (defence in depth with SameSite=Lax and CSRF)."""
    origin = request.headers.get('origin')
    if origin is None:
        return
    allowed = request.app.state.auth_settings.allowed_origins or {f'{request.url.scheme}://{request.url.netloc}'}
    if origin not in allowed:
        raise ProblemError(403, 'PERMISSION_DENIED', 'cross-origin request refused')


def session_token(request: Request) -> str | None:
    token = request.cookies.get(request.app.state.auth_settings.cookie_name)
    return token if token and len(token) <= 128 else None


def check_csrf(request: Request, token: str):
    if request.method in UNSAFE_METHODS and not csrf_matches(token, request.headers.get('x-csrf-token')):
        raise ProblemError(403, 'PERMISSION_DENIED', 'missing or invalid CSRF token')


def _open_context(request: Request, conn, token: str, touch: bool) -> dict:
    result = conn.execute('SELECT lv_identity.session_context(%s, %s)', (token_hash(token), touch)).fetchone()[0]
    if not result['valid']:
        raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
    return result['session']


@contextmanager
def session_transaction(request: Request, *, permission: str | None = None, tenant: bool = True, touch: bool = True):
    """The boundary itself: verified session -> verified tenant -> required permission, inside ONE transaction.

    Used directly (as a context manager) by state-changing routes that must COMMIT before they respond, so that a
    commit-time failure (for example a deferred constraint) can never be reported to the client as success.
    """
    if permission is not None and permission not in PERMISSIONS:
        raise ValueError(f'unknown permission {permission!r}')
    check_transport(request)
    token = session_token(request)
    if token is None:
        raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
    check_csrf(request, token)
    if request.method in UNSAFE_METHODS:
        check_origin(request)
    with request.app.state.pool.connection() as conn:
        with conn.transaction():
            session = _open_context(request, conn, token, touch)
            if (tenant or permission) and session['tenant_id'] is None:
                raise ProblemError(403, 'PERMISSION_DENIED', 'select a tenant first (POST /api/v1/auth/switch-tenant)')
            if permission is not None and permission not in session['permissions']:
                raise ProblemError(403, 'PERMISSION_DENIED', f'requires {permission}')
            yield RequestContext(conn, session, token)


def require_session(*, permission: str | None = None, tenant: bool = True, touch: bool = True):
    """Dependency factory. permission=None with tenant=False accepts any valid session (e.g. GET /me)."""
    if permission is not None and permission not in PERMISSIONS:
        raise ValueError(f'unknown permission {permission!r}')

    def dependency(request: Request):
        with session_transaction(request, permission=permission, tenant=tenant, touch=touch) as ctx:
            yield ctx
    dependency.__name__ = f'require_session_{permission or "any"}'
    dependency.lv_permission = permission
    return dependency


def require_permission(permission: str):
    return require_session(permission=permission, tenant=True)


def require_read_only(permission: str = 'read'):
    """Dependency for financial READ routes: the same boundary as require_permission, then the transaction is switched to
    READ ONLY. Session bookkeeping (session_context's last_seen_at touch) has already happened inside the boundary and still
    commits; after the switch PostgreSQL refuses every write to a non-temporary table for the rest of the request, whatever
    the route code or a called function attempts (ReadOnlySqlTransaction -> 500, nothing written)."""
    if permission not in PERMISSIONS:
        raise ValueError(f'unknown permission {permission!r}')

    def dependency(request: Request):
        with session_transaction(request, permission=permission, tenant=True) as ctx:
            ctx.conn.execute('SET TRANSACTION READ ONLY')
            yield ctx
    dependency.__name__ = f'require_read_only_{permission}'
    dependency.lv_permission = permission
    return dependency
