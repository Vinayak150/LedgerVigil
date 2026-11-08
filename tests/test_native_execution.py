from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest
from tools.dev_db import connection
from test_proposal_drafts import world,seed,signed_in,fingerprint,counters
from test_simulation import api
from test_native_approval import ready,post as approve

def post(c,pid,key=None,match='"v4"',revision=1):return c.post(f'/api/v1/proposals/{pid}/execute',headers={'Idempotency-Key':key or uuid4().hex,'If-Match':match},json={'revision':revision})
@pytest.fixture
def approved(ready):
 a,c,pid,run=ready;r=approve(c,pid);assert r.status_code==200,r.text;return a,c,pid,run

def test_native_execution_commits_once_and_replays(database,world,approved):
 a,c,pid,run=approved;before=counters(database);r=post(c,pid,key='execute-once');assert r.status_code==200,r.text
 b=r.json();assert b['state']=='RECONCILED' and b['proposal_state']=='EXECUTED' and b['comparison']['comparison']=='MATCHED'
 assert c.get('/api/v1/executions/'+b['id']).json()==b
 again=post(c,pid,key='execute-once');assert again.status_code==200 and again.json()==b and again.headers['Idempotent-Replayed']=='true'
 assert post(c,pid).status_code==409
 assert a.get('/api/v1/proposals/'+pid).json()['state']=='EXECUTED'
 assert a.get('/api/v1/simulations/'+run['id']).json()==run
 with connection(database=database) as db:
  assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(1,)
  assert db.execute("SELECT count(*) FROM lv.commit_batch WHERE tenant_id=%s AND ledger_id=%s AND kind::text='EXECUTION'",(world['A'],world['l1']['ledger'])).fetchone()==(1,)

def test_twenty_executors_produce_one_financial_effect(database,approved):
 _,c,pid,_=approved
 with ThreadPoolExecutor(20) as pool:rs=list(pool.map(lambda _:post(c,pid),range(20)))
 assert sum(r.status_code==200 for r in rs)==1,[(r.status_code,r.text) for r in rs]
 assert sum(r.status_code==409 for r in rs)==19
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

def test_concurrent_same_key_retries_are_replayed_without_double_posting(database,approved):
 _,c,pid,_=approved
 with ThreadPoolExecutor(12) as pool:rs=list(pool.map(lambda _:post(c,pid,key='same'),range(12)))
 assert any(r.status_code==200 for r in rs)
 assert all(r.status_code==200 or (r.status_code==409 and r.json()['code']=='REQUEST_IN_PROGRESS') for r in rs)
 assert post(c,pid,key='same').headers['Idempotent-Replayed']=='true'
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

