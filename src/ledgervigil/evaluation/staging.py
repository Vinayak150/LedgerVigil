"""Evaluation-only PostgreSQL staging; no authoritative source admission or posting."""
from datetime import datetime,timezone
from fractions import Fraction
from uuid import uuid7
import hashlib,time
from psycopg.types.json import Jsonb
from .retail import PROFILE,SOURCE,PUBLIC_SHA256,canonical,assess

DDL="""
CREATE SCHEMA lv_eval;
REVOKE ALL ON SCHEMA lv_eval FROM PUBLIC;
CREATE TABLE lv_eval.run(
 id uuid PRIMARY KEY,source_sha256 text NOT NULL,profile text NOT NULL,ledger_id uuid NOT NULL,
 provenance jsonb NOT NULL,completed_report jsonb NOT NULL,
 UNIQUE(source_sha256,profile,ledger_id),CHECK(source_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TABLE lv_eval.retail_line(
 run_id uuid NOT NULL REFERENCES lv_eval.run(id),sheet text NOT NULL,row_no integer NOT NULL CHECK(row_no>1),
 raw jsonb NOT NULL,row_sha256 text NOT NULL,invoice text,customer text,country text,invoice_date date,
 quantity bigint,unit_minor bigint,extended_minor bigint,reasons text[] NOT NULL,
 candidate boolean NOT NULL,CHECK(candidate=(cardinality(reasons)=0)),
 CHECK(NOT candidate OR (quantity>0 AND unit_minor>0 AND extended_minor IS NOT NULL AND customer IS NOT NULL AND invoice_date IS NOT NULL)),
 PRIMARY KEY(run_id,sheet,row_no)
);
CREATE INDEX retail_invoice ON lv_eval.retail_line(run_id,invoice);
CREATE INDEX retail_content ON lv_eval.retail_line(run_id,row_sha256);
"""

def setup(conn):
 with conn.transaction():conn.execute(DDL)


def independent_minor(raw_price,raw_quantity):
 """Independent rational arithmetic, not Decimal or the kernel implementation."""
 try:
  unit=Fraction(raw_price)*100;product=unit*int(raw_quantity)
  return (int(unit) if unit.denominator==1 else None,int(product) if product.denominator==1 else None)
 except (ValueError,TypeError,ZeroDivisionError):return None,None


def ingest(conn,rows,source_hash,ledger_id,*,fault_after=None):
 start=time.monotonic();prior=conn.execute('SELECT completed_report FROM lv_eval.run WHERE source_sha256=%s AND profile=%s AND ledger_id=%s',(source_hash,PROFILE,ledger_id)).fetchone()
 if prior:return {**prior[0],'idempotent_replayed':True,'retry_seconds':round(time.monotonic()-start,6)}
 provenance=SOURCE if source_hash==PUBLIC_SHA256 else {'classification':'SYNTHETIC_TEST','currency':'GBP','accounting_review':'UNREVIEWED','settlement_ground_truth':False}
 run=str(uuid7());stream=hashlib.sha256();count=0;oracle_checked=0
 with conn.transaction():
  conn.execute('INSERT INTO lv_eval.run VALUES(%s,%s,%s,%s,%s,%s)',(run,source_hash,PROFILE,ledger_id,Jsonb(provenance),Jsonb({})))
  with conn.cursor().copy('COPY lv_eval.retail_line FROM STDIN') as copy:
   for row in rows:
    result=assess(row);count+=1
    if result['extended_minor'] is not None:
     unit,extended=independent_minor(row.values[5],row.values[3]);oracle_checked+=1
     if unit!=result['unit_minor'] or extended!=result['extended_minor']:raise AssertionError('INDEPENDENT_MONEY_MISMATCH')
    stream.update(canonical([row.sheet,row.row,row.sha256]).encode()+b'\n')
    copy.write_row((run,row.sheet,row.row,Jsonb(row.payload),row.sha256,result['invoice'],result['customer'],result['country'],result['invoice_date'],result['quantity'],result['unit_minor'],result['extended_minor'],result['reasons'],result['candidate']))
    if fault_after is not None and count>=fault_after:raise RuntimeError('INJECTED_IMPORT_INTERRUPTION')
  if count==0:raise ValueError('EMPTY_SOURCE')
  # Read committed-to-be staging back, independently checking completeness and retained payload hashes.
  observed=hashlib.sha256();read_count=0
  with conn.cursor(name='retail_readback') as cur:
   cur.itersize=5000;cur.execute('SELECT sheet,row_no,raw,row_sha256 FROM lv_eval.retail_line WHERE run_id=%s ORDER BY sheet,row_no',(run,))
   for sheet,row_no,raw,row_hash in cur:
    if hashlib.sha256(canonical(raw).encode()).hexdigest()!=row_hash:raise AssertionError('RETAINED_PAYLOAD_HASH_MISMATCH')
    observed.update(canonical([sheet,row_no,row_hash]).encode()+b'\n');read_count+=1
  if read_count!=count or observed.hexdigest()!=stream.hexdigest():raise AssertionError('ROW_COMPLETENESS_MISMATCH')
  mismatch=conn.execute("SELECT count(*) FROM lv_eval.retail_line WHERE run_id=%s AND extended_minor IS NOT NULL AND (unit_minor IS DISTINCT FROM (raw->'values'->>5)::numeric*100 OR extended_minor IS DISTINCT FROM (raw->'values'->>5)::numeric*(raw->'values'->>3)::numeric*100)",(run,)).fetchone()[0]
  if mismatch:raise AssertionError('POSTGRES_NUMERIC_MISMATCH')
  summary=summarize(conn,run)
  elapsed=time.monotonic()-start
  report={'run_id':run,'profile':PROFILE,'source_sha256':source_hash,'provenance':provenance,'retrieved_and_evaluated_at':datetime.now(timezone.utc).isoformat(),
   'row_stream_sha256':stream.hexdigest(),'rows':count,'row_payload_hashes_verified':read_count,'independent_rational_money_checks':oracle_checked,'postgres_numeric_mismatches':mismatch,
   'observed_staging_and_readback_seconds':round(elapsed,3),'observed_rows_per_second':round(count/elapsed,1),'environment_note':'single native PostgreSQL evaluation run; includes parse, kernel, staging, hash readback, indexes and analysis; not a production SLA',
   'idempotent_replayed':False,'summary':summary,'claims':{'settlement_accuracy':None,'fraud_accuracy':None,'accounting_approval':False,'production_data_imported':False,'all_raw_rows_retained':True,'financial_posting_performed':False,
   'limitation':'Retail invoice lines contain no payments, processor fees, bank deposits or matched settlement labels. Candidate rows are an offline compatibility classification, not verified invoices or settlement truth.'}}
  conn.execute('UPDATE lv_eval.run SET completed_report=%s WHERE id=%s',(Jsonb(report),run))
 return report


