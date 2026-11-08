"""RFC 6238 primitives for the proposed issuer MFA design.

This module is not an authorization gate. A caller must consume the matched
step atomically against the account's durable replay counter.
"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PERIOD_SECONDS = 30
TOTP_DIGITS = 6
_RECOVERY_FORMAT = re.compile(r'[A-Z2-7]{26}')


def new_secret() -> bytes:
    return secrets.token_bytes(20)


def setup_key(secret: bytes) -> str:
    return base64.b32encode(secret).decode('ascii').rstrip('=')


def at_step(secret: bytes, step: int, *, digits: int = TOTP_DIGITS) -> str:
    if not 0 <= step < 2**64 or digits not in (6, 8):
        raise ValueError('invalid TOTP parameters')
    mac = hmac.digest(secret, struct.pack('>Q', step), 'sha1')
    offset = mac[-1] & 15
    truncated = int.from_bytes(mac[offset:offset+4], 'big') & 0x7fff_ffff
    return str(truncated % 10**digits).zfill(digits)


def match_step(secret: bytes, code: str, epoch_seconds: int) -> int | None:
    if re.fullmatch(r'[0-9]{6}', code) is None or epoch_seconds < 0:
        return None
    current = epoch_seconds // PERIOD_SECONDS
    matches = [
        step for step in range(max(0, current-1), current+2)
        if hmac.compare_digest(at_step(secret, step), code)
    ]
    # Choosing the latest match also consumes a rare repeated code in this window.
    return max(matches, default=None)


def _aad(user: UUID, enrollment: UUID) -> bytes:
    return b'ledgervigil-totp:v1:' + user.bytes + enrollment.bytes


def seal_secret(key: bytes, user: UUID, enrollment: UUID, secret: bytes) -> bytes:
    if len(key) != 32 or len(secret) != 20:
        raise ValueError('invalid MFA material length')
    nonce = secrets.token_bytes(12)
    return b'\x01' + nonce + AESGCM(key).encrypt(nonce, secret, _aad(user, enrollment))


def open_secret(key: bytes, user: UUID, enrollment: UUID, envelope: bytes) -> bytes:
    if len(key) != 32 or len(envelope) != 49 or envelope[0] != 1:
        raise ValueError('invalid MFA envelope')
    return AESGCM(key).decrypt(envelope[1:13], envelope[13:], _aad(user, enrollment))


def new_recovery_code() -> str:
    return setup_key(secrets.token_bytes(16))


def recovery_digest(user: UUID, code: str) -> bytes | None:
    if _RECOVERY_FORMAT.fullmatch(code) is None:
        return None
    return hashlib.sha256(b'ledgervigil-recovery:v1:' + user.bytes + code.encode('ascii')).digest()
