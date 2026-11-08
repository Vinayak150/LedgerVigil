"""Actual catalog/connection tests; never change development ACLs, global roles or settings."""
from copy import deepcopy
from uuid import uuid4
from pathlib import Path
import json,stat
import pytest,psycopg
from psycopg import sql
from tools import recovery_database as profile,verify_recovery as recovery
from tools.dev_db import connection

@pytest.fixture
def security_source(database):
 with connection(database=database) as c:p=profile.capture(c)
 yield database,p

@pytest.fixture
def target(security_source):
 source,p=security_source;name='lv_migration_test_'+uuid4().hex;oid=profile.create_target(name,p)
 try:yield name,oid
 finally:
  with connection(autocommit=True) as c:c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))

def test_restore_uses_private_initial_connections_and_exact_acl(security_source,target):
 source,p=security_source;name,oid=target
 with pytest.raises(psycopg.OperationalError):
  with connection('lv_app',name):pass
 with connection(database=name) as c:assert c.execute('SELECT datconnlimit FROM pg_database WHERE datname=current_database()').fetchone()==(0,)
 result=profile.apply(source,name,oid,p);assert all(result.values())
 with connection(database=name) as c:
  profile.assert_profile(p,profile.capture(c));assert c.execute("SELECT has_database_privilege('public',current_database(),'CONNECT')").fetchone()==(False,)
 with connection('lv_app',name) as c:assert c.execute('SELECT current_database()').fetchone()==(name,)

@pytest.mark.parametrize('name',['ledgervigil_dev','postgres','production','lv_migration_test_bad'])
def test_live_or_arbitrary_target_is_refused_before_sql(security_source,name):
 _,p=security_source
 with pytest.raises(ValueError):profile.create_target(name,p)
 with pytest.raises(ValueError):profile.apply('ledgervigil_dev',name,1,p)

def test_source_identity_or_stale_target_oid_is_refused(security_source,target):
 source,p=security_source;name,oid=target
 for src,identity in [(name,oid),(source,oid+999999)]:
  with pytest.raises(ValueError,match='TARGET_IDENTITY'):profile.apply(src,name,identity,p)

@pytest.mark.parametrize('fault',['grantor','unknown_grantee','public_grant_option','unknown_setting','unsafe_setting','duplicate_acl','duplicate_setting','nonlibc','template','disallowed_connections','foreign_tablespace','boolean_limit'])
def test_unsupported_or_ambiguous_profiles_fail_before_restore(security_source,fault):
 _,p=security_source;p=deepcopy(p)
 if fault=='grantor':p['acl'][0]['grantor']='lv_app'
 elif fault=='unknown_grantee':p['acl'][0]['grantee']='unknown_role'
 elif fault=='public_grant_option':p['acl'].append({'grantor':p['owner'],'grantee':'PUBLIC','privilege':'CONNECT','grantable':True})
 elif fault in ['unknown_setting','unsafe_setting']:p['settings']=[{'role':None,'name':'custom.secret' if fault=='unknown_setting' else 'session_preload_libraries','value':'unsupported'}]
 elif fault=='duplicate_acl':p['acl'].append(p['acl'][0])
 elif fault=='duplicate_setting':p['settings']=[{'role':None,'name':'statement_timeout','value':'2s'}]*2
 elif fault=='nonlibc':p['locale_provider']='i'
 elif fault=='template':p['is_template']=True
 elif fault=='disallowed_connections':p['allow_connections']=False
 elif fault=='foreign_tablespace':p['tablespace']='unknown'
 else:p['connection_limit']=True
 with pytest.raises(ValueError):profile.create_target('lv_migration_test_'+uuid4().hex,p)


def test_nondefault_owner_grant_options_and_scoped_settings_restore_without_global_mutation(security_source,target):
 source,p=security_source;name,oid=target;p=deepcopy(p);p['owner']='lv_owner';p['connection_limit']=17
 for row in p['acl']:row['grantor']='lv_owner'
 next(row for row in p['acl'] if row['grantee']=='lv_app' and row['privilege']=='CONNECT')['grantable']=True
 p['settings']=[{'role':None,'name':'statement_timeout','value':'5s'},{'role':'lv_app','name':'statement_timeout','value':'1250ms'},{'role':None,'name':'search_path','value':'pg_catalog, public'}]
 with connection() as c:global_before=profile.role_fingerprint(c)
 profile.apply(source,name,oid,p)
 with connection(database=name) as c:profile.assert_profile(p,profile.capture(c))
 with connection('lv_app',name) as c:
  assert c.execute('SHOW statement_timeout').fetchone()==('1250ms',)
  assert c.execute('SHOW search_path').fetchone()==('pg_catalog, public',)
 with connection() as c:assert profile.role_fingerprint(c)==global_before