def summarize(conn,run):
 total,candidates,quarantined=conn.execute('SELECT count(*),count(*) FILTER(WHERE candidate),count(*) FILTER(WHERE NOT candidate) FROM lv_eval.retail_line WHERE run_id=%s',(run,)).fetchone()
 sheets=[{'sheet':s,'rows':n,'candidate_rows':v} for s,n,v in conn.execute('SELECT sheet,count(*),count(*) FILTER(WHERE candidate) FROM lv_eval.retail_line WHERE run_id=%s GROUP BY sheet ORDER BY sheet',(run,))]
 reasons={r:n for r,n in conn.execute('SELECT reason,count(*) FROM lv_eval.retail_line CROSS JOIN LATERAL unnest(reasons) reason WHERE run_id=%s GROUP BY reason ORDER BY reason',(run,))}
 duplicates=conn.execute('SELECT count(*),coalesce(sum(n-1),0)::bigint FROM (SELECT count(*) n FROM lv_eval.retail_line WHERE run_id=%s GROUP BY row_sha256 HAVING count(*)>1) d',(run,)).fetchone()
 # Invoice ids identify source groups, not declared economic events. Any bad line or inconsistent identity holds the whole group.
 groups=conn.execute("WITH groups AS (SELECT invoice,bool_and(candidate) clean,count(DISTINCT customer)=1 AND count(DISTINCT country)=1 AND count(DISTINCT invoice_date)=1 consistent,sum(extended_minor) FILTER(WHERE candidate) amount FROM lv_eval.retail_line WHERE run_id=%s AND invoice IS NOT NULL GROUP BY invoice) SELECT count(*),count(*) FILTER(WHERE clean AND consistent),count(*) FILTER(WHERE NOT(clean AND consistent)),coalesce(sum(amount) FILTER(WHERE clean AND consistent),0)::text FROM groups",(run,)).fetchone()
 overlap=conn.execute('SELECT count(*) FROM (SELECT invoice FROM lv_eval.retail_line WHERE run_id=%s AND invoice IS NOT NULL GROUP BY invoice HAVING count(DISTINCT sheet)>1) a',(run,)).fetchone()[0]
 dates=conn.execute('SELECT min(invoice_date)::text,max(invoice_date)::text FROM lv_eval.retail_line WHERE run_id=%s',(run,)).fetchone()
 # Diagnostic only: never rounds or admits a source amount. A near-penny value may be an Excel representation artifact.
 precision=conn.execute("WITH prices AS (SELECT (raw->'values'->>5)::numeric value FROM lv_eval.retail_line WHERE run_id=%s AND raw->'values'->>5 ~ '^-?[0-9]+(\\.[0-9]+)?([eE][+-]?[0-9]+)?$'), diffs AS (SELECT abs(value*100-round(value*100)) delta FROM prices) SELECT count(*) FILTER(WHERE delta>0),count(*) FILTER(WHERE delta>0 AND delta<=0.000000001),count(*) FILTER(WHERE delta>0.000000001) FROM diffs",(run,)).fetchone()

 return {'rows':total,'candidate_rows':candidates,'quarantined_rows':quarantined,'per_sheet':sheets,'reason_row_counts_nonexclusive':reasons,'duplicate_content_groups':duplicates[0],'additional_duplicate_content_rows_retained':duplicates[1],
 'source_invoice_groups':groups[0],'complete_positive_consistent_invoice_groups':groups[1],'held_invoice_groups':groups[2],'candidate_invoice_extended_minor_sum':groups[3],
 'candidate_sum_scope':'raw line extensions for fully candidate, identity-consistent groups; duplicates retained, tax/discount/shipping semantics unverified; not accounting revenue',
 'price_precision_diagnostic':{'stored_numeric_prices_not_whole_pence':precision[0],'within_one_billionth_penny_of_whole_penny':precision[1],'larger_sub_penny_difference':precision[2],'diagnostic_threshold_pence':'0.000000001','acceptance_policy_changed':False},'invoice_ids_present_in_both_sheets':overlap,'first_source_date':dates[0],'last_source_date':dates[1]}
