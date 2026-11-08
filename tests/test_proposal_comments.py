"""Contracted discussion over real PostgreSQL; no invented READY state or evidence relabeling."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import time
import psycopg,pytest
from tools.dev_db import connection,migrate
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.proposals import comments
from test_proposal_drafts import world,seed,api,client,signed_in,draft,create,fingerprint,counters  # noqa:F401
from test_simulation import new_draft
from test_submission import act

EXCLUDED={'lv.proposal_comment','lv.api_idempotency','lv_identity.audit_event','lv_identity.audit_chain_head'}
def post(c,pid,body='Review the source statement before any decision.',key=None,match=None,**kw):
    headers={'Idempotency-Key':key or uuid4().hex}
    if match is not None:headers['If-Match']=match
    return c.post(f'/api/v1/proposals/{pid}/comments',headers=headers,**({'json':{'body':body}} if not kw else kw))
def get(c,pid,**kw):return c.get(f'/api/v1/proposals/{pid}/comments',**kw)
@pytest.fixture
def proposal(api,world):
    a=signed_in(api,world,'analyst');return a,new_draft(a,world)

def test_analyst_controller_discussion_preserves_every_other_table_and_proposal(database,api,world,proposal):
    a,pid=proposal;controller=signed_in(api,world,'controller');before=fingerprint(database,EXCLUDED);ctr=counters(database);head=a.get('/api/v1/proposals/'+pid).json()
    text='  Evidence question: ₹ and café. <img src=x onerror=alert(1)>\nNo approval.  '
    first=post(a,pid,text,match='"v1"');assert first.status_code==201,first.text;c=first.json()
    assert (c['authored_by'],c['revision'],c['proposal_version'],c['position'],c['body'])==(world['users']['analyst']['id'],1,1,1,text)
    second=post(controller,pid,'Controller: discussion is not an approval.');assert second.status_code==201 and second.json()['position']==2
    r=get(controller,pid);assert r.status_code==200 and len(r.json()['items'])==2 and r.json()['current_version']==1
    assert get(controller,pid,headers={'If-None-Match':r.headers['etag']}).status_code==304
    assert a.get('/api/v1/proposals/'+pid).json()==head and fingerprint(database,EXCLUDED)==before and counters(database)==ctr
    with connection(database=database) as c:
        audit=c.execute("SELECT detail FROM lv_identity.audit_event WHERE event_type='PROPOSAL_COMMENTED' AND detail->>'proposal_id'=%s",(pid,)).fetchall();assert len(audit)==2 and all('body_hash' in row[0] and 'body' not in row[0] for row in audit)

def test_blocked_and_withdrawn_proposals_keep_discussion_without_state_changes(database,api,world,proposal):
    a,pid=proposal;assert act(a,pid).json()['proposal']['state']=='BLOCKED';before=fingerprint(database,EXCLUDED)
    assert post(signed_in(api,world,'controller'),pid).status_code==201
    assert fingerprint(database,EXCLUDED)==before
    assert act(a,pid,'withdraw','"v2"').status_code==200
    assert post(a,pid,'Historical discussion only.').status_code==201
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='WITHDRAWN'

def test_permissions_tenants_anonymous_csrf_and_attribution(api,world,proposal):
    a,pid=proposal
    assert post(client(api),pid).status_code==401 and get(client(api),pid).status_code==401
    admin=signed_in(api,world,'admin');assert post(admin,pid).status_code==403 and get(admin,pid).status_code==200
    b=signed_in(api,world,'analystB');assert post(b,pid).status_code==post(b,uuid4()).status_code==404
    assert get(b,pid).status_code==get(b,uuid4()).status_code==404
    del a.headers['X-CSRF-Token'];assert post(a,pid).status_code==403
    c=signed_in(api,world,'analyst');assert post(c,pid,json={'body':'forged','authored_by':world['users']['controller']['id']}).status_code==422

def test_cross_origin_refused_before_comment(api,world,proposal):
    a,pid=proposal;a.headers['Origin']='https://untrusted.invalid';assert post(a,pid).status_code==403
    assert get(a,pid).json()['items']==[]

def test_missing_key_and_direct_runtime_permission_backstop(database,api,world,proposal):
    from ledgervigil.auth.crypto import token_hash
    a,pid=proposal;assert a.post(f'/api/v1/proposals/{pid}/comments',json={'body':'missing key'}).status_code==400
    admin=signed_in(api,world,'admin');token=admin.cookies.get('__Host-lv_session')
    with connection('lv_app',database) as c:
        response=c.execute('SELECT lv.add_proposal_comment(%s,%s,%s,%s,%s,NULL,NULL,%s,%s)',(token_hash(token),pid,'direct','0'*64,'No runtime bypass','127.0.0.1','direct-comment-probe')).fetchone()[0]
        assert response['status']==403
    assert get(a,pid).json()['items']==[]

def test_read_transaction_is_read_only_and_304_requires_authentication(api,proposal,monkeypatch):
    a,pid=proposal;original=comments._read_proposal
    def checked(ctx,pid):
        assert ctx.conn.execute('SHOW transaction_read_only').fetchone()==('on',)
        return original(ctx,pid)
    monkeypatch.setattr(comments,'_read_proposal',checked);r=get(a,pid);assert r.status_code==200
    assert get(client(api),pid,headers={'If-None-Match':r.headers['etag']}).status_code==401

@pytest.mark.parametrize('payload',[{},[],{'body':''},{'body':' \n\t'}, {'body':'x'*4001},{'body':12},{'body':'x','revision':1},{'body':'\x00'},{'body':'\ud800'}])
def test_invalid_plain_text_is_422_without_comment(api,proposal,payload):
    a,pid=proposal;r=post(a,pid,content=__import__('json').dumps(payload))
    assert r.status_code==422,r.text;assert get(a,pid).json()['items']==[]

def test_duplicate_json_keys_unknown_query_and_maximum_body(api,proposal):
    a,pid=proposal;assert post(a,pid,content='{"body":"one","body":"two"}').status_code==422
    assert post(a,pid,json={'body':'one'},params={'ignored':'x'}).status_code==400
    assert post(a,pid,'🙂'*4000).status_code==201
    assert post(a,pid,content=b'x'*1048577).status_code==422

def test_success_refusal_and_original_revision_replay(api,proposal):
    a,pid=proposal;r=post(a,pid,'First comment','same',match='"v1"');assert r.status_code==201
    retry=post(a,pid,'First comment','same',match='"v1"');assert retry.json()==r.json() and retry.headers['Idempotent-Replayed']=='true'
    assert post(a,pid,'Changed comment','same',match='"v1"').json()['code']=='IDEMPOTENCY_KEY_REUSED'
    edited=a.patch('/api/v1/proposals/'+pid,headers={'If-Match':'"v1"'},json={'rationale':'Later revision'});assert edited.status_code==200
    assert post(a,pid,'First comment','same',match='"v1"').json()==r.json()
    refusal=post(a,pid,'Unseen revision','refused',match='"v1"');assert refusal.status_code==412
    again=post(a,pid,'Unseen revision','refused',match='"v1"');assert again.status_code==412 and again.headers['Idempotent-Replayed']=='true'
    current=post(a,pid,'Seen revision',match='"v2"');assert current.status_code==201 and current.json()['revision']==2 and current.json()['position']==2
    assert [c['revision'] for c in get(a,pid).json()['items']]==[1,2]

def test_signed_pages_are_scoped_to_proposal_and_current_read(api,world,proposal):
    a,pid=proposal
    for n in range(3):assert post(a,pid,str(n)).status_code==201
    r=get(a,pid,params={'limit':'2'});assert len(r.json()['items'])==2;cursor=r.json()['next_cursor'];assert cursor
    assert [c['position'] for c in get(a,pid,params={'cursor':cursor}).json()['items']]==[3]
    other=new_draft(a,world);assert get(a,other,params={'cursor':cursor}).status_code==400
    assert get(a,pid,params={'cursor':'junk'}).status_code==400
    assert get(a,pid,params={'cursor':''}).status_code==400
    assert get(a,pid,params={'status':'READY'}).status_code==400
    assert get(a,pid,params=[('limit','1'),('limit','2')]).status_code==422

def test_immutable_history_force_rls_and_runtime_function_privileges(database,api,world,proposal):
    a,pid=proposal;assert post(a,pid).status_code==201
    with connection(database=database) as c:
        assert c.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='lv.proposal_comment'::regclass").fetchone()==(True,True)
        assert c.execute("SELECT proconfig FROM pg_proc WHERE oid='lv.add_proposal_comment(bytea,uuid,text,text,text,text,jsonb,text,text)'::regprocedure").fetchone()[0]==['search_path=pg_catalog, pg_temp']
        assert c.execute("SELECT has_function_privilege('public', 'lv.add_proposal_comment(bytea,uuid,text,text,text,text,jsonb,text,text)', 'EXECUTE')").fetchone()==(False,)
    for statement in ['UPDATE lv.proposal_comment SET body=\'tampered\'','DELETE FROM lv.proposal_comment']:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection(database=database) as c:c.execute(statement)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection('lv_app',database) as c:c.execute(statement)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connection('lv_app',database) as c:c.execute('INSERT INTO lv.proposal_comment DEFAULT VALUES')
    with connection('lv_app',database,autocommit=True) as c:
        with tenant_transaction(c,world['B']):assert c.execute('SELECT count(*) FROM lv.proposal_comment').fetchone()==(0,)

def test_concurrent_comments_get_gapless_positions_and_same_key_has_one_effect(database,api,world,proposal):
    a,pid=proposal;b=signed_in(api,world,'controller');clients=[a,b]
    with ThreadPoolExecutor(4) as pool:rs=list(pool.map(lambda n:post(clients[n%2],pid,'Concurrent '+str(n)),range(8)))
    assert all(r.status_code==201 for r in rs),[(r.status_code,r.text) for r in rs]
    assert [c['position'] for c in get(a,pid).json()['items']]==list(range(1,9))
    with ThreadPoolExecutor(2) as pool:rs=list(pool.map(lambda _:post(a,pid,'Same key','competing'),range(2)))
    assert sorted(r.status_code for r in rs) in ([201,201],[201,409])
    assert len(get(a,pid).json()['items'])==9

def test_same_session_inflight_returns_409_before_touch_lock(database,api,proposal):
    a,pid=proposal;b=client(api);b.cookies.update(a.cookies);b.headers.update(a.headers)
    with connection(database=database) as owner:
        owner.execute('SELECT 1 FROM lv.proposal WHERE id=%s FOR UPDATE',(pid,))
        with ThreadPoolExecutor(2) as pool:
            pending=pool.submit(post,a,pid,'pending','locked')
            try:
                end=time.monotonic()+5;blocked=False
                while time.monotonic()<end:
                    with connection(database=database,autocommit=True) as monitor:blocked=monitor.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=%s AND query LIKE 'SELECT lv.add_proposal_comment%%' AND wait_event_type='Lock')",(database,)).fetchone()[0]
                    if blocked:break
                    time.sleep(.02)
                assert blocked
                assert pool.submit(post,b,pid,'pending','locked').result(timeout=3).status_code==409
            finally:owner.rollback()
            assert pending.result(timeout=5).status_code==201

def test_revocation_refuses_cached_and_before_write_requests(database,api,world,proposal,monkeypatch):
    a,pid=proposal;assert post(a,pid,'cached','revoked').status_code==201
    def revoke():
        with connection(database=database) as c:c.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['analyst']['id']))
    monkeypatch.setattr(comments,'_before_write',revoke);assert post(a,pid,'racing revoke').status_code==403
    assert post(a,pid,'cached','revoked').status_code==403
    assert len(get(signed_in(api,world,'controller'),pid).json()['items'])==1

def test_disabled_user_race_is_401(database,api,world,proposal,monkeypatch):
    a,pid=proposal
    def disable():
        with connection(database=database) as c:c.execute('UPDATE lv_identity.app_user SET disabled_at=clock_timestamp() WHERE id=%s',(world['users']['analyst']['id'],))
    monkeypatch.setattr(comments,'_before_write',disable);assert post(a,pid).status_code==401

def test_failure_after_wrapper_rolls_back_comment_audit_and_cache(database,proposal,monkeypatch):
    a,pid=proposal;before=fingerprint(database,set())
    def fail(_):raise RuntimeError('injected transport boundary failure')
    monkeypatch.setattr(comments,'_after_write',fail);assert post(a,pid).status_code==500
    assert fingerprint(database,set())==before

@pytest.mark.parametrize('remove',['audit','cache'])
def test_deferred_validator_refuses_missing_atomic_artifact(database,proposal,remove):
    a,pid=proposal;signature='lv.add_proposal_comment(bytea,uuid,text,text,text,text,jsonb,text,text)'
    with connection(database=database) as c:original=c.execute('SELECT pg_get_functiondef(%s::regprocedure)',(signature,)).fetchone()[0]
    if remove=='audit':
        start=original.index("  PERFORM lv_identity.audit_append('PROPOSAL_COMMENTED'");end=original.index('\n  response:=',start);broken=original[:start]+original[end:]
    else:
        start=original.index(' INSERT INTO lv.api_idempotency(');end=original.index('\n RETURN response',start);broken=original[:start]+original[end:]
    with connection(database=database) as c:c.execute(broken)
    try:assert post(a,pid).status_code==500 and get(a,pid).json()['items']==[]
    finally:
        with connection(database=database) as c:c.execute(original)

def test_populated_comment_history_blocks_destructive_downgrade(database,proposal):
    a,pid=proposal;assert post(a,pid).status_code==201
    with pytest.raises(Exception):migrate(database,'0016_source_registry',downgrade=True,disposable=True)
    with connection(database=database) as c:assert c.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0038_identity_issuer',)
