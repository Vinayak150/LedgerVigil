"""RFC 9457 problem responses with the Stage 2B extensions (code, class, retryable, request_id)."""
from fastapi.responses import JSONResponse

CLASSES = {'SEGREGATION_OF_DUTIES':'AUTHORIZATION','ALREADY_DECIDED':'CONFLICT','PROPOSAL_STALE':'CONFLICT','UNAUTHENTICATED': 'AUTHENTICATION', 'PERMISSION_DENIED': 'AUTHORIZATION', 'NOT_FOUND': 'NOT_FOUND',
           'SCHEMA_INVALID': 'VALIDATION', 'RATE_LIMITED': 'RATE_LIMITED', 'INTERNAL': 'INFRASTRUCTURE',
           'DEPENDENCY_UNAVAILABLE': 'INFRASTRUCTURE',
           # Stage 2B §4.2 codes used by period close (0009).
           'IDEMPOTENCY_KEY_REQUIRED': 'VALIDATION', 'IDEMPOTENCY_KEY_REUSED': 'CONFLICT', 'REQUEST_IN_PROGRESS': 'CONFLICT',
           'VERSION_MISMATCH': 'CONFLICT', 'VERSION_REQUIRED': 'CONFLICT', 'INVALID_STATE_TRANSITION': 'CONFLICT',
           'CAPABILITY_UNAVAILABLE': 'UNSUPPORTED',
           # Stage 2B §4.2 list-endpoint codes (native financial reads).
           'UNKNOWN_QUERY_PARAMETER': 'VALIDATION', 'CURSOR_MISMATCH': 'VALIDATION',
           # Stage 2B §4.2 codes used by proposal drafts (0010).
           'MONEY_MUST_BE_STRING': 'VALIDATION', 'MONEY_SCALE_EXCEEDED': 'VALIDATION', 'CURRENCY_MISMATCH': 'VALIDATION',
           'UNSUPPORTED_BY_PROFILE': 'UNSUPPORTED',
           # Stage 2B §4.2 (simulation foundation, 0011). SIMULATION_VERSION_UNAVAILABLE: the contract gives replay a 409 for a
           # pinned version that is no longer installed but names no code (docs/SIMULATION.md SM-G3).
           'PRECONDITION_FAILED': 'PRECONDITION', 'SIMULATION_VERSION_UNAVAILABLE': 'CONFLICT'}
TITLES = {'SEGREGATION_OF_DUTIES':'Separation of duties required','ALREADY_DECIDED':'Revision already decided','PROPOSAL_STALE':'Proposal is stale','UNAUTHENTICATED': 'Authentication required', 'PERMISSION_DENIED': 'Permission denied', 'NOT_FOUND': 'Not found',
          'SCHEMA_INVALID': 'Request is not valid', 'RATE_LIMITED': 'Too many attempts', 'INTERNAL': 'Internal error',
          'DEPENDENCY_UNAVAILABLE': 'Service unavailable', 'IDEMPOTENCY_KEY_REQUIRED': 'Idempotency-Key required',
          'IDEMPOTENCY_KEY_REUSED': 'Idempotency-Key reused', 'REQUEST_IN_PROGRESS': 'Request in progress',
          'VERSION_MISMATCH': 'Version mismatch', 'VERSION_REQUIRED': 'Version required',
          'INVALID_STATE_TRANSITION': 'Invalid state transition', 'CAPABILITY_UNAVAILABLE': 'Capability unavailable',
          'UNKNOWN_QUERY_PARAMETER': 'Unknown query parameter', 'CURSOR_MISMATCH': 'Cursor not valid for this request',
          'MONEY_MUST_BE_STRING': 'Money amount must be a string', 'MONEY_SCALE_EXCEEDED': 'Money scale exceeded',
          'CURRENCY_MISMATCH': 'Currency mismatch', 'UNSUPPORTED_BY_PROFILE': 'Unsupported by profile',
          'PRECONDITION_FAILED': 'Precondition failed', 'SIMULATION_VERSION_UNAVAILABLE': 'Pinned simulation version unavailable'}


class ProblemError(Exception):
    def __init__(self, status: int, code: str, detail: str | None = None, headers: dict | None = None, errors: list | None = None,
                 extra: dict | None = None):
        super().__init__(code)
        self.status, self.code, self.detail, self.headers, self.errors = status, code, detail, headers or {}, errors
        self.extra = extra or {}


RETRYABLE = frozenset({'RATE_LIMITED', 'REQUEST_IN_PROGRESS'})     # Stage 2B §4.2; INTERNAL stays false because no route here proves "no state changed"


EXTRA_FIELDS = ('current_version', 'state', 'reason')   # Problem schema extensions (Stage 2B Part 3 / §4.2)


def problem_response(request, status, code, detail=None, headers=None, errors=None, retryable=None, extra=None):
    retryable = code in RETRYABLE if retryable is None else retryable
    body = {'type': f'urn:ledgervigil:problem:{code.lower().replace("_", "-")}', 'title': TITLES.get(code, code), 'status': status,
            'code': code, 'class': CLASSES.get(code, 'INFRASTRUCTURE'), 'retryable': retryable,
            'request_id': getattr(request.state, 'request_id', None)}
    if detail:
        body['detail'] = detail
    if errors is not None:
        body['errors'] = errors
    for name in EXTRA_FIELDS:
        if extra and name in extra:
            body[name] = extra[name]
    return JSONResponse(body, status_code=status, headers=headers, media_type='application/problem+json')
