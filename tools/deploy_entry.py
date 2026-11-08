"""Portable process entry point for container or VM deployments (no Mac paths, no .env files, no Docker socket).

    python -m tools.deploy_entry prepare     create/verify roles and database prerequisites (superuser, once)
    python -m tools.deploy_entry migrate     upgrade to head as restricted lv_owner (never downgrades)
    python -m tools.deploy_entry readiness   preflight: role, migration head, restore interlock, gates, anchors; JSON, no secrets
    python -m tools.deploy_entry bind-restored --report REPORT.json   after a PASSED recovery verification of THIS database,
                                             record it so financial writes may resume (migrator login; restore interlock)
    python -m tools.deploy_entry api         uvicorn API as lv_app
    python -m tools.deploy_entry worker -Q maintenance,native-exports      Celery worker as lv_worker
    python -m tools.deploy_entry scheduler   maintenance/export/pack/provider relay (single owner, advisory lock)

Secrets are read from files named by *_FILE variables (container secret mounts) and kept in-process:
    LV_DATABASE_URL_FILE (api), LV_WORKER_DATABASE_URL_FILE (worker, scheduler), LV_MIGRATOR_DATABASE_URL_FILE (migrate),
    LV_SUPERUSER_DATABASE_URL_FILE and LV_ROLE_PASSWORD_DIR (prepare: one file per role, named after the role).
Nothing here prints a DSN or password. Every failure prints only the exception class.
"""
import argparse
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ('lv_owner', 'lv_migrator', 'lv_app', 'lv_seed', 'lv_ingest', 'lv_worker')
EXPECTED_HEAD = '0037_restore_interlock'


def _secret(var):
    path = os.environ.get(var)
    if not path:
        raise RuntimeError(f'{var} is required')
    value = Path(path).read_text().strip()
    if not value:
        raise RuntimeError(f'{var} names an empty file')
    return value


def _from_file(target, source):
    if not os.environ.get(target) and os.environ.get(source):
        os.environ[target] = _secret(source)


