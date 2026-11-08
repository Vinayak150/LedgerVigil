"""Public native freshness and immutable re-simulation, using real PostgreSQL."""
from uuid import uuid4
import pytest
import psycopg
from configuration_support import configuration_connection
from tools.dev_db import connection
from ledgervigil.db.tenant import tenant_transaction
from test_proposal_drafts import world,seed,signed_in,client,problem,fingerprint,counters  # noqa:F401
from test_simulation import api,new_draft  # noqa:F401
from test_submission import act

EXCLUDE={'lv.proof_pack','lv.proposal','lv.proposal_revision','lv.proposal_reference','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head','lv.proposal_transition','lv.staged_workflow','lv.reservation_event','lv.proposal_freshness_check','lv.simulation_run','lv.ledger_snapshot','lv.proposal_read_set','lv.verdict'}

def submit(api,w):
 a=signed_in(api,w,'analyst');pid=new_draft(a,w);r=act(a,pid);assert r.status_code==200,r.text
 return a,pid,r.json()['simulation']

def change_account(database,w):
 with configuration_connection(database) as c:
  with tenant_transaction(c,w['A']):c.execute('UPDATE lv.account SET name=name WHERE id=%s',(w['l1']['chart']['BANK_OPERATING'],))

def make_stale(database,api,w):
 a,pid,old=submit(api,w);change_account(database,w);r=act(a,pid,'revalidate',match='"v2"');assert r.status_code==200,r.text
 assert r.json()['proposal']['state']=='STALE',r.text
 return a,pid,old

def test_fresh_check_retains_state_and_records_explicit_point_in_time_receipt(database,api,world):
 a,pid,_=submit(api,world);before=fingerprint(database,EXCLUDE);ctr=counters(database)
 r=act(a,pid,'revalidate',match='"v2"');assert r.status_code==200,r.text
 assert r.json()['freshness']['status']=='FRESH' and r.json()['proposal']['version']==2 and r.headers['etag']=='"v2"'
 assert r.json()['proposal']['state']=='BLOCKED' and r.json()['freshness']['changes']==[]
 assert fingerprint(database,EXCLUDE)==before and counters(database)==ctr

def test_resimulation_creates_new_revision_and_preserves_original_run_and_money(database,api,world):
 a,pid,old=make_stale(database,api,world);before=fingerprint(database,EXCLUDE);ctr=counters(database)
 key=uuid4().hex;r=act(a,pid,'resimulate',match='"v3"',key=key);assert r.status_code==200,r.text
 p,s=r.json()['proposal'],r.json()['simulation'];assert (p['state'],p['revision'],p['version'])==('BLOCKED',2,4)
 assert s['revision']==2 and s['id']!=old['id'] and s['outcome']['aggregate']=='BLOCKED_INSUFFICIENT'
 assert a.get('/api/v1/simulations/'+old['id']).json()==old
 assert a.post('/api/v1/simulations/'+old['id']+'/replay').json()['identical']
 retry=act(a,pid,'resimulate',match='"v3"',key=key);assert retry.status_code==200 and retry.json()==r.json() and retry.headers['Idempotent-Replayed']=='true'
 history=a.get('/api/v1/proposals/'+pid+'/revisions').json()['items'];assert [h['change'] for h in history]==['CREATED','RESIMULATED']
 assert history[0]['content_hash']==history[1]['content_hash'] and history[0]['operations']==history[1]['operations']
 assert fingerprint(database,EXCLUDE)==before and counters(database)==ctr
 with connection(database=database) as c:
  assert c.execute('SELECT status,revision FROM lv.staged_workflow WHERE proposal_id=%s',(pid,)).fetchone()==('RESERVED',2)


def test_changed_statement_conflict_is_seen_without_a_sequence_change(database,api,world):
 import json
 from ledgervigil.ingestion.store import ingest_statement
 from ledgervigil.ingestion import statement_format as fmt
 a,pid,_=submit(api,world);ctr=counters(database)
 with connection(database=database) as c:
  raw=c.execute('SELECT f.content FROM lv.staged_document s JOIN lv.import_batch b ON b.id=s.import_batch_id JOIN lv.source_file f ON f.id=b.source_file_id WHERE s.id=%s',(world['s1'],)).fetchone()[0]
 data=json.loads(bytes(raw));data['statement'].update(fee='1001.00',net='51499.50')
 with connection('lv_ingest',database,autocommit=True) as c:result=ingest_statement(c,world['A'],world['l1']['ledger'],json.dumps(data).encode(),declared_format=fmt.FORMAT,declared_version=fmt.VERSION)
 assert result['status']=='REJECTED' and counters(database)==ctr
 r=act(a,pid,'revalidate',match='"v2"');assert r.status_code==200,r.text
 assert r.json()['freshness']['status']=='STALE' and any(v['reason']=='STATEMENT_CONFLICT_RANGE_CHANGED' for v in r.json()['freshness']['changes'])


