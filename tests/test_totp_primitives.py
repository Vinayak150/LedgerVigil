"""Published RFC vectors and tamper checks for the proposed issuer primitives."""
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag

from ledgervigil.auth import totp


@pytest.mark.parametrize('epoch,expected', [
    (59, '94287082'), (1111111109, '07081804'), (1111111111, '14050471'),
    (1234567890, '89005924'), (2000000000, '69279037'), (20000000000, '65353130'),
])
def test_sha1_rfc6238_appendix_b(epoch, expected):
    assert totp.at_step(b'12345678901234567890', epoch // 30, digits=8) == expected


def test_accepts_only_adjacent_time_steps_and_ascii_codes():
    secret = b'12345678901234567890'
    now = 30000
    assert totp.match_step(secret, totp.at_step(secret, 999), now) == 999
    assert totp.match_step(secret, totp.at_step(secret, 1001), now) == 1001
    # Compare against every code in the window so this test stays deterministic
    # even if independent random secrets happen to produce a collision.
    allowed = {totp.at_step(secret, i) for i in range(999, 1002)}
    unmatched = next(f'{i:06d}' for i in range(1000000) if f'{i:06d}' not in allowed)
    assert totp.match_step(secret, unmatched, now) is None
    assert totp.match_step(secret, '０１２３４５', now) is None


def test_envelope_rejects_a_different_account_enrollment_or_ciphertext():
    key, user, enrollment, secret = bytes(range(32)), uuid4(), uuid4(), totp.new_secret()
    first = totp.seal_secret(key, user, enrollment, secret)
    assert totp.open_secret(key, user, enrollment, first) == secret
    assert first != totp.seal_secret(key, user, enrollment, secret)
    for u, e, ciphertext in ((uuid4(), enrollment, first), (user, uuid4(), first),
                             (user, enrollment, first[:-1] + bytes([first[-1] ^ 1]))):
        with pytest.raises(InvalidTag):
            totp.open_secret(key, u, e, ciphertext)


def test_recovery_digest_is_bound_to_account_and_does_not_store_code():
    code = totp.new_recovery_code()
    user = uuid4()
    assert len(code) == 26
    digest = totp.recovery_digest(user, code)
    assert len(digest) == 32
    assert digest != totp.recovery_digest(uuid4(), code)
    assert code.encode() not in digest
    assert totp.recovery_digest(user, code+'\n') is None
