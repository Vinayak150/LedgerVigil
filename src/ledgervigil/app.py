"""LedgerVigil HTTP application. FastAPI owns authentication (ADR-015/ADR-027); there is no other identity authority."""
import logging
import os
import re
import uuid

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ledgervigil.auth.pool import ConnectionPool
from ledgervigil.auth.problems import ProblemError, problem_response
from ledgervigil.auth.routes import router as auth_router
from ledgervigil.auth.settings import AuthSettings, load_settings
from ledgervigil.periods.routes import router as periods_router
from ledgervigil.configuration.routes import router as configuration_router
from ledgervigil.proposals.routes import router as proposals_router
from ledgervigil.proposals.submission import router as workflow_router
from ledgervigil.proposals.comments import router as comments_router
from ledgervigil.proposals.approval import router as approval_router
from ledgervigil.proposals.rejection import router as rejection_router
from ledgervigil.execution.proof_export import router as execution_router
from ledgervigil.proposals.overrides import router as overrides_router
from ledgervigil.reads.ledgers import router as ledgers_router
from ledgervigil.reads.sources import router as sources_router
from ledgervigil.simulation.routes import router as simulations_router

from ledgervigil.ingestion.registry import router as source_registry_router
from ledgervigil.ingestion.index_routes import router as source_index_router

log = logging.getLogger('ledgervigil.app')
from ledgervigil import logredact  # noqa: E402
logredact.install()
REQUEST_ID = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

# D1: the accepted Stage 2B Part 3 `Problem` component. Runtime bodies (auth/problems.py) already match it; before this
# correction the generated contract declared every application/problem+json response without any schema. `state` and
# `reason` are existing runtime extensions (problems.EXTRA_FIELDS), documented here rather than forbidden.
PROBLEM_SCHEMA = {
    'type': 'object', 'title': 'Problem',
    'description': 'RFC 9457 problem details with the Stage 2B extensions code, class, retryable and request_id (Stage 2B Part 3/Part 4).',
    'required': ['type', 'title', 'status', 'code', 'class', 'retryable', 'request_id'],
    'properties': {
        'type': {'type': 'string', 'format': 'uri'}, 'title': {'type': 'string'}, 'status': {'type': 'integer'},
        'code': {'type': 'string'},
        'class': {'enum': ['VALIDATION', 'AUTHENTICATION', 'AUTHORIZATION', 'NOT_FOUND', 'CONFLICT', 'STALE', 'PRECONDITION',
                           'UNSUPPORTED', 'RATE_LIMITED', 'INFRASTRUCTURE']},
        'retryable': {'type': 'boolean'}, 'detail': {'type': 'string'}, 'request_id': {'type': 'string'},
        'current_version': {'type': ['integer', 'null']},
        'errors': {'type': 'array', 'items': {'type': 'object', 'properties': {'pointer': {'type': 'string'}, 'code': {'type': 'string'}}}},
        'state': {'type': 'string', 'description': 'LedgerVigil extension: current state where a conflict names it'},
        'reason': {'type': 'string', 'description': 'LedgerVigil extension: refusal reason qualifier (for example ACCOUNT_IN_USE)'}}}
PROBLEM_REF = {'$ref': '#/components/schemas/Problem'}


def _with_problem_schema(spec: dict) -> dict:
    """Attach the Problem component and reference it from every problem+json response that declares no schema."""
    spec.setdefault('components', {}).setdefault('schemas', {})['Problem'] = PROBLEM_SCHEMA
    for ops in spec.get('paths', {}).values():
        for op in ops.values():
            for response in op.get('responses', {}).values():
                content = response.get('content', {}).get('application/problem+json')
                if content is not None and 'schema' not in content:
                    content['schema'] = dict(PROBLEM_REF)
    return spec


def _env_connect():
    """Runtime database login from LV_DATABASE_URL. The pool verifies that it is the restricted lv_app role."""
    url = os.environ.get('LV_DATABASE_URL')
    if not url:
        raise RuntimeError('LV_DATABASE_URL is not configured')
    return psycopg.connect(url, autocommit=False)