@pytest.mark.parametrize('fault',['public_connect','missing_connect','grant_option','owner','connection_limit','database_setting','role_setting'])
def test_real_catalog_tampering_is_detected(security_source,target,fault):
 source,p=security_source;name,oid=target;profile.apply(source,name,oid,p)
 with connection() as c:
  commands={
   'public_connect':sql.SQL('GRANT CONNECT ON DATABASE {} TO PUBLIC'),
   'missing_connect':sql.SQL('REVOKE CONNECT ON DATABASE {} FROM lv_app'),
   'grant_option':sql.SQL('GRANT CONNECT ON DATABASE {} TO lv_app WITH GRANT OPTION'),
   'owner':sql.SQL('ALTER DATABASE {} OWNER TO lv_owner'),
   'connection_limit':sql.SQL('ALTER DATABASE {} CONNECTION LIMIT 3'),
   'database_setting':sql.SQL("ALTER DATABASE {} SET statement_timeout TO '3s'"),
   'role_setting':sql.SQL("ALTER ROLE lv_app IN DATABASE {} SET lock_timeout TO '4s'")}
  c.execute(commands[fault].format(sql.Identifier(name)))
 with connection(database=name) as c:
  with pytest.raises(ValueError,match='PROFILE_MISMATCH'):profile.assert_profile(p,profile.capture(c))

@pytest.mark.parametrize('change',['namespace_owner','namespace_acl','default_acl'])
def test_namespace_and_default_privileges_are_in_the_semantic_catalog(database,change):
 with connection(database=database) as c:
  before=recovery.schema_digest(c)
  statement={'namespace_owner':'ALTER SCHEMA lv_meta OWNER TO postgres','namespace_acl':'GRANT USAGE ON SCHEMA lv_meta TO lv_app','default_acl':'ALTER DEFAULT PRIVILEGES FOR ROLE lv_owner IN SCHEMA lv GRANT SELECT ON TABLES TO lv_app'}[change]
  c.execute(statement);assert recovery.schema_digest(c)!=before;c.rollback()


def test_failed_profile_restore_never_reports_pass_and_cleans_its_database(database,tmp_path,monkeypatch):
 names=[];original=profile.apply
 def fail(source,name,oid,p):names.append(name);raise ValueError('INJECTED_PROFILE_RESTORE_FAILURE')
 monkeypatch.setattr(profile,'apply',fail)
 with pytest.raises(ValueError,match='INJECTED_PROFILE'):recovery.run(database,tmp_path/'failed')
 assert not(tmp_path/'failed/REPORT.json').exists() and len(names)==1
 with connection() as c:assert c.execute('SELECT oid FROM pg_database WHERE datname=%s',(names[0],)).fetchone() is None
 assert stat.S_IMODE((tmp_path/'failed/DATABASE_PROFILE.json').stat().st_mode)==0o600


@pytest.mark.parametrize('value',['pg_catalog, public','"pg_catalog, public"','"schema with space", pg_catalog'])
def test_list_setting_keeps_exact_catalog_and_effective_value(security_source,target,value):
 source,p=security_source;name,oid=target;p=deepcopy(p);p['settings']=[{'role':None,'name':'search_path','value':value}]
 profile.apply(source,name,oid,p)
 with connection('lv_app',name) as c:assert c.execute('SHOW search_path').fetchone()==(value,)
 with connection(database=name) as c:profile.assert_profile(p,profile.capture(c))


def test_complete_restore_carries_nondefault_database_profile(database,tmp_path):
 from tools.dev_db import prepare_database,migrate
 name='lv_migration_test_'+uuid4().hex
 with connection(autocommit=True) as c:
  c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)));oid=c.execute('SELECT oid FROM pg_database WHERE datname=%s',(name,)).fetchone()[0]
 try:
  prepare_database(name);migrate(name)
  with connection(database=name) as c:p=profile.capture(c)
  p['owner']='lv_owner';p['connection_limit']=19
  for row in p['acl']:row['grantor']='lv_owner'
  p['settings']=[{'role':None,'name':'statement_timeout','value':'5s'},{'role':'lv_app','name':'statement_timeout','value':'1250ms'},{'role':None,'name':'search_path','value':'pg_catalog, public'}]
  profile.apply(database,name,oid,p)
  r=recovery.run(name,tmp_path/'complete');assert r['status']=='PASS' and r['database_profile']['scoped_settings_count']==3 and r['database_profile']['source_profile_unchanged']
  assert r['database_profile']['global_roles_memberships_and_global_role_settings_unchanged'] and r['disposable_restore_database_removed']
  with connection(database=name) as c:profile.assert_profile(p,profile.capture(c))
  assert stat.S_IMODE((tmp_path/'complete/REPORT.json').stat().st_mode)==0o600
 finally:
  with connection(autocommit=True) as c:c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
