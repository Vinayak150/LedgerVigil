from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import json,pytest
from tools.dev_db import connection,migrate
from test_proposal_drafts import world,seed,signed_in,fingerprint,counters
from test_simulation import api,new_draft
from test_submission import act
from test_configuration_boundary import admin,patch
from test_native_approval import ready,post as approve
from test_native_execution import post as execute
from test_proposal_comments import post as comment

@pytest.mark.parametrize('body,pointer',[({'name':''},'/name'),({'name':3},'/name'),({'active':True},'/active'),({'other':1},'/other'),({'a~/b':1},'/a~0~1b'),({},''),([],''),({'name':'valid','active':False},''),({'name':'\ud800'},'/name'),({'bad\ud800':1},'')])
def test_validation_has_field_pointers(database,world,admin,body,pointer):
 before=fingerprint(database,{'lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'})
 r=admin.patch('/api/v1/accounts/'+world['l1']['chart']['BANK_OPERATING'],headers={'If-Match':'"v'+str(account(database,world)[1])+'"','Content-Type':'application/json'},content=json.dumps(body))
 assert r.status_code==422 and r.json()['code']=='SCHEMA_INVALID',r.text
 assert pointer in [e['pointer'] for e in r.json()['errors']]
 assert fingerprint(database,{'lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'})==before

@pytest.mark.parametrize('raw',['{','{"name":"a","name":"b"}','{"name":NaN}'])
def test_invalid_json_keeps_parser_errors(database,world,admin,raw):
 r=admin.patch('/api/v1/accounts/'+world['l1']['chart']['BANK_OPERATING'],headers={'If-Match':'"v'+str(account(database,world)[1])+'"','Content-Type':'application/json'},content=raw)
 assert r.status_code==422 and r.json()['errors']==[{'pointer':'','code':'json'}]

def account(database,w):
 with connection(database=database) as db:return db.execute('SELECT name,version FROM lv.account WHERE id=%s',(w['l1']['chart']['BANK_OPERATING'],)).fetchone()

def test_exact_noop_preserves_version_history_and_proposal_freshness(database,api,world,admin):
 a=signed_in(api,world,'analyst');pid=new_draft(a,world);assert act(a,pid).status_code==200
 name,version=account(database,world);before=fingerprint(database,{'lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'});seq=counters(database)
 r=patch(admin,world,{'name':name},match='"v'+str(version)+'"');assert r.status_code==200 and r.json()['version']==version and r.headers['etag']=='"v'+str(version)+'"'
 assert account(database,world)==(name,version) and fingerprint(database,{'lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'})==before and counters(database)==seq
 with connection(database=database) as db:assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='CONFIGURATION_ACCOUNT_UNCHANGED' AND detail->>'account_id'=%s",(world['l1']['chart']['BANK_OPERATING'],)).fetchone()==(1,)
 assert act(a,pid,'revalidate',match='"v2"').json()['freshness']['status']=='FRESH'
 # A stale version is still refused, even if the body equals the current name.
 assert patch(admin,world,{'name':name},match='"v0"').status_code==412

def test_parallel_noops_and_real_edit_keep_cas(database,world,admin):
 name,v=account(database,world);match='"v'+str(v)+'"'
 with ThreadPoolExecutor(4) as pool:rs=list(pool.map(lambda _:patch(admin,world,{'name':name},match=match),range(4)))
 assert all(r.status_code==200 and r.json()['version']==v for r in rs)
 with ThreadPoolExecutor(2) as pool:rs=list(pool.map(lambda body:patch(admin,world,body,match=match),[{'name':name},{'name':'SYNTHETIC real rename'}]))
 assert rs[1].status_code==200 and rs[0].status_code in (200,412) and account(database,world)[1]==v+1
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.configuration_change WHERE account_id=%s',(world['l1']['chart']['BANK_OPERATING'],)).fetchone()==(1,)

