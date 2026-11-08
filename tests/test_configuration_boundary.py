"""RG-05 runtime privilege closure and authenticated, audited account edits."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import json,pytest,psycopg
from tools.dev_db import connection
from ledgervigil.db.tenant import tenant_transaction
from test_proposal_drafts import world,seed,signed_in,fingerprint,counters
from test_simulation import api,new_draft
from test_submission import act
from test_native_execution import approved
from test_native_approval import ready

TABLES=('ledger','account','account_role_binding','party','fiscal_period')

def patch(c,w,body=None,match=None,account=None):
 aid=account or w['l1']['chart']['BANK_OPERATING']
 if match is None:
  with connection(database=c.app.state.test_database) as db:match='"v'+str(db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()[0])+'"'
 return c.patch('/api/v1/accounts/'+str(aid),json=body or {'name':'SYNTHETIC audited rename'},headers={'If-Match':match})

@pytest.fixture
def admin(api,world,database):
 api.state.test_database=database
 return signed_in(api,world,'admin')

@pytest.mark.parametrize('table',TABLES)
def test_runtime_login_has_no_direct_configuration_dml(database,world,table):
 with connection('lv_app',database) as c:
  assert c.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE')",('lv.'+table,)).fetchone()==(False,)
  with pytest.raises(psycopg.errors.InsufficientPrivilege):
   with c.transaction():
    c.execute("SELECT set_config('app.tenant_id',%s,true)",(str(world['A']),));c.execute('UPDATE lv.'+table+' SET tenant_id=tenant_id')


def test_audited_rename_has_only_configuration_effect_and_stales_readset(database,api,world,admin):
 a=signed_in(api,world,'analyst');pid=new_draft(a,world);assert act(a,pid).status_code==200
 aid=world['l1']['chart']['BANK_OPERATING'];before=fingerprint(database,{'lv.account','lv.configuration_change','lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'});seq=counters(database)
 with connection(database=database) as db:version=db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()[0]
 r=patch(admin,world,{'name':'SYNTHETIC renamed bank ₹'},match='"v'+str(version)+'"');assert r.status_code==200,r.text;assert r.json()['version']==version+1 and r.headers['ETag']=='"v'+str(version+1)+'"'
 assert fingerprint(database,{'lv.account','lv.configuration_change','lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'})==before and counters(database)==seq
 with connection(database=database) as db:
  assert db.execute('SELECT actor_id::text,previous_version,resulting_version FROM lv.configuration_change WHERE account_id=%s',(aid,)).fetchall()==[(world['users']['admin']['id'],version,version+1)]
  assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='CONFIGURATION_ACCOUNT_CHANGED' AND detail->>'account_id'=%s",(aid,)).fetchone()==(1,)
 r=a.post('/api/v1/proposals/'+pid+'/revalidate',headers={'If-Match':'"v2"','Idempotency-Key':uuid4().hex});assert r.status_code==200 and r.json()['proposal']['state']=='STALE'

@pytest.mark.parametrize('role',['analyst','controller','automation','analystB'])
def test_non_administrators_cannot_edit(api,world,admin,role):
 c=signed_in(api,world,role);r=patch(c,world,match='"v1"');assert r.status_code==403

@pytest.mark.parametrize('body',[{}, {'name':' '},{'name':'\x00'},{'name':'\ud800'},{'name':'a'*121},{'active':True},{'active':0},{'name':'valid','type':'ASSET'},{'tenant_id':str(uuid4())},{'version':1}])
def test_invalid_patch_schema_cannot_mutate_any_configuration(database,world,admin,body):
 before=fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})
 with connection(database=database) as db:version=db.execute('SELECT version FROM lv.account WHERE id=%s',(world['l1']['chart']['BANK_OPERATING'],)).fetchone()[0]
 r=admin.patch('/api/v1/accounts/'+world['l1']['chart']['BANK_OPERATING'],content=json.dumps(body),headers={'Content-Type':'application/json','If-Match':'"v'+str(version)+'"'})
 assert r.status_code==422
 assert fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})==before


def test_masked_ids_and_cas_permissions_are_rechecked(database,world,admin):
 foreign=world['lb']['chart']['BANK_OPERATING'];r=patch(admin,world,account=foreign,match='"v1"');assert r.status_code==404
 r=patch(admin,world,account=uuid4(),match='"v1"');assert r.status_code==404
 aid=world['l1']['chart']['BANK_OPERATING'];assert admin.patch('/api/v1/accounts/'+aid,json={'name':'x'}).status_code==428
 with connection(database=database) as db:version=db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()[0]
 token='"v'+str(version)+'"';assert patch(admin,world,match=token).status_code==200
 assert patch(admin,world,match=token).status_code==412
 with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['admin']['id']))
 assert patch(admin,world,match=token).status_code==403


def test_bound_account_deactivation_is_refused(database,world,admin):
 r=patch(admin,world,{'active':False});assert r.status_code==409 and r.json()['reason']=='ACCOUNT_IN_USE'

@pytest.mark.parametrize('missing',['history','audit'])
def test_missing_atomic_history_or_audit_rolls_back(database,world,admin,missing):
 with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv.edit_account(bytea,uuid,text,jsonb,jsonb,text,text)'::regprocedure)").fetchone()[0]
 if missing=='history':start=original.index(' INSERT INTO lv.configuration_change');end=original.index("\n PERFORM lv_identity.audit_append('CONFIGURATION_ACCOUNT_CHANGED'",start)
 else:start=original.index(" PERFORM lv_identity.audit_append('CONFIGURATION_ACCOUNT_CHANGED'");end=original.index('\n RETURN jsonb_build_object',start)
 before=fingerprint(database,set());seq=counters(database)
 with connection(database=database) as db:db.execute(original[:start]+original[end:])
 try:assert patch(admin,world).status_code==500
 finally:
  with connection(database=database) as db:db.execute(original)
 assert fingerprint(database,set())==before and counters(database)==seq


def test_competing_cas_edits_have_one_winner(database,world,admin):
 aid=world['l1']['chart']['BANK_OPERATING']
 with connection(database=database) as db:v=db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()[0]
 with ThreadPoolExecutor(6) as pool:results=list(pool.map(lambda n:patch(admin,world,{'name':'SYNTHETIC writer '+str(n)},match='"v'+str(v)+'"'),range(6)))
 assert [r.status_code for r in results].count(200)==1 and [r.status_code for r in results].count(412)==5


def test_zero_balance_unbound_deactivation_and_literal_name(database,world,admin):
 from configuration_support import configuration_connection
 from conftest import account
 with configuration_connection(database) as db:
  with tenant_transaction(db,world['A']):aid=account(db,world['A'],world['l1']['ledger'],'9876')
 literal="SYNTHETIC '; DROP TABLE lv.account; --"
 r=patch(admin,world,{'name':literal},account=aid);assert r.status_code==200 and r.json()['name']==literal
 r=patch(admin,world,{'active':False},account=aid);assert r.status_code==200 and r.json()['active'] is False
 assert patch(admin,world,{'active':True},account=aid).status_code==422


def test_unbound_nonzero_balance_cannot_be_deactivated(database,world,admin,seed):
 from configuration_support import configuration_connection
 from conftest import account
 from ledgervigil.ledger.seed import seed_manual_journal
 with configuration_connection(database) as db:
  with tenant_transaction(db,world['A']):aid=account(db,world['A'],world['l1']['ledger'],'9876')
 seed_manual_journal(seed,world['A'],world['l1']['ledger'],{'source_ref':'nonzero-unbound','effective_date':'2026-10-14','description':'SYNTHETIC','lines':[{'account_id':str(aid),'side':'D','amount':'0.01'},{'account_id':world['l1']['chart']['OPENING_BALANCE_EQUITY'],'side':'C','amount':'0.01'}]})
 r=patch(admin,world,{'active':False},account=aid);assert r.status_code==409 and r.json()['reason']=='ACCOUNT_IN_USE'


def test_history_is_private_immutable_and_retains_exact_before_after(database,world,admin):
 aid=world['l1']['chart']['BANK_OPERATING'];assert patch(admin,world).status_code==200
 with connection('lv_app',database) as db:
  with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('SELECT * FROM lv.configuration_change')
 for command in ['UPDATE lv.configuration_change SET request_id=request_id','DELETE FROM lv.configuration_change']:
  with pytest.raises(psycopg.errors.CheckViolation):
   with connection(database=database) as db:db.execute(command)
 with connection(database=database) as db:
  before,after=db.execute('SELECT before_fields,after_fields FROM lv.configuration_change WHERE account_id=%s',(aid,)).fetchone()
  assert before['name']!=after['name'] and after['version']==before['version']+1


def test_database_writer_rechecks_shape_and_verified_session_tenant(database,world,admin):
 from ledgervigil.auth.crypto import token_hash
 from psycopg.types.json import Jsonb
 token=admin.cookies.get('lv_session')
 if token is None:token=next(v for k,v in admin.cookies.items() if 'session' in k)
 aid=world['l1']['chart']['BANK_OPERATING']
 with connection(database=database) as db:v=db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()[0]
 with connection('lv_app',database) as db:
  with pytest.raises(psycopg.errors.InsufficientPrivilege,match='different session'):
   with db.transaction():
    db.execute("SELECT set_config('app.tenant_id',%s,true)",(str(world['B']),))
    db.execute('SELECT lv.edit_account(%s,%s,%s,%s,NULL,%s,%s)',(token_hash(token),aid,'"v'+str(v)+'"',Jsonb({'name':'forged context'}),'probe','guc'))
  r=db.execute('SELECT lv.edit_account(%s,%s,%s,%s,NULL,%s,%s)',(token_hash(token),aid,'"v'+str(v)+'"',Jsonb({'active':True}),'probe','shape')).fetchone()[0]
  assert r['status']==422,r
  r=db.execute('SELECT lv.edit_account(%s,%s,%s,%s,NULL,%s,%s)',(token_hash(token),world['lb']['chart']['BANK_OPERATING'],'"v1"',Jsonb({'name':'foreign'}),'probe','foreign')).fetchone()[0]
  assert r['status']==404



def test_revoked_after_http_boundary_cannot_edit(database,world,admin,monkeypatch):
 from ledgervigil.configuration import routes
 original=routes.token_hash
 def revoke(token):
  with connection(database=database) as db:db.execute("UPDATE lv_identity.membership SET status='REVOKED',revoked_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s",(world['A'],world['users']['admin']['id']))
  return original(token)
 before=counters(database)
 with connection(database=database) as db:history=db.execute('SELECT count(*) FROM lv.configuration_change WHERE tenant_id=%s',(world['A'],)).fetchone()
 with monkeypatch.context() as m:
  m.setattr(routes,'token_hash',revoke);r=patch(admin,world);assert r.status_code==401,r.text
 assert counters(database)==before
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.configuration_change WHERE tenant_id=%s',(world['A'],)).fetchone()==history


@pytest.mark.parametrize('first',['edit','execute'])
def test_configuration_and_execution_serialize_at_ledger_fence(database,world,admin,approved,monkeypatch,first):
 from test_native_execution import post as execute,_wait_for_lock
 from ledgervigil.execution import routes
 import threading,time
 _,controller,pid,_=approved;aid=world['l1']['chart']['BANK_OPERATING']
 with connection(database=database) as db:v=db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()[0]
 match='"v'+str(v)+'"';before=counters(database)
 with ThreadPoolExecutor(2) as pool:
  if first=='edit':
   # Keep a real lv_app edit transaction open after SQL, including its deferred history/audit checks.
   token=next(value for key,value in admin.cookies.items() if 'session' in key)
   from ledgervigil.auth.crypto import token_hash
   from psycopg.types.json import Jsonb
   with connection('lv_app',database) as db:
    r=db.execute('SELECT lv.edit_account(%s,%s,%s,%s,NULL,%s,%s)',(token_hash(token),aid,match,Jsonb({'name':'SYNTHETIC concurrent rename'}),'probe','edit-first')).fetchone()[0];assert r['status']==200
    future=pool.submit(execute,controller,pid)
    deadline=time.monotonic()+5
    with connection(database=database,autocommit=True) as monitor:
     while True:
      rows=monitor.execute("SELECT pid FROM pg_stat_activity WHERE datname=%s AND wait_event_type='Lock' AND query LIKE 'SELECT lv.execute_native%%'",(database,)).fetchall()
      if rows:break
      assert time.monotonic()<deadline;time.sleep(.01)
   response=future.result(timeout=10);assert response.status_code==409 and response.json()['code']=='PROPOSAL_STALE',response.text
   assert counters(database)==before
  else:
   entered=threading.Event();release=threading.Event()
   def hold(_):entered.set();assert release.wait(8)
   with monkeypatch.context() as m:
    m.setattr(routes,'_after_write',hold);future=pool.submit(execute,controller,pid)
    try:
     assert entered.wait(5);editing=pool.submit(patch,admin,world,{'name':'SYNTHETIC lost CAS'},match)
     deadline=time.monotonic()+5
     with connection(database=database,autocommit=True) as monitor:
      while True:
       rows=monitor.execute("SELECT pid FROM pg_stat_activity WHERE datname=%s AND wait_event_type='Lock' AND query LIKE 'SELECT lv.edit_account%%'",(database,)).fetchall()
       if rows:break
       assert time.monotonic()<deadline;time.sleep(.01)
    finally:release.set()
    assert future.result(timeout=10).status_code==200
    assert editing.result(timeout=10).status_code==412
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(int(first=='execute'),)


def test_migration_roundtrip_and_populated_history_refuses_downgrade():
 from test_processor_payments import disposable
 from tools.dev_db import migrate
 from test_proposal_drafts import world as setup
 from ledgervigil.app import create_app
 from ledgervigil.auth.settings import AuthSettings
 with disposable('0022_approved_withdrawal') as dbname:
  with connection('lv_app',dbname,autocommit=True) as app,connection('lv_seed',dbname,autocommit=True) as seeder:w=setup.__wrapped__(dbname,app,seeder)
  before=fingerprint(dbname,{'lv_meta.alembic_version','lv.configuration_change','lv.native_export_job','lv.proof_pack','lv.proof_pack_chain','lv.proof_pack_request'});seq=counters(dbname)
  migrate(dbname);assert fingerprint(dbname,{'lv_meta.alembic_version','lv.configuration_change','lv.native_export_job','lv.proof_pack','lv.proof_pack_chain','lv.proof_pack_request'})==before and counters(dbname)==seq
  migrate(dbname,'0022_approved_withdrawal',downgrade=True,disposable=True)
  with connection('lv_app',dbname) as db:assert db.execute("SELECT has_table_privilege(current_user,'lv.account','INSERT,UPDATE')").fetchone()==(True,)
  migrate(dbname);application=create_app(AuthSettings(),connect=lambda:connection('lv_app',dbname));application.state.test_database=dbname
  try:
   admin=signed_in(application,w,'admin');assert patch(admin,w).status_code==200
   retained=fingerprint(dbname,set())
   with pytest.raises(Exception,match='downgrade_empty'):migrate(dbname,'0022_approved_withdrawal',downgrade=True,disposable=True)
   assert fingerprint(dbname,set())==retained
  finally:application.state.pool.close()