def test_execution_requires_approval_permissions_independence_and_cooling(database,api,world,ready):
 from test_native_approval import age
 _,c,pid,_=ready
 assert post(c,pid,match='"v3"').status_code==409
 assert approve(c,pid).status_code==200
 for actor in ['analyst','admin','automation']:assert post(signed_in(api,world,actor),pid).status_code==403
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['A'],world['users']['analyst']['id']))
 assert post(signed_in(api,world,'analyst'),pid).json()['reason']=='MAKER'
 age(database,world,hours=1);assert post(c,pid).json()['reason']=='EXECUTE_COOLING_OFF'
 age(database,world)
 with connection(database=database) as db:db.execute('UPDATE lv_identity.membership_role SET granted_by=user_id WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 assert post(c,pid).json()['reason']=='SELF_GRANT'

def test_dependency_change_stales_without_posting(database,world,approved):
 from test_resimulation import change_account
 a,c,pid,_=approved;change_account(database,world);before=counters(database)
 r=post(c,pid,key='stale');assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE',r.text
 assert post(c,pid,key='stale').headers['Idempotent-Replayed']=='true'
 assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE' and counters(database)==before
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(0,)

def test_exact_financial_changes_human_attribution_replay_and_outbox(database,world,approved):
 from ledgervigil.ledger.replay import replay_events
 _,c,pid,_=approved;lid=world['l1']['ledger']
 with connection(database=database) as db:old=db.execute('SELECT last_seq,last_entry_no FROM lv.ledger_counter WHERE tenant_id=%s AND ledger_id=%s',(world['A'],lid)).fetchone()
 r=post(c,pid);assert r.status_code==200,r.text;b=r.json()
 with connection(database=database) as db:
  assert db.execute('SELECT last_seq,last_entry_no FROM lv.ledger_counter WHERE tenant_id=%s AND ledger_id=%s',(world['A'],lid)).fetchone()==(old[0]+1,old[1]+1)
  assert db.execute('SELECT kind::text,created_by,created_by_user::text FROM lv.commit_batch WHERE tenant_id=%s AND ledger_id=%s AND seq=%s',(world['A'],lid,b['commit_seq'])).fetchone()==('EXECUTION',None,world['users']['controller']['id'])
  lines=db.execute('SELECT a.code,j.side,j.amount FROM lv.journal_line j JOIN lv.account a ON(a.tenant_id,a.id)=(j.tenant_id,j.account_id) WHERE j.entry_id=%s ORDER BY j.line_no',(b['entry_id'],)).fetchall()
  assert [(s,str(v)) for _,s,v in lines]==[('D','51500.5000'),('D','1000.0000'),('C','52500.5000')]
  assert db.execute("SELECT status,proposal_id::text FROM lv.staged_workflow WHERE proposal_id=%s",(pid,)).fetchone()==('POSTED',pid)
  assert db.execute("SELECT topic,status,work_item_id,native_payload FROM lv.outbox WHERE native_execution_id=%s",(b['id'],)).fetchone()==('execution','PENDING',None,b)
  rows=db.execute('SELECT seq,previous_hash,hash,canonical_content FROM lv.ledger_event WHERE tenant_id=%s AND ledger_id=%s ORDER BY seq',(world['A'],lid)).fetchall()
  events=[dict(zip(['seq','previous_hash','hash','canonical_content'],row)) for row in rows];replayed=replay_events(events)
  assert replayed['head']==db.execute('SELECT event_head FROM lv.ledger_counter WHERE tenant_id=%s AND ledger_id=%s',(world['A'],lid)).fetchone()[0]

def test_revocation_before_write_and_after_cached_receipt(database,world,approved,monkeypatch):
 from ledgervigil.execution import routes
 _,c,pid,_=approved
 def revoke():
  with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 with monkeypatch.context() as m:m.setattr(routes,'_before_write',revoke);assert post(c,pid).status_code==403

def test_cached_success_requires_current_permission(database,world,approved):
 _,c,pid,_=approved;assert post(c,pid,key='revoked').status_code==200
 with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 assert post(c,pid,key='revoked').status_code==403

@pytest.mark.parametrize('body',[{},[],{'revision':True},{'revision':0},{'revision':1,'actor_id':'forged'}])
def test_strict_execution_body(database,approved,body):
 _,c,pid,_=approved;before=counters(database);r=c.post(f'/api/v1/proposals/{pid}/execute',headers={'If-Match':'"v4"','Idempotency-Key':uuid4().hex},json=body);assert r.status_code==422
 assert counters(database)==before

@pytest.mark.parametrize('missing',['audit','cache','transition','outbox','posted_claims','posting_match'])
def test_missing_atomic_artifact_rolls_back_financial_effect(database,approved,missing):
 _,c,pid,_=approved;before=counters(database);tables=fingerprint(database,{'lv.native_execution_incident','lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})
 with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv.execute_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text)'::regprocedure)").fetchone()[0]
 if missing=='audit':a=original.index(' PERFORM lv_identity.audit_append(CASE');b=original.index('\n ELSE\n PERFORM lv_identity.audit_append',a)
 elif missing=='cache':a=original.index(' INSERT INTO lv.api_idempotency');b=original.index('\n RETURN response',a)
 elif missing=='transition':a=original.index(' INSERT INTO lv.proposal_transition');b=original.index('\n PERFORM lv_identity.audit_append(CASE',a)
 elif missing=='outbox':a=original.index(' INSERT INTO lv.outbox');b=original.index('\n response:=',a)
 elif missing=='posted_claims':a=original.index(' WITH completed AS');b=original.index('\n INSERT INTO lv.outbox',a)
 else:a=original.index(' comparison:=lv.native_posting_match');b=original.index('\n UPDATE lv.proposal',a)
 replacement=" comparison:='{}'::jsonb;\n" if missing=='posting_match' else ''
 with connection(database=database) as db:db.execute(original[:a]+replacement+original[b:])
 try:
  r=post(c,pid);assert r.status_code==500 and not r.json()['retryable'],r.text
 finally:
  with connection(database=database) as db:db.execute(original)
 assert counters(database)==before and c.get('/api/v1/proposals/'+pid).json()['state']=='APPROVED'
 assert fingerprint(database,{'lv.native_execution_incident','lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})==tables
 with connection(database=database) as db:
  assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(0,)
  assert db.execute('SELECT count(*) FROM lv.native_execution_incident WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

def test_closed_period_before_execute_is_stale(database,api,world,approved):
 from test_period_staleness import close
 _,c,pid,_=approved;api.state.test_database=database;close(api,world);before=counters(database);r=post(c,pid)
 assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE' and counters(database)==before

def test_foreign_absent_and_anonymous_cannot_execute(database,api,world,approved):
 from test_proposal_drafts import client
 _,_,pid,_=approved
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['B'],world['users']['analystB']['id']))
 c=signed_in(api,world,'analystB');assert post(c,pid).status_code==post(c,uuid4()).status_code==404
 assert post(client(api),pid).status_code==401

def test_runtime_cannot_call_private_writer_or_mutate_execution_history(database,approved):
 import psycopg
 _,c,pid,_=approved;r=post(c,pid);assert r.status_code==200;r=r.json()
 for query,args in [('SELECT lv.post_native_settlement(%s)',(r['id'],)),('DELETE FROM lv.native_execution WHERE id=%s',(r['id'],)),('UPDATE lv.document_version SET created_by_user=NULL WHERE document_id=%s',(r['settlement_document_id'],))]:
  with pytest.raises(psycopg.errors.InsufficientPrivilege):
   with connection('lv_app',database) as db:db.execute(query,args)
 with pytest.raises(psycopg.errors.CheckViolation):
  with connection(database=database) as db:db.execute('DELETE FROM lv.native_execution WHERE id=%s',(r['id'],))

def _wait_for_lock(database,pid):
 import time
 deadline=time.monotonic()+5
 with connection(database=database,autocommit=True) as db:
  while time.monotonic()<deadline:
   row=db.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(pid,)).fetchone()
   if row and row[0]=='Lock':return
   time.sleep(.01)
 raise AssertionError('expected database lock wait was not observed')

def test_conflicting_import_commits_before_execute_and_forces_stale(database,api,world,approved):
 from ledgervigil.db.tenant import tenant_transaction
 from ledgervigil.ingestion.store import _ingest
 from ledgervigil.ingestion import statement_format as fmt
 from test_statement_ingestion import statement
 import threading
 _,c,pid,_=approved;cfg=world['l1'];raw=statement(cfg,fee='2000.00',net='48000.00');before=counters(database)
 # A real import transaction holds the shared fence until its conflict is durable.
 with ThreadPoolExecutor(1) as pool:
  with connection('lv_ingest',database,autocommit=True) as ing:
   with tenant_transaction(ing,world['A']):
    result=_ingest(ing,world['A'],cfg['ledger'],raw,fmt.FORMAT,fmt.VERSION);assert result['status']=='REJECTED'
    future=pool.submit(post,c,pid)
    import time
    deadline=time.monotonic()+5
    with connection(database=database,autocommit=True) as db:
     while time.monotonic()<deadline:
      rows=db.execute("SELECT pid FROM pg_stat_activity WHERE datname=%s AND wait_event_type='Lock' AND query LIKE 'SELECT lv.execute_native%%'",(database,)).fetchall()
      if rows:break
      time.sleep(.01)
     assert rows,'execute did not wait for in-flight ingestion'
   r=future.result(timeout=8)
 assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE',r.text
 assert counters(database)==before

def test_import_waits_for_atomic_execution_commit(database,world,approved,monkeypatch):
 from ledgervigil.execution import routes
 from ledgervigil.ingestion.store import ingest_statement
 from ledgervigil.ingestion import statement_format as fmt
 from test_statement_ingestion import statement
 import threading
 _,c,pid,_=approved;locked=threading.Event();release=threading.Event();started=threading.Event();pid_holder=[]
 def pause(_):locked.set();assert release.wait(8)
 def importer():
  with connection('lv_ingest',database,autocommit=True) as db:
   pid_holder.append(db.info.backend_pid);started.set()
   return ingest_statement(db,world['A'],world['l1']['ledger'],statement(world['l1'],fee='2000.00',net='48000.00'),declared_format=fmt.FORMAT,declared_version=fmt.VERSION)
 with monkeypatch.context() as m,ThreadPoolExecutor(2) as pool:
  m.setattr(routes,'_after_write',pause);execution=pool.submit(post,c,pid)
  try:
   assert locked.wait(5);imp=pool.submit(importer);assert started.wait(5);_wait_for_lock(database,pid_holder[0]);assert not imp.done()
  finally:release.set()
  r=execution.result(timeout=10);assert r.status_code==200,r.text;assert imp.result(timeout=10)['status']=='REJECTED'
 assert c.get('/api/v1/proposals/'+pid).json()['state']=='EXECUTED'

@pytest.mark.parametrize('fault,expected',[('balanced_wrong_account','typed settlement, versions, inclusions, projections and postings diverge'),('wrong_approved_request','new documents do not implement the approved operation'),('wrong_rule_version','unsupported native posting proof')])
def test_valid_sql_faults_are_caught_by_financial_guards_not_setup_errors(database,world,approved,monkeypatch,fault,expected):
 from ledgervigil.execution import routes
 import psycopg
 _,c,pid,_=approved;seen=[];tables=fingerprint(database,{'lv.native_execution_incident','lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})
 with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv.post_native_settlement(uuid)'::regprocedure)").fetchone()[0]
 if fault=='balanced_wrong_account':
  needle="line_counter, next_seq, clr.id, clr.requires_party, NULL, doc_id, 'C'"
  replacement="line_counter, next_seq, (SELECT account_id FROM lv.account_role_binding WHERE tenant_id=t AND ledger_id=p_ledger AND role='REVENUE_DEFAULT'), false, NULL, doc_id, 'C'"
 elif fault=='wrong_approved_request':
  needle=" gross_value := lv.parse_line_amount"
  replacement=" p_request:=jsonb_set(p_request,'{payout_date}',to_jsonb('2026-10-15'::text));\n gross_value := lv.parse_line_amount"
 else:
  needle="doc_id, 1, 'sp1-posting', '1.0', 'SYNTHETIC'"
  replacement="doc_id, 1, 'sp1-posting', '9.9', 'SYNTHETIC'"
 assert original.count(needle)==1
 with connection(database=database) as db:db.execute(original.replace(needle,replacement))
 try:
  with monkeypatch.context() as m:
   m.setattr(routes,'_on_defect',seen.append);r=post(c,pid);assert r.status_code==500 and not r.json()['retryable'],r.text
 finally:
  with connection(database=database) as db:db.execute(original)
 assert len(seen)==1 and isinstance(seen[0],psycopg.errors.CheckViolation),[type(e).__name__ for e in seen]
 assert seen[0].diag.message_primary==expected
 assert fingerprint(database,{'lv.native_execution_incident','lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})==tables
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.native_execution_incident WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

@pytest.mark.parametrize('fee,net',[('0.00','52500.50'),('52500.50','0.00')])
def test_zero_fee_or_zero_net_omits_only_the_zero_line(database,api,world,fee,net):
 from test_proposal_drafts import _statement,draft,settlement
 from test_submission import act
 from test_evidence_overrides import post as override
 from test_native_approval import age
 local=dict(world);payout='ZERO-'+fee.replace('.','-');st=_statement(database,world['l1'],payout,[('PMT-2001','50000.00'),('PMT-2002','2500.50')],'52500.50',fee,net);local['s1'],local['r1']=st['staged_document_id'],st['source_record_id']
 a=signed_in(api,world,'analyst');c=signed_in(api,world,'controller');r=a.post('/api/v1/proposals',headers={'Idempotency-Key':uuid4().hex},json=draft(local,operations=[settlement(local,payout=payout,fee=fee,net=net)]));assert r.status_code==201,r.text;pid=r.json()['id']
 assert act(a,pid).status_code==200;assert override(c,pid).status_code==200;age(database,world);assert approve(c,pid).status_code==200
 r=post(c,pid);assert r.status_code==200,r.text;assert r.json()['comparison']['line_count']==2

def test_period_close_racing_execution_has_a_serial_outcome(database,api,world,approved):
 from test_period_staleness import close,drain
 a,c,pid,_=approved;api.state.test_database=database
 with ThreadPoolExecutor(2) as pool:
  execution=pool.submit(post,c,pid);closing=pool.submit(close,api,world);r=execution.result(timeout=10);job=closing.result(timeout=10)
 assert r.status_code in (200,409),r.text
 assert drain(database,world['A'],job)['status']=='DONE'
 head=a.get('/api/v1/proposals/'+pid).json()
 assert head['state']==('EXECUTED' if r.status_code==200 else 'STALE')
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(int(r.status_code==200),)

def test_execution_reads_support_conditional_validation_only_after_authorization(database,api,world,approved):
 _,c,pid,_=approved;r=post(c,pid);assert r.status_code==200;eid=r.json()['id'];read=c.get('/api/v1/executions/'+eid);assert read.status_code==200 and read.headers['etag'].startswith('"r')
 assert c.get('/api/v1/executions/'+eid,headers={'If-None-Match':read.headers['etag']}).status_code==304
 b=signed_in(api,world,'analystB');assert b.get('/api/v1/executions/'+eid,headers={'If-None-Match':read.headers['etag']}).status_code==404

def test_late_statement_child_insert_is_refused_even_by_ingest_login(database,world,approved):
 import psycopg
 from ledgervigil.db.tenant import tenant_transaction
 before=fingerprint(database,set())
 with pytest.raises(psycopg.errors.CheckViolation,match='cannot append to a committed batch'):
  with connection('lv_ingest',database,autocommit=True) as db:
   with tenant_transaction(db,world['A']):db.execute("INSERT INTO lv.staged_statement_item(tenant_id,ledger_id,staged_document_id,line_no,item_ref,kind,amount,currency_exponent) VALUES(%s,%s,%s,99,'LATE-CHILD','PAYMENT','1.00',2)",(world['A'],world['l1']['ledger'],world['s1']))
 assert fingerprint(database,set())==before

def test_ingestion_lock_helper_does_not_grant_runtime_access(database):
 import psycopg
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  with connection('lv_app',database) as db:db.execute('SELECT * FROM lv.ingestion_ledger(%s)',(uuid4(),))

@pytest.mark.parametrize('committed',[False,True])
def test_commit_acknowledgement_loss_never_claims_rollback_or_duplicates(database,approved,monkeypatch,committed):
 from contextlib import contextmanager
 import psycopg
 from ledgervigil.execution import routes
 _,c,pid,_=approved;original=routes.session_transaction;first=True
 @contextmanager
 def interrupted(*args,**kwargs):
  nonlocal first
  if not first:
   with original(*args,**kwargs) as ctx:yield ctx
   return
  first=False
  with original(*args,**kwargs) as ctx:
   yield ctx
   if not committed:raise psycopg.OperationalError('controlled transport interruption before commit')
  raise psycopg.OperationalError('controlled acknowledgement loss after commit')
 with monkeypatch.context() as m:
  m.setattr(routes,'session_transaction',interrupted);r=post(c,pid,key='commit-ack-lost')
 assert r.status_code==500 and r.json()['reason']=='NATIVE_EXECUTION_OUTCOME_UNKNOWN' and not r.json()['retryable'],r.text
 assert 'no financial changes committed' not in r.json()['detail'].lower()
 assert c.get('/api/v1/proposals/'+pid).json()['state']==('EXECUTED' if committed else 'APPROVED')
 retry=post(c,pid,key='commit-ack-lost');assert retry.status_code==200,retry.text
 assert (retry.headers.get('Idempotent-Replayed')=='true')==committed
 with connection(database=database) as db:
  assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(1,)

def test_receipt_lookup_survives_cache_expiry_and_cannot_post(database,api,world,approved):
 a,c,pid,_=approved;url='/api/v1/proposals/'+pid+'/execution'
 assert a.get(url).status_code==404
 r=post(c,pid,key='receipt-lookup');assert r.status_code==200;receipt=r.json()
 with connection(database=database) as db:db.execute("DELETE FROM lv.api_idempotency WHERE route='/api/v1/proposals/{proposal_id}/execute'")
 before=fingerprint(database,{'lv_identity.session'});ctr=counters(database)
 read=a.get(url);assert read.status_code==200 and read.json()==receipt
 assert a.get(url,headers={'If-None-Match':read.headers['etag']}).status_code==304
 assert a.get(url+'?revision=1').status_code==400
 assert counters(database)==ctr and fingerprint(database,{'lv_identity.session'})==before

def test_receipt_lookup_auth_tenant_masking_and_revocation(database,api,world,approved):
 from test_proposal_drafts import client
 _,c,pid,_=approved;assert post(c,pid).status_code==200;url='/api/v1/proposals/'+pid+'/execution'
 b=signed_in(api,world,'analystB');foreign=b.get(url);missing=b.get('/api/v1/proposals/'+str(uuid4())+'/execution')
 assert foreign.status_code==missing.status_code==404
 assert {k:v for k,v in foreign.json().items() if k!='request_id'}=={k:v for k,v in missing.json().items() if k!='request_id'}
 assert client(api).get(url).status_code==401
 etag=c.get(url).headers['etag']
 with connection(database=database) as db:db.execute("UPDATE lv_identity.membership SET status='REVOKED',revoked_at=statement_timestamp() WHERE tenant_id=%s AND user_id=%s",(world['A'],world['users']['controller']['id']))
 assert c.get(url,headers={'If-None-Match':etag}).status_code==401
