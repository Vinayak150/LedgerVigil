"""Real PostgreSQL workflow proofs. No source relabeling, approval or financial execution."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import threading
import pytest
from tools.dev_db import connection
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.proposals import submission as workflow
from test_proposal_drafts import world,seed,signed_in,create,draft,settlement,client,problem,fingerprint,counters  # noqa:F401
from test_simulation import api,new_draft,get_run  # noqa:F401

TABLES={'lv.ledger_snapshot','lv.proposal_read_set','lv.verdict','lv.proposal_transition','lv.staged_workflow','lv.reservation_event','lv.simulation_run'}
EXCLUDED=TABLES|{'lv.proof_pack','lv.proposal','lv.proposal_revision','lv.proposal_reference','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'}

def act(c,pid,kind='submit',match='"v1"',key=None,**kw):
    headers={'Idempotency-Key':key or 'wf-'+uuid4().hex}
    if match is not None:headers['If-Match']=match
    return c.post(f'/api/v1/proposals/{pid}/{kind}',headers=headers,**kw)

def counts(db,tenant):
    with connection(database=db) as a:return {t:a.execute('SELECT count(*) FROM '+t+' WHERE tenant_id=%s',(tenant,)).fetchone()[0] for t in sorted(TABLES)}

def test_submit_is_blocked_and_atomically_pins_every_artifact_without_financial_effect(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);before=fingerprint(database,EXCLUDED);ctr=counters(database)
    r=act(a,pid);assert r.status_code==200,r.text
    body=r.json();p,s=body['proposal'],body['simulation']
    assert (p['state'],p['version'],p['revision'])==('BLOCKED',2,1)
    assert r.headers['etag']=='"v2"' and p['submitted_by']==world['users']['analyst']['id']
    assert p['latest_simulation_id']==s['id'] and p['snapshot']==s['snapshot'] and p['read_set']==s['read_set']
    assert s['outcome']['aggregate']=='BLOCKED_INSUFFICIENT' and not s['outcome']['eligible_for_execution']
    assert len([e for e in p['read_set'] if e['entity_type']=='ACCOUNT'])==7
    assert get_run(a,s['id'])==s
    assert a.post(f"/api/v1/simulations/{s['id']}/replay").json()['identical'] is True
    n=counts(database,world['A']);assert n['lv.ledger_snapshot']==1 and n['lv.verdict']==14 and n['lv.proposal_transition']==2
    assert fingerprint(database,EXCLUDED)==before and counters(database)==ctr
    with connection(database=database) as db:
        assert db.execute('SELECT status,proposal_id::text FROM lv.staged_workflow WHERE tenant_id=%s',(world['A'],)).fetchall()==[('RESERVED',pid)]


def test_withdraw_releases_and_another_draft_can_reserve(database,api,world):
    a=signed_in(api,world,'analyst');p1=new_draft(a,world);p2=new_draft(a,world)
    assert act(a,p1).status_code==200
    held=act(a,p2);assert problem(held,422,'PRECONDITION_FAILED')['reason']=='STAGED_DOCUMENT_RESERVED'
    r=act(a,p1,'withdraw','"v2"');assert r.status_code==200,r.text
    assert r.json()['state']=='WITHDRAWN' and r.headers['etag']=='"v3"'
    r=act(a,p2);assert r.status_code==200,r.text
    with connection(database=database) as db:
        assert db.execute('SELECT status,version,proposal_id::text FROM lv.staged_workflow WHERE tenant_id=%s',(world['A'],)).fetchall()==[('RESERVED',4,p2)]
        assert [r[0] for r in db.execute('SELECT event FROM lv.reservation_event WHERE tenant_id=%s ORDER BY version',(world['A'],))]==['RESERVED','RELEASED','RESERVED']


def test_key_replays_success_and_refusal_and_changed_request_is_rejected(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);key='submit-idem'
    r=act(a,pid,key=key);assert r.status_code==200,r.text;n=counts(database,world['A'])
    retry=act(a,pid,key=key);assert retry.status_code==200 and retry.json()==r.json() and retry.headers['idempotent-replayed']=='true'
    assert counts(database,world['A'])==n
    problem(act(a,pid,key=key,match='"v2"'),422,'IDEMPOTENCY_KEY_REUSED')
    p2=new_draft(a,world);key='refusal-idem';bad=act(a,p2,key=key)
    assert bad.status_code==422
    retry=act(a,p2,key=key);assert retry.status_code==422 and retry.headers['idempotent-replayed']=='true'


def test_permissions_tenants_csrf_and_maker_are_enforced(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    problem(act(client(api),pid),401,'UNAUTHENTICATED')
    for role in ('admin','controller','automation'):
        c=signed_in(api,world,role);problem(act(c,pid),403,'PERMISSION_DENIED')
    b=signed_in(api,world,'analystB');problem(act(b,pid),404,'NOT_FOUND');problem(act(b,uuid4()),404,'NOT_FOUND')
    no=signed_in(api,world,'analyst');del no.headers['X-CSRF-Token'];problem(act(no,pid),403,'PERMISSION_DENIED')
    assert counts(database,world['A'])['lv.simulation_run']==0


def test_versions_and_invalid_body_refuse_before_workflow_effects(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    problem(act(a,pid,match=None),428,'VERSION_REQUIRED')
    problem(act(a,pid,match='"v99"'),412,'VERSION_MISMATCH')
    problem(act(a,pid,json={'state':'APPROVED'}),422,'SCHEMA_INVALID')
    assert all(v==0 for v in counts(database,world['A']).values())


@pytest.mark.parametrize('stage',['after_begin','before_finish','after_finish'])
def test_injected_failure_rolls_back_every_workflow_artifact_and_reservation(database,api,world,monkeypatch,stage):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);before=fingerprint(database,EXCLUDED);n=counts(database,world['A'])
    def fail(*_):raise RuntimeError('deliberate workflow failure')
    monkeypatch.setattr(workflow,'_'+stage,fail)
    problem(act(a,pid),500,'INTERNAL')
    assert counts(database,world['A'])==n and fingerprint(database,EXCLUDED)==before
    assert a.get(f'/api/v1/proposals/{pid}').json()['state']=='DRAFT'


def test_typed_rejection_keeps_draft_and_no_simulation_or_reservation(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world,operations=[settlement(world,fee='1000.00',net='51500.49')])
    r=act(a,pid);assert problem(r,422,'PRECONDITION_FAILED')['reason']=='SETTLEMENT_TOTALS'
    assert counts(database,world['A'])['lv.simulation_run']==0 and counts(database,world['A'])['lv.staged_workflow']==0
    assert a.get(f'/api/v1/proposals/{pid}').json()['state']=='DRAFT'


def test_competing_proposals_have_exactly_one_reservation_winner(database,api,world):
    a=signed_in(api,world,'analyst');b=signed_in(api,world,'analyst');p=[new_draft(a,world),new_draft(a,world)];barrier=threading.Barrier(2)
    def run(n):barrier.wait(timeout=10);return act([a,b][n],p[n])
    with ThreadPoolExecutor(max_workers=2) as pool:rs=list(pool.map(run,range(2)))
    assert sorted(r.status_code for r in rs)==[200,422],[(r.status_code,r.text) for r in rs]
    assert counts(database,world['A'])['lv.simulation_run']==1


def test_inflight_duplicate_returns_409_even_with_the_same_session_cookie(database,api,world,monkeypatch):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);b=client(api);b.cookies.update(a.cookies);b.headers.update(a.headers)
    entered=threading.Event();release=threading.Event()
    def pause():entered.set();assert release.wait(timeout=10)
    monkeypatch.setattr(workflow,'_after_begin',pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(act,a,pid,key='inflight')
        try:
            assert entered.wait(timeout=10)
            other=pool.submit(act,b,pid,key='inflight').result(timeout=4)
            problem(other,409,'REQUEST_IN_PROGRESS')
        finally:release.set()
        assert pending.result(timeout=10).status_code==200


def test_revoked_propose_permission_before_finish_rolls_back(database,api,world,monkeypatch):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    def revoke():
        with connection(database=database) as owner:owner.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['analyst']['id']))
    monkeypatch.setattr(workflow,'_before_finish',revoke)
    problem(act(a,pid),403,'PERMISSION_DENIED')
    assert counts(database,world['A'])['lv.simulation_run']==0 and counts(database,world['A'])['lv.staged_workflow']==0


def test_withdraw_draft_and_idempotent_withdrawal_do_not_create_simulations(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);key='withdraw-idem'
    r=act(a,pid,'withdraw',key=key);assert r.status_code==200,r.text
    assert r.json()['state']=='WITHDRAWN' and r.json()['version']==2
    retry=act(a,pid,'withdraw',key=key);assert retry.json()==r.json() and retry.headers['idempotent-replayed']=='true'
    assert counts(database,world['A'])['lv.simulation_run']==0


def test_identity_mismatch_and_multioperation_are_explicit_refusals(database,api,world):
    a=signed_in(api,world,'analyst')
    wrong=new_draft(a,world,operations=[settlement(world,payout='wrong-payout')])
    assert problem(act(a,wrong),422,'PRECONDITION_FAILED')['reason']=='STATEMENT_IDENTITY_MISMATCH'
    two=new_draft(a,world,operations=[settlement(world),settlement(world)])
    problem(act(a,two),422,'CAPABILITY_UNAVAILABLE')
    assert counts(database,world['A'])['lv.staged_workflow']==0


def test_staged_read_status_and_etag_track_reservation_without_changing_source_facts(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    with connection(database=database) as c:batch=c.execute('SELECT import_batch_id FROM lv.staged_document WHERE id=%s',(world['s1'],)).fetchone()[0]
    path=f'/api/v1/imports/{batch}/staged-documents'
    before=a.get(path);assert before.status_code==200,before.text
    assert before.json()['items'][0]['status']=='VALID' and before.json()['items'][0]['version']==1
    assert act(a,pid).status_code==200
    during=a.get(path,headers={'If-None-Match':before.headers['etag']});assert during.status_code==200
    row=during.json()['items'][0];assert (row['status'],row['validation_status'],row['version'],row['reserved_by_proposal_id'])==('RESERVED','VALID',2,pid)
    assert act(a,pid,'withdraw','"v2"').status_code==200
    after=a.get(path).json()['items'][0];assert after['status']=='VALID' and after['version']==3
    assert after['financial_fingerprint']==before.json()['items'][0]['financial_fingerprint']


def test_edit_and_submit_same_cookie_serialize_without_deadlock(database,api,world,monkeypatch):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);b=client(api);b.cookies.update(a.cookies);b.headers.update(a.headers)
    entered=threading.Event();release=threading.Event()
    def pause():entered.set();assert release.wait(timeout=10)
    monkeypatch.setattr(workflow,'_after_begin',pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submitting=pool.submit(act,a,pid)
        assert entered.wait(timeout=10)
        editing=pool.submit(b.patch,f'/api/v1/proposals/{pid}',json={'rationale':'concurrent edit'},headers={'If-Match':'"v1"'})
        release.set()
        assert submitting.result(timeout=10).status_code==200
        problem(editing.result(timeout=10),409,'INVALID_STATE_TRANSITION')


def test_edit_wins_then_stale_submit_has_no_workflow_effects(database,api,world):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    assert a.patch(f'/api/v1/proposals/{pid}',json={'rationale':'new revision'},headers={'If-Match':'"v1"'}).status_code==200
    problem(act(a,pid),412,'VERSION_MISMATCH')
    assert counts(database,world['A'])['lv.simulation_run']==0


def test_pool_of_two_connections_serves_four_simultaneous_submissions(database,api,world):
    from ledgervigil.auth.pool import ConnectionPool
    api.state.pool.close();api.state.pool=ConnectionPool(lambda:connection('lv_app',database),max_size=2)
    clients=[signed_in(api,world,'analyst') for _ in range(4)];pids=[new_draft(clients[0],world) for _ in range(4)]
    barrier=threading.Barrier(4)
    def run(n):barrier.wait(timeout=10);return act(clients[n],pids[n])
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(run,n) for n in range(4)]
        codes=sorted(f.result(timeout=10).status_code for f in futures)
    assert codes==[200,422,422,422]


def test_commit_validator_refuses_incomplete_dependency_artifact_even_after_finish(database,api,world,monkeypatch):
    # Fault on the server-computed bundle; database completeness re-derivation must independently refuse it at COMMIT.
    from ledgervigil.simulation import bundle
    original=bundle.capture_submitted
    def missing(*args):
        value=original(*args);value['read_set']=[r for r in value['read_set'] if r['entity_type']!='ACCOUNT'];return value
    monkeypatch.setattr(bundle,'capture_submitted',missing)
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    problem(act(a,pid),500,'INTERNAL')
    assert counts(database,world['A'])['lv.staged_workflow']==0


def test_synthetic_evidence_cannot_be_manufactured_into_ready(database,api,world,monkeypatch):
    from ledgervigil.simulation import engine
    original=engine.evaluate
    def altered(b):
        out=original(b)
        for v in out['verdicts']:
            if v['policy_id']=='P-EVD-01':v['outcome']='PASS'
        out['aggregate']='CLEAR';return out
    monkeypatch.setattr(engine,'evaluate',altered)
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    problem(act(a,pid),500,'INTERNAL')
    assert counts(database,world['A'])['lv.simulation_run']==0


@pytest.mark.parametrize('kind',['configuration','posting','period'])
def test_concurrent_ledger_changes_wait_for_the_submission_snapshot(database,api,world,monkeypatch,kind):
    import queue
    from test_posting import wait_for_block
    from test_account_readsets import journal_body
    from ledgervigil.ledger.seed import seed_manual_journal
    from period_close_support import admin_token,close_period
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);started=threading.Event();writer_pid=queue.Queue()
    token=admin_token(database,world['A']) if kind=='period' else None
    with connection(database=database) as db:
        old_seq=db.execute('SELECT last_seq FROM lv.ledger_counter WHERE ledger_id=%s',(world['l1']['ledger'],)).fetchone()[0]
        period=db.execute('SELECT id FROM lv.fiscal_period WHERE ledger_id=%s',(world['l1']['ledger'],)).fetchone()[0]
        bank=str(world['l1']['chart']['BANK_OPERATING']);old_version=db.execute('SELECT version FROM lv.account WHERE id=%s',(bank,)).fetchone()[0]
    pool=ThreadPoolExecutor(max_workers=1);future=[]
    def writer():
        with connection('lv_migrator' if kind=='configuration' else ('lv_app' if kind=='period' else 'lv_seed'),database,autocommit=True) as c:
            if kind=='configuration':c.execute('SET ROLE lv_owner')
            writer_pid.put(c.info.backend_pid);started.set()
            if kind=='configuration':
                with tenant_transaction(c,world['A']):return c.execute("UPDATE lv.account SET name='SYNTHETIC later rename' WHERE id=%s RETURNING version",(bank,)).fetchone()[0]
            if kind=='posting':return seed_manual_journal(c,world['A'],world['l1']['ledger'],journal_body(world['l1'],'during-submit'))
            from period_close_support import close_call
            with c.transaction():return close_call(c,token,period)
    def pause():
        future.append(pool.submit(writer));assert started.wait(timeout=5)
        with connection(database=database) as db:
            blocker=db.execute("SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND query LIKE 'SELECT lv.workflow_begin%' AND pid<>pg_backend_pid()").fetchone()
        assert blocker
        wait_for_block(database,writer_pid.get(timeout=5),blocker[0])
    monkeypatch.setattr(workflow,'_after_begin',pause)
    try:
        result=act(a,pid);assert result.status_code==200,result.text
        future[0].result(timeout=10)
    finally:pool.shutdown(wait=True)
    run=result.json()['simulation'];assert run['snapshot']['seq']==old_seq
    assert {r['entity_id']:r['version'] for r in run['read_set'] if r['entity_type']=='ACCOUNT'}[bank]==old_version
    assert a.post(f"/api/v1/simulations/{run['id']}/replay").json()['identical'] is True


def test_database_history_is_immutable_and_runtime_cannot_write_workflow_tables(database,api,world):
    import psycopg
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);assert act(a,pid).status_code==200
    for t in TABLES:
        if t=='lv.simulation_run':continue
        with connection('lv_app',database,autocommit=True) as c:
            with tenant_transaction(c,world['A']):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with c.transaction():c.execute('DELETE FROM '+t)
    with connection(database=database) as owner:
        for t in TABLES-{'lv.staged_workflow'}:
            with pytest.raises(psycopg.errors.CheckViolation,match='immutable'):
                with owner.transaction():owner.execute('DELETE FROM '+t+' WHERE tenant_id=%s',(world['A'],))


@pytest.mark.parametrize('cause',['duplicate','closed','settled'])
def test_live_preconditions_leave_draft_and_release_tentative_reservations(database,api,world,seed,cause):
    from test_simulation import settle
    from period_close_support import admin_token,close_period
    a=signed_in(api,world,'analyst')
    if cause=='settled':
        cfg=world['l2'];settle(seed,cfg,'STL-OTHER',[(world['q1'],'50000.00')],'50000.00','0.00','50000.00')
        pid=new_draft(a,world,ledger=cfg['ledger'],operations=[settlement(world,(world['q1'],'50000.00'),statement=world['s2'],gross='50000.00',fee='1000.00',net='49000.00',payout='STL-9001')],evidence_refs=[])
        reason='ITEM_ALREADY_SETTLED'
    else:
        cfg=world['l1'];pid=new_draft(a,world)
        if cause=='duplicate':
            settle(seed,cfg,'STL-3001',[(world['p1'],'50000.00'),(world['p2'],'2500.50')],'52500.50','1000.00','51500.50');reason='DUPLICATE_SETTLEMENT'
        else:
            with connection(database=database) as db:period=db.execute('SELECT id FROM lv.fiscal_period WHERE ledger_id=%s',(cfg['ledger'],)).fetchone()[0]
            assert close_period(database,admin_token(database,world['A']),period)['status']==200;reason='PERIOD_CLOSED'
    before=fingerprint(database,EXCLUDED);r=act(a,pid)
    assert problem(r,422,'PRECONDITION_FAILED')['reason']==reason
    assert fingerprint(database,EXCLUDED)==before and counts(database,world['A'])['lv.staged_workflow']==0
    assert a.get(f'/api/v1/proposals/{pid}').json()['state']=='DRAFT'


def test_user_disabled_before_final_permission_check_cannot_commit(database,api,world,monkeypatch):
    a=signed_in(api,world,'analyst');pid=new_draft(a,world)
    def disable():
        with connection(database=database) as c:c.execute('UPDATE lv_identity.app_user SET disabled_at=statement_timestamp() WHERE id=%s',(world['users']['analyst']['id'],))
    monkeypatch.setattr(workflow,'_before_finish',disable)
    problem(act(a,pid),401,'UNAUTHENTICATED')
    assert counts(database,world['A'])['lv.simulation_run']==0 and counts(database,world['A'])['lv.staged_workflow']==0


def test_source_ingestion_waits_for_the_submission_capture_domain(database,api,world,monkeypatch):
    import queue
    from test_posting import wait_for_block
    from test_proposal_drafts import _statement
    from ledgervigil.ingestion.store import ingest_statement
    from ledgervigil.ingestion import statement_format as fmt
    import json
    a=signed_in(api,world,'analyst');pid=new_draft(a,world);writer_pid=queue.Queue();started=threading.Event()
    cfg=world['l1'];raw=json.dumps({'format':fmt.FORMAT,'version':fmt.VERSION,'data_classification':'SYNTHETIC','statement':{
    'tenant':str(world['A']),'ledger':str(cfg['ledger']),'processor':'processor-1','payout':'STL-3001','date':'2026-10-14','currency':'INR',
    'items':[{'id':'PMT-2001','kind':'PAYMENT','amount':'50000.00'},{'id':'PMT-2002','kind':'PAYMENT','amount':'2500.50'}],
    'gross':'52500.50','fee':'1000.01','net':'51500.49','memo':'SYNTHETIC later conflicting evidence'}}).encode()
    pool=ThreadPoolExecutor(max_workers=1);future=[]
    def writer():
        with connection('lv_ingest',database,autocommit=True) as c:
            writer_pid.put(c.info.backend_pid);started.set()
            return ingest_statement(c,world['A'],cfg['ledger'],raw,declared_format=fmt.FORMAT,declared_version=fmt.VERSION)
    def pause():
        future.append(pool.submit(writer));assert started.wait(timeout=5)
        with connection(database=database) as db:blocker=db.execute("SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND query LIKE 'SELECT lv.workflow_begin%' AND pid<>pg_backend_pid()").fetchone()[0]
        wait_for_block(database,writer_pid.get(timeout=5),blocker)
    monkeypatch.setattr(workflow,'_after_begin',pause)
    try:
        result=act(a,pid);assert result.status_code==200,result.text
        assert future[0].result(timeout=10)['status']=='REJECTED'
    finally:pool.shutdown(wait=True)
    from test_simulation import _bundle
    captured=_bundle(database,result.json()['simulation']['id'])
    assert captured['facts']['statement_conflicts']==[]
