from uuid import UUID,uuid4
import json,os
from ledgervigil.maintenance.runtime import connect
from .anchors import GitAnchors
from .integrity import canonical,verify,seal,archive,digest

def _run_job(tenant,job,*,connection_factory=connect,store=None,worker=None,lease=None):
 t,j=UUID(str(tenant)),UUID(str(job));worker=worker or 'pack-'+uuid4().hex
 if store is None:
  path=os.environ.get('LV_ANCHOR_REPOSITORY')
  if not path:return {'status':'ANCHOR_CONFIGURATION_REQUIRED'}
  store=GitAnchors(path)
 store.validate() # Reject missing/corrupt stores before claiming or sealing.
 with connection_factory() as c:claim=c.execute('SELECT lv.pack_claim(%s,%s,%s)',(t,j,worker)).fetchone()[0]
 if claim is None:return {'status':'NOT_CLAIMED'}
 f=claim['fence']
 if lease is not None:lease['f']=f
 if (store.root/'.git').is_dir():
  anchored=store.read(t)
  if anchored is not None:
   with connection_factory() as c:valid=c.execute('SELECT lv.pack_anchor_preflight(%s,%s,%s)',(t,anchored['chain_seq'],anchored['chain_hash'])).fetchone()[0]
   if not valid:raise ValueError('Database diverges from existing external anchor')
 if claim['state']=='DRAFTING':
  with connection_factory() as c:
   c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ,READ ONLY');content=c.execute('SELECT lv.pack_snapshot(%s,%s,%s,%s)',(t,j,worker,f)).fetchone()[0]
  store.validate()
  with connection_factory() as c:claim=c.execute('SELECT lv.pack_seal(%s,%s,%s,%s,%s)',(t,j,worker,f,canonical(content).decode())).fetchone()[0]
  if claim is None:return {'status':'LOST'}
 else:content=json.loads(claim['content'])
 if content.get('execution') and not content['execution'].get('completion_notification'):raise ValueError('Missing durable completion notification')
 pack={'manifest_schema':'lv-proofpack/1','content':content,'content_hash':claim['content_hash'],'chain':{'previous_chain_head':claim['previous_chain_head'],'chain_hash':claim['chain_hash']},'sealed_at':claim['sealed_at']};verify(pack)
 snapshot=content['snapshot'];execution=content.get('execution');sequence=str(execution['receipt']['commit_seq']) if execution else str(snapshot['seq'])
 facts=json.loads(content['simulation']['input_canonical'])['facts']
 record={'tenant_id':str(t),'pack_id':str(j),'content_hash':claim['content_hash'],'chain_hash':claim['chain_hash'],'previous_chain_head':claim['previous_chain_head'],'chain_seq':claim['chain_seq'],'ledger_id':content['ledger']['id'],'ledger_seq':sequence,'batch_head':execution['batch_hash'] if execution else facts['batch_head'],'event_head':execution['event_hash'] if execution else facts['event_head']}
 bundle=archive(pack);record['bundle_hash']=digest(bundle)
 oid=store.append(record)
 with connection_factory() as c:done=c.execute('SELECT lv.pack_anchor(%s,%s,%s,%s,%s,%s)',(t,j,worker,f,oid,bundle)).fetchone()[0]
 return {'status':'ANCHORED' if done else 'LOST','anchor_commit':oid}

def relay(c,publish):
 with c.transaction():
  jobs=[r[0] for r in c.execute('SELECT lv.pack_due(25)')]
  for job in jobs:publish(job['tenant_id'],job['job_id']);c.execute('SELECT lv.pack_dispatch(%s,%s)',(job['tenant_id'],job['job_id']))
 return len(jobs)

def run_job(tenant,job,*,connection_factory=connect,store=None,worker=None):
 worker=worker or 'pack-'+uuid4().hex;lease={}
 try:return _run_job(tenant,job,connection_factory=connection_factory,store=store,worker=worker,lease=lease)
 except Exception as exc:
  try:
   with connection_factory() as c:
    # Scope/fence are obtained through the restricted lease helper, not arbitrary table access.
    if 'f' in lease:c.execute('SELECT lv.pack_fail(%s,%s,%s,%s,%s)',(tenant,job,worker,lease['f'],type(exc).__name__))
  except Exception:pass
  return {'status':'FAILED','error_class':type(exc).__name__}
