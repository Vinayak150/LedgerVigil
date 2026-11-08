"""Test support: obtain a REAL authenticated administrator session and close periods through lv.close_period.

Used by earlier test files whose closed-period scenarios previously relied on the unaudited `UPDATE lv.fiscal_period`
(R-02). The session is created through the same runtime entry points as POST /auth/login (login_prepare, Argon2id
verification in Python, login_finish); no session row is fabricated and no privileged shortcut is used.
"""
import secrets
from contextlib import contextmanager
from uuid import uuid4

from tools.dev_db import connection
from tools.identity_admin import create_user, grant, owner_transaction
from ledgervigil.auth.crypto import new_session_token, token_hash, verify_password


def admin_token(database, tenant, roles=('ADMINISTRATOR',)):
    """Provision a synthetic user with `roles` in `tenant` and sign it in; returns the session token."""
    email = f'period-admin-{uuid4().hex[:10]}@test.ledgervigil.invalid'
    password = secrets.token_urlsafe(18)
    with owner_transaction(database) as owner:
        user = create_user(owner, email, 'Synthetic period administrator', password)
        grant(owner, tenant, user, list(roles))
    token = new_session_token()
    with connection('lv_authn', database) as c:
        prepared = c.execute("SELECT lv_identity.login_prepare(%s, 'test-support', 'test-login')", (email,)).fetchone()[0]
        c.commit()
        ok = verify_password(prepared['password_hash'], password)
        result = c.execute("SELECT lv_identity.login_finish(%s, %s, %s, NULL, 'test-support', 'test-login')",
                           (prepared['attempt_id'], ok, token_hash(token))).fetchone()[0]
        c.commit()
    assert result['authenticated'] and result['session']['tenant_id'] == str(tenant), result
    return token


def close_call(conn, token, period_id, if_match='"v1"', key=None, request_id='test-close'):
    """Bind the verified session and call lv.close_period in the CURRENT transaction (caller controls commit)."""
    bound = conn.execute('SELECT lv_identity.session_context(%s, false)', (token_hash(token),)).fetchone()[0]
    assert bound['valid'], bound
    return conn.execute("SELECT lv.close_period(%s, %s, %s, %s, 'test-support', %s)",
                        (token_hash(token), period_id, if_match, key or uuid4().hex, request_id)).fetchone()[0]


def close_period(database, token, period_id, if_match='"v1"', key=None):
    """Close in its own committed transaction as the runtime login; returns the function's result."""
    with connection('lv_app', database) as c:
        result = close_call(c, token, period_id, if_match, key)
    return result


@contextmanager
def held_close(database, token, period_id, if_match='"v1"', key=None):
    """Run the close but keep its transaction open (locks held) until the block exits; commits on normal exit."""
    c = connection('lv_app', database)
    try:
        result = close_call(c, token, period_id, if_match, key)
        yield c, result
        c.commit()
    finally:
        c.close()
