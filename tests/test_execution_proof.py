from copy import deepcopy
from uuid import uuid4
import hashlib,json,subprocess,sys
from pathlib import Path
import pytest
from tools.dev_db import connection
from test_proposal_drafts import world,seed,signed_in,fingerprint,counters,client
from test_simulation import api
from test_native_execution import approved,post
from test_native_approval import ready
from ledgervigil.execution.proof_verify import verify,ProofInvalid,canon,seal,sha,CLAIMS

def download_native(database,world,c,eid):
 from ledgervigil.exports.runtime import run_job
 r=c.post('/api/v1/executions/'+eid+'/exports',headers={'Idempotency-Key':uuid4().hex},json={'format':'JSON'});assert r.status_code==202,r.text;j=r.json()['id']
 result=run_job(world['A'],j,connection_factory=lambda:connection('lv_worker',database));assert result['status']=='DONE',result
 r=c.post('/api/v1/execution-proof-exports/'+j+'/download-link');assert r.status_code==200,r.text;url=r.json()['url'];return url,c.get(url)

@pytest.fixture
def proof(database,world,approved):
 a,c,pid,_=approved;r=post(c,pid);assert r.status_code==200;url,data=download_native(database,world,c,r.json()['id']);assert data.status_code==200,data.text
 return c,c,pid,url,data


def encoded(b):return (canon(b)+'\n').encode()
def reseal(b):return encoded(seal(b['scope'],b['artifacts']))

def test_download_is_exact_deterministic_read_only_and_independently_verifiable(database,proof):
 a,_,pid,url,r=proof;before=fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'});seq=counters(database);digest=r.headers['X-Proof-SHA256']
 assert r.headers['Content-Disposition'].startswith('attachment; filename="ledgervigil-execution-') and r.headers['X-Content-Type-Options']=='nosniff' and r.headers['Cache-Control']=='no-store'
 result=verify(r.content,digest);assert result['valid'] and result['accounting']=='UNREVIEWED' and result['authenticity']=='NOT_ESTABLISHED'
 assert a.get(url).content==r.content
 assert a.get(url,headers={'If-None-Match':r.headers['X-Proof-SHA256']}).status_code==200
 assert a.get(url+'&include=credentials').status_code==400
 assert fingerprint(database,{'lv_identity.audit_event','lv_identity.audit_chain_head','lv_identity.session'})==before and counters(database)==seq
 raw=r.content.decode();assert not any('"'+k+'":' in raw for k in ['session_id','idempotency_key','request_hash','password_hash','token_hash','csrf_token'])
 # Financial batch payloads retain their request_hash inside a canonical string;
 # no identity/session table or raw credential field is exported.
 assert json.loads(raw)['claims']==CLAIMS

def test_later_ledger_activity_does_not_change_an_existing_proof(database,world,proof):
 from test_posting import request as journal,post as seed_post
 a,_,_,url,r=proof
 with connection('lv_seed',database,autocommit=True) as seed:seed_post(seed,world['l1'],journal(world['l1'],ref='after-proof',amount='3.25'))
 assert a.get(url).content==r.content

def test_export_requires_auth_masks_tenants_and_checks_auth_before_304(database,api,world,proof):
 a,c,_,url,r=proof
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING')",(world['B'],world['users']['analystB']['id']))
 b=signed_in(api,world,'analystB');other=b.get(url);absent=b.get(url.replace(url.split('/')[4],str(uuid4())))
 assert other.status_code==403 and absent.status_code==403
 assert 'artifacts' not in other.json() and 'artifacts' not in absent.json()
 assert client(api).get(url).status_code==401
 with connection(database=database) as db:db.execute("UPDATE lv_identity.membership SET status='REVOKED',revoked_at=statement_timestamp() WHERE tenant_id=%s AND user_id=%s",(world['A'],world['users']['controller']['id']))
 assert c.get(url,headers={'If-None-Match':r.headers['X-Proof-SHA256']}).status_code==401

def raw_export(database,world,eid):
 from ledgervigil.execution.proof_export import export_proof
 from ledgervigil.db.tenant import tenant_transaction
 with connection('lv_app',database,autocommit=True) as db,tenant_transaction(db,world['A']):
  db.execute('SET TRANSACTION READ ONLY');return export_proof(db,eid)

@pytest.mark.parametrize('limit',['MAX_BYTES','MAX_HISTORY'])
def test_generator_limits_fail_closed_without_financial_writes(database,world,proof,monkeypatch,limit):
 from ledgervigil.execution import proof_export
 from ledgervigil.auth.problems import ProblemError
 *_,response=proof;eid=response.json()['scope']['execution_id'];before=fingerprint(database,set());monkeypatch.setattr(proof_export,limit,1)
 with pytest.raises(ProblemError) as error:raw_export(database,world,eid)
 assert error.value.status==422 and fingerprint(database,set())==before

