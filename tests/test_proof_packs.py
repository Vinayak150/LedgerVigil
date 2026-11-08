import json
from uuid import uuid4
import pytest
from tools.dev_db import connection
from test_proposal_drafts import world,seed,signed_in,counters
from test_simulation import api
from test_native_approval import ready
from test_native_execution import approved,post
from ledgervigil.proofpacks.runtime import run_job
from ledgervigil.proofpacks.anchors import GitAnchors
from ledgervigil.proofpacks.integrity import canonical,verify,IntegrityError,ZERO


def test_native_execution_seals_chains_and_anchors_prior_blocked_pack(database,world,approved,tmp_path):
 _,c,pid,_=approved;r=post(c,pid);assert r.status_code==200,r.text
 store=GitAnchors(tmp_path/'external-repo').initialize();seq=counters(database)
 with connection(database=database) as db:jobs=[str(r[0]) for r in db.execute('SELECT id FROM lv.proof_pack WHERE tenant_id=%s ORDER BY created_at,id',(world['A'],))]
 assert len(jobs)==2
 for j in jobs:
  result=run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database));assert result['status']=='ANCHORED',result
 with connection(database=database) as db:
  rows=db.execute('SELECT content_canonical,content_hash,previous_chain_head,chain_hash,chain_seq,sealed_at::text FROM lv.proof_pack WHERE tenant_id=%s ORDER BY chain_seq',(world['A'],)).fetchall()
 assert rows[0][2]==ZERO and rows[1][2]==rows[0][3]
 for content,ch,prev,head,n,at in rows:assert verify({'manifest_schema':'lv-proofpack/1','content':json.loads(content),'content_hash':ch,'chain':{'previous_chain_head':prev,'chain_hash':head},'sealed_at':at})==ch
 assert store.read(world['A'])['chain_hash']==rows[-1][3] and counters(database)==seq


def test_canonical_utf16_order_and_non_ascii_profile():
 assert canonical({'\ue000':1,'\U00010000':'café'})=='{"𐀀":"café","":1}'.encode()
 for bad in [1.1,9007199254740992,'\ud800']:
  with pytest.raises(IntegrityError):canonical(bad)


def test_pack_metadata_export_and_independent_git_verification(database,api,world,approved,tmp_path,monkeypatch):
 from tools.verify_proof_pack import check
 from ledgervigil.exports.runtime import run_job as export_run
 from ledgervigil.proofpacks import routes
 _,c,pid,_=approved;assert post(c,pid).status_code==200
 store=GitAnchors(tmp_path/'external').initialize();monkeypatch.setenv('LV_ANCHOR_REPOSITORY',str(store.root))
 with connection(database=database) as db:jobs=[str(r[0]) for r in db.execute('SELECT id FROM lv.proof_pack WHERE tenant_id=%s ORDER BY created_at,id',(world['A'],))]
 for j in jobs:assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 r=c.post('/api/v1/proposals/'+pid+'/proof-packs',headers={'Idempotency-Key':'pack-once'},json={'revision':1});assert r.status_code==202,r.text
 assert c.post('/api/v1/proposals/'+pid+'/proof-packs',headers={'Idempotency-Key':'pack-once'},json={'revision':1}).json()==r.json()
 pack_id=r.json()['proof_pack']['id'];assert c.get('/api/v1/proof-packs/'+pack_id+'/verify').json()['chain_ok'] is True
 r=c.post('/api/v1/proof-packs/'+pack_id+'/exports',headers={'Idempotency-Key':'bundle-once'},json={'format':'BUNDLE'});assert r.status_code==202,r.text;j=r.json()['id']
 result=export_run(world['A'],j,connection_factory=lambda:connection('lv_worker',database));assert result['status']=='DONE',result
 r=c.post('/api/v1/execution-proof-exports/'+j+'/download-link');assert r.status_code==200;r=c.get(r.json()['url']);assert r.status_code==200 and 'application/zip' in r.headers['content-type']
 assert check(r.content,store.root)['external_anchor']=='LOCAL_GIT_VERIFIED'


def test_recovery_detects_missing_anchored_rows(database,world,approved,tmp_path):
 from ledgervigil.proofpacks.recovery import check_database
 _,c,pid,_=approved;assert post(c,pid).status_code==200;store=GitAnchors(tmp_path/'anchors').initialize()
 with connection(database=database) as db:jobs=[str(r[0]) for r in db.execute('SELECT id FROM lv.proof_pack WHERE tenant_id=%s ORDER BY created_at,id',(world['A'],))]
 for j in jobs:assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 with connection(database=database) as db:
  assert check_database(db,store)['status']=='ANCHORED_PREFIX_VERIFIED'
  # Disposable rollback simulation; commit no tampering and retain the trusted external Git repository.
  db.execute('ALTER TABLE lv.proof_pack DISABLE TRIGGER USER');db.execute('DELETE FROM lv.proof_pack WHERE tenant_id=%s AND chain_seq=2',(world['A'],))
  with pytest.raises(IntegrityError,match='older'):check_database(db,store)
  db.rollback()


