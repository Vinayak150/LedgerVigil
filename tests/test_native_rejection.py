"""Native PostgreSQL rejection tests: shared decision races, exact reason, rollback and no posting."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import json
import pytest,psycopg
from tools.dev_db import connection,migrate
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.proposals import rejection
from test_proposal_drafts import world,seed,signed_in,client,fingerprint,counters
from test_simulation import api,new_draft
from test_native_approval import ready,age,post as approve
from test_resimulation import change_account
SKIP={'lv.proof_pack','lv.proposal','lv.approval_check','lv.proposal_decision','lv.proposal_transition','lv.api_idempotency','lv.staged_workflow','lv.reservation_event','lv_identity.audit_event','lv_identity.audit_chain_head'}
def post(c,pid,key=None,match='"v3"',body=None):
 h={'Idempotency-Key':key or uuid4().hex}
 if match is not None:h['If-Match']=match
 return c.post(f'/api/v1/proposals/{pid}/reject',headers=h,json={'revision':1,'comment':'Synthetic statement lacks independent evidence.'} if body is None else body)
def test_rejection_exact_reason_release_history_and_no_financial_effect(database,ready):
 a,c,pid,run=ready;before=fingerprint(database,SKIP);ctr=counters(database);comment='  Exact ₹ <img src=x>\nMissing bank confirmation.  '
 r=post(c,pid,body={'revision':1,'comment':comment});assert r.status_code==200,r.text
 assert (r.json()['state'],r.json()['version'],r.json()['allowed_actions'])==('REJECTED',4,[]) and r.headers['etag']=='"v4"'
 assert a.get('/api/v1/simulations/'+run['id']).json()==run
 h=c.get(f'/api/v1/proposals/{pid}/reviews').json();d=h['items'][-1]
 assert h['unsupported_kinds']==[] and d['kind']=='REJECTION' and d['comment']==comment and 'approved_by' not in d
 assert d['rejected_by']!=d['submitted_by'] and d['revision']==1 and d['simulation_id']==run['id']
 with connection(database=database) as db:
  assert db.execute("SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s AND status='RESERVED'",(pid,)).fetchone()==(0,)
  assert db.execute("SELECT count(*) FROM lv.reservation_event WHERE proposal_id=%s AND event='RELEASED'",(pid,)).fetchone()==(1,)
  assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='PROPOSAL_REJECTED' AND detail->>'proposal_id'=%s",(pid,)).fetchone()==(1,)
 assert fingerprint(database,SKIP)==before and counters(database)==ctr
 for action in ['approve','reject']:
  rr=approve(c,pid) if action=='approve' else post(c,pid)
  assert rr.status_code==409 and rr.json()['code']=='ALREADY_DECIDED'
 assert c.post(f'/api/v1/proposals/{pid}/execute',headers={'Idempotency-Key':uuid4().hex,'If-Match':'"v4"'},json={'revision':1}).status_code==409

def test_same_key_retry_and_changed_reason(database,ready):
 _,c,pid,_=ready;r=post(c,pid,key='original');assert r.status_code==200,r.text
 again=post(c,pid,key='original');assert again.json()==r.json() and again.headers['Idempotent-Replayed']=='true'
 bad=post(c,pid,key='original',body={'revision':1,'comment':'Changed'});assert bad.status_code==422 and bad.json()['code']=='IDEMPOTENCY_KEY_REUSED'
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

@pytest.mark.parametrize('body',[{},[],{'revision':1},{'revision':1,'comment':None},{'revision':1,'comment':''},{'revision':1,'comment':' \n\t'},{'revision':1,'comment':'\u00a0\u3000'},{'revision':True,'comment':'reason'},{'revision':0,'comment':'reason'},{'revision':1,'comment':42},{'revision':1,'comment':'\ud800'},{'revision':1,'comment':'\0'},{'revision':1,'comment':'x'*4001},{'revision':1,'comment':'reason','rejected_by':'forged'}])
def test_required_reason_and_strict_body_never_create_decision(database,ready,body):
 _,c,pid,_=ready;r=c.post(f'/api/v1/proposals/{pid}/reject',headers={'Idempotency-Key':uuid4().hex,'If-Match':'"v3"'},content=json.dumps(body));assert r.status_code==422,r.text
 assert c.get('/api/v1/proposals/'+pid).json()['state']=='READY_FOR_REVIEW'
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(0,)

def test_twenty_mixed_approve_reject_requests_share_one_decision(database,ready):
 _,c,pid,_=ready
 with ThreadPoolExecutor(20) as pool:rs=list(pool.map(lambda i:approve(c,pid) if i%2 else post(c,pid),range(20)))
 assert sum(r.status_code==200 for r in rs)==1,[(r.status_code,r.text) for r in rs]
 assert sum(r.status_code==409 and r.json()['code']=='ALREADY_DECIDED' for r in rs)==19
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

def test_concurrent_duplicate_key_creates_one_rejection(database,ready):
 _,c,pid,_=ready
 with ThreadPoolExecutor(12) as pool:rs=list(pool.map(lambda _:post(c,pid,key='same-key'),range(12)))
 assert any(r.status_code==200 for r in rs)
 assert all(r.status_code==200 or (r.status_code==409 and r.json()['code']=='REQUEST_IN_PROGRESS') for r in rs)
 assert post(c,pid,key='same-key').headers['Idempotent-Replayed']=='true'
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

def test_changed_dependency_is_stale_without_false_rejection(database,world,ready):
 a,c,pid,run=ready;change_account(database,world);ctr=counters(database)
 r=post(c,pid,key='stale');assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE',r.text
 assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE'
 assert post(c,pid,key='stale').headers['Idempotent-Replayed']=='true'
 with connection(database=database) as db:
  assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(0,)
  assert db.execute("SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s AND status='RESERVED'",(pid,)).fetchone()==(0,)
 assert a.get('/api/v1/simulations/'+run['id']).json()==run and counters(database)==ctr

def test_permissions_maker_prior_editors_cooling_and_self_grant(database,api,world,ready):
 from test_submission import act
 from test_evidence_overrides import post as override
 _,c,pid,_=ready
 for role in ['analyst','admin','automation']:assert post(signed_in(api,world,role),pid).status_code==403
 age(database,world,hours=1);assert post(c,pid).json()['reason']=='REVIEW_COOLING_OFF';age(database,world)
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['A'],world['users']['analyst']['id']))
 assert post(signed_in(api,world,'analyst'),pid).json()['reason']=='MAKER_OR_EDITOR'
 a=signed_in(api,world,'analyst');other=new_draft(a,world);editor=signed_in(api,world,'automation');assert editor.patch('/api/v1/proposals/'+other,headers={'If-Match':'"v1"'},json={'rationale':'SYNTHETIC editor'}).status_code==200
 assert act(a,pid,'withdraw',match='"v3"').status_code==200 and act(a,other,match='"v2"').status_code==200 and override(c,other,match='"v3"').status_code==200
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['A'],world['users']['automation']['id']))
 assert post(signed_in(api,world,'automation'),other,match='"v4"',body={'revision':2,'comment':'reason'}).json()['reason']=='MAKER_OR_EDITOR'
 with connection(database=database) as db:db.execute('UPDATE lv_identity.membership_role SET granted_by=user_id WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 assert post(c,other,match='"v4"',body={'revision':2,'comment':'reason'}).json()['reason']=='SELF_GRANT'

def test_revocation_before_write_and_after_cached_success(database,world,ready,monkeypatch):
 _,c,pid,_=ready
 def revoke():
  with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 with monkeypatch.context() as m:m.setattr(rejection,'_before_write',revoke);assert post(c,pid).status_code==403

def test_cached_success_refused_after_membership_revocation(database,world,ready):
 _,c,pid,_=ready;assert post(c,pid,key='revoke').status_code==200
 with connection(database=database) as db:db.execute("UPDATE lv_identity.membership SET status='REVOKED',revoked_at=statement_timestamp() WHERE tenant_id=%s AND user_id=%s",(world['A'],world['users']['controller']['id']))
 assert post(c,pid,key='revoke').status_code==401

def test_version_revision_query_origin_and_csrf(ready):
 _,c,pid,_=ready
 assert post(c,pid,match=None).status_code==428 and post(c,pid,match='"v999"').status_code==412
 assert post(c,pid,body={'revision':2,'comment':'reason'}).status_code==412
 assert c.post(f'/api/v1/proposals/{pid}/reject',json={'revision':1,'comment':'reason'}).status_code==400
 assert c.post(f'/api/v1/proposals/{pid}/reject?extra=1',headers={'Idempotency-Key':uuid4().hex},json={'revision':1,'comment':'reason'}).status_code==400
 c.headers['Origin']='https://foreign.invalid';assert post(c,pid).status_code==403;c.headers['Origin']='https://testserver';del c.headers['X-CSRF-Token'];assert post(c,pid).status_code==403

def test_absent_foreign_anonymous_and_read_isolation(database,api,world,ready):
 _,c,pid,_=ready;assert post(c,pid).status_code==200
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['B'],world['users']['analystB']['id']))
 b=signed_in(api,world,'analystB');assert post(b,pid).status_code==post(b,uuid4()).status_code==404
 assert b.get(f'/api/v1/proposals/{pid}/reviews').status_code==404 and post(client(api),pid).status_code==401
 with connection('lv_app',database,autocommit=True) as db:
  with tenant_transaction(db,world['B']):assert db.execute('SELECT count(*) FROM lv.proposal_decision').fetchone()==(0,)

def test_rollback_raw_transition_and_immutable_reason(database,ready,monkeypatch):
 _,c,pid,_=ready;before=fingerprint(database,set())
 def fail(_):raise RuntimeError('injected reject rollback')
 with monkeypatch.context() as m:m.setattr(rejection,'_after_write',fail);assert post(c,pid).status_code==500
 assert fingerprint(database,set())==before
 with pytest.raises(psycopg.errors.CheckViolation):
  with connection(database=database) as db:
   db.execute("SELECT set_config('app.tenant_id',(SELECT tenant_id::text FROM lv.proposal WHERE id=%s),true)",(pid,));db.execute("UPDATE lv.proposal SET state='REJECTED',version=version+1 WHERE id=%s",(pid,))
 assert post(c,pid).status_code==200
 for query in ["UPDATE lv.proposal_decision SET comment='altered' WHERE proposal_id=%s","DELETE FROM lv.proposal_decision WHERE proposal_id=%s"]:
  with pytest.raises(psycopg.errors.CheckViolation):
   with connection(database=database) as db:db.execute(query,(pid,))
  with pytest.raises(psycopg.errors.InsufficientPrivilege):
   with connection('lv_app',database) as db:db.execute(query,(pid,))

@pytest.mark.parametrize('missing',['audit','cache','transition','decision','release'])
def test_deferred_guards_detect_missing_atomic_artifact(database,ready,missing):
 _,c,pid,_=ready
 with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv.reject_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text)'::regprocedure)").fetchone()[0]
 if missing=='audit':start=original.index(' PERFORM lv_identity.audit_append(CASE');end=original.index('\n ELSE\n PERFORM lv_identity.audit_append',start)
 elif missing=='cache':start=original.index(' INSERT INTO lv.api_idempotency');end=original.index('\n RETURN response',start)
 elif missing=='decision':start=original.index(' INSERT INTO lv.proposal_decision');end=original.index("\n UPDATE lv.proposal SET state='REJECTED'",start)
 elif missing=='release':start=original.index(' WITH released AS',original.index("\n UPDATE lv.proposal SET state='REJECTED'"));end=original.index('\n response:=',start)
 else:start=original.index(' INSERT INTO lv.proposal_transition');end=original.index('\n PERFORM lv_identity.audit_append(CASE',start)
 with connection(database=database) as db:db.execute(original[:start]+original[end:])
 try:assert post(c,pid).status_code==500
 finally:
  with connection(database=database) as db:db.execute(original)
 assert c.get('/api/v1/proposals/'+pid).json()['state']=='READY_FOR_REVIEW'

def test_downgrade_refuses_retained_rejection(database,ready):
 _,c,pid,_=ready;assert post(c,pid).status_code==200
 with pytest.raises(Exception):migrate(database,'0019_native_approval',downgrade=True,disposable=True)
 with connection(database=database) as db:assert db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0038_identity_issuer',)


@pytest.mark.parametrize('comment',['', ' \n\t', '\u00a0\u3000', '\u001c\u0085', 'x'*4001])
def test_database_rechecks_reason_if_api_validation_is_bypassed(database,ready,monkeypatch,comment):
 _,c,pid,_=ready
 monkeypatch.setattr(rejection,'parse',lambda raw:({'revision':1,'comment':comment},'0'*64,None))
 r=post(c,pid);assert r.status_code==422 and r.json()['code']=='SCHEMA_INVALID'
 assert c.get('/api/v1/proposals/'+pid).json()['state']=='READY_FOR_REVIEW'

def test_rejected_proposal_is_terminal_but_released_statement_can_be_proposed_again(ready,world):
 from test_submission import act
 a,c,pid,_=ready;assert post(c,pid).status_code==200
 assert act(a,pid,'withdraw',match='"v4"').status_code==409
 assert act(a,pid,'resimulate',match='"v4"').status_code==409
 other=new_draft(a,world);r=act(a,other,match='"v1"');assert r.status_code==200,r.text
 assert r.json()['proposal']['state']=='BLOCKED' and c.get('/api/v1/proposals/'+pid).json()['state']=='REJECTED'
