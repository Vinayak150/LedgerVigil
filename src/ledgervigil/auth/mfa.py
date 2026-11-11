"""Issuer-only second-factor enrollment and step-up for synthetic accounts."""
from datetime import datetime
import os
from pathlib import Path
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from . import totp
from .boundary import check_transport, session_token, source_key
from .crypto import token_hash
from .lifecycle import _session_call
from .problems import ProblemError
from ledgervigil.provider.settings import ConfigurationError, _read_private

router = APIRouter(prefix='/api/v1/auth/mfa', tags=['auth'])


class State(BaseModel):
    configured: bool
    enrolled: bool
    verified_at: datetime | None
    reset_after: datetime | None = None


class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['START', 'CONFIRM', 'CHALLENGE', 'RECOVERY', 'REGENERATE', 'RESET_REQUEST', 'RESET_CANCEL', 'RESET_COMPLETE']
    code: str | None = Field(default=None, max_length=26, repr=False)


class Receipt(BaseModel):
    setup_key: str | None = Field(default=None, repr=False)
    recovery_codes: list[str] | None = Field(default=None, repr=False)


def _key():
    filename = os.environ.get('LV_MFA_KEY_FILE')
    if not filename:
        raise ProblemError(503, 'DEPENDENCY_UNAVAILABLE', 'authenticator setup is unavailable')
    try:
        key = bytes.fromhex(_read_private(Path(filename)).decode('ascii').strip())
        if len(key) != 32:
            raise ValueError('invalid key length')
    except (OSError, ValueError, ConfigurationError):
        raise ProblemError(503, 'DEPENDENCY_UNAVAILABLE', 'authenticator setup is unavailable') from None
    return key


@router.get('', response_model=State, operation_id='getAuthenticatorState', openapi_extra={'x-lv-permission': 'any-session'})
def status(request: Request):
    check_transport(request)
    token = session_token(request)
    if token is None:
        raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
    with request.app.state.issuer_pool.connection() as conn, conn.transaction():
        state = conn.execute('SELECT lv_identity.mfa_prepare(%s,%s,%s,%s)',
                             (token_hash(token), 'STATUS', source_key(request), request.state.request_id)).fetchone()[0]
    if state['status'] != 'OK':
        raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
    return State(configured=bool(os.environ.get('LV_MFA_KEY_FILE')),
                 enrolled=state['enrolled'], verified_at=state['verified_at'], reset_after=state.get('reset_after'))


def _finish(conn, arguments, body, prepared, key):
    user = UUID(prepared['user_id'])
    enrollment, envelope, step, digest, codes, raw_codes, setup = None, None, None, None, None, None, None
    if body.action == 'START':
        enrollment = uuid4()
        secret = totp.new_secret()
        envelope = totp.seal_secret(key, user, enrollment, secret)
        setup = totp.setup_key(secret)
    else:
        enrollment = UUID(prepared['enrollment'])
        if body.action == 'RECOVERY' or (body.action.startswith('RESET_') and len(body.code or '') == 26):
            candidate = totp.recovery_digest(user, body.code or '')
            digest = candidate.hex() if candidate else None
        else:
            secret = totp.open_secret(key, user, enrollment, bytes.fromhex(prepared['envelope']))
            step = totp.match_step(secret, body.code or '', int(time.time()))
            if step is not None and body.action in ('CONFIRM', 'REGENERATE'):
                raw_codes = [totp.new_recovery_code() for _ in range(10)]
                codes = [totp.recovery_digest(user, code).hex() for code in raw_codes]
    accepted = conn.execute('SELECT lv_identity.mfa_finish(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                            (*arguments, enrollment, envelope, step, digest, codes)).fetchone()[0]
    return accepted, Receipt(setup_key=setup, recovery_codes=raw_codes)


@router.post('', response_model=Receipt, operation_id='changeAuthenticator', openapi_extra={'x-lv-permission': 'any-session'},
             responses={401: {'description': 'No session or proof refused'}, 403: {'description': 'Reauthentication required'},
                        409: {'description': 'Wrong enrollment state'}, 429: {'description': 'Rate limited'},
                        503: {'description': 'Authenticator key unavailable'}})
def change(body: Change, request: Request):
    from cryptography.exceptions import InvalidTag

    token, source, request_id = _session_call(request)
    arguments = (token_hash(token), body.action, source, request_id)
    with request.app.state.issuer_pool.connection() as conn, conn.transaction():
        prepared = conn.execute('SELECT lv_identity.mfa_prepare(%s,%s,%s,%s)', arguments).fetchone()[0]
        if prepared['status'] == 'OK':
            try:
                accepted, receipt = _finish(conn, arguments, body, prepared, _key())
            except InvalidTag:
                raise ProblemError(503, 'DEPENDENCY_UNAVAILABLE', 'authenticator setup is unavailable') from None
    match prepared['status']:
        case 'INVALID':
            raise ProblemError(401, 'UNAUTHENTICATED', 'no valid session')
        case 'THROTTLED':
            raise ProblemError(429, 'RATE_LIMITED', 'too many authenticator attempts', headers={'Retry-After': '900'})
        case 'REAUTH_REQUIRED':
            raise ProblemError(403, 'PERMISSION_DENIED', 'confirm your password first')
        case 'COOLING_OFF':
            raise ProblemError(409, 'COOLING_OFF', 'authenticator removal has a mandatory 24-hour waiting period')
        case 'WRONG_STATE':
            raise ProblemError(409, 'CONFLICT', 'authenticator state changed; reload and try again')
    if not accepted:
        raise ProblemError(401, 'UNAUTHENTICATED', 'the authenticator proof was not accepted')
    return receipt