def test_rfc8785_supported_profile_matches_ecmascript():
 import subprocess
 sample={'\r':'tab\t quote" backslash\\ and /','\u20ac':'€ café 🚀','1':9007199254740991,'\U0001f600':{'\ufb33':'עברית','\ue000':None,'\u0080':True},'n':[-9007199254740991,0,1]}
 js="const fs=require('fs');function c(v){if(Array.isArray(v))return '['+v.map(c).join(',')+']';if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+c(v[k])).join(',')+'}';return JSON.stringify(v)}process.stdout.write(c(JSON.parse(fs.readFileSync(0,'utf8'))))"
 assert subprocess.run(['node','-e',js],input=json.dumps(sample).encode(),stdout=subprocess.PIPE,check=True).stdout==canonical(sample)


def test_x1_link_contains_no_database_session_credential(database,api,world,approved):
 import base64
 from ledgervigil.auth.crypto import token_hash
 from ledgervigil.exports.runtime import run_job as export_run
 _,c,pid,_=approved;r=post(c,pid);assert r.status_code==200;e=r.json()['id'];r=c.post('/api/v1/executions/'+e+'/exports',headers={'Idempotency-Key':'x1'},json={'format':'JSON'});assert r.status_code==202;j=r.json()['id'];assert export_run(world['A'],j,connection_factory=lambda:connection('lv_worker',database))['status']=='DONE'
 url=c.post('/api/v1/execution-proof-exports/'+j+'/download-link').json()['url'];token=url.split('token=')[1];claims=json.loads(base64.urlsafe_b64decode(token+'='*(-len(token)%4))[:-32]);session=c.cookies.get(api.state.auth_settings.cookie_name)
 assert claims['s']!=token_hash(session).hex()
 import psycopg
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  with connection('lv_app',database) as db:db.execute("SELECT lv.native_export_api(%s,'STATUS',%s,NULL,'probe','probe')",(bytes.fromhex(claims['s']),j))
 assert c.get('/api/v1/executions/'+e+'/proof').status_code==410
 with connection(database=database) as db:assert db.execute("SELECT has_function_privilege('lv_app','lv.authorize_proof_export(bytea,uuid,text,text,text,text,text)','EXECUTE')").fetchone()==(False,)


def test_git_acknowledgement_interruption_is_idempotently_recovered(database,world,approved,tmp_path,monkeypatch):
 from ledgervigil.proofpacks import runtime
 _,c,pid,_=approved;assert post(c,pid).status_code==200;store=GitAnchors(tmp_path/'repo').initialize()
 with connection(database=database) as db:j=str(db.execute('SELECT id FROM lv.proof_pack WHERE tenant_id=%s ORDER BY created_at,id LIMIT 1',(world['A'],)).fetchone()[0])
 original=store.append
 def interrupted(record):
  original(record);raise OSError('controlled acknowledgement interruption after Git commit')
 with monkeypatch.context() as m:m.setattr(store,'append',interrupted);assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='FAILED'
 oid=store.git('rev-parse','HEAD');assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED';assert store.git('rev-parse','HEAD')==oid


def test_sealed_snapshot_does_not_change_when_later_decisions_arrive(database,api,world,ready,tmp_path):
 from test_native_approval import post as approve
 a,c,pid,_=ready;store=GitAnchors(tmp_path/'repo').initialize()
 with connection(database=database) as db:j=str(db.execute('SELECT id FROM lv.proof_pack WHERE tenant_id=%s ORDER BY created_at,id LIMIT 1',(world['A'],)).fetchone()[0])
 assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 before=a.get('/api/v1/proof-packs/'+j+'/manifest').json();assert approve(c,pid).status_code==200
 assert a.get('/api/v1/proof-packs/'+j+'/manifest').json()==before


