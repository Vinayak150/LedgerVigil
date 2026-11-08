"""Fenced period staleness on real PostgreSQL; no human approval or synthetic evidence bypass."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import threading
import psycopg
import pytest
from tools.dev_db import connection
from ledgervigil.maintenance import runtime
from test_proposal_drafts import world,seed,signed_in,client,problem,fingerprint,counters  # noqa:F401
from test_simulation import api,new_draft  # noqa:F401
from test_submission import act

EXCLUDE={'lv.proposal','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head','lv.proposal_transition','lv.staged_workflow','lv.reservation_event','lv.maintenance_event','lv.work_item','lv.outbox'}

def worker(db):return connection('lv_worker',db)
def close(application,w,ledger='l1'):
    a=signed_in(application,w,'admin')
    with connection(database=application.state.test_database) as db:
        period=db.execute('SELECT id FROM lv.fiscal_period WHERE ledger_id=%s ORDER BY start_date LIMIT 1',(w[ledger]['ledger'],)).fetchone()[0]
    r=a.post(f'/api/v1/periods/{period}/close',headers={'If-Match':'"v1"','Idempotency-Key':uuid4().hex});assert r.status_code==200,r.text
    return r.json()['staleness_job']['id']

def setup_job(database,api,world):
    api.state.test_database=database
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);s=act(a,pid);assert s.status_code==200,s.text
    j=close(api,world)
    return a,pid,j,s.json()['simulation']

def drain(db,t,j,name='test-worker'):
    return runtime.run_job(t,j,worker=name,connection_factory=lambda:worker(db))

def test_period_scan_releases_and_preserves_pinned_financial_state(database,api,world):
    a,pid,j,sim=setup_job(database,api,world);before=fingerprint(database,EXCLUDE);ctr=counters(database)
    result=drain(database,world['A'],j);assert result['status']=='DONE',result
    p=a.get('/api/v1/proposals/'+pid).json();assert (p['state'],p['version'],p['revision'])==('STALE',3,1)
    assert p['latest_simulation_id']==sim['id'] and p['allowed_actions']==['RESIMULATE','WITHDRAW']
    assert a.get('/api/v1/simulations/'+sim['id']).json()==sim
    assert fingerprint(database,EXCLUDE)==before and counters(database)==ctr
    with connection(database=database) as c:
        assert c.execute('SELECT status,proposal_id FROM lv.staged_workflow WHERE tenant_id=%s',(world['A'],)).fetchall()==[('VALID',None)]
        assert c.execute("SELECT user_id,session_id,worker_job_id FROM lv.proposal_transition WHERE proposal_id=%s AND action='stale'",(pid,)).fetchone()==(None,None,pytest.importorskip('uuid').UUID(j))
        assert c.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='PROPOSAL_STALED' AND tenant_id=%s",(world['A'],)).fetchone()[0]==1
    assert drain(database,world['A'],j)['status']=='NOT_CLAIMED'
    r=act(a,pid,'withdraw',match='"v3"');assert r.status_code==200 and r.json()['state']=='WITHDRAWN'

def claim(db,t,j,name='one'):
    with worker(db) as c:return runtime.claim(c,t,j,name)

def age(db,j,*,lease=False):
    with connection(database=db) as c:
        if lease:c.execute("UPDATE lv.work_item SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(j,))
        else:c.execute("UPDATE lv.outbox SET available_at=clock_timestamp()-interval '1 second' WHERE work_item_id=%s",(j,))

def test_concurrent_duplicates_have_one_claim(database,api,world):
    _,_,j,_=setup_job(database,api,world);barrier=threading.Barrier(2)
    def run(n):barrier.wait();return claim(database,world['A'],j,n)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,['one','two']))
    assert sum(r is not None for r in results)==1
    with connection(database=database) as c:assert c.execute('SELECT attempts,fencing_token FROM lv.work_item WHERE id=%s',(j,)).fetchone()==(1,1)

@pytest.mark.parametrize('name,fence', [('wrong',1),('one',999),(None,1),('one',None)])
def test_wrong_owner_token_and_null_fence_cannot_act(database,api,world,name,fence):
    a,pid,j,_=setup_job(database,api,world);claim(database,world['A'],j)
    with worker(database) as c:
        assert runtime.step(c,world['A'],j,name,fence)=={'status':'LOST'}
        assert c.execute('SELECT lv.maintenance_fail(%s,%s,%s,%s,%s)',(world['A'],j,name,fence,'INFRA_RETRYABLE')).fetchone()[0] is False
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'


def test_foreign_and_absent_job_identifiers_are_not_claimed(database,api,world):
    _,_,j,_=setup_job(database,api,world)
    assert claim(database,world['B'],j) is None and claim(database,world['A'],str(uuid4())) is None
    with worker(database) as c:assert runtime.step(c,world['B'],j,'one',1)=={'status':'LOST'}


def test_expired_claim_sweeper_fences_old_worker_and_honors_backoff(database,api,world):
    a,pid,j,_=setup_job(database,api,world);old=claim(database,world['A'],j);age(database,j,lease=True)
    with worker(database) as c:
        assert runtime.step(c,world['A'],j,'one',old['fencing_token'])=={'status':'LOST'}
        assert c.execute('SELECT lv.maintenance_sweep(100)').fetchone()[0]>=1
    assert claim(database,world['A'],j,'two') is None  # an old dispatched message cannot bypass the new backoff
    age(database,j);new=claim(database,world['A'],j,'two');assert new['fencing_token']>old['fencing_token'] and new['attempt']==2
    with worker(database) as c:assert runtime.step(c,world['A'],j,'one',old['fencing_token'])=={'status':'LOST'}
    with worker(database) as c:assert runtime.step(c,world['A'],j,'two',new['fencing_token'])['state']=='STALE'
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE'


def test_retry_exhaustion_is_durable_dead_and_audited(database,api,world):
    a,pid,j,_=setup_job(database,api,world)
    for n in range(5):
        token=claim(database,world['A'],j);assert token
        with worker(database) as c:assert c.execute('SELECT lv.maintenance_fail(%s,%s,%s,%s,%s)',(world['A'],j,'one',token['fencing_token'],'INFRA_RETRYABLE')).fetchone()[0]
        age(database,j)
    assert claim(database,world['A'],j) is None
    with connection(database=database) as c:
        assert c.execute('SELECT state,attempts,last_error_class FROM lv.work_item WHERE id=%s',(j,)).fetchone()==('DEAD',5,'INFRA_RETRYABLE')
        assert c.execute("SELECT count(*) FROM lv_identity.audit_event WHERE tenant_id=%s AND event_type='MAINTENANCE_JOB_DEAD'",(world['A'],)).fetchone()[0]>=1
    with worker(database) as c:assert c.execute('SELECT lv.maintenance_health()').fetchone()[0]['DEAD']>=1
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'


def test_failed_stale_transaction_rolls_back_state_release_audit_and_cursor(database,api,world):
    a,pid,j,_=setup_job(database,api,world);token=claim(database,world['A'],j)
    with connection(database=database) as c:
        c.execute("CREATE FUNCTION lv.test_refuse_stale_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event_type='PROPOSAL_STALED' THEN RAISE EXCEPTION 'injected audit failure'; END IF; RETURN NEW; END $$")
        c.execute('CREATE TRIGGER test_refuse_stale_audit BEFORE INSERT ON lv_identity.audit_event FOR EACH ROW EXECUTE FUNCTION lv.test_refuse_stale_audit()')
    try:
        with pytest.raises(psycopg.Error):
            with worker(database) as c:runtime.step(c,world['A'],j,'one',token['fencing_token'])
        assert a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'
        with connection(database=database) as c:
            assert c.execute('SELECT scan_cursor,stale_count FROM lv.work_item WHERE id=%s',(j,)).fetchone()==(None,0)
            assert c.execute('SELECT status,proposal_id FROM lv.staged_workflow WHERE proposal_id=%s',(pid,)).fetchone()[0]=='RESERVED'
            assert c.execute("SELECT count(*) FROM lv.maintenance_event WHERE job_id=%s AND event='STALE'",(j,)).fetchone()[0]==0
    finally:
        with connection(database=database) as c:c.execute('DROP TRIGGER test_refuse_stale_audit ON lv_identity.audit_event; DROP FUNCTION lv.test_refuse_stale_audit()')


def test_no_direct_worker_data_access_or_runtime_worker_function_access(database,api,world):
    _,_,j,_=setup_job(database,api,world)
    for role in ['lv_app','lv_seed','lv_ingest']:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection(role,database) as c:c.execute('SELECT lv.maintenance_claim(%s,%s,%s)',(world['A'],j,'one'))
    for table in ['proposal','journal_line','work_item','outbox','maintenance_event']:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with worker(database) as c:c.execute('SELECT * FROM lv.'+table)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with worker(database) as c:c.execute('SELECT lv.maintenance_context(%s)',(world['A'],))


def test_owner_cannot_forge_a_system_stale_transition(database,api,world):
    a,pid,_,_=setup_job(database,api,world)
    with pytest.raises(psycopg.errors.CheckViolation):
        with connection(database=database) as c:c.execute("UPDATE lv.proposal SET state='STALE',version=version+1 WHERE id=%s",(pid,))
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'


def test_publish_before_dispatch_crash_republishes_ids_only(database,api,world):
    _,_,j,_=setup_job(database,api,world);published=[]
    def send(t,job):published.append((t,job))
    def crash(r):
        if r['job_id']==j:raise RuntimeError('injected after publish before dispatched commit')
    # The full suite has earlier tenants' queued jobs. Reach this job through bounded FIFO batches.
    with pytest.raises(RuntimeError):
        for _ in range(100):
            with worker(database) as c:runtime.relay(c,send,after_publish=crash)

    with connection(database=database) as c:assert c.execute('SELECT status FROM lv.outbox WHERE work_item_id=%s',(j,)).fetchone()[0]=='PENDING'
    with worker(database) as c:runtime.relay(c,send)
    assert published.count((str(world['A']),j))==2
    assert all(len(p)==2 and all(isinstance(v,str) for v in p) for p in published)
    assert drain(database,world['A'],j)['status']=='DONE'
    assert drain(database,world['A'],j)['status']=='NOT_CLAIMED'


def test_redis_loss_after_dispatch_is_recoverable_from_postgres(database,api,world):
    _,_,j,_=setup_job(database,api,world)
    with worker(database) as c:runtime.relay(c,lambda *_:None) # transport lost these messages
    with connection(database=database) as c:c.execute("UPDATE lv.outbox SET dispatched_at=clock_timestamp()-interval '31 seconds' WHERE work_item_id=%s",(j,))
    with worker(database) as c:c.execute('SELECT lv.maintenance_sweep(100)')
    with connection(database=database) as c:assert c.execute("SELECT count(*) FROM lv.outbox WHERE work_item_id=%s AND status='PENDING'",(j,)).fetchone()[0]==1
    assert drain(database,world['A'],j)['status']=='DONE'


def test_terminal_and_unrelated_drafts_are_untouched(database,api,world):
    api.state.test_database=database;a=signed_in(api,world,'analyst');pid=new_draft(a,world);other=new_draft(a,world)
    assert act(a,pid).status_code==200
    # Closing L2's period must not invalidate L1's pinned dependencies.
    j=close(api,world,'l2');assert drain(database,world['A'],j)['status']=='DONE'
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'
    assert a.get('/api/v1/proposals/'+other).json()['state']=='DRAFT'
    assert act(a,pid,'withdraw',match='"v2"').status_code==200
    j=close(api,world);assert drain(database,world['A'],j)['status']=='DONE'
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='WITHDRAWN'


def test_partial_progress_resumes_without_duplicate_stale_effects(database,api,world,monkeypatch):
    from test_proposal_drafts import _statement,settlement
    a=signed_in(api,world,'analyst');api.state.test_database=database;p1=new_draft(a,world);assert act(a,p1).status_code==200
    s=_statement(database,world['l1'],'STL-3002',[('PMT-2001','50000.00'),('PMT-2002','2500.50')],'52500.50','1000.00','51500.50')
    op=settlement(world);op.update(payout_id='STL-3002',statement_document_id=s['staged_document_id'])
    p2=new_draft(a,world,operations=[op],evidence_refs=[{'kind':'STAGED_DOCUMENT','id':s['staged_document_id']}]);assert act(a,p2).status_code==200
    j=close(api,world)
    def fail_after_progress(result):
        if result['status']=='PROGRESS':raise RuntimeError('injected worker loss after committed progress')
    monkeypatch.setattr(runtime,'_after_step',fail_after_progress)
    assert drain(database,world['A'],j)['status']=='FAILED'
    monkeypatch.setattr(runtime,'_after_step',None);age(database,j)
    assert drain(database,world['A'],j)['status']=='DONE'
    with connection(database=database) as c:
        assert c.execute('SELECT scanned_count,stale_count,attempts FROM lv.work_item WHERE id=%s',(j,)).fetchone()==(2,2,2)
        assert c.execute("SELECT count(*) FROM lv.proposal_transition WHERE proposal_id IN (%s,%s) AND action='stale'",(p1,p2)).fetchone()[0]==2


def test_scheduler_exclusivity(database):
    stop=threading.Event();stop.set()
    with worker(database) as c:
        assert c.execute('SELECT pg_try_advisory_lock(%s)',(runtime.SCHEDULER_LOCK,)).fetchone()[0];c.commit()
        assert runtime.scheduler(lambda:worker(database),lambda *_:None,stop) is False
    assert runtime.scheduler(lambda:worker(database),lambda *_:None,stop) is True


def test_maintenance_history_is_immutable_and_tenant_scoped(database,api,world):
    _,_,j,_=setup_job(database,api,world);assert drain(database,world['A'],j)['status']=='DONE'
    from ledgervigil.db.tenant import tenant_transaction
    with connection('lv_app',database,autocommit=True) as c:
        with tenant_transaction(c,world['B']):assert c.execute('SELECT * FROM lv.maintenance_event WHERE job_id=%s',(j,)).fetchall()==[]
        with tenant_transaction(c,world['A']):assert c.execute('SELECT count(*) FROM lv.maintenance_event WHERE job_id=%s',(j,)).fetchone()[0]>=3
    for verb in ['UPDATE lv.maintenance_event SET event=event','DELETE FROM lv.maintenance_event']:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection(database=database) as c:c.execute(verb+' WHERE job_id=%s',(j,))


def test_withdraw_race_has_one_release_and_no_partial_state(database,api,world):
    a,pid,j,_=setup_job(database,api,world);barrier=threading.Barrier(2)
    def scan():barrier.wait();return drain(database,world['A'],j)
    def withdraw():barrier.wait();return act(a,pid,'withdraw',match='"v2"')
    with ThreadPoolExecutor(2) as pool:
        x=pool.submit(scan);y=pool.submit(withdraw);result=x.result(timeout=10);r=y.result(timeout=10)
    assert result['status']=='DONE'
    if r.status_code==412:r=act(a,pid,'withdraw',match='"v3"')
    assert r.status_code==200 and r.json()['state']=='WITHDRAWN'
    with connection(database=database) as c:
        assert c.execute("SELECT count(*) FROM lv.reservation_event WHERE proposal_id=%s AND event='RELEASED'",(pid,)).fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM lv.staged_workflow WHERE proposal_id=%s AND status='RESERVED'",(pid,)).fetchone()[0]==0

@pytest.mark.parametrize('name', ['maintenance_due','maintenance_sweep'])
@pytest.mark.parametrize('bound',[None,0,101])
def test_unbounded_maintenance_requests_are_refused(database,name,bound):
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        with worker(database) as c:c.execute('SELECT lv.'+name+'(%s)',(bound,))


def test_commit_validator_independently_refuses_missing_reservation_release(database,api,world):
    a,pid,j,_=setup_job(database,api,world);token=claim(database,world['A'],j)
    with connection(database=database) as c:
        original=c.execute("SELECT pg_get_functiondef('lv.maintenance_step(uuid,uuid,text,bigint)'::regprocedure)").fetchone()[0]
        start=original.index(' WITH released AS (');end=original.index(' PERFORM lv_identity.audit_append',start)
        c.execute(original[:start]+original[end:])
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            with worker(database) as c:runtime.step(c,world['A'],j,'one',token['fencing_token'])
        assert a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'
        with connection(database=database) as c:assert c.execute('SELECT status FROM lv.staged_workflow WHERE proposal_id=%s',(pid,)).fetchone()[0]=='RESERVED'
    finally:
        with connection(database=database) as c:c.execute(original)


def test_expired_lease_cannot_be_renewed_after_tentative_effects(database,api,world):
    a,pid,j,_=setup_job(database,api,world);token=claim(database,world['A'],j)
    with connection(database=database) as c:
        original=c.execute("SELECT pg_get_functiondef('lv.maintenance_step(uuid,uuid,text,bigint)'::regprocedure)").fetchone()[0]
        needle=' UPDATE lv.work_item SET scan_cursor=p.id'
        assert needle in original
        # Deterministic mid-step expiry avoids a timing-dependent sleep test.
        c.execute(original.replace(needle," UPDATE lv.work_item SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=t AND id=j;\n"+needle,1))
    try:
        with pytest.raises(psycopg.errors.SerializationFailure):
            with worker(database) as c:runtime.step(c,world['A'],j,'one',token['fencing_token'])
        assert a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'
        with connection(database=database) as c:assert c.execute('SELECT scan_cursor,stale_count FROM lv.work_item WHERE id=%s',(j,)).fetchone()==(None,0)
    finally:
        with connection(database=database) as c:c.execute(original)
