"""Real PostgreSQL lock races, deterministic lock barriers, no sleeps used as evidence."""
from concurrent.futures import ThreadPoolExecutor
import time,pytest
from tools.dev_db import connection,migrate
from test_proposal_drafts import world,seed,signed_in,fingerprint,counters
from test_simulation import api
from test_configuration_boundary import patch

@pytest.fixture
def admin(api,world):return signed_in(api,world,'admin')

def blocked(database,pid):
 end=time.monotonic()+10
 while time.monotonic()<end:
  with connection(database=database) as c:
   row=c.execute('SELECT cardinality(pg_blocking_pids(%s))',(pid,)).fetchone()
   if row and row[0]:return
  time.sleep(.01)
 raise AssertionError('Expected PostgreSQL lock wait was not observed')

def edit_wait(database):
 end=time.monotonic()+10
 while time.monotonic()<end:
  with connection(database=database) as c:
   rows=c.execute("SELECT pid FROM pg_stat_activity WHERE datname=%s AND query LIKE 'SELECT lv.edit_account%%' AND wait_event_type='Lock'",(database,)).fetchall()
   if rows:return rows[0][0]
  time.sleep(.01)
 raise AssertionError('Account SQL was not blocked at the intended barrier')

def revoke(db,kind,w):
 if kind=='membership':db.execute('SELECT lv_identity.admin_revoke_membership(%s,%s)',(w['A'],w['users']['admin']['id']))
 elif kind=='user':db.execute('SELECT lv_identity.admin_disable_user(%s)',(w['users']['admin']['id'],))
 elif kind=='session':db.execute('SELECT lv_identity.admin_revoke_sessions(%s)',(w['users']['admin']['id'],))
 else:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(w['A'],w['users']['admin']['id']))

@pytest.mark.parametrize('kind',['membership','user','session','role'])
@pytest.mark.parametrize('first',['revocation','edit'])
def test_edit_and_revocation_have_consistent_winner(database,api,world,admin,kind,first):
 t=world['A'];uid=world['users']['admin']['id'];aid=world['l1']['chart']['BANK_OPERATING'];seq=counters(database)
 with connection(database=database) as db:
  before=db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()[0];deadlocks=db.execute('SELECT deadlocks FROM pg_stat_database WHERE datname=%s',(database,)).fetchone()[0]
 with ThreadPoolExecutor(2) as pool:
  if first=='revocation':
   with connection(database=database) as rev:
    table='app_user' if kind=='user' else 'session' if kind=='session' else 'membership_role' if kind=='role' else 'membership'
    where='id=%s' if kind=='user' else 'user_id=%s';rev.execute('SELECT 1 FROM lv_identity.'+table+' WHERE '+where+' FOR UPDATE',(uid,))
    future=pool.submit(patch,admin,world,{'name':'SYNTHETIC contested edit'},'"v'+str(before)+'"');edit_wait(database);revoke(rev,kind,world);rev.commit()
   response=future.result(timeout=15);assert response.status_code in (401,403),response.text;expected=0
  else:
   with connection(database=database) as ledger:
    ledger.execute('SELECT 1 FROM lv.ledger WHERE id=%s FOR UPDATE',(world['l1']['ledger'],));future=pool.submit(patch,admin,world,{'name':'SYNTHETIC edit wins'},'"v'+str(before)+'"');edit_wait(database)
    rev=connection(database=database);rev_pid=rev.info.backend_pid
    def do_revoke():
     try:revoke(rev,kind,world);rev.commit()
     finally:rev.close()
    revoked=pool.submit(do_revoke);blocked(database,rev_pid);ledger.commit()
   response=future.result(timeout=15);revoked.result(timeout=15);assert response.status_code==200,response.text;expected=1
 with connection(database=database) as db:
  assert db.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchone()==(before+expected,)
  assert db.execute('SELECT count(*) FROM lv.configuration_change WHERE account_id=%s',(aid,)).fetchone()==(expected,)
  assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='CONFIGURATION_ACCOUNT_CHANGED' AND detail->>'account_id'=%s",(aid,)).fetchone()==(expected,)
  assert db.execute('SELECT deadlocks FROM pg_stat_database WHERE datname=%s',(database,)).fetchone()==(deadlocks,)
 assert counters(database)==seq and patch(admin,world,match='"v'+str(before+expected)+'"').status_code in (401,403)


def test_forward_and_backward_function_restoration(migration_database):
 database=migration_database
 sig='lv.edit_account(bytea,uuid,text,jsonb,jsonb,text,text)'
 with connection(database=database) as db:fixed=db.execute('SELECT pg_get_functiondef(%s::regprocedure)',(sig,)).fetchone()[0]
 migrate(database,'0026_proof_packs',downgrade=True,disposable=True)
 with connection(database=database) as db:old=db.execute('SELECT pg_get_functiondef(%s::regprocedure)',(sig,)).fetchone()[0]
 assert old.index('session_context(tok,true)')<old.index('app_user WHERE')
 migrate(database)
 with connection(database=database) as db:assert db.execute('SELECT pg_get_functiondef(%s::regprocedure)',(sig,)).fetchone()==(fixed,)
 assert fixed.index('ORDER BY role FOR SHARE')<fixed.index('session WHERE id=s.id FOR UPDATE')<fixed.index('session_context(tok,true)')
