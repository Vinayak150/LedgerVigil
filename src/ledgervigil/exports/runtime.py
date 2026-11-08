"""Fenced native artifact generation using only the restricted worker login."""
from uuid import UUID,uuid4
import psycopg
from ledgervigil.maintenance.runtime import connect
from ledgervigil.execution.proof_verify import seal,canon,verify,ProofInvalid

def run_job(tenant,job,*,connection_factory=connect,worker=None):
 t,j=UUID(str(tenant)),UUID(str(job));worker=worker or 'export-'+uuid4().hex;f=None
 try:
  with connection_factory() as c:claim=c.execute('SELECT lv.native_export_claim(%s,%s,%s)',(t,j,worker)).fetchone()[0]
  if claim is None:return {'status':'NOT_CLAIMED'}
  f=claim['fence']
  with connection_factory() as c:
   c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
   snapshot=c.execute('SELECT lv.native_export_snapshot(%s,%s,%s,%s)',(t,j,worker,f)).fetchone()[0]
  if 'bundle_hex' in snapshot:
   data=bytes.fromhex(snapshot['bundle_hex'])
  else:
   data=(canon(seal(snapshot['scope'],snapshot['artifacts']))+'\n').encode();verify(data)
  with connection_factory() as c:done=c.execute('SELECT lv.native_export_finish(%s,%s,%s,%s,%s,NULL)',(t,j,worker,f,data)).fetchone()[0]
  return {'status':'DONE' if done else 'LOST'}
 except Exception as exc:
  error='REVOKED' if isinstance(exc,psycopg.errors.InsufficientPrivilege) else 'TERMINAL' if isinstance(exc,ProofInvalid) or (isinstance(exc,psycopg.Error) and exc.sqlstate in ('23514','54000','22023')) else 'RETRYABLE'
  if f is not None:
   try:
    with connection_factory() as c:c.execute('SELECT lv.native_export_finish(%s,%s,%s,%s,NULL,%s)',(t,j,worker,f,error))
   except Exception:pass
  return {'status':'FAILED','error_class':error,'exception_class':type(exc).__name__}

def relay(c,publish):
 with c.transaction():
  jobs=[r[0] for r in c.execute('SELECT lv.native_export_due(25)')]
  for job in jobs:
   publish(job['tenant_id'],job['job_id'])
   c.execute('SELECT lv.native_export_dispatch(%s,%s)',(job['tenant_id'],job['job_id']))
 return len(jobs)
