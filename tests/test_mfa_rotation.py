"""Rotation preserves counters and replay history and is all-or-nothing."""
import secrets

import pytest
from cryptography.exceptions import InvalidTag

from tools.dev_db import connection
from tools.mfa_rotate import rotate
from test_mfa_lifecycle import api, database, enrolled, seed, world, key_file  # noqa: F401


def test_rotation_is_replay_safe_and_idempotent(database, enrolled, key_file):
    new_key = secrets.token_bytes(32)
    with connection(database=database) as c:
        before = c.execute('SELECT user_id,enrollment,last_step,recovery,reset_after FROM lv_identity.mfa_factor').fetchall()
        assert rotate(c, key_file, new_key) == {'changed': 1, 'already_current': 0}
        assert c.execute('SELECT user_id,enrollment,last_step,recovery,reset_after FROM lv_identity.mfa_factor').fetchall() == before
    with connection(database=database) as c:
        assert rotate(c, key_file, new_key) == {'changed': 0, 'already_current': 1}


def test_wrong_rotation_key_rolls_back(database, enrolled):
    with connection(database=database) as c:
        before = c.execute('SELECT envelope FROM lv_identity.mfa_factor').fetchall()
    with pytest.raises(InvalidTag):
        with connection(database=database) as c:
            rotate(c, secrets.token_bytes(32), secrets.token_bytes(32))
    with connection(database=database) as c:
        assert c.execute('SELECT envelope FROM lv_identity.mfa_factor').fetchall() == before


def test_rotation_refuses_busy_audit_before_touching_factors(database, enrolled, key_file):
    from psycopg.errors import LockNotAvailable
    with connection(database=database) as busy:
        busy.execute("SELECT lv_identity.audit_append('MFA_TEST','ALLOWED',NULL,NULL,NULL,'test','test','{}'::jsonb)")
        with pytest.raises(LockNotAvailable):
            with connection(database=database) as rotator:
                rotate(rotator, key_file, secrets.token_bytes(32))


def test_keyring_reads_old_and_new_envelopes_without_fallback_keys_in_api(tmp_path, monkeypatch):
    from uuid import uuid4
    from ledgervigil.auth.mfa import open_enrollment
    from ledgervigil.auth.totp import new_secret, seal_secret
    user, enrollment = uuid4(), uuid4()
    old, new = secrets.token_bytes(32), secrets.token_bytes(32)
    key_path = tmp_path / 'synthetic-previous-key'
    key_path.write_text(old.hex())
    key_path.chmod(0o600)
    secret = new_secret()
    envelope = seal_secret(old, user, enrollment, secret)
    monkeypatch.setenv('LV_MFA_PREVIOUS_KEY_FILES', str(key_path))
    assert open_enrollment(new, user, enrollment, envelope) == secret
    assert open_enrollment(new, user, enrollment, seal_secret(new, user, enrollment, secret)) == secret
    monkeypatch.delenv('LV_MFA_PREVIOUS_KEY_FILES')
    with pytest.raises(InvalidTag):
        open_enrollment(new, user, enrollment, envelope)
