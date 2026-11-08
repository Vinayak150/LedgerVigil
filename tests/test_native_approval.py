from configuration_support import configuration_connection
"""Accepted native independent approval; synthetic fixtures, real PostgreSQL. No posting."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import json,time
import pytest,psycopg
from tools.dev_db import connection,migrate
from tools.identity_admin import owner_transaction,create_user,grant
from ledgervigil.auth.crypto import token_hash
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.proposals import approval
from test_proposal_drafts import world,seed,signed_in,client,fingerprint,counters,settlement
from test_simulation import api,new_draft
from test_resimulation import submit,change_account
from test_submission import act
from test_evidence_overrides import post as override
SKIP={'lv.proposal','lv.approval_check','lv.proposal_decision','lv.proposal_transition','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'}
def age(database,world,user='controller',hours=48):
 with connection(database=database) as db:db.execute("UPDATE lv_identity.membership_role SET granted_at=statement_timestamp()-(%s*interval '1 hour') WHERE tenant_id=%s AND user_id=%s",(hours,world['A'],world['users'][user]['id']))
def post(c,pid,key=None,match='"v3"',body=None):
 h={'Idempotency-Key':key or uuid4().hex}
 if match is not None:h['If-Match']=match
 return c.post(f'/api/v1/proposals/{pid}/approve',headers=h,json={'revision':1} if body is None else body)
@pytest.fixture
def ready(database,api,world):
 a,pid,run=submit(api,world);c=signed_in(api,world,'controller');r=override(c,pid);assert r.status_code==200,r.text;age(database,world);return a,c,pid,run

def test_native_approval_is_independent_revision_bound_and_has_no_financial_effect(database,ready):
 a,c,pid,run=ready;before=fingerprint(database,SKIP);ctr=counters(database)
 assert c.get('/api/v1/proposals/'+pid).json()['allowed_actions']==['APPROVE','REJECT']
 r=post(c,pid,body={'revision':1,'comment':'Exact ₹ <img src=x>\nIndependent synthetic review; no execution.'});assert r.status_code==200,r.text
 assert (r.json()['state'],r.json()['version'])==('APPROVED',4) and r.headers['etag']=='"v4"' and r.json()['allowed_actions']==['EXECUTE']
 assert a.get('/api/v1/simulations/'+run['id']).json()==run and not run['outcome']['eligible_for_execution']
 h=c.get(f'/api/v1/proposals/{pid}/reviews').json();assert [x['kind'] for x in h['items']]==['INSUFFICIENT_EVIDENCE_OVERRIDE','APPROVAL']
 d=h['items'][1];assert d['approved_by']!=d['submitted_by'] and d['revision']==1 and d['simulation_id']==run['id'] and d['comment'].startswith('Exact ₹')
 assert fingerprint(database,SKIP)==before and counters(database)==ctr
 assert c.post(f'/api/v1/proposals/{pid}/execute',json={'revision':1}).status_code==400  # new execute route requires its own key/version; approval does not execute

def test_recent_grants_have_no_approval_action_and_cannot_approve(database,world,ready):
 _,c,pid,_=ready;age(database,world,hours=1)
 assert c.get('/api/v1/proposals/'+pid).json()['allowed_actions']==[]
 r=post(c,pid);assert r.status_code==403 and r.json()['reason']=='REVIEW_COOLING_OFF'
 age(database,world,hours=24);assert post(c,pid).status_code==200

def test_changed_dependency_marks_stale_releases_reservation_and_records_no_decision(database,world,ready):
 a,c,pid,_=ready;change_account(database,world);before=counters(database)
 r=post(c,pid,key='stale');assert r.status_code==409,r.text;assert r.json()['code']=='PROPOSAL_STALE' and r.json()['current_version']==4
 assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE'
 assert post(c,pid,key='stale').headers['Idempotent-Replayed']=='true'
 with connection(database=database) as db:
  assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(0,)
  assert db.execute("SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s AND status='RESERVED'",(pid,)).fetchone()==(0,)
 assert counters(database)==before

def test_second_decision_is_conflict_and_original_retry_replays(ready):
 _,c,pid,_=ready;r=post(c,pid,key='decision');assert r.status_code==200,r.text
 repeat=post(c,pid,key='decision');assert repeat.json()==r.json() and repeat.headers['Idempotent-Replayed']=='true'
 assert post(c,pid).json()['code']=='ALREADY_DECIDED'
 assert post(c,pid,key='decision',body={'revision':1,'comment':'changed'}).json()['code']=='IDEMPOTENCY_KEY_REUSED'

def test_permissions_maker_and_all_prior_editors_are_excluded(database,api,world,ready):
 _,c,pid,_=ready
 for name in ['analyst','admin','automation']:assert post(signed_in(api,world,name),pid).status_code==403
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['A'],world['users']['analyst']['id']))
 assert post(signed_in(api,world,'analyst'),pid).json()['reason']=='MAKER_OR_EDITOR'
 # An actual automation-developer edit before submission must exclude that editor later.
 a=signed_in(api,world,'analyst');other=new_draft(a,world);editor=signed_in(api,world,'automation');r=editor.patch('/api/v1/proposals/'+other,headers={'If-Match':'"v1"'},json={'rationale':'SYNTHETIC recorded editor'});assert r.status_code==200
 assert act(a,pid,'withdraw',match='"v3"').status_code==200
 assert act(a,other,match='"v2"').status_code==200
 assert override(c,other,match='"v3"').status_code==200
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['A'],world['users']['automation']['id']))
 refused=post(signed_in(api,world,'automation'),other,match='"v4"',body={'revision':2});assert refused.status_code==403 and refused.json()['reason']=='MAKER_OR_EDITOR'

def test_atomic_rollback_and_raw_promotion_are_refused(database,ready,monkeypatch):
 _,c,pid,_=ready;before=fingerprint(database,set())
 def fail(_):raise RuntimeError('injected approval rollback')
 monkeypatch.setattr(approval,'_after_write',fail);assert post(c,pid).status_code==500
 assert fingerprint(database,set())==before
 with pytest.raises(psycopg.errors.CheckViolation):
  with connection(database=database) as db:db.execute("UPDATE lv.proposal SET state='APPROVED',version=version+1 WHERE id=%s",(pid,))

def test_twenty_competing_approvals_have_exactly_one_decision(ready):
 _,c,pid,_=ready
 with ThreadPoolExecutor(20) as pool:responses=list(pool.map(lambda _:post(c,pid),range(20)))
 assert sum(r.status_code==200 for r in responses)==1
 assert sum(r.status_code==409 and r.json()['code']=='ALREADY_DECIDED' for r in responses)==19,[(r.status_code,r.text) for r in responses]
 assert len([x for x in c.get(f'/api/v1/proposals/{pid}/reviews').json()['items'] if x['kind']=='APPROVAL'])==1

def test_period_close_after_approval_is_consumed_and_preserves_historical_decision(database,api,world,ready):
 from test_period_staleness import close,drain
 a,c,pid,run=ready;assert post(c,pid).status_code==200
 api.state.test_database=database;job=close(api,world);ctr=counters(database);before=fingerprint(database,SKIP|{'lv.staged_workflow','lv.reservation_event','lv.maintenance_event','lv.work_item','lv.outbox'})
 assert drain(database,world['A'],job)['status']=='DONE'
 p=a.get('/api/v1/proposals/'+pid).json();assert (p['state'],p['version'])==('STALE',5)
 assert len([x for x in c.get(f'/api/v1/proposals/{pid}/reviews').json()['items'] if x['kind']=='APPROVAL'])==1
 assert a.get('/api/v1/simulations/'+run['id']).json()==run and counters(database)==ctr
 assert fingerprint(database,SKIP|{'lv.staged_workflow','lv.reservation_event','lv.maintenance_event','lv.work_item','lv.outbox'})==before
 assert act(a,pid,'resimulate',match='"v5"').status_code==422  # closed-period gate stays enforced

def test_period_closed_before_approval_is_stale_without_decision(database,api,world,ready):
 from test_period_staleness import close
 a,c,pid,_=ready;api.state.test_database=database;close(api,world);ctr=counters(database)
 r=post(c,pid);assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE'
 assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE' and counters(database)==ctr

@pytest.mark.parametrize('body',[{},[],{'revision':True},{'revision':0},{'revision':1,'comment':'\ud800'},{'revision':1,'comment':'\0'},{'revision':1,'comment':'x'*4001},{'revision':1,'approved_by':'forged'}])
def test_invalid_approval_bodies_never_create_a_decision(database,ready,body):
 _,c,pid,_=ready;r=c.post(f'/api/v1/proposals/{pid}/approve',headers={'Idempotency-Key':uuid4().hex,'If-Match':'"v3"'},content=json.dumps(body));assert r.status_code==422,r.text
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(0,)

def test_key_version_revision_query_and_csrf_requirements(ready):
 _,c,pid,_=ready
 assert post(c,pid,match=None).status_code==428 and post(c,pid,match='"v999"').status_code==412
 revision_refusal=post(c,pid,body={'revision':2});assert revision_refusal.status_code==412 and revision_refusal.json()['reason']=='REVISION_MISMATCH'
 assert c.post(f'/api/v1/proposals/{pid}/approve',json={'revision':1}).status_code==400
 assert c.post(f'/api/v1/proposals/{pid}/approve?extra=1',headers={'Idempotency-Key':uuid4().hex},json={'revision':1}).status_code==400
 del c.headers['X-CSRF-Token'];assert post(c,pid).status_code==403

def test_foreign_and_absent_identifiers_are_indistinguishable(database,api,world,ready):
 _,_,pid,_=ready
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['B'],world['users']['analystB']['id']))
 b=signed_in(api,world,'analystB');assert post(b,pid).status_code==post(b,uuid4()).status_code==404
 assert post(client(api),pid).status_code==401

def test_self_granted_review_is_refused_even_after_cooling(database,world,ready):
 _,c,pid,_=ready
 with connection(database=database) as db:db.execute('UPDATE lv_identity.membership_role SET granted_by=user_id WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 r=post(c,pid);assert r.status_code==403 and r.json()['reason']=='SELF_GRANT'

def test_revocation_race_and_cached_receipt_require_live_review_permission(database,world,ready,monkeypatch):
 _,c,pid,_=ready
 def revoke():
  with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 monkeypatch.setattr(approval,'_before_write',revoke);assert post(c,pid).status_code==403

def test_cached_success_is_not_replayed_to_revoked_reviewer(database,world,ready):
 _,c,pid,_=ready;assert post(c,pid,key='revoke').status_code==200
 with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 assert post(c,pid,key='revoke').status_code==403

def test_force_rls_history_immutability_and_runtime_backstop(database,world,ready):
 _,c,pid,_=ready;assert post(c,pid).status_code==200
 for table in ['approval_check','proposal_decision']:
  with connection(database=database) as db:assert db.execute('SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass',('lv.'+table,)).fetchone()==(True,True)
  with pytest.raises(psycopg.errors.CheckViolation):
   with connection(database=database) as db:db.execute('DELETE FROM lv.'+table)
  with pytest.raises(psycopg.errors.InsufficientPrivilege):
   with connection('lv_app',database) as db:db.execute('DELETE FROM lv.'+table)
  with connection('lv_app',database,autocommit=True) as db:
   with tenant_transaction(db,world['B']):assert db.execute('SELECT count(*) FROM lv.'+table).fetchone()==(0,)

def test_unrelated_sequence_change_is_explicit_capability_refusal_not_global_stale(database,api,world):
 with configuration_connection(database) as db:
  with tenant_transaction(db,world['A']):period=db.execute("INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date) VALUES(%s,%s,'2026-09-01','2026-10-01') RETURNING id",(world['A'],world['l1']['ledger'])).fetchone()[0]
 a,pid,_=submit(api,world);c=signed_in(api,world,'controller');assert override(c,pid).status_code==200;age(database,world)
 r=signed_in(api,world,'admin').post(f'/api/v1/periods/{period}/close',headers={'If-Match':'"v1"','Idempotency-Key':uuid4().hex});assert r.status_code==200
 r=post(c,pid);assert r.status_code==422 and r.json()['reason']=='FULL_CAPTURE_CHANGED'
 assert a.get('/api/v1/proposals/'+pid).json()['state']=='READY_FOR_REVIEW'

@pytest.mark.parametrize('missing',['audit','cache','transition','decision'])
def test_deferred_proof_rejects_missing_atomic_artifact(database,ready,missing):
 _,c,pid,_=ready
 with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv.approve_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text)'::regprocedure)").fetchone()[0]
 if missing=='audit':start=original.index(" PERFORM lv_identity.audit_append(CASE");end=original.index('\n ELSE\n PERFORM lv_identity.audit_append',start)
 elif missing=='cache':start=original.index(' INSERT INTO lv.api_idempotency');end=original.index('\n RETURN response',start)
 elif missing=='decision':start=original.index(' INSERT INTO lv.proposal_decision');end=original.index("\n UPDATE lv.proposal SET state='APPROVED'",start)
 else:start=original.index(' INSERT INTO lv.proposal_transition');end=original.index('\n PERFORM lv_identity.audit_append(CASE',start)
 with connection(database=database) as db:db.execute(original[:start]+original[end:])
 try:assert post(c,pid).status_code==500
 finally:
  with connection(database=database) as db:db.execute(original)
 assert c.get('/api/v1/proposals/'+pid).json()['state']=='READY_FOR_REVIEW'

def test_retained_decision_refuses_downgrade(database,ready):
 _,c,pid,_=ready;assert post(c,pid).status_code==200
 with pytest.raises(Exception):migrate(database,'0018_evidence_overrides',downgrade=True,disposable=True)
 with connection(database=database) as db:assert db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0029_account_patch',)


def test_exact_cached_approval_receipt_is_required_at_commit(database,ready):
 _,c,pid,_=ready
 with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv.approve_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text)'::regprocedure)").fetchone()[0]
 needle="(response->>'status')::smallint,response,transaction_timestamp()"
 injected="(response->>'status')::smallint,CASE WHEN response->>'status'='200' THEN jsonb_set(response,'{body,state}',to_jsonb('READY_FOR_REVIEW'::text)) ELSE response END,transaction_timestamp()"
 assert original.count(needle)==1
 with connection(database=database) as db:db.execute(original.replace(needle,injected))
 try:assert post(c,pid).status_code==500
 finally:
  with connection(database=database) as db:db.execute(original)
 assert c.get('/api/v1/proposals/'+pid).json()['state']=='READY_FOR_REVIEW'
