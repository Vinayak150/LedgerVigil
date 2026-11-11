"""RG03 native settlement equivalence; synthetic business inputs, actual PostgreSQL and HTTP."""
from uuid import uuid4
import pytest,json
from tools.dev_db import connection
from test_proposal_drafts import world,seed,signed_in,counters,fingerprint,_payment,_statement,settlement
from test_simulation import api,new_draft
from test_submission import act
from test_native_approval import post as approve
from test_native_execution import post as execute
from test_evidence_overrides import post as override

@pytest.fixture
def pair(database,api,world,seed):
 w=world;pay=_payment(seed,w['l1'],'PMT-independent','100.00')
 statement=_statement(database,w['l1'],'STL-independent',[('PMT-independent','100.00')],'100.00','1.00','99.00')
 a=signed_in(api,w,'analyst');c=signed_in(api,w,'controller');ids=[]
 for kw in [{},{'operations':[settlement(w,(pay,'100.00'),statement=statement['staged_document_id'],gross='100.00',fee='1.00',net='99.00',payout='STL-independent')],'evidence_refs':[{'kind':'STAGED_DOCUMENT','id':statement['staged_document_id']},{'kind':'SOURCE_RECORD','id':statement['source_record_id']}]}]:
  pid=new_draft(a,w,**kw);r=act(a,pid);assert r.status_code==200,r.text
  assert override(c,pid).status_code==200
  r=approve(c,pid);assert r.status_code==200,r.text;ids.append(pid)
 return a,c,ids

def history(database,pid):
 with connection(database=database) as db:
  return {n:db.execute('SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),\'[]\') FROM lv.'+n+' t WHERE proposal_id=%s',(pid,)).fetchone()[0] for n in ['simulation_run','proposal_revision','proposal_decision','proposal_override']}

def test_two_independent_approved_payouts_execute_without_reapproval(database,pair):
 a,c,(first,second)=pair;before=history(database,second)
 r=execute(c,first);assert r.status_code==200,r.text
 assert 'EXECUTE' in c.get('/api/v1/proposals/'+second).json()['allowed_actions']
 r=execute(c,second,key='second-exact');assert r.status_code==200,r.text
 assert history(database,second)==before
 replay=execute(c,second,key='second-exact');assert replay.status_code==200 and replay.json()==r.json()
 with connection(database=database) as db:
  row=db.execute('SELECT equivalent,input_canonical::jsonb->\'snapshot\'->>\'seq\',seq FROM lv.semantic_recheck WHERE proposal_id=%s',(second,)).fetchone()
  assert row and row[0] and int(row[1])==row[2]
  assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id IN (%s,%s)',(first,second)).fetchone()==(2,)

def test_maker_revalidation_keeps_approved_revision_and_money(database,pair):
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;original=history(database,second);seq=counters(database)
 r=act(a,second,'revalidate',match='"v4"');assert r.status_code==200,r.text
 assert r.json()['proposal']['state']=='APPROVED' and r.json()['proposal']['version']==4 and r.json()['freshness']['status']=='FRESH'
 assert history(database,second)==original and counters(database)==seq
 assert execute(c,second).status_code==200

def test_configuration_change_still_requires_new_review(database,pair):
 a,c,(first,second)=pair;assert execute(c,first).status_code==200
 with connection(database=database) as db:
  account=db.execute("SELECT entity_id FROM lv.proposal_read_set WHERE proposal_id=%s AND entity_type='ACCOUNT' LIMIT 1",(second,)).fetchone()[0]
  db.execute('UPDATE lv.account SET name=name WHERE id=%s',(account,))
 seq=counters(database);r=execute(c,second);assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE',r.text
 assert counters(database)==seq
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.semantic_recheck WHERE proposal_id=%s',(second,)).fetchone()==(0,)

@pytest.mark.parametrize('action',['approve','override'])
def test_unrelated_posting_can_be_rechecked_before_review_actions(database,api,world,seed,action):
 a=signed_in(api,world,'analyst');c=signed_in(api,world,'controller');pid=new_draft(a,world);assert act(a,pid).status_code==200
 if action=='approve':assert override(c,pid).status_code==200
 _payment(seed,world['l1'],'PMT-extra-positive','100.00');seq=counters(database)
 r=approve(c,pid) if action=='approve' else override(c,pid);assert r.status_code==200,r.text
 assert counters(database)==seq
 with connection(database=database) as db:assert db.execute('SELECT equivalent FROM lv.semantic_recheck WHERE proposal_id=%s',(pid,)).fetchone()==(True,)

