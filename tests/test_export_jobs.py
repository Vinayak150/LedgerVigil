from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest,psycopg
from tools.dev_db import connection
from test_proposal_drafts import world,seed,signed_in,counters,fingerprint
from test_simulation import api
from test_native_approval import ready
from test_native_execution import approved,post
from ledgervigil.exports import runtime
from ledgervigil.execution.proof_verify import verify

@pytest.fixture
def export(database,world,approved):
 a,c,pid,_=approved;r=post(c,pid);assert r.status_code==200;eid=r.json()['id'];return a,c,eid

def create(c,eid,key=None,body=None):return c.post('/api/v1/executions/'+eid+'/exports',json=body if body is not None else {'format':'JSON'},headers={'Idempotency-Key':key or uuid4().hex})
def work(database,t,j,worker='test-worker'):return runtime.run_job(t,j,connection_factory=lambda:connection('lv_worker',database),worker=worker)
def get(c,j):return c.get('/api/v1/execution-proof-exports/'+j)
def link(c,j):return c.post('/api/v1/execution-proof-exports/'+j+'/download-link')

def test_job_build_store_issue_and_download_exact_verified_bytes(database,world,export):
 a,c,e=export;before=fingerprint(database,{'lv.native_export_job','lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'});seq=counters(database)
 r=create(c,e);assert r.status_code==202,r.text;j=r.json()['id'];assert get(c,j).json()['state']=='QUEUED';assert link(c,j).status_code==409
 result=work(database,world['A'],j);assert result['status']=='DONE',result
 status=get(c,j);assert status.json()['state']=='DONE';l=link(c,j);assert l.status_code==200,l.text
 r=c.get(l.json()['url']);assert r.status_code==200,r.text;assert verify(r.content,r.headers['X-Proof-SHA256'])['valid'];assert status.json()['content_hash']==r.headers['X-Proof-SHA256']
 from test_execution_proof import raw_export
 assert r.content==raw_export(database,world,e)
 assert len(r.content)==status.json()['bytes'] and r.headers['Cache-Control']=='no-store'
 assert work(database,world['A'],j)['status']=='NOT_CLAIMED'
 assert fingerprint(database,{'lv.native_export_job','lv_identity.session','lv_identity.audit_event','lv_identity.audit_chain_head'})==before and counters(database)==seq


def test_concurrent_idempotent_requests_have_one_job_and_one_request_audit(database,world,export):
 _,c,e=export
 with ThreadPoolExecutor(8) as pool:rs=list(pool.map(lambda _:create(c,e,key='eight-requests'),range(8)))
 assert all(r.status_code==202 for r in rs),[(r.status_code,r.text) for r in rs]
 ids={r.json()['id'] for r in rs};assert len(ids)==1
 with connection(database=database) as db:assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='NATIVE_EXPORT_REQUESTED' AND detail->>'job_id'=%s",(next(iter(ids)),)).fetchone()==(1,)

@pytest.mark.parametrize('role',['analyst','admin','analystB'])
def test_export_permissions_and_ownership(api,world,export,role):
 _,c,e=export;j=create(c,e).json()['id'];other=signed_in(api,world,role);assert create(other,e).status_code==403;assert get(other,j).status_code==403


def test_other_exporter_cannot_read_foreign_or_other_user_jobs(database,api,world,export):
 _,c,e=export;j=create(c,e).json()['id'];other=signed_in(api,world,'automation');assert get(other,j).status_code==404
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING')",(world['B'],world['users']['analystB']['id']))
 other=signed_in(api,world,'analystB');assert create(other,e).status_code==404;assert get(other,j).status_code==404


def test_link_tampering_expiry_and_session_rotation_fail(database,world,export,monkeypatch):
 from ledgervigil.exports import routes
 _,c,e=export;j=create(c,e).json()['id'];assert work(database,world['A'],j)['status']=='DONE';url=link(c,j).json()['url']
 assert c.get(url[:-3]+'XXX').status_code==403
 with monkeypatch.context() as m:m.setattr(routes,'_now',lambda:9999999999);assert c.get(url).status_code==403
 # Rotation changes the session hash in the signature scope.
 token=c.cookies.get(c.app.state.auth_settings.cookie_name) if hasattr(c,'app') else None
 with connection(database=database) as db:db.execute("UPDATE lv_identity.session SET revoked_at=clock_timestamp(),revoke_reason='ADMIN_REVOKED' WHERE user_id=%s AND revoked_at IS NULL",(world['users']['controller']['id'],))
 assert c.get(url).status_code==401


