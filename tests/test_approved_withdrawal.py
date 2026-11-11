"""RG-01: independent accepted maker escape, no recapture or invalidation changes."""
from uuid import uuid4
import pytest
from tools.dev_db import connection
from test_proposal_drafts import world,seed,signed_in,draft,create,fingerprint,counters
from test_simulation import api
from test_native_execution import approved,post as execute
from test_native_approval import ready
from test_submission import act
from ledgervigil.db.tenant import tenant_transaction
from period_close_support import admin_token,close_period

EXCLUDE={'lv.proposal','lv.proposal_transition','lv.reservation_event','lv.staged_workflow','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'}

def september_close(database,w,app):
 from configuration_support import configuration_connection
 with configuration_connection(database) as setup,tenant_transaction(setup,w['A']):
  per=setup.execute("INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date) VALUES(%s,%s,'2026-09-01','2026-10-01') RETURNING id",(w['A'],w['l1']['ledger'])).fetchone()[0]
 assert close_period(database,admin_token(database,w['A']),str(per))['status']==200


def test_unrelated_period_close_no_longer_traps_approved_maker(database,world,app,approved):
 a,c,pid,_=approved;september_close(database,world,app)
 assert c.get('/api/v1/proposals/'+pid).json()['allowed_actions']==['EXECUTE']  # RG03 now permits a locked semantic recheck; maker withdrawal remains available.
 before=fingerprint(database,EXCLUDE);seq=counters(database)
 r=act(a,pid,'withdraw',match='"v4"');assert r.status_code==200,r.text
 assert r.json()['state']=='WITHDRAWN' and r.json()['version']==5
 with connection(database=database) as db:
  assert db.execute('SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s AND status=\'RESERVED\'',(pid,)).fetchone()==(0,)
  assert db.execute("SELECT count(*) FROM lv.reservation_event WHERE proposal_id=%s AND event='RELEASED'",(pid,)).fetchone()==(1,)
  assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(0,)
 assert fingerprint(database,EXCLUDE)==before and counters(database)==seq
 r=create(a,draft(world,rationale='SYNTHETIC replacement after approved withdrawal'));assert r.status_code==201;r=act(a,r.json()['id']);assert r.status_code==200,r.text


def test_approval_simulation_and_maker_actions_survive_withdrawal(database,approved):
 a,c,pid,run=approved
 assert a.get('/api/v1/proposals/'+pid).json()['allowed_actions']==['REVALIDATE','WITHDRAW']
 assert c.get('/api/v1/proposals/'+pid).json()['allowed_actions']==['EXECUTE']
 with connection(database=database) as db:
  before={t:db.execute('SELECT row_to_json(t) FROM lv.'+t+' t WHERE proposal_id=%s ORDER BY id',(pid,)).fetchall() for t in ['proposal_decision','approval_check','simulation_run']}
 r=act(a,pid,'withdraw',match='"v4"',key='approved-withdraw');assert r.status_code==200
 retry=act(a,pid,'withdraw',match='"v4"',key='approved-withdraw');assert retry.status_code==200 and retry.json()==r.json() and retry.headers.get('Idempotent-Replayed')=='true'
 assert act(a,pid,'withdraw',match='"v4"',key='approved-withdraw',json={'changed':True}).status_code==422
 assert act(a,pid,'withdraw',match='"v5"').status_code==409
 assert execute(c,pid,match='"v5"').status_code==409
 assert a.get('/api/v1/simulations/'+run['id']).json()==run
 with connection(database=database) as db:
  assert {t:db.execute('SELECT row_to_json(t) FROM lv.'+t+' t WHERE proposal_id=%s ORDER BY id',(pid,)).fetchall() for t in before}==before
  assert db.execute("SELECT count(*) FROM lv.proposal_transition WHERE proposal_id=%s AND from_state='APPROVED' AND action='withdraw'",(pid,)).fetchone()==(1,)

@pytest.mark.parametrize('actor,status',[('controller',403),('automation',403),('admin',403),('analystB',404)])
def test_only_current_tenant_maker_can_withdraw_approved(api,world,approved,actor,status):
 _,_,pid,_=approved;other=signed_in(api,world,actor);assert act(other,pid,'withdraw',match='"v4"').status_code==status
 assert other.get('/api/v1/proposals/'+pid).status_code==(404 if actor=='analystB' else 200)
 if actor!='analystB':assert 'WITHDRAW' not in other.get('/api/v1/proposals/'+pid).json()['allowed_actions']


def test_version_revocation_and_terminal_boundaries(database,world,approved):
 a,c,pid,_=approved
 assert act(a,pid,'withdraw',match='"v3"').status_code==412
 assert act(a,pid,'withdraw',match=None).status_code==428
 assert execute(c,pid).status_code==200
 assert act(a,pid,'withdraw',match='"v5"').status_code==409
 with connection(database=database) as db:db.execute("UPDATE lv_identity.membership SET status='REVOKED',revoked_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s",(world['A'],world['users']['analyst']['id']))
 assert act(a,pid,'withdraw',match='"v5"').status_code==401