def prepare():
    import psycopg
    from psycopg import sql
    admin_dsn = _secret('LV_SUPERUSER_DATABASE_URL_FILE')
    pwdir = Path(os.environ.get('LV_ROLE_PASSWORD_DIR', ''))
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        database = admin.execute('SELECT current_database()').fetchone()[0]
        existing = {r[0] for r in admin.execute('SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)', (list(ROLES),))}
        for role in ROLES:
            if role in existing:
                continue
            if role == 'lv_owner':
                login = sql.SQL('NOLOGIN')
            else:
                password = (pwdir / role).read_text().strip()
                if len(password) < 32:
                    raise RuntimeError('role passwords must be at least 32 characters')
                login = sql.SQL('LOGIN PASSWORD {}').format(sql.Literal(password))
            admin.execute(sql.SQL('CREATE ROLE {} {} NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role), login))
        for role in ROLES:
            actual = admin.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin,rolinherit FROM pg_roles WHERE rolname=%s', (role,)).fetchone()
            if actual != (False, False, False, False, False, role != 'lv_owner', False):
                raise RuntimeError('unexpected privileges on an application role; refusing to continue')
        members = admin.execute('SELECT p.rolname,c.rolname FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member '
                                'WHERE c.rolname = ANY(%s)', (list(ROLES),)).fetchall()
        if any(pair != ('lv_owner', 'lv_migrator') for pair in members):
            raise RuntimeError('unexpected role memberships; refusing to continue')
        admin.execute('GRANT lv_owner TO lv_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE')
        admin.execute('CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public')
        admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
        admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO lv_migrator,lv_app,lv_seed,lv_ingest,lv_worker').format(sql.Identifier(database)))
        admin.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO lv_owner').format(sql.Identifier(database)))
        admin.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        admin.execute('CREATE SCHEMA IF NOT EXISTS lv_meta AUTHORIZATION lv_owner')
        admin.execute('REVOKE ALL ON SCHEMA lv_meta FROM PUBLIC')
    return {'status': 'PREPARED', 'roles': list(ROLES)}


def migrate():
    from alembic import command
    from alembic.config import Config
    url = _secret('LV_MIGRATOR_DATABASE_URL_FILE')
    if not url.startswith('postgresql+psycopg://'):
        url = 'postgresql+psycopg://' + url.split('://', 1)[1] if '://' in url else url
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.attributes['connection_url'] = url
    command.upgrade(cfg, 'head')
    return {'status': 'MIGRATED', 'target': 'head'}


def readiness():
    """Preflight without secrets: the runtime logins are the restricted roles, the schema is at the expected head, gates are
    as configured, and the anchor directory is present. Exit status 0 only when every check passes."""
    import psycopg
    checks = {}
    for var, role in (('LV_DATABASE_URL', 'lv_app'), ('LV_WORKER_DATABASE_URL', 'lv_worker')):
        _from_file(var, var + '_FILE')
        if not os.environ.get(var):
            continue
        try:
            with psycopg.connect(os.environ[var], connect_timeout=3) as c:
                who = c.execute('SELECT current_user, session_user').fetchone()
                checks[f'{role}_login'] = who == (role, role)
                if role == 'lv_app':
                    checks['restore_interlock'] = c.execute('SELECT lv.recovery_state()').fetchone()[0]
        except Exception as exc:
            checks[f'{role}_login'] = 'FAILED:' + type(exc).__name__
    if os.environ.get('LV_MIGRATOR_DATABASE_URL_FILE'):
        try:
            url = _secret('LV_MIGRATOR_DATABASE_URL_FILE').replace('postgresql+psycopg://', 'postgresql://')
            with psycopg.connect(url, connect_timeout=3) as c:
                c.execute('SET ROLE lv_owner')
                head = c.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()
                checks['migration_head'] = (head[0] if head else None) == EXPECTED_HEAD
        except Exception as exc:
            checks['migration_head'] = 'FAILED:' + type(exc).__name__
    anchors = os.environ.get('LV_ANCHOR_REPOSITORY')
    checks['anchor_repository_present'] = bool(anchors) and Path(anchors).is_dir()
    checks['detection_gate'] = 'ON' if os.environ.get('LV_DETECTION_ENABLED') == '1' else 'OFF'
    checks['provider_sandbox_gate'] = 'ON' if os.environ.get('LV_PROVIDER_SANDBOX_ENABLED') == '1' else 'OFF'
    checks['environment'] = os.environ.get('LV_ENVIRONMENT', 'production')
    ok = all(v is True for k, v in checks.items() if k.endswith('_login') or k in ('anchor_repository_present', 'migration_head')) \
        and checks.get('restore_interlock', 'BOUND') == 'BOUND'
    return {'status': 'READY' if ok else 'NOT_READY', 'checks': checks}


def bind_restored(report_path):
    """Record that THIS database passed recovery verification. Refuses unless the report says PASS; stores only its SHA-256."""
    import hashlib
    import psycopg
    data = Path(report_path).read_bytes()
    if json.loads(data).get('status') != 'PASS':
        raise RuntimeError('the verification report does not say PASS')
    url = _secret('LV_MIGRATOR_DATABASE_URL_FILE').replace('postgresql+psycopg://', 'postgresql://')
    with psycopg.connect(url) as c:
        c.execute('SET ROLE lv_owner')
        state = c.execute('SELECT lv.recovery_bind(%s)', (hashlib.sha256(data).hexdigest(),)).fetchone()[0]
    return {'status': state, 'report_sha256': hashlib.sha256(data).hexdigest()}


def api(host, port):
    _from_file('LV_DATABASE_URL', 'LV_DATABASE_URL_FILE')
    _from_file('LV_CURSOR_KEY', 'LV_CURSOR_KEY_FILE')
    if os.environ.get('LV_ENVIRONMENT', 'production') != 'development' and not os.environ.get('LV_CURSOR_KEY'):
        raise RuntimeError('LV_CURSOR_KEY(_FILE) is required outside development so list cursors survive restarts')
    from ledgervigil import logredact
    logredact.install()
    import uvicorn
    forwarded = os.environ.get('LV_FORWARDED_ALLOW_IPS', '127.0.0.1')
    uvicorn.run('ledgervigil.app:app', host=host, port=port, access_log=True, proxy_headers=True, forwarded_allow_ips=forwarded,
                timeout_graceful_shutdown=20, limit_concurrency=int(os.environ.get('LV_API_MAX_CONCURRENCY', '64')))


def worker(queues):
    _from_file('LV_WORKER_DATABASE_URL', 'LV_WORKER_DATABASE_URL_FILE')
    if not re.fullmatch(r'[a-z-]+(,[a-z-]+)*', queues):
        raise RuntimeError('invalid queue list')
    from ledgervigil.maintenance.celery_app import application
    application.worker_main(['worker', '--pool=prefork', '--concurrency=' + os.environ.get('LV_WORKER_CONCURRENCY', '2'), '--queues=' + queues,
                             '--loglevel=WARNING', '--without-gossip', '--without-mingle'])


def scheduler():
    _from_file('LV_WORKER_DATABASE_URL', 'LV_WORKER_DATABASE_URL_FILE')
    from ledgervigil.maintenance.celery_app import publish
    from ledgervigil.maintenance.runtime import connect, scheduler as run
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if not run(connect, publish, stop):
        raise SystemExit('another scheduler already owns the lock')


def main(argv=None):
    from ledgervigil import logredact
    logredact.install()   # every process started here redacts secrets at the log record (scheduler and workers included)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('action', choices=['prepare', 'migrate', 'readiness', 'bind-restored', 'api', 'worker', 'scheduler'])
    p.add_argument('--report')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8000)
    p.add_argument('-Q', '--queues', default='maintenance,native-exports')
    a = p.parse_args(argv)
    if a.action == 'prepare':
        print(json.dumps(prepare()))
    elif a.action == 'migrate':
        print(json.dumps(migrate()))
    elif a.action == 'bind-restored':
        if not a.report:
            raise RuntimeError('--report is required')
        print(json.dumps(bind_restored(a.report)))
    elif a.action == 'readiness':
        result = readiness()
        print(json.dumps(result))
        return 0 if result['status'] == 'READY' else 1
    elif a.action == 'api':
        api(a.host, a.port)
    elif a.action == 'worker':
        worker(a.queues)
    else:
        scheduler()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print('LedgerVigil process failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