def test_request_idempotency_masks_other_tenants_and_requires_export_permission(database,api,world,ready):
 a,c,pid,_=ready;r=a.post('/api/v1/proposals/'+pid+'/proof-packs',headers={'Idempotency-Key':'same-pack'},json={'revision':1});assert r.status_code==202;pack=r.json()['proof_pack']['id']
 b=signed_in(api,world,'analystB');assert b.get('/api/v1/proof-packs/'+pack).status_code==404
 assert a.post('/api/v1/proof-packs/'+pack+'/exports',headers={'Idempotency-Key':'bad-export'},json={'format':'BUNDLE'}).status_code==403
 assert a.post('/api/v1/proposals/'+pid+'/proof-packs',headers={'Idempotency-Key':'same-pack'},json={'revision':2}).status_code==422


def test_expired_third_lease_is_terminal_and_old_fence_cannot_finish(database,world,ready):
 _,_,pid,_=ready
 with connection(database=database) as db:j=str(db.execute('SELECT id FROM lv.proof_pack WHERE proposal_id=%s',(pid,)).fetchone()[0])
 for i in range(3):
  with connection('lv_worker',database) as db:r=db.execute('SELECT lv.pack_claim(%s,%s,%s)',(world['A'],j,'dead-worker')).fetchone()[0]
  assert r['fence']==i+1
  with connection(database=database) as db:db.execute("UPDATE lv.proof_pack SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(j,))
 with connection('lv_worker',database) as db:assert db.execute('SELECT lv.pack_claim(%s,%s,%s)',(world['A'],j,'recovery-worker')).fetchone()==(None,)
 with connection(database=database) as db:assert db.execute('SELECT state,last_error FROM lv.proof_pack WHERE id=%s',(j,)).fetchone()==('DEAD','LEASE_RETRIES_EXHAUSTED')


def test_omitted_request_audit_rolls_back_and_internal_create_is_private(database,api,world,ready):
 import psycopg
 from test_proposal_drafts import fingerprint
 a,_,pid,_=ready
 with connection(database=database) as db:definition=db.execute("SELECT pg_get_functiondef('lv.pack_request(bytea,uuid,integer,text,text,text)'::regprocedure)").fetchone()[0]
 start=definition.index("  PERFORM lv_identity.audit_append('PROOF_PACK_REQUESTED'");end=definition.index('\n',start)
 before=fingerprint(database,set())
 with connection(database=database) as db:db.execute(definition[:start]+definition[end:])
 try:assert a.post('/api/v1/proposals/'+pid+'/proof-packs',headers={'Idempotency-Key':'missing-audit'},json={'revision':1}).status_code==500
 finally:
  with connection(database=database) as db:db.execute(definition)
 assert fingerprint(database,set())==before
 with connection('lv_app',database) as db:
  with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute("SELECT lv.pack_api(NULL,'CREATE',%s,1,'probe','probe')",(pid,))


@pytest.mark.parametrize('event',['PROOF_PACK_SEALED','PROOF_PACK_ANCHORED'])
def test_omitted_completion_audit_never_acknowledges_success(database,world,ready,tmp_path,event):
 _,_,pid,_=ready;store=GitAnchors(tmp_path/'external').initialize()
 signature='lv.pack_seal(uuid,uuid,text,bigint,text)' if event.endswith('SEALED') else 'lv.pack_anchor(uuid,uuid,text,bigint,text,bytea)'
 with connection(database=database) as db:
  j=str(db.execute('SELECT id FROM lv.proof_pack WHERE proposal_id=%s',(pid,)).fetchone()[0]);definition=db.execute('SELECT pg_get_functiondef(%s::regprocedure)',(signature,)).fetchone()[0]
 start=definition.index(" PERFORM lv_identity.audit_append('"+event+"'");end=definition.index(';',start)+1
 with connection(database=database) as db:db.execute(definition[:start]+definition[end:])
 try:assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='FAILED'
 finally:
  with connection(database=database) as db:db.execute(definition)
 with connection(database=database) as db:assert db.execute('SELECT state FROM lv.proof_pack WHERE id=%s',(j,)).fetchone()[0]!='ANCHORED'
 assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'


def test_committed_anchor_survives_missing_worktree_and_bundle_mutations(database,world,ready,tmp_path):
 import io,zipfile
 from tools.verify_proof_pack import check
 from ledgervigil.proofpacks.recovery import check_database
 _,_,pid,_=ready;store=GitAnchors(tmp_path/'external').initialize()
 with connection(database=database) as db:j=str(db.execute('SELECT id FROM lv.proof_pack WHERE proposal_id=%s',(pid,)).fetchone()[0])
 assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 with connection(database=database) as db:data=bytes(db.execute('SELECT bundle FROM lv.proof_pack WHERE id=%s',(j,)).fetchone()[0])
 assert check(data,store.root)['valid']
 (store.root/'heads'/(str(world['A'])+'.json')).unlink()
 with connection(database=database) as db:assert check_database(db,store)['status']=='ANCHORED_PREFIX_VERIFIED'
 with zipfile.ZipFile(io.BytesIO(data)) as z:entries={i.filename:z.read(i.filename) for i in z.infolist()}
 assert b'&lt;' not in entries['report.html'] or b'<img src=x>' not in entries['report.html']
 for name in ['manifest.json','report.html','../escape']:
  broken=io.BytesIO()
  with zipfile.ZipFile(broken,'w') as z:
   for key,value in entries.items():z.writestr(key,value+(b' ' if name==key else b''))
   if name.startswith('..'):z.writestr(name,b'escape')
  with pytest.raises(IntegrityError):check(broken.getvalue(),store.root)


def test_empty_roundtrip_and_history_downgrade_refusal(database,world,ready):
 from tools.dev_db import migrate
 from test_proposal_drafts import fingerprint
 # This fixture has BLOCKED history; destructive downgrade must roll back atomically.
 before=fingerprint(database,set())
 with pytest.raises(Exception):migrate(database,'0025_export_jobs',downgrade=True,disposable=True)
 assert fingerprint(database,set())==before


def test_empty_migration_roundtrip_and_existing_notification_backfill():
 from tools.dev_db import migrate
 from conftest import database as database_fixture
 from test_simulation import api as api_fixture
 from test_native_approval import ready as prepare,post as approve
 db_generator=database_fixture.__wrapped__();fresh=next(db_generator);api_generator=api_fixture.__wrapped__(fresh);application=next(api_generator)
 try:
  migrate(fresh,'0025_export_jobs',downgrade=True,disposable=True)
  from legacy_issuer_support import use_historical_issuer
  issuer=application.state.issuer_pool
  use_historical_issuer(application,fresh,'0025_export_jobs')
  with connection('lv_app',fresh,autocommit=True) as app,connection('lv_seed',fresh,autocommit=True) as seed_conn:w=world.__wrapped__(fresh,app,seed_conn)
  a,c,pid,_=prepare.__wrapped__(fresh,application,w);assert approve(c,pid).status_code==200;assert post(c,pid).status_code==200
  before=counters(fresh);migrate(fresh)
  application.state.issuer_pool=issuer
  with connection('lv_worker',fresh) as db:first=[r[0] for r in db.execute('SELECT lv.pack_due(25)')]
  with connection('lv_worker',fresh) as db:second=[r[0] for r in db.execute('SELECT lv.pack_due(25)')]
  assert len(first)==len(second)==1 and first==second
  with connection(database=fresh) as db:assert db.execute('SELECT count(*) FROM lv.proof_pack WHERE proposal_id=%s',(pid,)).fetchone()==(1,)
  assert counters(fresh)==before
 finally:
  next(api_generator,None);db_generator.close()


def test_interleaved_tenant_git_retry_retains_its_original_commit(tmp_path):
 from ledgervigil.proofpacks.integrity import digest,chain
 store=GitAnchors(tmp_path/'external').initialize();a,b=str(uuid4()),str(uuid4())
 def record(t):
  h=digest(t.encode());return {'tenant_id':t,'pack_id':str(uuid4()),'content_hash':h,'chain_hash':chain(h,ZERO),'previous_chain_head':ZERO,'chain_seq':1}
 first=record(a);second=record(b);a_commit=store.append(first);b_commit=store.append(second)
 assert a_commit!=b_commit and store.append(first)==a_commit
 assert store.find(a,first['pack_id'])[1]==a_commit


def test_readable_report_escapes_source_content_and_keeps_exact_amount_strings():
 from ledgervigil.proofpacks.integrity import seal,readable,archive
 from tools.verify_proof_pack import check
 content={'tenant_id':str(uuid4()),'proof_pack_id':str(uuid4()),'proposal':{'id':str(uuid4()),'revision':1},'ledger':{'id':str(uuid4())},'accounting_review_status':'UNREVIEWED','operations':[{'amount':'52500.50','note':'<img src=x onerror=alert(1)>'}]}
 pack=seal(content,ZERO,'2026-11-06T00:00:00+00:00');report=readable(pack)
 assert b'52500.50' in report and b'&lt;img' in report and b'<img' not in report
 assert b'<h2>Proposed operations</h2>' in report and check(archive(pack))['valid']


@pytest.fixture
def database():
 from conftest import database as base
 generator=base.__wrapped__()
 try:yield next(generator)
 finally:generator.close()


def test_missing_store_does_not_claim_seal_or_create_directory(database,world,ready,tmp_path):
 _,_,pid,_=ready;store=GitAnchors(tmp_path/'missing')
 with connection(database=database) as db:j=str(db.execute('SELECT id FROM lv.proof_pack WHERE proposal_id=%s',(pid,)).fetchone()[0])
 assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='FAILED'
 assert not store.root.exists()
 with connection(database=database) as db:assert db.execute('SELECT state,attempts FROM lv.proof_pack WHERE id=%s',(j,)).fetchone()==('DRAFTING',0)


def test_reader_never_creates_lock_or_changes_repository(tmp_path):
 from ledgervigil.proofpacks.integrity import digest,chain
 store=GitAnchors(tmp_path/'repo').initialize();t=str(uuid4());h=digest(t.encode());r={'tenant_id':t,'pack_id':str(uuid4()),'content_hash':h,'chain_hash':chain(h,ZERO),'previous_chain_head':ZERO,'chain_seq':1}
 store.append(r);(store.root/'anchor.lock').unlink()
 before={str(p.relative_to(store.root)):p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
 assert store.read(t)==r and store.heads()==[r] and store.find(t,r['pack_id'])[0]==r
 assert before=={str(p.relative_to(store.root)):p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
 with pytest.raises(FileExistsError):store.initialize()


@pytest.mark.parametrize('field',['anchor_commit','bundle','chain_seq'])
def test_recovery_rejects_database_anchor_metadata_divergence(database,world,ready,tmp_path,field):
 from ledgervigil.proofpacks.recovery import check_database
 _,_,pid,_=ready;store=GitAnchors(tmp_path/'repo').initialize()
 with connection(database=database) as db:j=str(db.execute('SELECT id FROM lv.proof_pack WHERE proposal_id=%s',(pid,)).fetchone()[0])
 assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 with connection(database=database) as db:
  assert check_database(db,store)['status']=='ANCHORED_PREFIX_VERIFIED'
  db.execute('SET session_replication_role=replica')
  if field=='anchor_commit':db.execute("UPDATE lv.proof_pack SET anchor_commit=repeat('f',64) WHERE id=%s",(j,))
  elif field=='chain_seq':db.execute('UPDATE lv.proof_pack SET chain_seq=99 WHERE id=%s',(j,))
  else:db.execute("UPDATE lv.proof_pack SET bundle=bundle||decode('00','hex'),bundle_hash='sha256:'||encode(sha256(bundle||decode('00','hex')),'hex') WHERE id=%s",(j,))
  with pytest.raises(IntegrityError):check_database(db,store)
  db.rollback()



def test_corrupt_git_returns_cli_json_and_api_503(database,api,world,approved,tmp_path,monkeypatch):
 import subprocess,sys
 from pathlib import Path
 from ledgervigil.exports.runtime import run_job as export_run
 _,c,pid,_=approved;assert post(c,pid).status_code==200;store=GitAnchors(tmp_path/'repo').initialize();monkeypatch.setenv('LV_ANCHOR_REPOSITORY',str(store.root))
 with connection(database=database) as db:jobs=[str(r[0]) for r in db.execute('SELECT id FROM lv.proof_pack WHERE proposal_id=%s ORDER BY created_at,id',(pid,))]
 for j in jobs:assert run_job(world['A'],j,store=store,connection_factory=lambda:connection('lv_worker',database))['status']=='ANCHORED'
 job=c.post('/api/v1/proof-packs/'+jobs[-1]+'/exports',headers={'Idempotency-Key':'corruption'},json={'format':'BUNDLE'}).json()['id'];assert export_run(world['A'],job,connection_factory=lambda:connection('lv_worker',database))['status']=='DONE'
 url=c.post('/api/v1/execution-proof-exports/'+job+'/download-link').json()['url']
 with connection(database=database) as db:data=bytes(db.execute('SELECT bundle FROM lv.proof_pack WHERE id=%s',(jobs[-1],)).fetchone()[0])
 artifact=tmp_path/'proof.zip';artifact.write_bytes(data);head=store.git('rev-parse','HEAD');obj=store.root/'.git/objects'/head[:2]/head[2:];obj.chmod(0o600);obj.write_bytes(b'corrupted')
 result=subprocess.run([sys.executable,'-S',str(Path(__file__).parents[1]/'tools/verify_proof_pack.py'),str(artifact),'--anchor-repository',str(store.root)],capture_output=True,text=True)
 assert result.returncode==1 and json.loads(result.stdout)['valid'] is False and 'Traceback' not in result.stderr
 r=c.get(url);assert r.status_code==503 and 'X-Proof-SHA256' not in r.headers and r.content!=data