def test_unrelated_period_sequence_change_does_not_globally_invalidate(database,api,world):
 with configuration_connection(database) as c:
  with tenant_transaction(c,world['A']):period=c.execute("INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date) VALUES(%s,%s,'2026-09-01','2026-10-01') RETURNING id",(world['A'],world['l1']['ledger'])).fetchone()[0]
 a,pid,_=submit(api,world);admin=signed_in(api,world,'admin')
 r=admin.post(f'/api/v1/periods/{period}/close',headers={'If-Match':'"v1"','Idempotency-Key':uuid4().hex});assert r.status_code==200,r.text
 r=act(a,pid,'revalidate',match='"v2"');assert r.status_code==200,r.text
 assert r.json()['freshness']['status']=='FRESH' and r.json()['freshness']['observed_seq']>r.json()['freshness']['pinned_seq']


def test_closed_period_refuses_resimulation_without_new_revision_or_claim(database,api,world):
 from test_period_staleness import close,drain
 a,pid,old=submit(api,world);api.state.test_database=database;j=close(api,world);assert drain(database,world['A'],j)['status']=='DONE'
 r=act(a,pid,'resimulate',match='"v3"');assert r.status_code==422,r.text
 p=a.get('/api/v1/proposals/'+pid).json();assert (p['state'],p['revision'],p['version'])==('STALE',1,3)
 assert a.get('/api/v1/simulations/'+old['id']).json()==old
 with connection(database=database) as c:
  assert c.execute('SELECT count(*) FROM lv.proposal_revision WHERE proposal_id=%s',(pid,)).fetchone()[0]==1
  assert c.execute('SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s',(pid,)).fetchone()[0]==0

@pytest.mark.parametrize('action,state,match',[('revalidate','BLOCKED','"v2"'),('resimulate','STALE','"v3"')])
def test_auth_permission_and_foreign_ids_are_refused(database,api,world,action,state,match):
 a,pid,_=make_stale(database,api,world) if state=='STALE' else submit(api,world)
 assert act(client(api),pid,action,match=match).status_code==401
 for user in ['admin','controller','automation']:
  assert act(signed_in(api,world,user),pid,action,match=match).status_code==403
 b=signed_in(api,world,'analystB')
 assert act(b,pid,action,match=match).status_code==404
 assert act(b,str(uuid4()),action,match=match).status_code==404
 assert a.get('/api/v1/proposals/'+pid).json()['state']==state

@pytest.mark.parametrize('action,match',[('revalidate','"v2"'),('resimulate','"v3"')])
def test_required_version_body_and_idempotency_are_enforced(database,api,world,action,match):
 a,pid,_=make_stale(database,api,world) if action=='resimulate' else submit(api,world)
 assert act(a,pid,action,match=None).status_code==428
 assert act(a,pid,action,match='"v99"').status_code==412
 assert a.post(f'/api/v1/proposals/{pid}/{action}',headers={'If-Match':match}).status_code==400
 assert act(a,pid,action,match=match,json={'state':'READY_FOR_REVIEW'}).status_code==422
 assert act(a,pid,action,match=match,params={'unknown':'1'}).status_code==400


def test_freshness_replay_is_point_in_time_and_does_not_recheck(database,api,world):
 a,pid,_=submit(api,world);key=uuid4().hex
 old=act(a,pid,'revalidate',match='"v2"',key=key);assert old.json()['freshness']['status']=='FRESH'
 change_account(database,world)
 retry=act(a,pid,'revalidate',match='"v2"',key=key);assert retry.status_code==200 and retry.json()==old.json() and retry.headers['Idempotent-Replayed']=='true'
 assert act(a,pid,'revalidate',match='"v2"',key=key,json={'x':1}).status_code==422
 new=act(a,pid,'revalidate',match='"v2"');assert new.json()['freshness']['status']=='STALE'


def test_wrong_state_cannot_resimulate_or_edit_submitted_content(database,api,world):
 a,pid,_=submit(api,world)
 assert act(a,pid,'resimulate',match='"v2"').status_code==409
 change_account(database,world);assert act(a,pid,'revalidate',match='"v2"').status_code==200
 assert a.patch('/api/v1/proposals/'+pid,headers={'If-Match':'"v3"'},json={'rationale':'must not edit stale content'}).status_code==409
 assert act(a,pid,'withdraw',match='"v3"').status_code==200
 assert act(a,pid,'resimulate',match='"v4"').status_code==409


