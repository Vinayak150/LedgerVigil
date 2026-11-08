"""Authentication-flow demonstration (LOCAL development database, SYNTHETIC identities only).

Provisions a fresh set of synthetic users (random passwords held in a temporary 0600 file that is deleted at the end),
then drives the real FastAPI application in-process over https://testserver with the production (secure) cookie
configuration. Every request/response is recorded with secrets REDACTED: session cookies, CSRF tokens and passwords are
replaced by labels, and the run aborts if any secret value would appear in the written output.

Writes evidence/identity/demo-<UTC timestamp>/flows.json and summary.json. Exit 0 = every expectation held.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid7

from fastapi.testclient import TestClient
from psycopg import sql

from tools.dev_db import DEV_DB, ROOT, connection
from tools.identity_admin import provision_demo
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings

COOKIE = '__Host-lv_session'


def lv_fingerprint(database):
    with connection(database=database) as admin:
        tables = [r[0] for r in admin.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                              "WHERE n.nspname = 'lv' AND c.relkind = 'r' ORDER BY 1").fetchall()]
        return {t: hashlib.sha256('\n'.join(sorted(map(repr, admin.execute(sql.SQL('SELECT * FROM lv.{}').format(sql.Identifier(t))).fetchall())))
                                  .encode()).hexdigest() for t in tables}


class Recorder:
    def __init__(self):
        self.flows, self.secrets, self.labels, self.checks = [], {}, {}, []

    def secret(self, value, label):
        if value and value not in self.secrets:
            self.secrets[value] = label
        return value

    def count(self, prefix):
        return sum(label.startswith(prefix) for label in self.secrets.values()) + 1

    def redact(self, text):
        for value, label in sorted(self.secrets.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(value, f'<{label}>')
        return text

    def call(self, title, client, method, path, expect, **kw):
        request_headers = dict(kw.get('headers', {}))
        if client.cookies.get(COOKIE) or 'cookie' in {k.lower() for k in client.headers}:
            request_headers['Cookie'] = f'{COOKIE}=<current session token>'
        response = client.request(method, path, **kw)
        set_cookie = response.headers.get('set-cookie')
        if set_cookie:
            m = re.match(r'([^=]+)=("?)([^;"]*)', set_cookie)
            if m and m.group(3):
                self.secret(m.group(3), f'session-token-{self.count("session-token")}')
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get('csrf_token'):
            self.secret(body['csrf_token'], f'csrf-token-{self.count("csrf-token")}')
        entry = {'step': len(self.flows) + 1, 'title': title, 'request': {'method': method, 'path': path, 'headers': request_headers,
                                                                          'json': kw.get('json')},
                 'response': {'status': response.status_code, 'set_cookie': set_cookie, 'retry_after': response.headers.get('retry-after'),
                              'content_type': response.headers.get('content-type'), 'body': body},
                 'expected_status': expect, 'held': response.status_code == expect}
        self.flows.append(entry)
        self.checks.append((title, entry['held']))
        return response

    def check(self, title, held):
        self.checks.append((title, bool(held)))
        self.flows.append({'step': len(self.flows) + 1, 'title': title, 'check': True, 'held': bool(held)})


def main(database=DEV_DB):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT / 'evidence' / 'identity' / f'demo-{stamp}'
    rec = Recorder()
    before = lv_fingerprint(database)
    with tempfile.TemporaryDirectory(prefix='lv-demo-credentials-') as tmp:
        cred_path = Path(tmp) / 'credentials.json'
        provisioned = provision_demo(cred_path, database)
        assert oct(os.stat(cred_path).st_mode & 0o777) == '0o600'
        credentials = json.loads(cred_path.read_text())
    for email, password in credentials.items():
        rec.secret(password, 'password:' + email.split('.')[0])
    users = {email.split('.')[0]: email for email in credentials}
    tenants = provisioned['tenants']
    after_provision = lv_fingerprint(database)

    app = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database))

    def client(source):
        return TestClient(app, base_url='https://testserver', raise_server_exceptions=False, client=(source, 40000))

    try:
        a = client('127.0.0.11')
        r = rec.call('Analyst signs in (one membership: tenant selected automatically)', a, 'POST', '/api/v1/auth/login', 200,
                     json={'email': users['analyst'], 'password': credentials[users['analyst']]})
        csrf = r.json()['csrf_token']
        rec.check('Cookie is __Host-, Secure, HttpOnly, SameSite=Lax, Path=/', all(x in r.headers['set-cookie'] for x in
                  (f'{COOKIE}=', 'Secure', 'HttpOnly', 'SameSite=lax', 'Path=/')))
        rec.call('Current session', a, 'GET', '/api/v1/me', 200)
        rec.call('Current tenant (derived from the session, never from the request)', a, 'GET', '/api/v1/tenants/current', 200,
                 headers={'X-Tenant-Id': tenants['B']})
        rec.call('Logout without CSRF token is refused', a, 'POST', '/api/v1/auth/logout', 403)
        old = a.cookies.get(COOKIE)
        rec.call('Logout with the session-bound CSRF token', a, 'POST', '/api/v1/auth/logout', 204, headers={'X-CSRF-Token': csrf})
        replay = client('127.0.0.11'); replay.headers['Cookie'] = f'{COOKIE}={old}'
        rec.call('The logged-out cookie is no longer a session', replay, 'GET', '/api/v1/me', 401)

        w = client('127.0.0.12')
        wrong = rec.call('Wrong password: generic refusal', w, 'POST', '/api/v1/auth/login', 401,
                         json={'email': users['controller'], 'password': 'not-the-right-password'})
        unknown = rec.call('Unknown account: the same generic refusal', w, 'POST', '/api/v1/auth/login', 401,
                           json={'email': f'nobody.{uuid7().hex[:6]}@identity-demo.ledgervigil.invalid', 'password': 'not-the-right-password'})
        strip = lambda b: {k: v for k, v in b.items() if k != 'request_id'}
        rec.check('Wrong-password and unknown-account bodies are identical apart from request_id', strip(wrong.json()) == strip(unknown.json()))

        m = client('127.0.0.13')
        r = rec.call('Two-tenant user signs in: tenant selection required', m, 'POST', '/api/v1/auth/login', 200,
                     json={'email': users['multi'], 'password': credentials[users['multi']]})
        rec.check('No tenant, roles or permissions before selection', r.json()['tenant'] is None and r.json()['permissions'] == [])
        rec.call('Tenant routes refuse until a tenant is selected', m, 'GET', '/api/v1/tenants/current', 403)
        r2 = rec.call('Select tenant B (membership verified, new session issued)', m, 'POST', '/api/v1/auth/switch-tenant', 200,
                      json={'tenant_id': tenants['B']}, headers={'X-CSRF-Token': r.json()['csrf_token']})
        rec.call('Selecting a tenant without membership is 404', m, 'POST', '/api/v1/auth/switch-tenant', 404,
                 json={'tenant_id': str(uuid7())}, headers={'X-CSRF-Token': r2.json()['csrf_token']})
        rec.call('Current tenant is B', m, 'GET', '/api/v1/tenants/current', 200)

        d = client('127.0.0.14')
        r = rec.call('Administrator signs in', d, 'POST', '/api/v1/auth/login', 200,
                     json={'email': users['administrator'], 'password': credentials[users['administrator']]})
        rec.check('Administrator has no propose, review or execute permission', {'propose', 'review', 'execute'}.isdisjoint(r.json()['permissions']))
        rec.call('Permission catalog', d, 'GET', '/api/v1/permissions', 200)
        rec.call('There is no seeding route', d, 'POST', '/api/v1/seed', 404, headers={'X-CSRF-Token': r.json()['csrf_token']})
    finally:
        app.state.pool.close()
    rec.check('No lv (financial) table changed during the HTTP flows', lv_fingerprint(database) == after_provision)
    changed = sorted(t for t in before if before[t] != after_provision[t])
    rec.check('Provisioning changed only lv.tenant (two synthetic tenants)', changed == ['tenant'])

    text = rec.redact(json.dumps({'environment': 'Claude workspace (see package PROVENANCE); NOT a Mac result', 'label': 'SYNTHETIC',
                                  'cookie_mode': 'secure (production default)', 'tenants': tenants, 'users': list(users.values()),
                                  'flows': rec.flows}, indent=2))
    leaked = [label for value, label in rec.secrets.items() if value in text]
    if leaked:
        raise SystemExit(f'refusing to write: unredacted secrets {leaked}')
    out.mkdir(parents=True)
    (out / 'flows.json').write_text(text + '\n')
    summary = {'all_held': all(h for _, h in rec.checks), 'checks': [{'check': t, 'held': h} for t, h in rec.checks],
               'redacted_secret_count': len(rec.secrets), 'output': str(out.relative_to(ROOT))}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0 if summary['all_held'] else 1


if __name__ == '__main__':
    sys.exit(main())