def test_generator_refuses_inconsistent_reader_result(database,world,proof,monkeypatch):
 from ledgervigil.execution import proof_export
 *_,response=proof;eid=response.json()['scope']['execution_id'];original=proof_export.records;before=fingerprint(database,set())
 def broken(*args,**kwargs):
  rows=original(*args,**kwargs)
  if args[2]=='line':rows[0]['amount']='1.0000'
  return rows
 monkeypatch.setattr(proof_export,'records',broken)
 with pytest.raises(ProofInvalid):raw_export(database,world,eid)
 assert fingerprint(database,set())==before


def test_offline_cli_needs_only_standard_library_and_can_check_external_digest(proof,tmp_path):
 *_,r=proof;p=tmp_path/'proof.json';p.write_bytes(r.content);tool=Path(__file__).parents[1]/'tools/verify_execution_proof.py'
 run=subprocess.run([sys.executable,'-S',str(tool),str(p),'--expected-sha256',r.headers['X-Proof-SHA256']],capture_output=True,text=True)
 assert run.returncode==0 and json.loads(run.stdout)['supplied_digest']=='MATCHED',run.stderr
 run=subprocess.run([sys.executable,'-S',str(tool),str(p),'--expected-sha256','0'*64],capture_output=True,text=True);assert run.returncode==1 and json.loads(run.stdout)['reason']=='EXPECTED_DIGEST_MISMATCH'

@pytest.mark.parametrize('fault',['manifest','missing_artifact','scope','sequence','approval','exception','simulation','document','journal','balanced_wrong_account','original_request','inclusion','check','transition','actor','money_float','money_precision','claims','duplicate_key','unknown_field','hash','version_bool','payment_history_seq','captured_fact_scope'])
def test_tampered_or_incomplete_proofs_fail_even_when_artifact_hashes_are_rebuilt(proof,fault):
 *_,r=proof;b=json.loads(r.content);a=b['artifacts'];rebuilt=True
 if fault=='manifest':b['manifest'][0]['sha256']='0'*64;rebuilt=False
 elif fault=='missing_artifact':del a['journal.json'];rebuilt=False
 elif fault=='scope':b['scope']['tenant_id']=str(uuid4())
 elif fault=='sequence':a['history.json'].pop(0)
 elif fault=='approval':a['approval.json']['decision']['kind']='REJECTED'
 elif fault=='exception':a['approval.json']['overrides']=[]
 elif fault=='simulation':a['simulation.json']['outcome_canonical']=a['simulation.json']['outcome_canonical'].replace('52500.50','52500.51')
 elif fault=='document':a['documents.json'][0]['financial_fields']['currency']='USD'
 elif fault=='journal':a['journal.json']['lines'][0]['amount']='1.0000'
 elif fault=='balanced_wrong_account':a['journal.json']['lines'][-1]['account_id']=str(uuid4())
 elif fault=='original_request':next(v for v in a['documents.json'] if v['document_id']==a['execution.json']['receipt']['settlement_document_id'])['original_request']['payout_date']='2026-10-15'
 elif fault=='inclusion':a['inclusions.json'][0]['payment_version_after']+=1
 elif fault=='check':a['execution.json']['check']['report']['observed_seq']+=1
 elif fault=='transition':a['execution.json']['transition']['to_state']='APPROVED'
 elif fault=='actor':a['execution.json']['receipt']['executed_by']=a['approval.json']['decision']['submitted_by']
 elif fault=='money_float':a['journal.json']['lines'][0]['amount']=51500.5;rebuilt=False
 elif fault=='money_precision':a['journal.json']['lines'][0]['amount']='51500.5001'
 elif fault=='claims':b['claims']['signature']='VERIFIED';rebuilt=False
 elif fault=='duplicate_key':
  raw=r.content.replace(b'"format":',b'"format":"forged","format":',1)
  with pytest.raises(ProofInvalid,match='DUPLICATE_JSON_KEY'):verify(raw)
  return
 elif fault=='unknown_field':a['execution.json']['receipt']['session_token']='forbidden'
 elif fault=='hash':a['history.json'][-1]['event_hash']='0'*64
 elif fault=='captured_fact_scope':
  sim=a['simulation.json'];inp=json.loads(sim['input_canonical']);inp['facts']['tenant_id']=str(uuid4());sim['input_canonical']=canon(inp);sim['input_hash']=sha(sim['input_canonical'].encode());out=json.loads(sim['outcome_canonical']);out['input_hash']=sim['input_hash'];sim['outcome_canonical']=canon(out);sim['result_hash']=sha(sim['outcome_canonical'].encode())
 elif fault=='payment_history_seq':next(v for v in a['documents.json'] if v['financial_fields'].get('document_type')=='PAYMENT' and v['version']==1)['seq']='3'
 elif fault=='version_bool':a['revision.json']['revision']=True
 with pytest.raises(ProofInvalid):verify(reseal(b) if rebuilt else json.dumps(b).encode())