@pytest.mark.parametrize('missing',['release','audit','transition','cache'])
def test_withdrawal_failure_rolls_back_every_atomic_artifact(database,approved,missing):
 a,_,pid,_=approved
 signature='lv.workflow_withdraw(bytea,uuid,text,text,text,text,text)'
 with connection(database=database) as db:original=db.execute('SELECT pg_get_functiondef(%s::regprocedure)',(signature,)).fetchone()[0]
 if missing=='release':start=original.index(' FOR w IN SELECT');end=original.index(' END LOOP;',start)+len(' END LOOP;')
 elif missing=='audit':start=original.index(" PERFORM lv_identity.audit_append('PROPOSAL_WITHDRAWN'");end=original.index('\n RETURN lv.workflow_cache',start)
 elif missing=='transition':start=original.index(' INSERT INTO lv.proposal_transition');end=original.index('\n PERFORM lv_identity.audit_append',start)
 else:start=original.index(' RETURN lv.workflow_cache');end=original.index('\nEND',start)
 mutant=original[:start]+(" RETURN jsonb_build_object('status',200,'etag','v'||p.version::text,'body',lv.proposal_json(p));" if missing=='cache' else '')+original[end:]
 before=fingerprint(database,set());seq=counters(database)
 with connection(database=database) as db:db.execute(mutant)
 try:assert act(a,pid,'withdraw',match='"v4"').status_code==500
 finally:
  with connection(database=database) as db:db.execute(original)
 assert fingerprint(database,set())==before and counters(database)==seq
 assert a.get('/api/v1/proposals/'+pid).json()['state']=='APPROVED'

@pytest.mark.parametrize('winner',['withdraw','execute'])
def test_twenty_competing_requests_have_exactly_one_terminal_winner(database,approved,winner):
 from concurrent.futures import ThreadPoolExecutor
 import time
 a,c,pid,_=approved;seq=counters(database);before=fingerprint(database,EXCLUDE)
 first=(lambda:act(a,pid,'withdraw',match='"v4"')) if winner=='withdraw' else (lambda:execute(c,pid))
 expected='lv.workflow_withdraw' if winner=='withdraw' else 'lv.execute_native'
 with connection(database=database) as gate:
  gate.execute('SELECT 1 FROM lv.proposal WHERE id=%s FOR UPDATE',(pid,))
  with ThreadPoolExecutor(20) as pool:
   futures=[pool.submit(first)]
   try:
    deadline=time.monotonic()+5
    while True:
     with connection(database=database,autocommit=True) as monitor:
      waiting=monitor.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=%s AND wait_event_type='Lock' AND query LIKE %s)",(database,'SELECT '+expected+'%')).fetchone()[0]
     if waiting:break
     assert time.monotonic()<deadline;time.sleep(.01)
    # Exactly ten executors and ten maker withdrawals, with each ordering forced once.
    remaining_withdraw=9 if winner=='withdraw' else 10
    remaining_execute=10 if winner=='withdraw' else 9
    futures += [pool.submit(lambda:act(a,pid,'withdraw',match='"v4"')) for _ in range(remaining_withdraw)]
    futures += [pool.submit(lambda:execute(c,pid)) for _ in range(remaining_execute)]
   finally:gate.commit()
   responses=[f.result(timeout=20) for f in futures]
 assert responses[0].status_code==200 and sum(r.status_code==200 for r in responses)==1,[(r.status_code,r.text) for r in responses]
 assert all(r.status_code in (200,409,412) for r in responses)
 expected_state='WITHDRAWN' if winner=='withdraw' else 'EXECUTED';assert a.get('/api/v1/proposals/'+pid).json()['state']==expected_state
 with connection(database=database) as db:
  assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(int(winner=='execute'),)
  assert db.execute("SELECT count(*) FROM lv.proposal_transition WHERE proposal_id=%s AND action='withdraw'",(pid,)).fetchone()==(int(winner=='withdraw'),)
  assert db.execute("SELECT count(*) FROM lv.reservation_event WHERE proposal_id=%s AND event='RELEASED'",(pid,)).fetchone()==(int(winner=='withdraw'),)
 if winner=='withdraw':assert counters(database)==seq and fingerprint(database,EXCLUDE)==before

