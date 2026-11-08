from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest,psycopg
from tools.dev_db import connection
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ingestion.registry import MANIFEST
from test_proposal_drafts import world,seed,api,client,signed_in,_ledger,fingerprint  # noqa:F401

BASE='/api/v1/source-plan-registrations'
@pytest.fixture
def gbp(database,app,seed,world):return _ledger(app,database,seed,world['A'],currency='GBP')['ledger']
def post(c,ledger,key=None,**extra):return c.post(BASE,headers={'Idempotency-Key':key or uuid4().hex},json={'ledger_id':str(ledger),'manifest_sha256':MANIFEST,**extra})

def test_verified_create_read_replay_preserves_financial_state(database,api,world,gbp):
 a=signed_in(api,world,'analyst');excluded={'lv.source_plan_registration','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'};before=fingerprint(database,excluded)
 r=post(a,gbp,'repeat');assert r.status_code==200,r.text;p=r.json();assert p['held']==40932 and p['posting_eligibility']=='UNAVAILABLE'
 assert a.get(BASE+'/'+p['id']).json()==p
 again=post(a,gbp,'repeat');assert again.json()==p and again.headers['Idempotent-Replayed']=='true'
 assert fingerprint(database,excluded)==before
 with connection(database=database) as c:assert c.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='SOURCE_PLAN_REGISTERED' AND tenant_id=%s",(world['A'],)).fetchone()[0]==1

def test_cross_tenant_permissions_and_csrf(api,world,gbp):
 assert post(client(api),gbp).status_code==401
 for role in ['controller','admin']:assert post(signed_in(api,world,role),gbp).status_code==403
 a=signed_in(api,world,'analyst');r=post(a,gbp);assert r.status_code==200,r.text
 b=signed_in(api,world,'analystB');assert post(b,gbp).status_code==404
 assert b.get(BASE+'/'+r.json()['id']).status_code==404 and b.get(BASE+'/'+str(uuid4())).status_code==404
 del a.headers['X-CSRF-Token'];assert post(a,gbp).status_code==403

def test_forged_metadata_unknown_plan_wrong_currency_and_key_are_refused(api,world,gbp):
 a=signed_in(api,world,'analyst');assert post(a,gbp,source_rows=1).status_code==422
 assert post(a,gbp,manifest_sha256='0'*64).status_code==422
 assert post(a,world['l1']['ledger']).status_code==422
 assert a.post(BASE,json={'ledger_id':str(gbp),'manifest_sha256':MANIFEST}).status_code==400
 assert post(a,gbp,'same').status_code==200
 assert post(a,gbp,'same',manifest_sha256='0'*64).status_code==422

def test_registration_is_immutable_and_runtime_cannot_insert(database,api,world,gbp):
 a=signed_in(api,world,'analyst');assert post(a,gbp).status_code==200
 with pytest.raises(psycopg.errors.CheckViolation):
  with connection(database=database) as c:c.execute('DELETE FROM lv.source_plan_registration WHERE ledger_id=%s',(gbp,))
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  with connection('lv_app',database) as c:c.execute('DELETE FROM lv.source_plan_registration')
 with connection('lv_app',database,autocommit=True) as c:
  with tenant_transaction(c,world['B']):assert c.execute('SELECT count(*) FROM lv.source_plan_registration').fetchone()==(0,)

def test_competing_duplicate_registration_creates_one_snapshot(database,api,world,gbp):
 a=signed_in(api,world,'analyst');b=signed_in(api,world,'automation')
 with ThreadPoolExecutor(2) as pool:rs=list(pool.map(lambda c:post(c,gbp),[a,b]))
 assert [r.status_code for r in rs]==[200,200],[(r.status_code,r.text) for r in rs]
 assert rs[0].json()==rs[1].json()
 with connection(database=database) as c:assert c.execute('SELECT count(*) FROM lv.source_plan_registration WHERE ledger_id=%s',(gbp,)).fetchone()==(1,)


def test_missing_atomic_audit_cannot_commit(database,api,world,gbp):
 with connection(database=database) as c:
  original=c.execute("SELECT pg_get_functiondef('lv.register_source_plan(bytea,uuid,text,text,text,text,text)'::regprocedure)").fetchone()[0]
  start=original.index(" PERFORM lv_identity.audit_append('SOURCE_PLAN_REGISTERED'");end=original.index('\n response:=',start);c.execute(original[:start]+original[end:])
 try:
  a=signed_in(api,world,'analyst');assert post(a,gbp).status_code==500
  with connection(database=database) as c:assert c.execute('SELECT count(*) FROM lv.source_plan_registration WHERE ledger_id=%s',(gbp,)).fetchone()==(0,)
 finally:
  with connection(database=database) as c:c.execute(original)


def test_revoked_source_permission_cannot_replay_cached_result(database,api,world,gbp):
 a=signed_in(api,world,'analyst');assert post(a,gbp,'old').status_code==200
 with connection(database=database) as c:c.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['analyst']['id']))
 assert post(a,gbp,'old').status_code==403


def test_same_session_inflight_key_returns_in_progress(database,api,world,gbp):
 import time
 a=signed_in(api,world,'analyst');b=client(api);b.cookies.update(a.cookies);b.headers.update(a.headers)
 with connection(database=database) as owner:
  owner.execute('SELECT 1 FROM lv.ledger WHERE id=%s FOR UPDATE',(gbp,))
  with ThreadPoolExecutor(2) as pool:
   pending=pool.submit(post,a,gbp,'inflight')
   try:
    deadline=time.monotonic()+5;blocked=False
    while time.monotonic()<deadline:
     with connection(database=database,autocommit=True) as monitor:
      blocked=monitor.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=%s AND query LIKE 'SELECT lv.register_source_plan%%' AND wait_event_type='Lock')",(database,)).fetchone()[0]
     if blocked:break
     time.sleep(.02)
    assert blocked,'first request never reached ledger lock'
    other=pool.submit(post,b,gbp,'inflight').result(timeout=3);assert other.status_code==409 and other.json()['code']=='REQUEST_IN_PROGRESS'
   finally:owner.rollback()
   assert pending.result(timeout=5).status_code==200


def test_user_disabled_between_http_boundary_and_registration_is_401(database,api,world,gbp,monkeypatch):
 from ledgervigil.ingestion import registry
 a=signed_in(api,world,'analyst');original=registry.token_hash
 def disable(token):
  with connection(database=database) as c:c.execute('UPDATE lv_identity.app_user SET disabled_at=clock_timestamp() WHERE id=%s',(world['users']['analyst']['id'],))
  return original(token)
 monkeypatch.setattr(registry,'token_hash',disable)
 result=post(a,gbp);assert result.status_code==401,result.text
 with connection(database=database) as c:assert c.execute('SELECT count(*) FROM lv.source_plan_registration WHERE ledger_id=%s',(gbp,)).fetchone()==(0,)
