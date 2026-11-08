"""Evaluate a pinned public UCI workbook in a disposable native PostgreSQL database.

No HTTP upload, production source admission or invoice posting is implemented.
The original workbook is never modified. Reports are new files, never overwritten.
"""
from pathlib import Path
from uuid import uuid4,uuid7
import argparse,json,hashlib,platform,resource,sys,time
from psycopg import sql
from .dev_db import connection,prepare_database,migrate
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.evaluation.retail import Workbook,PUBLIC_SHA256,WorkbookRejected,file_hash
from ledgervigil.evaluation.staging import setup,ingest
from ledgervigil.evaluation.admission import export_plan,verify_plan


def fingerprint(conn):
 tables=conn.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('lv','lv_identity') ORDER BY schemaname,tablename").fetchall();result={}
 for schema,table in tables:
  q=sql.SQL('SELECT row_to_json(t)::text FROM {} t').format(sql.Identifier(schema,table))
  rows=sorted(r[0] for r in conn.execute(q));result[schema+'.'+table]={'rows':len(rows),'sha256':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
 return result


def pinned_workbook(path,sha256):
 if sha256!=PUBLIC_SHA256:raise WorkbookRejected('SOURCE_NOT_PINNED: this evaluator only admits the recorded original UCI workbook')
 return Workbook(path,sha256)

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--sha256',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--admission-plan',action='store_true',help='Export and independently verify a complete, non-posting invoice plan');args=p.parse_args()
 workbook=pinned_workbook(args.input,args.sha256)
 args.output.mkdir(parents=True,exist_ok=True)
 if (args.output/'REPORT.json').exists():raise SystemExit('Output already has a report; use a new run directory')
 name='lv_migration_test_'+uuid4().hex
 with connection(autocommit=True) as c:c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
 try:
  prepare_database(name);migrate(name);tenant=uuid7()
  with connection(database=name) as c:c.execute("INSERT INTO lv.tenant(tenant_id,name) VALUES(%s,'Public-data evaluation only')",(tenant,))
  with connection('lv_app',name,autocommit=True) as c:
   with tenant_transaction(c,tenant):ledger=c.execute("INSERT INTO lv.ledger(tenant_id,name,purpose,source_kind,currency,currency_exponent) VALUES(%s,'UCI evaluation scope','SANDBOX','NATIVE','GBP',2) RETURNING id",(tenant,)).fetchone()[0]
  with connection(database=name,autocommit=True) as c:
   before=fingerprint(c);setup(c)
   try:ingest(c,workbook.rows(),args.sha256,ledger,fault_after=10000)
   except RuntimeError as e:
    if str(e)!='INJECTED_IMPORT_INTERRUPTION':raise
   else:raise AssertionError('interruption not injected')
   assert c.execute('SELECT count(*) FROM lv_eval.retail_line').fetchone()==(0,) and c.execute('SELECT count(*) FROM lv_eval.run').fetchone()==(0,)
   report=ingest(c,workbook.rows(),args.sha256,ledger)
   # Supply a generator that raises if consumed: a same-source replay must return the recorded result.
   def forbidden():raise AssertionError('retry reparsed source');yield
   replay=ingest(c,forbidden(),args.sha256,ledger);assert replay['idempotent_replayed'] and replay['run_id']==report['run_id']
   assert before==fingerprint(c)
   if args.admission_plan:
    plan_started=time.monotonic()
    admission=export_plan(c,report['run_id'],args.output/'admission-plan')
    plan_export_seconds=time.monotonic()-plan_started;readback_started=time.monotonic()
    verified=verify_plan(args.output/'admission-plan')
    assert verified['verified'] and verified['header']['source_row_stream_sha256']==report['row_stream_sha256']
    report['admission_plan']={'manifest_sha256':verified['manifest_sha256'],'summary':admission['summary'],'compressed_bytes':admission['compressed_bytes'],'file_readback_verified':True,'plan_export_seconds':round(plan_export_seconds,3),'file_readback_seconds':round(time.monotonic()-readback_started,3),'financial_tables_unchanged':before==fingerprint(c)}
    assert report['admission_plan']['financial_tables_unchanged']
   root=Path(__file__).resolve().parents[1]
   report['implementation_files']={name:file_hash(root/name) for name in ['tools/evaluate_retail.py','src/ledgervigil/evaluation/retail.py','src/ledgervigil/evaluation/staging.py','src/ledger_kernel/money.py','src/ledgervigil/evaluation/admission.py']}
   report['practical_checks']={'injected_interruption_after_rows':10000,'partial_staging_rows_after_interruption':0,'same_hash_retry_replayed_without_reparsing':True,'source_bytes_unchanged':file_hash(args.input)==args.sha256,'published_instance_count':1067371,'matches_published_instance_count':report['rows']==1067371,'all_native_tables_unchanged':len(before),'native_table_fingerprints':before,'postgres_version':c.execute('SHOW server_version').fetchone()[0],'python':platform.python_version(),'platform':platform.platform(),'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)}
   plan=c.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT sheet,row_no,reasons FROM lv_eval.retail_line WHERE run_id=%s AND invoice=%s ORDER BY sheet,row_no LIMIT 100',(report['run_id'],'489434')).fetchone()[0]
   (args.output/'QUERY_PLAN.json').write_text(json.dumps(plan,indent=2)+'\n')
   (args.output/'REPORT.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k not in ('practical_checks','provenance','claims')},indent=2))
 finally:
  with connection(autocommit=True) as c:c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))

if __name__=='__main__':main()
