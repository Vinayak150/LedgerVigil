"""Dedicated localhost development DB setup. No production target support."""
from pathlib import Path
import argparse
import os
import secrets
import sys
import psycopg
from psycopg import sql
from sqlalchemy import URL
from alembic.config import Config
from alembic import command

ROOT = Path(__file__).resolve().parents[1]
DEV_DB = 'ledgervigil_dev'
DEV_PORT = int(os.environ.get('LV_DEV_DB_PORT', '55432'))
ROLE_PASSWORD_KEYS = {'lv_app':'APP_PASSWORD','lv_migrator':'MIGRATOR_PASSWORD','lv_seed':'SEED_PASSWORD','lv_ingest':'INGEST_PASSWORD','lv_worker':'WORKER_PASSWORD','lv_authn':'AUTHN_PASSWORD'}

def env_file(name):
    return dict(line.split('=',1) for line in (ROOT/name).read_text().splitlines() if line and not line.startswith('#'))

def connection(user='postgres', database=DEV_DB, *, autocommit=False):
    require_dev_database(database)
    password = env_file('.env')['POSTGRES_PASSWORD'] if user == 'postgres' else env_file('.env.roles')[ROLE_PASSWORD_KEYS[user]]
    return psycopg.connect(host='127.0.0.1',port=DEV_PORT,dbname=database,user=user,password=password,autocommit=autocommit)

def require_dev_database(name):
    import re
    if name != DEV_DB and not re.fullmatch(r'lv_migration_test_[0-9a-f]{32}',name):
        raise ValueError('Only the dedicated local development/test databases are supported')

def bootstrap_roles():
    with connection() as admin:
        names = ['lv_owner','lv_migrator','lv_app','lv_seed','lv_ingest','lv_worker','lv_authn']
        existing = admin.execute('SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)',(names,)).fetchall()
        path=ROOT/'.env.roles'
        if existing and not path.exists():
            raise RuntimeError('Existing role names without local ownership file; refusing to change them')
        if not path.exists():
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:
                f.write('MIGRATOR_PASSWORD='+secrets.token_hex(32)+'\nAPP_PASSWORD='+secrets.token_hex(32)+'\n')
        if path.stat().st_mode & 0o077:
            raise RuntimeError('Role credential file must be mode 0600')
        cfg=env_file('.env.roles')
        if 'SEED_PASSWORD' not in cfg:
            if ('lv_seed',) in existing:
                raise RuntimeError('Existing seed role without owned credential')
            with path.open('a') as f: f.write('SEED_PASSWORD='+secrets.token_hex(32)+'\n')
            cfg=env_file('.env.roles')
        if 'INGEST_PASSWORD' not in cfg:
            # Statement-ingestion login (Stage 2A §19.2 path A); added without changing existing credentials.
            if ('lv_ingest',) in existing:
                raise RuntimeError('Existing ingest role without owned credential')
            with path.open('a') as f: f.write('INGEST_PASSWORD='+secrets.token_hex(32)+'\n')
            cfg=env_file('.env.roles')
        if 'WORKER_PASSWORD' not in cfg:
            if ('lv_worker',) in existing:
                raise RuntimeError('Existing maintenance role without owned credential')
            with path.open('a') as f: f.write('WORKER_PASSWORD='+secrets.token_hex(32)+'\n')
            cfg=env_file('.env.roles')
        if 'AUTHN_PASSWORD' not in cfg:
            if ('lv_authn',) in existing:
                raise RuntimeError('Existing issuer role without an owned credential')
            with path.open('a') as f: f.write('AUTHN_PASSWORD='+secrets.token_hex(32)+'\n')
            cfg=env_file('.env.roles')
        for role in names:
            if (role,) not in existing:
                login = sql.SQL('NOLOGIN') if role=='lv_owner' else sql.SQL('LOGIN PASSWORD {}').format(sql.Literal(cfg[ROLE_PASSWORD_KEYS[role]]))
                admin.execute(sql.SQL('CREATE ROLE {} {} NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role),login))
        # Check existing attributes before changing memberships/grants.
        for role in names:
            actual=admin.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin,rolinherit FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
            if actual != (False,False,False,False,False,role!='lv_owner',False):
                raise RuntimeError('Unexpected privileges on a development role')
        inherited=admin.execute('SELECT parent.rolname,child.rolname FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles child ON child.oid=m.member WHERE child.rolname=ANY(%s)',(names,)).fetchall()
        if any(pair != ('lv_owner','lv_migrator') for pair in inherited):
            raise RuntimeError('Unexpected role memberships; refusing to proceed')
        admin.execute('GRANT lv_owner TO lv_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE')

def prepare_database(name=DEV_DB):
    require_dev_database(name)
    with connection(database=name) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public')
        admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
        admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO lv_migrator,lv_app,lv_seed,lv_ingest,lv_worker,lv_authn').format(sql.Identifier(name)))
        admin.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO lv_owner').format(sql.Identifier(name)))
        admin.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        admin.execute('CREATE SCHEMA IF NOT EXISTS lv_meta AUTHORIZATION lv_owner')
        admin.execute('REVOKE ALL ON SCHEMA lv_meta FROM PUBLIC')
        owner=admin.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='lv_meta'").fetchone()[0]
        if owner != 'lv_owner': raise RuntimeError('Unexpected migration schema owner')

def migrate(database=DEV_DB, target='head', *, downgrade=False, disposable=False):
    require_dev_database(database)
    if downgrade and (not disposable or database==DEV_DB):
        raise ValueError('Downgrades are supported only for explicitly disposable test databases')
    cfg=Config(str(ROOT/'alembic.ini'))
    cfg.attributes['connection_url']=URL.create('postgresql+psycopg',username='lv_migrator',password=env_file('.env.roles')['MIGRATOR_PASSWORD'],host='127.0.0.1',port=DEV_PORT,database=database)
    (command.downgrade if downgrade else command.upgrade)(cfg,target)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','migrate'])
    args=parser.parse_args()
    try:
        if args.action=='prepare':
            bootstrap_roles();prepare_database()
            print('Dedicated development roles and migration prerequisites ready.')
        else:
            migrate();print('Database upgraded to the current revision as restricted lv_owner.')
    except Exception as exc:
        # Don't render SQL statements that could contain credentials.
        print('Database task failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
