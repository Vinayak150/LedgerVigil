"""LOCAL identity administration for SYNTHETIC demonstration users (development database only).

This is an operator command, not an HTTP route and not available to the runtime service: it connects through the
migration login as the schema owner (as the other local tools do) and calls owner-only lv_identity.admin_* functions,
each of which writes a security audit event.

  provision-demo --credentials-file PATH   create two synthetic tenants and five synthetic users with RANDOM passwords,
                 [--tag TAG]               written only to PATH (created with mode 0600; refuses to overwrite). Emails are
                                            <role>.<TAG>@identity-demo.ledgervigil.invalid; TAG defaults to random hex so
                                            the command can be repeated on one database
  disable-user --email E                   disable a user and end all of their sessions
  revoke-membership --email E --tenant T   revoke one membership and end its sessions
  revoke-sessions --email E                end all of a user's sessions

There is no default password, no development bypass and no automatic administrator login. Synthetic users are marked
email-verified by this command (labelled MARKED_BY_LOCAL_PROVISIONING in the audit chain) because no email delivery exists.
"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID

from tools.dev_db import DEV_DB, ROOT, connection
from ledgervigil.auth.crypto import hash_password

DOMAIN = 'identity-demo.ledgervigil.invalid'      # RFC 6761 reserved TLD: cannot reach a real mailbox
DEMO_USERS = (('analyst', 'Synthetic analyst', ('ANALYST',), 'A'),
              ('controller', 'Synthetic controller', ('CONTROLLER',), 'A'),
              ('automation', 'Synthetic automation developer', ('AUTOMATION_DEVELOPER',), 'A'),
              ('administrator', 'Synthetic administrator', ('ADMINISTRATOR',), 'A'),
              ('multi', 'Synthetic analyst in two tenants', ('ANALYST',), 'AB'))


def owner_transaction(database=DEV_DB):
    conn = connection('lv_migrator', database)
    conn.execute('SET LOCAL ROLE lv_owner')
    return conn


def create_tenant(conn, name) -> UUID:
    tenant = conn.execute('SELECT uuidv7()').fetchone()[0]
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
    conn.execute('INSERT INTO lv.tenant(tenant_id, name) VALUES (%s, %s)', (tenant, name))
    conn.execute("SELECT set_config('app.tenant_id', '', true)")
    return tenant


def create_user(conn, email, display_name, password, verified=True) -> UUID:
    return conn.execute('SELECT lv_identity.admin_create_user(%s, %s, %s, %s)', (email, display_name, hash_password(password), verified)).fetchone()[0]


def grant(conn, tenant, user, roles):
    conn.execute('SELECT lv_identity.admin_grant_membership(%s, %s, %s::lv_identity.member_role[])', (tenant, user, list(roles)))


def user_id(conn, email) -> UUID:
    row = conn.execute('SELECT id FROM lv_identity.app_user WHERE email = lower(btrim(%s))', (email,)).fetchone()
    if row is None:
        raise SystemExit('no such user')
    return row[0]


def _safe_credentials_path(path: Path) -> Path:
    path = path.resolve()
    for forbidden in (ROOT / 'docs', ROOT / 'evidence', ROOT.parent / 'docs'):
        if forbidden.resolve() in path.parents:
            raise SystemExit('refusing to write credentials inside documentation or evidence folders')
    if path.exists():
        raise SystemExit('credentials file already exists; refusing to overwrite')
    return path


def provision_demo(credentials_file: Path, database=DEV_DB, tag=None) -> dict:
    target = _safe_credentials_path(credentials_file)
    tag = tag or secrets.token_hex(3)
    if not tag.isalnum() or len(tag) > 16:
        raise SystemExit('tag must be 1-16 letters or digits')
    passwords = {name: secrets.token_urlsafe(18) for name, *_ in DEMO_USERS}
    with owner_transaction(database) as conn:
        tenants = {'A': create_tenant(conn, 'SYNTHETIC identity demonstration tenant A'),
                   'B': create_tenant(conn, 'SYNTHETIC identity demonstration tenant B')}
        users = {}
        for name, display, roles, where in DEMO_USERS:
            email = f'{name}.{tag}@{DOMAIN}'
            uid = create_user(conn, email, display, passwords[name])
            for t in where:
                grant(conn, tenants[t], uid, roles)
            users[email] = {'user_id': str(uid), 'roles': list(roles), 'tenants': [str(tenants[t]) for t in where]}
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({f'{name}.{tag}@{DOMAIN}': passwords[name] for name, *_ in DEMO_USERS}, f, indent=2)
    return {'tenants': {k: str(v) for k, v in tenants.items()}, 'users': users, 'credentials_file': str(target),
            'label': 'SYNTHETIC demonstration identities; passwords are random and stored only in the credentials file'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('provision-demo'); p.add_argument('--credentials-file', type=Path, required=True); p.add_argument('--tag')
    p = sub.add_parser('disable-user'); p.add_argument('--email', required=True)
    p = sub.add_parser('revoke-membership'); p.add_argument('--email', required=True); p.add_argument('--tenant', type=UUID, required=True)
    p = sub.add_parser('revoke-sessions'); p.add_argument('--email', required=True)
    args = parser.parse_args(argv)
    if args.command == 'provision-demo':
        print(json.dumps(provision_demo(args.credentials_file, tag=args.tag), indent=2))
        return
    with owner_transaction() as conn:
        uid = user_id(conn, args.email)
        if args.command == 'disable-user':
            conn.execute('SELECT lv_identity.admin_disable_user(%s)', (uid,))
        elif args.command == 'revoke-membership':
            conn.execute('SELECT lv_identity.admin_revoke_membership(%s, %s)', (args.tenant, uid))
        else:
            conn.execute('SELECT lv_identity.admin_revoke_sessions(%s)', (uid,))
    print(json.dumps({'command': args.command, 'user_id': str(uid), 'done': True}))


if __name__ == '__main__':
    sys.exit(main())