def test_new_clearing_failure_does_not_reuse_approval(database,api,world,seed,pair):
 from ledgervigil.ledger.seed import seed_manual_journal
 a,c,(_,second)=pair;chart=world['l1']['chart']
 seed_manual_journal(seed,world['A'],world['l1']['ledger'],{'source_ref':'drain-clearing','effective_date':'2026-10-10','description':'SYNTHETIC negative-boundary probe','lines':[{'account_id':chart['BANK_OPERATING'],'side':'D','amount':'52600.50'},{'account_id':chart['PROCESSOR_CLEARING'],'side':'C','amount':'52600.50'}]})
 seq=counters(database);original=history(database,second);r=execute(c,second)
 assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE',r.text
 assert counters(database)==seq and history(database,second)==original
 with connection(database=database) as db:
  r=db.execute('SELECT equivalent,outcome_canonical::jsonb FROM lv.semantic_recheck WHERE proposal_id=%s',(second,)).fetchone();assert r and r[0] is False
  assert r[1]['status']=='REJECTED' or any(v['policy_id']=='P-CLR-02' and v['outcome']=='FAIL' for v in r[1]['verdicts'])

@pytest.mark.parametrize('fault',['postings','policy','source','clearing_fact','projection_comparator','audit'])
def test_tampered_refresh_cannot_commit_money(database,pair,monkeypatch,fault):
 from ledgervigil.proposals import semantic
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;before=counters(database);stored=history(database,second)
 original=None
 if fault=='projection_comparator':
  with connection(database=database) as db:
   original=db.execute("SELECT pg_get_functiondef('lv.semantic_projection(jsonb)'::regprocedure)").fetchone()[0]
   db.execute("CREATE OR REPLACE FUNCTION lv.semantic_projection(o jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,pg_temp AS 'SELECT null::jsonb'")
 elif fault=='audit':
  with connection(database=database) as db:
   original=db.execute("SELECT pg_get_functiondef('lv.semantic_finish(bytea,uuid,text,text,text,text)'::regprocedure)").fetchone()[0]
   start=original.index(' PERFORM lv_identity.audit_append(');end=original.index('\n RETURN equal;',start)
   db.execute(original[:start]+original[end:])
 else:
  def corrupt(ctx,b,o):
   if fault=='postings':o['predicted_postings'][0]['amount']['amount']='0.01'
   elif fault=='policy':o['verdicts'][0]['outcome']='FAIL'
   elif fault=='clearing_fact':next(v for v in o['verdicts'] if v['policy_id']=='P-CLR-02')['facts']['clearing_balance_after']['amount']='999.99'
   else:b['pinned_sources']['statement']['source_file_sha256']='0'*64
  monkeypatch.setattr(semantic,'_after_evaluate',corrupt)
 try:
  r=execute(c,second);assert r.status_code in (409,500),r.text
  assert counters(database)==before and history(database,second)==stored
  if fault in ('source','clearing_fact','projection_comparator','audit'):
   assert r.status_code==500
   with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.semantic_recheck WHERE proposal_id=%s',(second,)).fetchone()==(0,)
 finally:
  if original:
   with connection(database=database) as db:db.execute(original)

def test_rechecks_are_tenant_scoped_and_immutable(database,world,pair):
 import psycopg
 from ledgervigil.db.tenant import tenant_transaction
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;assert act(a,second,'revalidate',match='"v4"').status_code==200
 for command in ['UPDATE lv.semantic_recheck SET equivalent=false','DELETE FROM lv.semantic_recheck']:
  with pytest.raises(psycopg.errors.CheckViolation):
   with connection(database=database) as db:db.execute(command)
  with pytest.raises(psycopg.errors.InsufficientPrivilege):
   with connection('lv_app',database) as db:db.execute(command)
 with connection('lv_app',database,autocommit=True) as db:
  with tenant_transaction(db,world['B']):assert db.execute('SELECT count(*) FROM lv.semantic_recheck').fetchone()==(0,)

def test_unfinished_recheck_cannot_commit(database,pair):
 import psycopg
 from ledgervigil.auth.crypto import token_hash
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;seq=counters(database)
 with pytest.raises(psycopg.errors.CheckViolation):
  with connection('lv_app',database) as db:
   r=db.execute('SELECT lv.semantic_begin(%s,%s,%s,%s,%s,%s,%s)',(token_hash(c.cookies.get('__Host-lv_session')),second,'execute','"v4"','unfinished','127.0.0.1','unfinished-probe')).fetchone()[0]
   assert r is not None
 assert counters(database)==seq

