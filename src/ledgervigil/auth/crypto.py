"""Password hashing (Argon2id via argon2-cffi), session tokens and session-bound CSRF tokens."""
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2-cffi defaults follow RFC 9106's second recommended profile (argon2id, t=3, m=64 MiB, p=4).
_HASHER = PasswordHasher()
# Verified against when the account is unknown, so an unknown email costs the same Argon2 work as a wrong password.
_DUMMY_HASH = _HASHER.hash(secrets.token_urlsafe(32))
_CSRF_LABEL = b'ledgervigil-csrf-v1'
MAX_PASSWORD_LENGTH = 1024


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError('password must be 12 to 1024 characters')
    return _HASHER.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-shape check: always runs one Argon2id verification; False for unknown accounts or bad input."""
    try:
        ok = _HASHER.verify(stored_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return bool(ok) and stored_hash is not None


def new_session_token() -> str:
    return secrets.token_urlsafe(32)          # 256 bits from the OS CSPRNG


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode('ascii')).digest()


def csrf_token_for(session_token: str) -> str:
    """CSRF token derived from the session token (HMAC). Nothing extra is stored, it changes with every session rotation,
    and a token from one session never validates for another."""
    return hmac.new(session_token.encode('ascii'), _CSRF_LABEL, hashlib.sha256).hexdigest()


def csrf_matches(session_token: str, presented: str | None) -> bool:
    return isinstance(presented, str) and hmac.compare_digest(csrf_token_for(session_token), presented)