def detection_enabled() -> bool:
    """PROPOSED DET-6: the detection slice is off unless LV_DETECTION_ENABLED=1; its routes are then absent from the contract."""
    return os.environ.get('LV_DETECTION_ENABLED') == '1'


def provider_sandbox_enabled() -> bool:
    """PROPOSED PROV-1: the QuickBooks SANDBOX read-only connection is off unless LV_PROVIDER_SANDBOX_ENABLED=1; its routes are
    then absent from the contract. No setting enables a provider write or a production provider."""
    return os.environ.get('LV_PROVIDER_SANDBOX_ENABLED') == '1'


def create_app(settings: AuthSettings | None = None, connect=None, *, detection: bool | None = None, provider: bool | None = None) -> FastAPI:
    app = FastAPI(title='LedgerVigil', version='0.0.1')
    app.state.auth_settings = settings or load_settings()
    app.state.pool = ConnectionPool(connect or _env_connect)

    @app.middleware('http')
    async def request_id(request: Request, call_next):
        presented = request.headers.get('x-request-id')
        request.state.request_id = presented if presented and REQUEST_ID.match(presented) else uuid.uuid4().hex
        response = await call_next(request)
        response.headers['X-Request-Id'] = request.state.request_id
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ProblemError)
    async def problem(request: Request, exc: ProblemError):
        return problem_response(request, exc.status, exc.code, exc.detail, exc.headers, exc.errors, extra=exc.extra)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError):
        # Never echo submitted values (they may contain a password); report only locations and error types.
        errors = [{'pointer': '/' + '/'.join(str(p) for p in e.get('loc', ())[1:]), 'code': e.get('type', 'invalid')} for e in exc.errors()]
        return problem_response(request, 422, 'SCHEMA_INVALID', 'request body or parameters are not valid', errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {401: 'UNAUTHENTICATED', 403: 'PERMISSION_DENIED', 404: 'NOT_FOUND'}.get(exc.status_code, 'SCHEMA_INVALID')
        return problem_response(request, exc.status_code, code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        cause = exc
        while cause is not None:   # restore interlock (0037): a clear, actionable refusal instead of a generic 500
            if getattr(cause, 'sqlstate', None) == 'LV503':
                return problem_response(request, 503, 'RECOVERY_VERIFICATION_REQUIRED',
                                        'this database was restored and has not passed recovery verification; financial writes are paused')
            cause = cause.__cause__ or cause.__context__
        log.error('unhandled error request_id=%s type=%s', getattr(request.state, 'request_id', None), type(exc).__name__)
        return problem_response(request, 500, 'INTERNAL', 'unexpected error')

    @app.get('/health', include_in_schema=False)
    def health():
        return {'status': 'ok', 'milestone': 'development-foundation'}

    from ledgervigil.exports.routes import router as export_router
    app.include_router(export_router)
    from ledgervigil.proofpacks.routes import router as pack_router
    app.include_router(pack_router)
    app.include_router(auth_router)
    from ledgervigil.auth.lifecycle import router as lifecycle_router
    app.include_router(lifecycle_router)
    app.include_router(source_registry_router)
    app.include_router(source_index_router)
    app.include_router(ledgers_router)
    app.include_router(periods_router)
    app.include_router(configuration_router)
    app.include_router(proposals_router)
    app.include_router(sources_router)
    app.include_router(simulations_router)
    app.include_router(workflow_router)
    app.include_router(comments_router)
    app.include_router(overrides_router)
    app.include_router(approval_router)
    app.include_router(rejection_router)
    app.include_router(execution_router)
    if detection_enabled() if detection is None else detection:
        from ledgervigil.detection.routes import router as detection_router
        app.include_router(detection_router)
    if provider_sandbox_enabled() if provider is None else provider:
        from ledgervigil.provider.routes import router as provider_router
        app.include_router(provider_router)

    default_openapi = app.openapi

    def openapi():
        if app.openapi_schema is None:
            app.openapi_schema = _with_problem_schema(default_openapi())
        return app.openapi_schema

    app.openapi = openapi
    return app


app = create_app()
