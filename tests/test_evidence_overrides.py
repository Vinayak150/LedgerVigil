from configuration_support import configuration_connection
"""ADR-014 exceptions through real native PostgreSQL; raw simulation verdicts are never rewritten."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import time,json
import pytest,psycopg
from tools.dev_db import connection,migrate
from tools.identity_admin import owner_transaction
from ledgervigil.auth.crypto import token_hash
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.proposals import overrides
from test_proposal_drafts import world,seed,signed_in,client,fingerprint,counters,settlement  # noqa:F401
from test_simulation import api,new_draft
from test_resimulation import submit,change_account
from test_submission import act

EXCLUDED={'lv.proposal','lv.proposal_override','lv.proposal_transition','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'}
def post(c,pid,key=None,match='"v2"',reason='SYNTHETIC sandbox exception for an independent reviewer; source remains unreviewed.',policies=None,**kw):
    headers={'Idempotency-Key':key or uuid4().hex}
    if match is not None:headers['If-Match']=match
    return c.post(f'/api/v1/proposals/{pid}/overrides',headers=headers,**(kw or {'json':{'policy_ids':['P-EVD-01'] if policies is None else policies,'reason':reason}}))
def history(c,pid,**kw):return c.get(f'/api/v1/proposals/{pid}/reviews',**kw)
@pytest.fixture
def blocked(api,world):
    a,pid,run=submit(api,world);return a,signed_in(api,world,'controller'),pid,run

def test_controller_exception_is_atomic_and_preserves_raw_verdicts_and_money(database,blocked):
    a,c,pid,run=blocked;before=fingerprint(database,EXCLUDED);ctr=counters(database)
    reason='  Explicit SYNTHETIC-only exception ₹ <img src=x>\nNot an approval.  '
    r=post(c,pid,reason=reason);assert r.status_code==200,r.text;p=r.json()
    assert (p['state'],p['revision'],p['version'])==('READY_FOR_REVIEW',1,3) and r.headers['etag']=='"v3"' and p['accounting_status']=='UNREVIEWED'
    assert a.get('/api/v1/simulations/'+run['id']).json()==run and run['outcome']['aggregate']=='BLOCKED_INSUFFICIENT' and not run['outcome']['eligible_for_execution']
    h=history(c,pid);assert h.status_code==200 and len(h.json()['items'])==1
    record=h.json()['items'][0];assert record['reason']==reason and record['original_outcome']=='INSUFFICIENT_EVIDENCE' and record['simulation_id']==run['id'] and record['applies_to_current_revision']
    assert record['overridden_by']!=p['created_by'] and h.json()['unsupported_kinds']==[]
    assert history(c,pid,headers={'If-None-Match':h.headers['etag']}).status_code==304
    assert fingerprint(database,EXCLUDED)==before and counters(database)==ctr
    with connection(database=database) as db:assert db.execute('SELECT status,proposal_id::text FROM lv.staged_workflow WHERE proposal_id=%s',(pid,)).fetchone()==('RESERVED',pid)

def test_any_fail_refuses_exception_without_fabricating_verdicts(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world,operations=[settlement(world,fee='1001.00',net='51499.50')]);r=act(a,pid);assert r.status_code==200,r.text
    assert r.json()['simulation']['outcome']['aggregate']=='BLOCKED_FAIL';before=fingerprint(database,{'lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'})
    refused=post(signed_in(api,world,'controller'),pid);assert refused.status_code==422,refused.text
    assert refused.json()['reason']=='FAIL_CANNOT_BE_OVERRIDDEN' and fingerprint(database,{'lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'})==before

def test_permission_matrix_foreign_ids_csrf_origin_and_maker_exclusion(database,api,world,blocked):
    a,c,pid,_=blocked
    assert post(client(api),pid).status_code==401
    for role in ['analyst','admin','automation']:assert post(signed_in(api,world,role),pid).status_code==403
    with owner_transaction(database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING')",(world['B'],world['users']['analystB']['id']))
    b=signed_in(api,world,'analystB');assert post(b,pid).status_code==post(b,uuid4()).status_code==404
    assert history(b,pid).status_code==history(b,uuid4()).status_code==404
    del c.headers['X-CSRF-Token'];assert post(c,pid).status_code==403
    c=signed_in(api,world,'controller');c.headers['Origin']='https://untrusted.invalid';assert post(c,pid).status_code==403
    with owner_transaction(database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING')",(world['A'],world['users']['analyst']['id']))
    maker=signed_in(api,world,'analyst');r=post(maker,pid);assert r.status_code==403 and r.json()['code']=='SEGREGATION_OF_DUTIES'

def test_direct_runtime_cannot_bypass_controller_role(database,api,world,blocked):
    _,_,pid,_=blocked;admin=signed_in(api,world,'admin');token=admin.cookies.get('__Host-lv_session')
    with connection('lv_app',database) as db:
        r=db.execute('SELECT lv.override_evidence(%s,%s,%s,%s,%s,%s,NULL,%s,%s)',(token_hash(token),pid,'"v2"','runtime-probe','0'*64,__import__('psycopg').types.json.Jsonb({'policy_ids':['P-EVD-01'],'reason':'No role bypass'}),'127.0.0.1','direct-override')).fetchone()[0]
        assert r['status']==403

def test_required_version_key_and_replay_are_bound_to_original_request(blocked):
    a,c,pid,run=blocked
    assert post(c,pid,match=None).status_code==428 and post(c,pid,match='"v99"').status_code==412
    assert c.post(f'/api/v1/proposals/{pid}/overrides',json={'policy_ids':['P-EVD-01'],'reason':'No key'}).status_code==400
    first=post(c,pid,key='replay');assert first.status_code==200,first.text
    retry=post(c,pid,key='replay');assert retry.json()==first.json() and retry.headers['Idempotent-Replayed']=='true'
    assert post(c,pid,key='replay',reason='Changed reason').json()['code']=='IDEMPOTENCY_KEY_REUSED'
    assert post(c,pid,key='replay',match='"v3"').json()['code']=='IDEMPOTENCY_KEY_REUSED'
    assert post(c,pid,match='"v3"').status_code==409

@pytest.mark.parametrize('body',[{},[],{'policy_ids':['P-EVD-01'],'reason':''},{'policy_ids':['P-EVD-01'],'reason':'\n\t '},{'policy_ids':['P-EVD-01'],'reason':'x'*4001},{'policy_ids':['P-EVD-01'],'reason':'\ud800'},{'policy_ids':['P-EVD-01'],'reason':'x','overridden_by':'forged'}])
def test_invalid_reason_or_attribution_is_rejected(blocked,body):
    _,c,pid,_=blocked;r=post(c,pid,content=json.dumps(body));assert r.status_code==422,r.text
    assert history(c,pid).json()['items']==[]

@pytest.mark.parametrize('ids',[[],['P-BAL-01'],['P-EVD-01','P-EVD-01'],['P-EVD-01','P-FEE-01']])
def test_unsupported_or_duplicate_policy_selection_is_explicitly_refused(blocked,ids):
    _,c,pid,_=blocked;r=post(c,pid,policies=ids);assert r.status_code==422 and r.json()['code']=='CAPABILITY_UNAVAILABLE'

def test_stale_dependencies_require_maker_revalidation_without_mutation(database,blocked):
    a,c,pid,_=blocked
    # Change one actual captured account version before the action, not a fabricated simulation result.
    p=a.get('/api/v1/proposals/'+pid).json()
    with connection(database=database) as db:
        account=db.execute("SELECT entity_id FROM lv.proposal_read_set WHERE proposal_id=%s AND entity_type='ACCOUNT' LIMIT 1",(pid,)).fetchone()[0];db.execute('UPDATE lv.account SET name=name WHERE id=%s',(account,))
    before=fingerprint(database,{'lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'})
    r=post(c,pid);assert r.status_code==409 and r.json()['code']=='PROPOSAL_STALE'
    assert a.get('/api/v1/proposals/'+pid).json()==p and fingerprint(database,{'lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'})==before
    assert act(a,pid,'revalidate',match='"v2"').json()['proposal']['state']=='STALE'

def test_unrelated_sequence_change_requires_fresh_equivalence_before_override(database,api,world):
    with configuration_connection(database) as db:
        with tenant_transaction(db,world['A']):period=db.execute("INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date) VALUES(%s,%s,'2026-09-01','2026-10-01') RETURNING id",(world['A'],world['l1']['ledger'])).fetchone()[0]
    a,pid,_=submit(api,world);admin=signed_in(api,world,'admin');r=admin.post(f'/api/v1/periods/{period}/close',headers={'If-Match':'"v1"','Idempotency-Key':uuid4().hex});assert r.status_code==200,r.text
    assert act(a,pid,'revalidate',match='"v2"').json()['freshness']['status']=='FRESH'
    r=post(signed_in(api,world,'controller'),pid);assert r.status_code==200,r.text
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='READY_FOR_REVIEW'

def test_withdrawal_after_override_releases_claims_and_keeps_history(database,blocked):
    a,c,pid,_=blocked;assert post(c,pid).status_code==200
    r=act(a,pid,'withdraw',match='"v3"');assert r.status_code==200 and r.json()['state']=='WITHDRAWN'
    assert len(history(c,pid).json()['items'])==1
    with connection(database=database) as db:assert db.execute("SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s AND status='RESERVED'",(pid,)).fetchone()==(0,)

def test_override_does_not_carry_to_a_new_revision(database,api,world,blocked):
    a,c,pid,old=blocked;assert post(c,pid).status_code==200
    change_account(database,world);assert act(a,pid,'revalidate',match='"v3"').json()['proposal']['state']=='STALE'
    fresh=act(a,pid,'resimulate',match='"v4"');assert fresh.status_code==200,fresh.text;assert fresh.json()['proposal']['state']=='BLOCKED' and fresh.json()['proposal']['revision']==2
    h=history(c,pid);assert not h.json()['items'][0]['applies_to_current_revision']
    r=post(c,pid,match='"v5"');assert r.status_code==200,r.text
    first=history(c,pid,params={'limit':'1'}).json();assert len(first['items'])==1 and first['next_cursor']
    second=history(c,pid,params={'cursor':first['next_cursor']}).json();assert second['items'][0]['revision']==2 and second['items'][0]['id']!=first['items'][0]['id']
    other=new_draft(a,world);assert history(c,other,params={'cursor':first['next_cursor']}).status_code==400

def test_read_query_validation_and_conditional_authentication(api,blocked):
    _,c,pid,_=blocked;r=history(c,pid);assert r.status_code==200
    assert history(client(api),pid,headers={'If-None-Match':r.headers['etag']}).status_code==401
    assert history(c,pid,params={'cursor':''}).status_code==400
    assert history(c,pid,params={'unknown':'x'}).status_code==400
    assert history(c,pid,params=[('limit','1'),('limit','2')]).status_code==422

def test_force_rls_immutable_rows_and_narrow_privileges(database,world,blocked):
    _,c,pid,_=blocked;assert post(c,pid).status_code==200
    with connection(database=database) as db:
        assert db.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='lv.proposal_override'::regclass").fetchone()==(True,True)
    for command in ["UPDATE lv.proposal_override SET reason='tampered'",'DELETE FROM lv.proposal_override']:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection(database=database) as db:db.execute(command)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection('lv_app',database) as db:db.execute(command)
    with connection('lv_app',database,autocommit=True) as db:
        with tenant_transaction(db,world['B']):assert db.execute('SELECT count(*) FROM lv.proposal_override').fetchone()==(0,)

def test_competing_controller_requests_make_one_transition(blocked):
    _,c,pid,_=blocked
    with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda _:post(c,pid),range(2)))
    assert sorted(r.status_code for r in responses)==[200,412],[(r.status_code,r.text) for r in responses]
    assert len(history(c,pid).json()['items'])==1

def test_same_session_duplicate_returns_in_progress_before_touch(database,api,blocked):
    _,c,pid,_=blocked;other=client(api);other.cookies.update(c.cookies);other.headers.update(c.headers)
    with connection(database=database) as owner:
        owner.execute('SELECT 1 FROM lv.proposal WHERE id=%s FOR UPDATE',(pid,))
        with ThreadPoolExecutor(2) as pool:
            pending=pool.submit(post,c,pid,key='pending')
            try:
                end=time.monotonic()+5;blocked_on_lock=False
                while time.monotonic()<end:
                    with connection(database=database,autocommit=True) as monitor:blocked_on_lock=monitor.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=%s AND query LIKE 'SELECT lv.override_evidence%%' AND wait_event_type='Lock')",(database,)).fetchone()[0]
                    if blocked_on_lock:break
                    time.sleep(.02)
                assert blocked_on_lock
                assert pool.submit(post,other,pid,key='pending').result(timeout=3).status_code==409
            finally:owner.rollback()
            assert pending.result(timeout=5).status_code==200

def test_revoked_controller_before_sql_and_cached_replay_are_refused(database,world,blocked,monkeypatch):
    _,c,pid,_=blocked
    def revoke():
        with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
    monkeypatch.setattr(overrides,'_before_write',revoke);assert post(c,pid).status_code==403

def test_revocation_prevents_replay_of_a_previously_successful_override(database,world,blocked):
    _,c,pid,_=blocked;assert post(c,pid,key='cached').status_code==200
    with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
    assert post(c,pid,key='cached').status_code==403

def test_disabled_controller_before_wrapper_is_401(database,world,blocked,monkeypatch):
    _,c,pid,_=blocked
    def disable():
        with connection(database=database) as db:db.execute('UPDATE lv_identity.app_user SET disabled_at=clock_timestamp() WHERE id=%s',(world['users']['controller']['id'],))
    monkeypatch.setattr(overrides,'_before_write',disable);assert post(c,pid).status_code==401

def test_draft_cannot_be_overridden_and_get_is_read_only(api,world,blocked,monkeypatch):
    a,c,pid,_=blocked;other=new_draft(a,world);assert post(c,other,match='"v1"').status_code==409
    original=overrides._read_proposal
    def checked(ctx,pid):
        assert ctx.conn.execute('SHOW transaction_read_only').fetchone()==('on',)
        return original(ctx,pid)
    monkeypatch.setattr(overrides,'_read_proposal',checked);assert history(c,pid).status_code==200
    assert post(c,pid,json={'policy_ids':['P-EVD-01'],'reason':'Exact'},params={'unknown':'1'}).status_code==400

def test_reason_limit_and_raw_state_promotion_backstop(database,blocked):
    _,c,pid,_=blocked
    with pytest.raises(psycopg.errors.CheckViolation):
        with connection(database=database) as db:db.execute("UPDATE lv.proposal SET state='READY_FOR_REVIEW',version=version+1 WHERE id=%s",(pid,))
    assert post(c,pid,reason='🙂'*4000).status_code==200

def test_failure_after_wrapper_rolls_back_transition_and_audit(database,blocked,monkeypatch):
    _,c,pid,_=blocked;before=fingerprint(database,set())
    def fail(_):raise RuntimeError('injected rollback')
    monkeypatch.setattr(overrides,'_after_write',fail);assert post(c,pid).status_code==500
    assert fingerprint(database,set())==before

@pytest.mark.parametrize('missing',['audit','cache','transition'])
def test_deferred_guards_refuse_missing_atomic_artifacts(database,blocked,missing):
    _,c,pid,_=blocked
    with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv.override_evidence(bytea,uuid,text,text,text,jsonb,jsonb,text,text)'::regprocedure)").fetchone()[0]
    if missing=='audit':start=original.index(" PERFORM lv_identity.audit_append('PROPOSAL_OVERRIDDEN'");end=original.index('\n response:=',start)
    elif missing=='cache':start=original.index(' INSERT INTO lv.api_idempotency(');end=original.index('\n RETURN response',start)
    else:start=original.index(' INSERT INTO lv.proposal_transition(');end=original.index("\n PERFORM lv_identity.audit_append('PROPOSAL_OVERRIDDEN'",start)
    with connection(database=database) as db:db.execute(original[:start]+original[end:])
    try:assert post(c,pid).status_code==500 and history(c,pid).json()['items']==[]
    finally:
        with connection(database=database) as db:db.execute(original)

def test_override_history_blocks_destructive_downgrade(database,blocked):
    _,c,pid,_=blocked;assert post(c,pid).status_code==200
    with pytest.raises(Exception):migrate(database,'0017_proposal_comments',downgrade=True,disposable=True)
    with connection(database=database) as db:assert db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0037_restore_interlock',)