def test_chained_packs_and_offline_exports_retain_new_and_original_evidence(database,world,pair,tmp_path,monkeypatch):
 from ledgervigil.proofpacks.runtime import run_job
 from ledgervigil.proofpacks.anchors import GitAnchors
 from ledgervigil.execution.proof_verify import seal,canon,verify,ProofInvalid
 from ledgervigil.exports.runtime import run_job as export_run
 from ledgervigil.auth.crypto import token_hash
 from ledgervigil.proofpacks.integrity import readable,seal as pack_seal,ZERO
 import copy
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;assert execute(c,second).status_code==200
 # The durable JSON exporter and standalone verifier must support the older approved capture.
 with connection(database=database) as db:eid=str(db.execute('SELECT id FROM lv.native_execution WHERE proposal_id=%s',(second,)).fetchone()[0])
 r=c.post('/api/v1/executions/'+eid+'/exports',headers={'Idempotency-Key':'semantic-json'},json={'format':'JSON'})
 assert r.status_code==202,r.text;j=r.json()['id']
 result=export_run(world['A'],j,connection_factory=lambda:connection('lv_worker',database));assert result['status']=='DONE',result
 with connection(database=database) as db:
  data=bytes(db.execute('SELECT artifact FROM lv.native_export_job WHERE id=%s',(j,)).fetchone()[0])
 proof=json.loads(data);assert verify(data)['valid'] and proof['format'].endswith('/2')
 sim=proof['artifacts']['simulation.json'];assert sim['semantic_rechecks']
 broken=copy.deepcopy(proof['artifacts']);broken['simulation.json']['semantic_rechecks'][0]['equivalent']=False
 with pytest.raises(ProofInvalid):verify((canon(seal(proof['scope'],broken))+'\n').encode())
 store=GitAnchors(tmp_path/'trusted-anchor').initialize()
 with connection(database=database) as db:jobs=[str(r[0]) for r in db.execute('SELECT id FROM lv.proof_pack WHERE tenant_id=%s ORDER BY created_at,id',(world['A'],))]
 for job in jobs:assert run_job(world['A'],job,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 with connection(database=database) as db:rows=db.execute('SELECT content_canonical FROM lv.proof_pack WHERE proposal_id=%s ORDER BY chain_seq',(second,)).fetchall()
 contents=[json.loads(r[0]) for r in rows];assert 'semantic_rechecks' not in contents[0] and contents[-1]['semantic_rechecks'][0]['equivalent']
 assert 'Fresh settlement equivalence checks' in readable(pack_seal(contents[-1],ZERO,'2026-11-06T00:00:00Z')).decode()

def test_recheck_history_blocks_destructive_downgrade(database,pair):
 import psycopg
 from tools.dev_db import migrate
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;assert act(a,second,'revalidate',match='"v4"').status_code==200
 with pytest.raises(Exception):migrate(database,'0030_override_independence',downgrade=True,disposable=True)
 with connection(database=database) as db:
  # The whole multi-step downgrade is one refused Alembic run, so the head stays at the newest revision (0032 after the rebase).
  assert db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0039_mfa',)
  assert db.execute('SELECT count(*) FROM lv.semantic_recheck WHERE proposal_id=%s',(second,)).fetchone()==(1,)

@pytest.mark.parametrize('winner',['revocation','recheck'])
def test_refresh_and_role_revocation_use_identity_first_locks(database,world,pair,monkeypatch,winner):
 from concurrent.futures import ThreadPoolExecutor
 from threading import Event
 from ledgervigil.proposals import semantic
 from test_account_lock_order import blocked as wait_blocked
 from test_workflow_lock_order import wait_writer
 import time
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;before=counters(database);uid=world['users']['controller']['id']
 with ThreadPoolExecutor(2) as pool:
  if winner=='revocation':
   with connection(database=database) as owner:
    owner.execute('SELECT 1 FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s FOR UPDATE',(world['A'],uid))
    future=pool.submit(execute,c,second);wait_writer(database,'semantic_begin')
    owner.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],uid));owner.commit()
   r=future.result(timeout=15);assert r.status_code==403,r.text;assert counters(database)==before
  else:
   locked=Event();release=Event()
   def pause(ctx):locked.set();assert release.wait(10)
   monkeypatch.setattr(semantic,'_before_evaluate',pause);future=pool.submit(execute,c,second);assert locked.wait(10)
   revoke=connection(database=database);backend=revoke.info.backend_pid
   def revoker():
    try:revoke.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],uid));revoke.commit()
    finally:revoke.close()
   job=pool.submit(revoker)
   try:wait_blocked(database,backend)
   finally:release.set()
   assert future.result(timeout=15).status_code==200;job.result(timeout=15)
   assert execute(c,second).status_code==403

