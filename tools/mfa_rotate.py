"""Operator-only transactional MFA envelope rotation. Never logs keys or factor contents."""
import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from ledgervigil.auth.totp import open_secret, seal_secret
from ledgervigil.provider.settings import _read_private


def rotate(conn, old_key, new_key):
    if len(old_key) != 32 or len(new_key) != 32 or old_key == new_key:
        raise ValueError('distinct 32-byte keys are required')
    conn.execute("SET LOCAL lock_timeout='3s'")
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('mfa-envelope-rotation',0))")
    conn.execute('LOCK TABLE lv_identity.audit_chain_head IN SHARE ROW EXCLUSIVE MODE NOWAIT')
    conn.execute('LOCK TABLE lv_identity.mfa_factor IN ACCESS EXCLUSIVE MODE NOWAIT')
    changed = already = 0
    for user, enrollment, envelope in conn.execute(
            'SELECT user_id,enrollment,envelope FROM lv_identity.mfa_factor ORDER BY user_id').fetchall():
        try:
            secret = open_secret(new_key, UUID(str(user)), UUID(str(enrollment)), bytes(envelope))
            already += 1
            continue
        except InvalidTag:
            secret = open_secret(old_key, UUID(str(user)), UUID(str(enrollment)), bytes(envelope))
        replacement = seal_secret(new_key, UUID(str(user)), UUID(str(enrollment)), secret)
        conn.execute('UPDATE lv_identity.mfa_factor SET envelope=%s WHERE user_id=%s',
                     (replacement, user))
        changed += 1
    conn.execute("SELECT lv_identity.audit_append('MFA_KEY_ROTATED','ALLOWED',NULL,NULL,NULL,%s,%s,%s::jsonb)",
                 ('mfa-key-rotation', uuid4().hex, json.dumps({'changed': str(changed), 'already_current': str(already)})))
    return {'changed': changed, 'already_current': already}


def main():
    import psycopg
    from tools.deploy_entry import _secret

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-key-file', type=Path, required=True)
    parser.add_argument('--new-key-file', type=Path, required=True)
    args = parser.parse_args()
    # An application/operator process consumes the files; values never enter chat or logs.
    old_key = bytes.fromhex(_read_private(args.old_key_file).decode('ascii').strip())
    new_key = bytes.fromhex(_read_private(args.new_key_file).decode('ascii').strip())
    url = _secret('LV_MIGRATOR_DATABASE_URL_FILE').replace('postgresql+psycopg://', 'postgresql://')
    with psycopg.connect(url) as conn:
        if conn.execute('SELECT session_user').fetchone() != ('lv_migrator',):
            raise RuntimeError('the restricted migration operator login is required')
        conn.execute('SET LOCAL ROLE lv_owner')
        result = rotate(conn, old_key, new_key)
    print(json.dumps({'status': 'COMMITTED', **result}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not print command arguments, DSNs, paths, secret buffers or database errors.
        raise SystemExit('MFA key rotation failed: ' + type(exc).__name__) from None