def test_reformatting_or_wrong_external_digest_is_not_silently_accepted(proof):
 *_,r=proof
 with pytest.raises(ProofInvalid,match='NONCANONICAL_PROOF'):verify(json.dumps(json.loads(r.content),indent=2).encode())
 with pytest.raises(ProofInvalid,match='EXPECTED_DIGEST_MISMATCH'):verify(r.content,'0'*64)

def test_current_read_permission_is_required(database,world,proof):
 a,_,_,url,_=proof
 with connection(database=database) as db:db.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s',(world['A'],world['users']['controller']['id']))
 assert a.get(url).status_code==403

def test_generator_read_only_transaction_blocks_authorized_definer_write(database,api,world,proof):
 import psycopg
 from ledgervigil.auth.crypto import token_hash
 from ledgervigil.db.tenant import tenant_transaction
 c,*_=proof;token=c.cookies.get(api.state.auth_settings.cookie_name);before=fingerprint(database,set())
 with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
  with connection('lv_app',database,autocommit=True) as db,tenant_transaction(db,world['A']):
   db.execute('SET TRANSACTION READ ONLY');db.execute('SELECT lv_identity.session_context(%s,true)',(token_hash(token),))
 assert fingerprint(database,set())==before

@pytest.mark.parametrize('fee,net',[('0.00','52500.50'),('52500.50','0.00')])
def test_two_line_zero_fee_or_zero_net_proofs_remain_valid(database,api,world,fee,net):
 from test_proposal_drafts import _statement,draft,settlement
 from test_submission import act
 from test_evidence_overrides import post as override
 from test_native_approval import age,post as approve
 local=dict(world);payout='PROOF-ZERO-'+fee.replace('.','-');st=_statement(database,world['l1'],payout,[('PMT-2001','50000.00'),('PMT-2002','2500.50')],'52500.50',fee,net);local['s1'],local['r1']=st['staged_document_id'],st['source_record_id']
 a=signed_in(api,world,'analyst');c=signed_in(api,world,'controller');r=a.post('/api/v1/proposals',headers={'Idempotency-Key':uuid4().hex},json=draft(local,operations=[settlement(local,payout=payout,fee=fee,net=net)]));assert r.status_code==201;r=r.json();pid=r['id']
 assert act(a,pid).status_code==200 and override(c,pid).status_code==200;age(database,world);assert approve(c,pid).status_code==200
 executed=post(c,pid);assert executed.status_code==200
 _,r=download_native(database,world,c,executed.json()['id']);assert r.status_code==200,r.text
 assert verify(r.content)['valid'] and len(r.json()['artifacts']['journal.json']['lines'])==2

def test_revised_proposal_keeps_all_editors_and_allows_the_accepted_editor_executor(database,api,world):
 from test_proposal_drafts import draft
 from test_submission import act
 from test_evidence_overrides import post as override
 from test_native_approval import age,post as approve
 a=signed_in(api,world,'analyst');editor=signed_in(api,world,'automation');c=signed_in(api,world,'controller')
 r=a.post('/api/v1/proposals',headers={'Idempotency-Key':uuid4().hex},json=draft(world));assert r.status_code==201;pid=r.json()['id']
 assert editor.patch('/api/v1/proposals/'+pid,headers={'If-Match':'"v1"'},json={'rationale':'SYNTHETIC revised proof ₹ café 🚀'}).status_code==200
 assert act(a,pid,match='"v2"').status_code==200;assert override(c,pid,match='"v3"').status_code==200;age(database,world)
 assert approve(c,pid,match='"v4"',body={'revision':2}).status_code==200
 with connection(database=database) as db:db.execute("INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,grant_source,granted_at) VALUES(%s,%s,'CONTROLLER','LOCAL_PROVISIONING',statement_timestamp()-interval '48 hours')",(world['A'],world['users']['automation']['id']))
 # A newly privileged editor must authenticate the added role with MFA.
 editor=signed_in(api,world,'automation')
 r=post(editor,pid,match='"v5"',revision=2);assert r.status_code==200,r.text
 _,r=download_native(database,world,c,r.json()['id']);assert r.status_code==200,r.text;assert verify(r.content)['valid']
 ex=r.json()['artifacts']['execution.json'];assert ex['editors']==sorted([world['users']['analyst']['id'],world['users']['automation']['id']]) and ex['receipt']['executed_by']==world['users']['automation']['id']
