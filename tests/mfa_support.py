"""Real MFA setup for non-MFA workflow tests containing synthetic BOOKS fixtures.
This module is test-only: no application bypass, flags, or trusted session stamping.
"""
import base64
import os
import secrets
import tempfile
import time
from pathlib import Path

from ledgervigil.auth.totp import at_step

_private = tempfile.TemporaryDirectory(prefix='lv-synthetic-test-mfa-')
_key = Path(_private.name) / 'key'
_key.write_text(secrets.token_hex(32))
_key.chmod(0o600)


def establish_mfa(application, client, password, session):
    if not session.get('mfa_required') or not set(session.get('permissions', [])) & {
            'review', 'execute', 'administer', 'ledger.bootstrap'}:
        return
    if not os.environ.get('LV_MFA_KEY_FILE'):
        os.environ['LV_MFA_KEY_FILE'] = str(_key)
    cache = getattr(application.state, 'synthetic_mfa', None)
    if cache is None:
        cache = application.state.synthetic_mfa = {}
    user = session['user']['id']
    assert client.post('/api/v1/auth/reauthenticate', json={'password': password}).status_code == 200
    if user not in cache:
        setup = client.post('/api/v1/auth/mfa', json={'action': 'START'})
        assert setup.status_code == 200
        secret = base64.b32decode(setup.json()['setup_key'])
        result = client.post('/api/v1/auth/mfa', json={'action': 'CONFIRM',
                            'code': at_step(secret, int(time.time()) // 30)})
        assert result.status_code == 200
        cache[user] = result.json()['recovery_codes']
    else:
        assert cache[user], 'test consumed its allocated recovery proofs'
        code = cache[user].pop()
        assert client.post('/api/v1/auth/mfa', json={'action': 'RECOVERY', 'code': code}).status_code == 200
