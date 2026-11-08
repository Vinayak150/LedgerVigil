"""Six HTTP writers against real revocation races with observed database lock barriers."""
from concurrent.futures import ThreadPoolExecutor
import pytest,time,json
from tools.dev_db import connection,migrate
from test_proposal_drafts import world,seed,signed_in,counters,_ledger
from test_simulation import api,new_draft
from test_native_approval import ready,post as approve
from test_native_execution import post as execute
from test_native_rejection import post as reject
from test_evidence_overrides import post as override
from test_proposal_comments import post as comment
from test_source_registry import post as registration
from test_resimulation import submit
from test_account_lock_order import blocked,revoke
NAMES={'comment':'add_proposal_comment','approve':'approve_native','reject':'reject_native','execute':'execute_native','override':'override_evidence','registration':'register_source_plan'}
EVENTS={'comment':'PROPOSAL_COMMENTED','approve':'PROPOSAL_APPROVED','reject':'PROPOSAL_REJECTED','execute':'PROPOSAL_EXECUTED','override':'PROPOSAL_OVERRIDDEN','registration':'SOURCE_PLAN_REGISTERED'}

def wait_writer(database,fn):
 end=time.monotonic()+12
 while time.monotonic()<end:
  with connection(database=database) as db:
   if db.execute("SELECT 1 FROM pg_stat_activity WHERE datname=%s AND query LIKE %s AND wait_event_type='Lock'",(database,'SELECT lv.'+fn+'%')).fetchone():return
  time.sleep(.01)
 raise AssertionError('Expected writer lock wait missing: '+fn)

@pytest.mark.parametrize('action',list(NAMES))
@pytest.mark.parametrize('kind',['membership','user','session','role'])
@pytest.mark.parametrize('first',['revocation','write'])
def test_writer_revocation_race(database,api,world,action,kind,first):
 if action in ('approve','reject','execute'):
  _,client,pid,_=ready.__wrapped__(database,api,world)
  if action=='execute':assert approve(client,pid).status_code==200
  call=lambda: {'approve':approve,'reject':reject,'execute':execute}[action](client,pid)
  actor='controller';lid=world['l1']['ledger']
 elif action=='override':
  _,pid,_=submit(api,world);client=signed_in(api,world,'controller');call=lambda:override(client,pid);actor='controller';lid=world['l1']['ledger']
 elif action=='comment':
  client=signed_in(api,world,'analyst');pid=new_draft(client,world);call=lambda:comment(client,pid);actor='analyst';lid=world['l1']['ledger']
 else:
  client=signed_in(api,world,'analyst');actor='analyst';pid=None
  with connection('lv_app',database,autocommit=True) as raw,connection('lv_seed',database,autocommit=True) as seed_conn:lid=_ledger(raw,database,seed_conn,world['A'],currency='GBP')['ledger']
  call=lambda:registration(client,lid)
 uid=world['users'][actor]['id'];w={'A':world['A'],'users':{'admin':{'id':uid}}};seq=counters(database)
 def state():
  with connection(database=database) as db:
   audit=db.execute('SELECT count(*) FROM lv_identity.audit_event WHERE user_id=%s AND tenant_id=%s AND event_type=%s',(uid,world['A'],EVENTS[action])).fetchone()[0]
   session=db.execute('SELECT last_seen_at FROM lv_identity.session WHERE user_id=%s AND tenant_id=%s ORDER BY created_at DESC LIMIT 1',(uid,world['A'])).fetchone()[0]
  return audit,session
 before=state()
 with connection(database=database) as db:dl=db.execute('SELECT deadlocks FROM pg_stat_database WHERE datname=%s',(database,)).fetchone()[0]
 with ThreadPoolExecutor(2) as pool:
  if first=='revocation':
   with connection(database=database) as rev:
    table='app_user' if kind=='user' else 'session' if kind=='session' else 'membership_role' if kind=='role' else 'membership';where='id=%s' if kind=='user' else 'user_id=%s'
    rev.execute('SELECT 1 FROM lv_identity.'+table+' WHERE '+where+' FOR UPDATE',(uid,));future=pool.submit(call);wait_writer(database,NAMES[action]);revoke(rev,kind,w);rev.commit()
   response=future.result(timeout=15);assert response.status_code in (401,403),response.text;effect=0
  else:
   with connection(database=database) as barrier:
    if pid:barrier.execute('SELECT 1 FROM lv.proposal WHERE id=%s FOR UPDATE',(pid,))
    else:barrier.execute('SELECT 1 FROM lv.ledger WHERE id=%s FOR UPDATE',(lid,))
    future=pool.submit(call);wait_writer(database,NAMES[action]);rev=connection(database=database);rpid=rev.info.backend_pid
    def run_revoke():
     try:revoke(rev,kind,w);rev.commit()
     finally:rev.close()
    revocation=pool.submit(run_revoke);blocked(database,rpid);barrier.commit()
   response=future.result(timeout=15);revocation.result(timeout=15);assert response.status_code==(201 if action=='comment' else 200),response.text;effect=1
 after=state();assert after[0]==before[0]+effect,(action,before,after,response.text)
 if first=='revocation':assert after[1]==before[1]
 with connection(database=database) as db:
  assert db.execute('SELECT deadlocks FROM pg_stat_database WHERE datname=%s',(database,)).fetchone()==(dl,)
  if action=='execute':assert db.execute('SELECT count(*) FROM lv.native_execution WHERE proposal_id=%s',(pid,)).fetchone()==(effect,)
  if action in ('approve','reject'):assert db.execute('SELECT count(*) FROM lv.proposal_decision WHERE proposal_id=%s',(pid,)).fetchone()==(effect,)
 if action!='execute' or not effect:assert counters(database)==seq
 assert call().status_code in (401,403)


def test_all_six_function_roundtrip(migration_database):
 database=migration_database
 def definitions():
  with connection(database=database) as db:return {n:db.execute("SELECT pg_get_functiondef(p.oid),p.proacl::text,pg_get_userbyid(p.proowner) FROM pg_proc p JOIN pg_namespace s ON s.oid=p.pronamespace WHERE s.nspname='lv' AND p.proname=%s",(n,)).fetchone() for n in NAMES.values()}
 fixed=definitions();migrate(database,'0027_account_lock_order',downgrade=True,disposable=True);old=definitions();migrate(database);assert definitions()==fixed
 for n in NAMES.values():
  assert old[n][1:]==fixed[n][1:]
  assert old[n][0].index('session_context(tok,true)')<old[n][0].index('PERFORM 1 FROM lv_identity.membership_role WHERE')
  assert fixed[n][0].index('ORDER BY role FOR SHARE')<fixed[n][0].index('session WHERE id=s.id FOR UPDATE')<fixed[n][0].index('session_context(tok,true)')