def test_unchanged_request_is_not_authorization_or_masking_bypass(database,api,world,admin):
 name,v=account(database,world);match='"v'+str(v)+'"';a=signed_in(api,world,'analyst')
 assert patch(a,world,{'name':name},match=match).status_code==403
 assert patch(admin,world,{'name':name},account=world['lb']['chart']['BANK_OPERATING'],match=match).status_code==404
 with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['admin']['id']))
 assert patch(admin,world,{'name':name},match=match).status_code==403

def test_omitting_unchanged_audit_rolls_back(database,world,admin):
 name,v=account(database,world);before=fingerprint(database,set())
 sig='lv.edit_account(bytea,uuid,text,jsonb,jsonb,text,text)'
 with connection(database=database) as db:original=db.execute('SELECT pg_get_functiondef(%s::regprocedure)',(sig,)).fetchone()[0]
 start=original.index(" PERFORM lv_identity.audit_append('CONFIGURATION_ACCOUNT_UNCHANGED'");end=original.index(';',start)+1
 with connection(database=database) as db:db.execute(original[:start]+original[end:])
 try:assert patch(admin,world,{'name':name},match='"v'+str(v)+'"').status_code==500
 finally:
  with connection(database=database) as db:db.execute(original)
 assert fingerprint(database,set())==before

@pytest.mark.parametrize('action',['approve','execute','comment'])
def test_exact_success_key_cannot_replay_after_role_removal(database,api,world,action):
 if action=='comment':
  c=signed_in(api,world,'analyst');pid=new_draft(c,world);actor='analyst';fn=comment
 else:
  _,c,pid,_=ready.__wrapped__(database,api,world);actor='controller';fn=approve if action=='approve' else execute
  if action=='execute':assert approve(c,pid).status_code==200
 key=uuid4().hex;first=fn(c,pid,key=key);assert first.status_code in (200,201),first.text
 with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users'][actor]['id']))
 before=fingerprint(database,set());r=fn(c,pid,key=key);assert r.status_code==403,r.text;assert fingerprint(database,set())==before

def test_0029_roundtrip(database):
 sig='lv.edit_account(bytea,uuid,text,jsonb,jsonb,text,text)'
 with connection(database=database) as db:fixed=db.execute('SELECT pg_get_functiondef(%s::regprocedure),proacl::text FROM pg_proc WHERE oid=%s::regprocedure',(sig,sig)).fetchone()
 migrate(database,'0028_workflow_lock_order',downgrade=True,disposable=True)
 with connection(database=database) as db:old=db.execute('SELECT pg_get_functiondef(%s::regprocedure),proacl::text FROM pg_proc WHERE oid=%s::regprocedure',(sig,sig)).fetchone()
 assert 'CONFIGURATION_ACCOUNT_UNCHANGED' not in old[0] and fixed[1]==old[1]
 migrate(database)
 with connection(database=database) as db:assert db.execute('SELECT pg_get_functiondef(%s::regprocedure),proacl::text FROM pg_proc WHERE oid=%s::regprocedure',(sig,sig)).fetchone()==fixed


def test_repeated_inactive_patch_is_audited_without_version_bump(database,world,admin):
 from configuration_support import configuration_connection
 from conftest import account as make_account
 from ledgervigil.db.tenant import tenant_transaction
 with configuration_connection(database) as db:
  with tenant_transaction(db,world['A']):aid=str(make_account(db,world['A'],world['l1']['ledger'],'9853'))
 first=patch(admin,world,{'active':False},account=aid,match='"v1"');assert first.status_code==200 and first.json()['version']==2
 second=patch(admin,world,{'active':False},account=aid,match='"v2"');assert second.status_code==200 and second.json()['version']==2
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.configuration_change WHERE account_id=%s',(aid,)).fetchone()==(1,)


def test_unicode_and_whitespace_are_not_silently_normalized(database,world,admin):
 _,v=account(database,world)
 for name in ['SYNTHETIC café','SYNTHETIC cafe\u0301',' SYNTHETIC cafe\u0301 ']:
  r=patch(admin,world,{'name':name},match='"v'+str(v)+'"');assert r.status_code==200 and r.json()['name']==name and r.json()['version']==v+1;v+=1
 assert patch(admin,world,{'name':name},match='"v'+str(v)+'"').json()['version']==v