def test_stale_fence_cannot_complete_after_lease_recovery(database,world,export):
 _,c,e=export;j=create(c,e).json()['id']
 with connection('lv_worker',database) as db:claim=db.execute('SELECT lv.native_export_claim(%s,%s,%s)',(world['A'],j,'old')).fetchone()[0]
 with connection(database=database) as db:db.execute("UPDATE lv.native_export_job SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(j,))
 with connection('lv_worker',database) as db:
  list(db.execute('SELECT lv.native_export_due(25)'))
  assert db.execute('SELECT lv.native_export_finish(%s,%s,%s,%s,NULL,%s)',(world['A'],j,'old',claim['fence'],'TERMINAL')).fetchone()==(False,)
 assert work(database,world['A'],j)['status']=='DONE'


def test_finished_artifact_is_immutable_and_private(database,world,export):
 _,c,e=export;j=create(c,e).json()['id'];assert work(database,world['A'],j)['status']=='DONE'
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  with connection('lv_app',database) as db:db.execute('SELECT artifact FROM lv.native_export_job')
 with pytest.raises(psycopg.errors.CheckViolation):
  with connection(database=database) as db:db.execute("UPDATE lv.native_export_job SET content_hash=repeat('0',64) WHERE id=%s",(j,))


def test_worker_credential_cannot_be_substituted_by_runtime(database,world,export):
 _,c,e=export;j=create(c,e).json()['id']
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  with connection('lv_app',database) as db:db.execute('SELECT lv.native_export_claim(%s,%s,%s)',(world['A'],j,'forged'))


def test_relay_failure_keeps_job_publishable(database,world,export):
 _,c,e=export;j=create(c,e).json()['id']
 def broken(*args):raise RuntimeError('transport unavailable')
 with pytest.raises(RuntimeError):
  with connection('lv_worker',database) as db:runtime.relay(db,broken)
 published=[]
 with connection('lv_worker',database) as db:runtime.relay(db,lambda t,j:published.append(j))
 assert j in published


def test_three_crashes_exhaust_budget_and_leave_audited_dead_job(database,world,export):
 _,c,e=export;j=create(c,e).json()['id']
 for i in range(3):
  with connection('lv_worker',database) as db:assert db.execute('SELECT lv.native_export_claim(%s,%s,%s)',(world['A'],j,'crashing')).fetchone()[0] is not None
  with connection(database=database) as db:db.execute("UPDATE lv.native_export_job SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(j,))
  with connection('lv_worker',database) as db:list(db.execute('SELECT lv.native_export_due(100)'))
 assert get(c,j).json()['state']=='DEAD' and work(database,world['A'],j)['status']=='NOT_CLAIMED'
 with connection(database=database) as db:assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='NATIVE_EXPORT_FAILED' AND detail->>'job_id'=%s",(j,)).fetchone()==(1,)


def test_revoked_requester_and_revoked_download_are_refused(database,world,export):
 _,c,e=export;j=create(c,e).json()['id']
 with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 assert work(database,world['A'],j)['error_class']=='REVOKED'
 with connection(database=database) as db:assert db.execute('SELECT state,artifact FROM lv.native_export_job WHERE id=%s',(j,)).fetchone()==('DENIED',None)
 assert get(c,j).status_code==403

@pytest.mark.parametrize('artifact',['missing_request_audit','missing_build_audit'])
def test_missing_audit_rolls_back_job_request_or_completion(database,world,export,artifact):
 _,c,e=export
 if artifact=='missing_build_audit':j=create(c,e).json()['id']
 with connection(database=database) as db:original=db.execute("SELECT pg_get_functiondef('lv_identity.audit_append(text,text,uuid,uuid,uuid,text,text,jsonb)'::regprocedure)").fetchone()[0]
 needle="NATIVE_EXPORT_REQUESTED" if artifact=='missing_request_audit' else 'NATIVE_EXPORT_BUILT'
 # A syntactically valid audit omission; other audit events remain functional.
 altered=original.replace('BEGIN',"BEGIN\n IF p_event='"+needle+"' THEN RETURN;END IF;",1)
 with connection(database=database) as db:db.execute(altered)
 try:
  if artifact=='missing_request_audit':assert create(c,e).status_code==500
  else:
   result=work(database,world['A'],j);assert result['status']=='FAILED' and result['exception_class']=='CheckViolation',result
   with connection(database=database) as db:assert db.execute('SELECT artifact FROM lv.native_export_job WHERE id=%s',(j,)).fetchone()==(None,)
 finally:
  with connection(database=database) as db:db.execute(original)


def test_signed_link_is_bound_to_session_tenant_user_and_job(api,database,world,export):
 _,c,e=export;j=create(c,e).json()['id'];assert work(database,world['A'],j)['status']=='DONE';url=link(c,j).json()['url']
 fresh=signed_in(api,world,'controller');assert fresh.get(url).status_code==403
 other=signed_in(api,world,'automation');assert other.get(url).status_code==403
 assert fresh.get(url.replace(j,str(uuid4()))).status_code==403
 assert fresh.get('/api/v1/execution-proof-exports/'+j+'/download').status_code==403


def test_retryable_error_and_same_idempotency_key_keep_one_job(database,world,export,monkeypatch):
 _,c,e=export;j=create(c,e,key='retrying').json()['id']
 with connection('lv_worker',database) as db:f=db.execute('SELECT lv.native_export_claim(%s,%s,%s)',(world['A'],j,'old')).fetchone()[0]['fence']
 with connection('lv_worker',database) as db:assert db.execute('SELECT lv.native_export_finish(%s,%s,%s,%s,NULL,%s)',(world['A'],j,'old',f,'RETRYABLE')).fetchone()==(True,)
 assert create(c,e,key='retrying').json()['id']==j
 with connection(database=database) as db:db.execute("UPDATE lv.native_export_job SET available_at=clock_timestamp()-interval '1 second' WHERE id=%s",(j,))
 assert work(database,world['A'],j)['status']=='DONE'
