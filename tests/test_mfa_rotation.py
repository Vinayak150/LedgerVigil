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