def test_previous_runs_are_paginated_and_scoped_after_resimulation(database,api,world):
 a,pid,old=make_stale(database,api,world);new=act(a,pid,'resimulate',match='"v3"');assert new.status_code==200,new.text
 r=a.get(f'/api/v1/proposals/{pid}/simulations',params={'limit':'1'});assert r.status_code==200,r.text
 first=r.json();assert first['items'][0]['revision']==2 and first['next_cursor']
 second=a.get(f'/api/v1/proposals/{pid}/simulations',params={'limit':'1','cursor':first['next_cursor']});assert second.status_code==200 and second.json()['items']==[old] and second.json()['next_cursor'] is None
 b=signed_in(api,world,'analystB');assert b.get(f'/api/v1/proposals/{pid}/simulations').status_code==404
 other=new_draft(a,world);assert a.get(f'/api/v1/proposals/{other}/simulations',params={'cursor':first['next_cursor']}).status_code==400

@pytest.mark.parametrize('phase',['_after_begin','_before_finish','_after_finish'])
def test_resimulation_failure_rolls_back_new_revision_pin_and_reservations(database,api,world,monkeypatch,phase):
 from ledgervigil.proposals import submission
 a,pid,old=make_stale(database,api,world)
 def fail(*_):raise RuntimeError('injected workflow failure')
 monkeypatch.setattr(submission,phase,fail)
 assert act(a,pid,'resimulate',match='"v3"').status_code==500
 p=a.get('/api/v1/proposals/'+pid).json();assert (p['state'],p['version'],p['revision'])==('STALE',3,1)
 with connection(database=database) as c:
  assert c.execute('SELECT count(*) FROM lv.proposal_revision WHERE proposal_id=%s',(pid,)).fetchone()[0]==1
  assert c.execute('SELECT count(*) FROM lv.simulation_run WHERE proposal_id=%s',(pid,)).fetchone()[0]==1
  assert c.execute('SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s',(pid,)).fetchone()[0]==0


def test_freshness_receipts_and_readsets_remain_immutable_and_isolated(database,api,world):
 a,pid,_=submit(api,world);r=act(a,pid,'revalidate',match='"v2"');assert r.status_code==200
 for table in ['proposal_freshness_check','proposal_read_set']:
  with pytest.raises(psycopg.errors.CheckViolation):
   with connection(database=database) as c:c.execute('DELETE FROM lv.'+table+' WHERE proposal_id=%s',(pid,))
  with pytest.raises(psycopg.errors.InsufficientPrivilege):
   with connection('lv_app',database) as c:c.execute('DELETE FROM lv.'+table+' WHERE proposal_id=%s',(pid,))
 with configuration_connection(database) as c:
  with tenant_transaction(c,world['B']):assert c.execute('SELECT * FROM lv.proposal_freshness_check WHERE proposal_id=%s',(pid,)).fetchall()==[]


def test_competing_resimulation_requests_commit_one_revision(database,api,world):
 from concurrent.futures import ThreadPoolExecutor
 import threading
 a,pid,_=make_stale(database,api,world);other=signed_in(api,world,'analyst');barrier=threading.Barrier(2)
 def run(c):barrier.wait();return act(c,pid,'resimulate',match='"v3"')
 with ThreadPoolExecutor(2) as pool:rows=list(pool.map(run,[a,other]))
 assert sorted(r.status_code for r in rows)==[200,412]
 with connection(database=database) as c:assert c.execute('SELECT count(*) FROM lv.proposal_revision WHERE proposal_id=%s',(pid,)).fetchone()[0]==2


def test_resimulation_same_key_inflight_and_completed_retry(database,api,world,monkeypatch):
 from concurrent.futures import ThreadPoolExecutor
 import threading
 from ledgervigil.proposals import submission
 a,pid,_=make_stale(database,api,world);b=client(api);b.cookies.update(a.cookies);b.headers.update(a.headers)
 entered=threading.Event();release=threading.Event()
 def pause():entered.set();assert release.wait(timeout=10)
 monkeypatch.setattr(submission,'_after_begin',pause)
 with ThreadPoolExecutor(2) as pool:
  pending=pool.submit(act,a,pid,'resimulate',match='"v3"',key='resim-inflight')
  try:
   assert entered.wait(timeout=10)
   other=pool.submit(act,b,pid,'resimulate',match='"v3"',key='resim-inflight').result(timeout=4)
   assert other.status_code==409 and other.json()['code']=='REQUEST_IN_PROGRESS'
  finally:release.set()
  original=pending.result(timeout=10);assert original.status_code==200,original.text
 replay=act(a,pid,'resimulate',match='"v3"',key='resim-inflight');assert replay.json()==original.json() and replay.headers['Idempotent-Replayed']=='true'


