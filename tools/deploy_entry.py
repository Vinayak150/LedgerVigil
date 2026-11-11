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
ROLES = ('lv_owner', 'lv_migrator', 'lv_app', 'lv_seed', 'lv_ingest', 'lv_worker', 'lv_authn')
EXPECTED_HEAD = '0040_mfa_reset'


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
        admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO lv_migrator,lv_app,lv_seed,lv_ingest,lv_worker,lv_authn').format(sql.Identifier(database)))
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
    from ledgervigil.auth.pool import verify_runtime_role
    checks = {'restore_interlock': 'NOT_CHECKED'}
    required = (('LV_DATABASE_URL', 'lv_app'), ('LV_WORKER_DATABASE_URL', 'lv_worker'),
                ('LV_AUTHN_DATABASE_URL', 'lv_authn'))
    for var, role in required:
        try:
            _from_file(var, var + '_FILE')
            if not os.environ.get(var):
                checks[f'{role}_login'] = 'MISSING'
                continue
            with psycopg.connect(os.environ[var], connect_timeout=3) as c:
                if role != 'lv_worker':
                    verify_runtime_role(c, role)
                    checks[f'{role}_login'] = True
                else:
                    checks[f'{role}_login'] = c.execute(
                        """SELECT current_user,session_user,rolsuper,rolbypassrls
                           FROM pg_roles WHERE rolname=current_user"""
                    ).fetchone() == (role, role, False, False)
                if role == 'lv_app':
                    checks['restore_interlock'] = c.execute('SELECT lv.recovery_state()').fetchone()[0]
        except Exception as exc:
            checks[f'{role}_login'] = 'FAILED:' + type(exc).__name__
    checks['migration_head'] = 'MISSING'
    if os.environ.get('LV_MIGRATOR_DATABASE_URL_FILE'):
        try:
            url = _secret('LV_MIGRATOR_DATABASE_URL_FILE').replace('postgresql+psycopg://', 'postgresql://')
            with psycopg.connect(url, connect_timeout=3) as c:
                if c.execute('SELECT current_user,session_user').fetchone() != ('lv_migrator','lv_migrator'):
                    raise RuntimeError('wrong migration role')
                c.execute('SET LOCAL ROLE lv_owner')
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
        and checks['restore_interlock'] == 'BOUND'
    return {'status': 'READY' if ok else 'NOT_READY', 'checks': checks}


def bind_restored(report_path):
    """Recheck the report against the stopped, restored target before recording a binding."""
    import hashlib
    import psycopg
    from psycopg import sql
    from tools.verify_recovery import assert_match, fingerprints, replay, schema_digest

    data = Path(report_path).read_bytes()
    report = json.loads(data)
    if (not isinstance(report, dict) or report.get('status') != 'PASS'
            or report.get('snapshot_consistent') is not True
            or report.get('restore_interlock') != 'RESTORED_UNVERIFIED'
            or report.get('schema_owners_grants_policies_constraints_and_functions_match') is not True
            or not isinstance(report.get('table_fingerprints'), dict)
            or not report['table_fingerprints']
            or not re.fullmatch(r'[0-9a-f]{64}', str(report.get('schema_sha256', '')))
            or not isinstance(report.get('ledger_replay'), dict)
            or report['ledger_replay'].get('sequence_heads_and_balances_match') is not True
            or not isinstance(report.get('external_anchor'), dict)):
        raise RuntimeError('a complete logical restore verification report is required')
    # A dedicated operator connection reads across FORCE RLS for comparison. Never mount
    # this credential in an API, worker or issuer. Runtime connections must be stopped.
    url = _secret('LV_SUPERUSER_DATABASE_URL_FILE').replace('postgresql+psycopg://', 'postgresql://')
    with psycopg.connect(url) as c:
        c.execute("SET LOCAL lock_timeout = '3s'")
        if c.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone() != (True,):
            raise RuntimeError('restore verification requires the operator connection')
        if c.execute('SELECT lv.recovery_state()').fetchone() != ('RESTORED_UNVERIFIED',):
            raise RuntimeError('target is not an unverified logical restore')
        tables = c.execute("""SELECT schemaname,tablename FROM pg_tables
            WHERE schemaname IN ('lv','lv_identity','lv_meta') ORDER BY 1,2""").fetchall()
        c.execute(sql.SQL('LOCK TABLE {} IN SHARE MODE NOWAIT').format(
            sql.SQL(',').join(sql.Identifier(schema, table) for schema, table in tables)))
        assert_match(report['table_fingerprints'], fingerprints(c))
        if schema_digest(c) != report['schema_sha256'] or replay(c) != report['ledger_replay']:
            raise RuntimeError('restore report does not match target schema and replay')
        has_packs = c.execute("""SELECT EXISTS(SELECT 1 FROM lv.proof_pack
            WHERE state IN ('SEALED','ANCHORED','ANCHOR_FAILED'))""").fetchone()[0]
        anchor_path = os.environ.get('LV_ANCHOR_REPOSITORY')
        if not anchor_path and (has_packs or report['external_anchor'].get('status') == 'ANCHORED_PREFIX_VERIFIED'):
            raise RuntimeError('the independent anchor repository is required for this restore')
        # A backup can predate the first proof pack. Its own lack of evidence cannot
        # decide whether a configured independent repository should be checked.
        if anchor_path:
            from ledgervigil.proofpacks.anchors import GitAnchors
            from ledgervigil.proofpacks.recovery import check_database
            check_database(c, GitAnchors(anchor_path))
        c.execute('SET LOCAL ROLE lv_owner')
        digest = hashlib.sha256(data).hexdigest()
        state = c.execute('SELECT lv.recovery_bind(%s)', (digest,)).fetchone()[0]
    return {'status': state, 'report_sha256': digest}


def api(host, port, *, issuer=False):
    os.environ['LV_SERVICE_MODE'] = 'issuer' if issuer else 'api'
    if issuer:
        _from_file('LV_AUTHN_DATABASE_URL', 'LV_AUTHN_DATABASE_URL_FILE')
        if not os.environ.get('LV_AUTHN_DATABASE_URL'):
            raise RuntimeError('the issuer database credential is required')
    else:
        _from_file('LV_DATABASE_URL', 'LV_DATABASE_URL_FILE')
        _from_file('LV_CURSOR_KEY', 'LV_CURSOR_KEY_FILE')
    if not issuer and os.environ.get('LV_ENVIRONMENT', 'production') != 'development' and not os.environ.get('LV_CURSOR_KEY'):
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
    p.add_argument('action', choices=['prepare', 'migrate', 'readiness', 'bind-restored', 'api', 'issuer', 'worker', 'scheduler'])
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
    elif a.action in {'api','issuer'}:
        api(a.host, a.port, issuer=a.action=='issuer')
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
