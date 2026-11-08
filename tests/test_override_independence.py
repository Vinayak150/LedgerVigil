"""RG08 user-approved independence, through actual HTTP/PostgreSQL transactions."""
from uuid import uuid4
import json
import pytest,psycopg
from tools.dev_db import connection,migrate
from tools.identity_admin import owner_transaction
from test_proposal_drafts import world,seed,signed_in,client,fingerprint,counters
from test_simulation import api,new_draft
from test_resimulation import submit
from test_submission import act
from test_evidence_overrides import blocked,post,history

def change(database,w,sql):
 with connection(database=database) as db:db.execute(sql,(w['A'],w['users']['controller']['id']))

@pytest.mark.parametrize('hours,allowed',[(0,False),(23,False),(24,True),(48,True)])
def test_cooling_boundary(database,world,blocked,hours,allowed):
 _,c,pid,_=blocked
 with connection(database=database) as db:db.execute("UPDATE lv_identity.membership_role SET granted_at=statement_timestamp()-(%s*interval '1 hour') WHERE tenant_id=%s AND user_id=%s",(hours,world['A'],world['users']['controller']['id']))
 before=counters(database);r=post(c,pid)
 assert r.status_code==(200 if allowed else 403),r.text
 if not allowed:assert r.json()['reason']=='REVIEW_COOLING_OFF' and history(c,pid).json()['items']==[]
 assert counters(database)==before

def test_self_grant_is_refused_after_cooling(database,world,blocked):
 _,c,pid,_=blocked
 change(database,world,'UPDATE lv_identity.membership_role SET granted_by=user_id WHERE tenant_id=%s AND user_id=%s')
 r=post(c,pid);assert (r.status_code,r.json()['reason'])==(403,'SELF_GRANT'),r.text

@pytest.mark.parametrize('attribution',['maker','submitter','editor'])
def test_all_contributors_are_excluded(database,api,world,attribution):
 a=signed_in(api,world,'analyst');pid=new_draft(a,world);editor=signed_in(api,world,'automation')
 if attribution=='editor':
  r=editor.patch('/api/v1/proposals/'+pid,headers={'If-Match':'"v1"'},json={'rationale':'SYNTHETIC editor attribution'})
  assert r.status_code==200,r.text
 r=act(a,pid,match='"v2"' if attribution=='editor' else '"v1"');assert r.status_code==200,r.text
 who='automation' if attribution=='editor' else 'analyst'
 with owner_transaction(database) as db:
  db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['A'],world['users'][who]['id']))
 c=signed_in(api,world,who);r=post(c,pid,match='"v3"' if attribution=='editor' else '"v2"')
 assert (r.status_code,r.json()['reason'])==(403,'MAKER_OR_EDITOR'),r.text
 assert history(c,pid).json()['items']==[]

@pytest.mark.parametrize('mode',['self_grant','recent_grant','role_removed'])
def test_original_key_replay_rechecks_current_authority(database,world,blocked,mode):
 _,c,pid,_=blocked;first=post(c,pid,key='original-key');assert first.status_code==200,first.text
 if mode=='self_grant':change(database,world,'UPDATE lv_identity.membership_role SET granted_by=user_id WHERE tenant_id=%s AND user_id=%s')
 elif mode=='recent_grant':change(database,world,'UPDATE lv_identity.membership_role SET granted_at=statement_timestamp() WHERE tenant_id=%s AND user_id=%s')
 else:change(database,world,'DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s')
 before=fingerprint(database,set());r=post(c,pid,key='original-key')
 assert r.status_code==403 and r.headers.get('Idempotent-Replayed')!='true',r.text
 assert fingerprint(database,set())==before
 # Earlier exception is immutable history, not silently rewritten.
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.proposal_override WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

def test_broken_action_guard_is_caught_at_commit(database,world,blocked):
 _,c,pid,_=blocked
 change(database,world,'UPDATE lv_identity.membership_role SET granted_at=statement_timestamp() WHERE tenant_id=%s AND user_id=%s')
 with connection(database=database) as db:
  original=db.execute("SELECT pg_get_functiondef('lv.override_evidence(bytea,uuid,text,text,text,jsonb,jsonb,text,text)'::regprocedure)").fetchone()[0]
  broken=original.replace("blocker:=lv.review_blocker(p,s.user_id);","blocker:=NULL;")
  assert broken!=original;db.execute(broken)
 try:
  before=counters(database);r=post(c,pid);assert r.status_code==500,r.text
  assert counters(database)==before
  with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.proposal_override WHERE proposal_id=%s',(pid,)).fetchone()==(0,)
 finally:
  with connection(database=database) as db:db.execute(original)

def test_function_roundtrip(migration_database):
 database=migration_database
 names=['override_evidence','validate_evidence_override']
 def state():
  with connection(database=database) as db:return db.execute("SELECT proname,pg_get_functiondef(p.oid),proacl::text,pg_get_userbyid(proowner) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='lv' AND proname=ANY(%s) ORDER BY proname",(names,)).fetchall()
 latest=state();migrate(database,'0029_account_patch',downgrade=True,disposable=True)
 try:
  previous=state();assert latest!=previous
  for a,b in zip(latest,previous):assert a[0]==b[0] and a[2:]==b[2:]
 finally:migrate(database)
 assert state()==latest