def test_permission_revocation_before_resimulation_finish_rolls_back(database,api,world,monkeypatch):
 from ledgervigil.proposals import submission
 a,pid,_=make_stale(database,api,world)
 def revoke():
  with connection(database=database) as c:c.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['analyst']['id']))
 monkeypatch.setattr(submission,'_before_finish',revoke)
 assert act(a,pid,'resimulate',match='"v3"').status_code==403
 with connection(database=database) as c:
  assert c.execute('SELECT state,revision,version FROM lv.proposal WHERE id=%s',(pid,)).fetchone()==('STALE',1,3)
  assert c.execute('SELECT count(*) FROM lv.simulation_run WHERE proposal_id=%s',(pid,)).fetchone()[0]==1
  assert c.execute('SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s',(pid,)).fetchone()[0]==0


def test_resimulation_refuses_statement_claimed_by_another_proposal(database,api,world):
 a,pid,_=make_stale(database,api,world);other=new_draft(a,world);assert act(a,other).status_code==200
 result=act(a,pid,'resimulate',match='"v3"');assert result.status_code==422 and result.json()['reason']=='STAGED_DOCUMENT_RESERVED'
 with connection(database=database) as c:
  assert c.execute('SELECT revision,state FROM lv.proposal WHERE id=%s',(pid,)).fetchone()==(1,'STALE')
  assert c.execute('SELECT proposal_id::text FROM lv.staged_workflow WHERE staged_document_id=%s',(world['s1'],)).fetchone()==(other,)


def test_freshness_failure_rolls_back_receipt_release_and_head(database,api,world,monkeypatch):
 from ledgervigil.proposals import submission
 a,pid,_=submit(api,world);change_account(database,world)
 def fail(*_):raise RuntimeError('injected post-revalidation failure')
 monkeypatch.setattr(submission,'_after_finish',fail)
 assert act(a,pid,'revalidate',match='"v2"').status_code==500
 with connection(database=database) as c:
  assert c.execute('SELECT state,version FROM lv.proposal WHERE id=%s',(pid,)).fetchone()==('BLOCKED',2)
  assert c.execute('SELECT count(*) FROM lv.proposal_freshness_check WHERE proposal_id=%s',(pid,)).fetchone()[0]==0
  assert c.execute('SELECT proposal_id::text FROM lv.staged_workflow WHERE staged_document_id=%s',(world['s1'],)).fetchone()==(pid,)


def test_identical_reimport_does_not_stale_a_pinned_statement(database,api,world):
 from ledgervigil.ingestion.store import ingest_statement
 from ledgervigil.ingestion import statement_format as fmt
 a,pid,_=submit(api,world)
 with connection(database=database) as c:raw=c.execute('SELECT f.content FROM lv.staged_document s JOIN lv.import_batch b ON b.id=s.import_batch_id JOIN lv.source_file f ON f.id=b.source_file_id WHERE s.id=%s',(world['s1'],)).fetchone()[0]
 with connection('lv_ingest',database,autocommit=True) as c:r=ingest_statement(c,world['A'],world['l1']['ledger'],bytes(raw)+b'\n',declared_format=fmt.FORMAT,declared_version=fmt.VERSION)
 assert r['status']=='DUPLICATE'
 r=act(a,pid,'revalidate',match='"v2"');assert r.status_code==200 and r.json()['freshness']['status']=='FRESH',r.text

@pytest.mark.parametrize('missing',['release','audit'])
def test_commit_validator_refuses_incomplete_freshness_effects(database,api,world,missing):
 a,pid,_=submit(api,world);change_account(database,world)
 with connection(database=database) as c:
  original=c.execute("SELECT pg_get_functiondef('lv.workflow_revalidate(bytea,uuid,text,text,text,text,text)'::regprocedure)").fetchone()[0]
  if missing=='release':
   start=original.index(' WITH released AS(');end=original.index('\n END IF;',start);bad=original[:start]+original[end:]
  else:
   start=original.index(" PERFORM lv_identity.audit_append('PROPOSAL_REVALIDATED'");end=original.index('\n RETURN lv.workflow_cache',start);bad=original[:start]+original[end:]
  c.execute(bad)
 try:
  r=act(a,pid,'revalidate',match='"v2"');assert r.status_code==500,r.text
  with connection(database=database) as c:
   assert c.execute('SELECT state,version FROM lv.proposal WHERE id=%s',(pid,)).fetchone()==('BLOCKED',2)
   assert c.execute('SELECT count(*) FROM lv.proposal_freshness_check WHERE proposal_id=%s',(pid,)).fetchone()[0]==0
 finally:
  with connection(database=database) as c:c.execute(original)