def test_empty_roundtrip_restores_every_predecessor_function_and_grant(migration_database):
 from tools.dev_db import migrate
 database=migration_database
 def catalog():
  with connection(database=database) as db:return db.execute("SELECT p.oid::regprocedure::text,pg_get_functiondef(p.oid),p.proacl::text,pg_get_userbyid(p.proowner) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('lv','lv_identity') ORDER BY 1").fetchall()
 migrate(database,'0037_restore_interlock',downgrade=True,disposable=True)
 head=catalog();migrate(database,'0030_override_independence',downgrade=True,disposable=True);old=catalog()
 assert len(head)>len(old)
 baseline=dict((r[0],r[1:]) for r in old)
 for signature,definition,grants,owner in head:
  if signature in baseline:assert baseline[signature][1:]==(grants,owner)
 migrate(database,'0037_restore_interlock');assert catalog()==head
 migrate(database)

def test_revalidated_but_unexecuted_pack_anchors_the_fresh_financial_prefix(fresh_pair,tmp_path):
 database,world,pair=fresh_pair
 from ledgervigil.proofpacks.runtime import run_job
 from ledgervigil.proofpacks.anchors import GitAnchors
 from ledgervigil.proofpacks.recovery import check_database
 a,c,(first,second)=pair;assert execute(c,first).status_code==200
 r=act(a,second,'revalidate',match='"v4"');assert r.status_code==200,r.text
 seq=r.json()['freshness']['observed_seq']
 r=c.post('/api/v1/proposals/'+second+'/proof-packs',headers={'Idempotency-Key':'fresh-approved-pack'},json={'revision':1});assert r.status_code==202,r.text
 store=GitAnchors(tmp_path/'trusted-fresh-prefix').initialize()
 with connection(database=database) as db:jobs=[str(row[0]) for row in db.execute('SELECT id FROM lv.proof_pack WHERE tenant_id=%s ORDER BY created_at,id',(world['A'],))]
 for job in jobs:assert run_job(world['A'],job,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 assert store.read(world['A'])['ledger_seq']==str(seq)
 with connection(database=database) as db:assert check_database(db,store)['status']=='ANCHORED_PREFIX_VERIFIED'

def test_populated_predecessor_upgrade_preserves_every_existing_nonversion_table(migration_database):
 from tools.dev_db import migrate
 database=migration_database;migrate(database,'0030_override_independence',downgrade=True,disposable=True)
 with connection('lv_app',database,autocommit=True) as runtime,connection('lv_seed',database,autocommit=True) as seed_connection:
  world.__wrapped__(database,runtime,seed_connection)
 before=fingerprint(database,{'lv_meta.alembic_version'})
 migrate(database)
 # New evidence/provenance tables start empty; existing table contents remain exact.
 after=fingerprint(database,{'lv_meta.alembic_version','lv.balance_dependency_step','lv.semantic_recheck','lv.detection_run','lv.detection_finding','lv.detection_run_finding','lv.detection_review','lv.detection_disposition','lv.provider_connection','lv.provider_oauth_state','lv.provider_credential','lv.provider_job','lv.provider_page','lv.provider_object','lv.database_binding','lv.database_binding_history','lv_identity.email_token','lv_identity.token_request','lv_identity.reauthentication','lv_identity.mfa_factor','lv_identity.mfa_attempt'})
 assert before==after

@pytest.fixture
def fresh_pair(migration_database):
 database=migration_database
 with connection('lv_app',database,autocommit=True) as runtime,connection('lv_seed',database,autocommit=True) as seed_connection:
  w=world.__wrapped__(database,runtime,seed_connection);generator=api.__wrapped__(database);application=next(generator)
  try:yield database,w,pair.__wrapped__(database,application,w,seed_connection)
  finally:
   try:next(generator)
   except StopIteration:pass

def test_wrong_revision_refuses_before_creating_refresh(database,pair):
 a,c,(first,second)=pair;assert execute(c,first).status_code==200;before=counters(database)
 r=execute(c,second,revision=999);assert r.status_code==412,r.text
 assert counters(database)==before
 with connection(database=database) as db:assert db.execute('SELECT count(*) FROM lv.semantic_recheck WHERE proposal_id=%s',(second,)).fetchone()==(0,)