@pytest.mark.parametrize('committed',[False,True])
def test_lost_withdrawal_acknowledgement_retries_original_key_once(database,approved,monkeypatch,committed):
 from contextlib import contextmanager
 import psycopg
 from ledgervigil.proposals import submission
 a,_,pid,_=approved;original=submission.session_transaction;first=True;before=fingerprint(database,EXCLUDE);seq=counters(database)
 @contextmanager
 def interrupted(*args,**kwargs):
  nonlocal first
  if not first:
   with original(*args,**kwargs) as ctx:yield ctx
   return
  first=False
  with original(*args,**kwargs) as ctx:
   yield ctx
   if not committed:raise psycopg.OperationalError('controlled precommit interruption')
  raise psycopg.OperationalError('controlled committed response loss')
 with monkeypatch.context() as m:
  m.setattr(submission,'session_transaction',interrupted);r=act(a,pid,'withdraw',match='"v4"',key='lost-withdraw')
 assert r.status_code==500
 assert a.get('/api/v1/proposals/'+pid).json()['state']==('WITHDRAWN' if committed else 'APPROVED')
 retry=act(a,pid,'withdraw',match='"v4"',key='lost-withdraw');assert retry.status_code==200 and (retry.headers.get('Idempotent-Replayed')=='true')==committed
 assert fingerprint(database,EXCLUDE)==before and counters(database)==seq
 with connection(database=database) as db:assert db.execute("SELECT count(*) FROM lv.proposal_transition WHERE proposal_id=%s AND action='withdraw'",(pid,)).fetchone()==(1,)


def test_migration_empty_roundtrip_restores_exact_predecessors():
 from tools.dev_db import prepare_database,migrate
 from psycopg import sql
 name='lv_migration_test_'+uuid4().hex
 with connection(autocommit=True) as db:db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
 def defs():
  with connection(database=name) as db:return [db.execute('SELECT pg_get_functiondef(%s::regprocedure)',('lv.'+sig,)).fetchone()[0] for sig in ['guard_proposal_update()','workflow_withdraw(bytea,uuid,text,text,text,text,text)']]
 try:
  prepare_database(name);migrate(name,'0021_native_execution');before=defs();migrate(name);assert defs()!=before
  migrate(name,'0021_native_execution',downgrade=True,disposable=True);assert defs()==before
  migrate(name)
 finally:
  with connection(autocommit=True) as db:db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_populated_upgrade_preserves_history_and_downgrade_refuses_withdrawal():
 from tools.dev_db import prepare_database,migrate
 from psycopg import sql
 from ledgervigil.app import create_app
 from ledgervigil.auth.settings import AuthSettings
 from test_proposal_drafts import world as fixture
 from test_native_approval import ready as ready_fixture,post as approve
 name='lv_migration_test_'+uuid4().hex;application=None
 with connection(autocommit=True) as db:db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
 try:
  prepare_database(name);migrate(name,'0021_native_execution')
  with connection('lv_app',name,autocommit=True) as raw,connection('lv_seed',name,autocommit=True) as s:w=fixture.__wrapped__(name,raw,s)
  application=create_app(AuthSettings(),connect=lambda:connection('lv_app',name), issuer_connect=lambda: connection('lv_authn', name))
  from legacy_issuer_support import use_historical_issuer
  issuer=application.state.issuer_pool
  use_historical_issuer(application,name,'0021_native_execution')
  a,c,pid,_=ready_fixture.__wrapped__(name,application,w);assert approve(c,pid).status_code==200
  before=fingerprint(name,{'lv_meta.alembic_version','lv.configuration_change','lv.native_export_job','lv.proof_pack','lv.proof_pack_chain','lv.proof_pack_request','lv.semantic_recheck','lv.balance_dependency_step','lv.detection_run','lv.detection_finding','lv.detection_run_finding','lv.detection_review','lv.detection_disposition','lv.provider_connection','lv.provider_oauth_state','lv.provider_credential','lv.provider_job','lv.provider_page','lv.provider_object','lv.database_binding','lv.database_binding_history','lv_identity.email_token','lv_identity.token_request','lv_identity.reauthentication','lv_identity.mfa_factor','lv_identity.mfa_attempt'});seq=counters(name);migrate(name)
  with connection(database=name) as check:
   assert check.execute('SELECT (SELECT count(*) FROM lv_identity.mfa_factor)+(SELECT count(*) FROM lv_identity.mfa_attempt)').fetchone()==(0,)
  application.state.issuer_pool=issuer
  assert fingerprint(name,{'lv_meta.alembic_version','lv.configuration_change','lv.native_export_job','lv.proof_pack','lv.proof_pack_chain','lv.proof_pack_request','lv.semantic_recheck','lv.balance_dependency_step','lv.detection_run','lv.detection_finding','lv.detection_run_finding','lv.detection_review','lv.detection_disposition','lv.provider_connection','lv.provider_oauth_state','lv.provider_credential','lv.provider_job','lv.provider_page','lv.provider_object','lv.database_binding','lv.database_binding_history','lv_identity.email_token','lv_identity.token_request','lv_identity.reauthentication','lv_identity.mfa_factor','lv_identity.mfa_attempt'})==before and counters(name)==seq
  assert act(a,pid,'withdraw',match='"v4"').status_code==200
  retained=fingerprint(name,set())
  with pytest.raises(Exception,match='downgrade_no_approved_withdraw'):migrate(name,'0021_native_execution',downgrade=True,disposable=True)
  assert fingerprint(name,set())==retained
  with connection(database=name) as db:assert db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0039_mfa',)
 finally:
  if application:application.state.pool.close()
  with connection(autocommit=True) as db:db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
