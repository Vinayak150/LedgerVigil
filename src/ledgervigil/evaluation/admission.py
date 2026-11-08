"""Immutable, non-posting invoice admission plans from retained source observations.

DATA-1/DATA-2 remain proposed. No rounding, row deletion, authoritative invoice,
SOURCE-trusted evidence, readiness, approval or financial effect is produced.
"""
from pathlib import Path
from itertools import groupby
from fractions import Fraction
from collections import Counter
import gzip,hashlib,json,os,re,shutil,sqlite3,tempfile
from .retail import PROFILE as SOURCE_PROFILE,SOURCE,PUBLIC_SHA256,SHEETS,SourceRow,assess,canonical,file_hash

PROFILE='lv-invoice-admission-plan/1'
FILE='invoice-plan.jsonl.gz'
MAX_MANIFEST_BYTES=65536
MAX_GROUP_ROWS=50000
MAX_LINE_BYTES=32*1024**2
MAX_PLAN_BYTES=1024**3
MAX_MINOR=10**18
_after_group=None
_before_manifest=None

class PlanRejected(ValueError):pass


def attribution(source_hash):
 return {key:SOURCE[key] for key in ('citation','url','doi','license','license_url')} if source_hash==PUBLIC_SHA256 else {'classification':'SYNTHETIC_TEST'}


class HashReader:
 def __init__(self,stream):self.stream=stream;self.digest=hashlib.sha256();self.bytes=0
 def read(self,n=-1):
  data=self.stream.read(n);self.digest.update(data);self.bytes+=len(data);return data


def hash_value(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def identity(raw,sheet,row):
 invoice=raw['values'][0]
 return ['invoice',invoice] if invoice else ['missing-invoice',sheet,row]


def checked_source(sheet,number,raw,row_hash):
 if sheet not in dict(SHEETS) or type(number) is not int or number<2 or not isinstance(raw,dict) or set(raw)!={'values','cell_issues'}:raise PlanRejected('SOURCE_ROW_SCHEMA')
 values=raw['values'];issues=raw['cell_issues']
 if not isinstance(values,list) or len(values)!=8 or any(v is not None and not isinstance(v,str) for v in values) or not isinstance(issues,list) or any(v not in ('FORMULA_CELL','UNSUPPORTED_CELL_TYPE') for v in issues):raise PlanRejected('SOURCE_ROW_SCHEMA')
 source=SourceRow(sheet,number,tuple(values),tuple(issues))
 if source.sha256!=row_hash:raise PlanRejected('SOURCE_PAYLOAD_HASH_MISMATCH')
 return source


def group_record(source_hash,rows):
 if not rows or len(rows)>MAX_GROUP_ROWS:raise PlanRejected('GROUP_BUDGET_EXCEEDED')
 first=rows[0];key=identity(first['raw'],first['sheet'],first['row']);reasons=set();total=0;hashes=[];sheets=set();customers=set();countries=set();times=set()
 for line in rows:
  if not isinstance(line,dict) or set(line)!={'sheet','row','raw','sha256'}:raise PlanRejected('SOURCE_REFERENCE_SCHEMA')
  source=checked_source(line['sheet'],line['row'],line['raw'],line['sha256'])
  if identity(line['raw'],line['sheet'],line['row'])!=key:raise PlanRejected('SOURCE_GROUP_IDENTITY_MISMATCH')
  result=assess(source);reasons.update('LINE_'+reason for reason in result['reasons'])
  hashes.append(source.sha256);sheets.add(source.sheet);customers.add(source.values[6]);countries.add(source.values[7]);times.add(source.values[4])
  if result['candidate']:
   # Recompute with rational arithmetic; a full group is eligible for an amount only if every line survives.
   pence=Fraction(source.values[5])*100;quantity=int(source.values[3])
   if pence.denominator!=1 or int(pence)*quantity!=result['extended_minor']:raise PlanRejected('GROUP_MONEY_MISMATCH')
   total+=result['extended_minor']
 if len(set(hashes))!=len(hashes):reasons.add('DUPLICATE_CONTENT_UNRESOLVED')
 if len(sheets)>1:reasons.add('CROSS_SHEET_INVOICE_ID_UNRESOLVED')
 if len(customers)>1:reasons.add('CUSTOMER_CONFLICT')
 if len(countries)>1:reasons.add('COUNTRY_CONFLICT')
 if len(times)>1:reasons.add('SOURCE_TIMESTAMP_CONFLICT')
 if total>=MAX_MINOR:reasons.add('INVOICE_AMOUNT_RANGE')
 if key[0]=='missing-invoice':reasons.add('MISSING_INVOICE_ID')
 amount=None if reasons else {'amount':f'{total//100}.{total%100:02d}','currency':'GBP'}
 body={'group_id':hash_value([PROFILE,source_hash,key]),'source_identity':key,'status':'HOLD' if reasons else 'CANDIDATE_REVIEW',
       'posting_eligibility':'UNAVAILABLE','accounting_status':'UNREVIEWED','hold_reasons':sorted(reasons),
       'source_line_count':len(rows),'amount':amount,'source_rows':rows}
 return {'type':'invoice_group','payload':body,'payload_sha256':hash_value(body)}


def _write(stream,value,digest):
 data=(canonical(value)+'\n').encode()
 if len(data)>MAX_LINE_BYTES:raise PlanRejected('GROUP_BYTE_BUDGET_EXCEEDED')
 stream.write(data);digest.update(data)


def export_plan(conn,run_id,destination):
 """Single read-only snapshot. Directory is exclusive; manifest is published last."""
 destination=Path(destination);destination.mkdir(parents=False,exist_ok=False);complete=False
 try:
  with conn.transaction():
   conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
   row=conn.execute('SELECT source_sha256,profile,completed_report,provenance FROM lv_eval.run WHERE id=%s',(run_id,)).fetchone()
   if not row:raise PlanRejected('RUN_NOT_FOUND')
   source_hash,source_profile,report,provenance=row
   if source_profile!=SOURCE_PROFILE or report.get('rows',0)<1 or report.get('source_sha256')!=source_hash:raise PlanRejected('RUN_NOT_COMPLETE')
   expected_provenance='PUBLIC_OBSERVATIONAL' if source_hash==PUBLIC_SHA256 else 'SYNTHETIC_TEST'
   if provenance.get('classification')!=expected_provenance:raise PlanRejected('SOURCE_CLASSIFICATION_MISMATCH')
   source_stream=hashlib.sha256();source_count=0
   with conn.cursor(name='admission_source_integrity') as cur:
    cur.itersize=5000;cur.execute('SELECT sheet,row_no,raw,row_sha256 FROM lv_eval.retail_line WHERE run_id=%s ORDER BY sheet COLLATE "C",row_no',(run_id,))
    for sheet,number,raw,sha in cur:
     checked_source(sheet,number,raw,sha);source_count+=1;source_stream.update((canonical([sheet,number,sha])+'\n').encode())
   if source_count!=report['rows'] or source_stream.hexdigest()!=report.get('row_stream_sha256'):raise PlanRejected('SOURCE_COMPLETENESS_MISMATCH')
   header={'type':'header','profile':PROFILE,'source_profile':source_profile,'source_sha256':source_hash,
           'classification':expected_provenance,'source_attribution':attribution(source_hash),'source_row_count':source_count,'source_row_stream_sha256':source_stream.hexdigest(),
           'policy_status':{'DATA-1':'PROPOSED','DATA-2':'PROPOSED'},'accounting_status':'UNREVIEWED','posting_eligibility':'UNAVAILABLE'}
   digest=hashlib.sha256();groups=0;counts=Counter();reasons=Counter();all_rows=0;amount=0;chain=hashlib.sha256()
   with (destination/FILE).open('xb') as output:
    with gzip.GzipFile(filename='',mode='wb',fileobj=output,mtime=0) as zipped:
     _write(zipped,header,digest)
     with conn.cursor(name='admission_groups') as cur:
      cur.itersize=5000
      # An explicit source key prevents missing ids merging into one invented invoice.
      cur.execute("SELECT sheet,row_no,raw,row_sha256,candidate,reasons,quantity,unit_minor,extended_minor,invoice,customer,country,invoice_date::text FROM lv_eval.retail_line WHERE run_id=%s ORDER BY (CASE WHEN coalesce(invoice,'')<>'' THEN 'I:'||invoice ELSE 'M:'||sheet||':'||row_no::text END) COLLATE \"C\",sheet COLLATE \"C\",row_no",(run_id,))
      def key(r):return tuple(identity(r[2],r[0],r[1]))
      for _,records in groupby(cur,key):
       lines=[]
       for r in records:
        source=checked_source(r[0],r[1],r[2],r[3]);expected=assess(source)
        if tuple(r[4:])!=(expected['candidate'],expected['reasons'],expected['quantity'],expected['unit_minor'],expected['extended_minor'],expected['invoice'],expected['customer'],expected['country'],expected['invoice_date']):raise PlanRejected('STAGED_ASSESSMENT_MISMATCH')
        lines.append({'sheet':r[0],'row':r[1],'raw':r[2],'sha256':r[3]})
        if len(lines)>MAX_GROUP_ROWS:raise PlanRejected('GROUP_BUDGET_EXCEEDED')
       record=group_record(source_hash,lines);body=record['payload'];groups+=1;all_rows+=len(lines);counts[body['status']]+=1;reasons.update(body['hold_reasons']);chain.update((record['payload_sha256']+'\n').encode())
       if body['amount']:amount+=int(Fraction(body['amount']['amount'])*100)
       _write(zipped,record,digest)
       if _after_group is not None:_after_group(groups)
     if all_rows!=source_count:raise PlanRejected('PLAN_ROW_COUNT_MISMATCH')
     footer={'type':'summary','groups':groups,'source_rows':all_rows,'status_counts':dict(sorted(counts.items())),'hold_reason_group_counts_nonexclusive':dict(sorted(reasons.items())),
             'candidate_review_amount_minor_sum':str(amount),'amount_scope':'exact whole-invoice candidates for review only; no posting authority','group_chain_sha256':chain.hexdigest()}
     _write(zipped,footer,digest)
    output.flush();os.fsync(output.fileno())
   manifest={'profile':PROFILE,'file':FILE,'compressed_bytes':(destination/FILE).stat().st_size,'compressed_sha256':file_hash(destination/FILE),'uncompressed_sha256':digest.hexdigest(),'header':header,'summary':footer}
   if _before_manifest is not None:_before_manifest()
   with (destination/'MANIFEST.json').open('x') as f:
    f.write(json.dumps(manifest,indent=2)+'\n');f.flush();os.fsync(f.fileno())
   complete=True
   return manifest
 finally:
  if not complete:shutil.rmtree(destination)


def _load(data):
 def pairs(items):
  result={}
  for k,v in items:
   if k in result:raise PlanRejected('DUPLICATE_JSON_KEY')
   result[k]=v
  return result
 def reject(_):raise PlanRejected('NON_INTEGER_JSON_NUMBER')
 try:return json.loads(data,object_pairs_hook=pairs,parse_float=reject,parse_constant=reject)
 except (ValueError,UnicodeError) as e:raise PlanRejected('INVALID_JSON') from e


def verify_plan(directory,*,expected_manifest_sha256=None,select_invoice=None):
 """Verify the complete artifact before returning a selected invoice or summary.

Hash integrity is not external signing. Pin expected_manifest_sha256 to a trusted
review package when authenticity matters. SQLite bounds coverage-check memory.
"""
 directory=Path(directory);manifest_file=directory/'MANIFEST.json'
 with manifest_file.open('rb') as f:manifest_bytes=f.read(MAX_MANIFEST_BYTES+1)
 if len(manifest_bytes)>MAX_MANIFEST_BYTES:raise PlanRejected('MANIFEST_BUDGET_EXCEEDED')
 manifest_sha=hashlib.sha256(manifest_bytes).hexdigest()
 if expected_manifest_sha256 is not None and manifest_sha!=expected_manifest_sha256:raise PlanRejected('MANIFEST_HASH_MISMATCH')
 manifest=_load(manifest_bytes)
 if manifest.get('profile')!=PROFILE or manifest.get('file')!=FILE:raise PlanRejected('MANIFEST_SCHEMA')
 if (directory/FILE).stat().st_size!=manifest.get('compressed_bytes') or file_hash(directory/FILE)!=manifest.get('compressed_sha256'):raise PlanRejected('PLAN_HASH_MISMATCH')
 digest=hashlib.sha256();chain=hashlib.sha256();groups=0;rows=0;counts=Counter();reasons=Counter();amount=0;header=None;footer=None;selected=None;total_bytes=0
 with tempfile.TemporaryDirectory(prefix='lv-plan-verify-') as temp:
  db=sqlite3.connect(Path(temp)/'coverage.sqlite');db.execute('CREATE TABLE refs(sheet TEXT,row_no INTEGER,sha TEXT,PRIMARY KEY(sheet,row_no)) WITHOUT ROWID');db.execute('CREATE TABLE groups(id TEXT PRIMARY KEY) WITHOUT ROWID')
  try:
   with (directory/FILE).open('rb') as compressed:
    hashed=HashReader(compressed)
    stream=gzip.GzipFile(fileobj=hashed,mode='rb')
    while data:=stream.readline(MAX_LINE_BYTES+1):
     total_bytes+=len(data)
     if len(data)>MAX_LINE_BYTES or total_bytes>MAX_PLAN_BYTES:raise PlanRejected('PLAN_BUDGET_EXCEEDED')
     value=_load(data)
     if data!=(canonical(value)+'\n').encode():raise PlanRejected('NONCANONICAL_PLAN')
     digest.update(data)
     if header is None:
      if value!=manifest.get('header') or value.get('type')!='header' or value.get('profile')!=PROFILE or value.get('posting_eligibility')!='UNAVAILABLE' or value.get('accounting_status')!='UNREVIEWED':raise PlanRejected('HEADER_MISMATCH')
      expected_class='PUBLIC_OBSERVATIONAL' if value.get('source_sha256')==PUBLIC_SHA256 else 'SYNTHETIC_TEST'
      if value.get('source_profile')!=SOURCE_PROFILE or value.get('classification')!=expected_class or value.get('policy_status')!={'DATA-1':'PROPOSED','DATA-2':'PROPOSED'} or not re.fullmatch('[0-9a-f]{64}',value.get('source_sha256','')) or type(value.get('source_row_count')) is not int or value['source_row_count']<1:raise PlanRejected('HEADER_SCOPE_MISMATCH')
      if value.get('source_attribution')!=attribution(value['source_sha256']):raise PlanRejected('SOURCE_ATTRIBUTION_MISMATCH')
      if set(value)!={'type','profile','source_profile','source_sha256','classification','source_attribution','source_row_count','source_row_stream_sha256','policy_status','accounting_status','posting_eligibility'}:raise PlanRejected('HEADER_SCHEMA')
      header=value;continue
     if footer is not None:raise PlanRejected('TRAILING_PLAN_CONTENT')
     if value.get('type')=='summary':footer=value;continue
     if value.get('type')!='invoice_group':raise PlanRejected('PLAN_RECORD_TYPE')
     body=value.get('payload',{});lines=body.get('source_rows',[])
     if value!=group_record(header['source_sha256'],lines):raise PlanRejected('INVOICE_PLAN_MISMATCH')
     try:
      db.execute('INSERT INTO groups VALUES(?)',(body['group_id'],))
      db.executemany('INSERT INTO refs VALUES(?,?,?)',[(r['sheet'],r['row'],r['sha256']) for r in lines])
     except sqlite3.IntegrityError:raise PlanRejected('DUPLICATE_SOURCE_REFERENCE') from None
     groups+=1;rows+=len(lines);counts[body['status']]+=1;reasons.update(body['hold_reasons']);chain.update((value['payload_sha256']+'\n').encode())
     if body['amount']:amount+=int(Fraction(body['amount']['amount'])*100)
     if select_invoice is not None and body['source_identity']==['invoice',select_invoice]:selected=body
   if hashed.bytes!=manifest.get('compressed_bytes') or hashed.digest.hexdigest()!=manifest.get('compressed_sha256'):raise PlanRejected('COMPRESSED_READBACK_MISMATCH')
   if footer is None or footer!=manifest.get('summary') or digest.hexdigest()!=manifest.get('uncompressed_sha256'):raise PlanRejected('PLAN_SUMMARY_OR_HASH_MISMATCH')
   checks={'groups':groups,'source_rows':rows,'status_counts':dict(sorted(counts.items())),'hold_reason_group_counts_nonexclusive':dict(sorted(reasons.items())),
           'candidate_review_amount_minor_sum':str(amount),'group_chain_sha256':chain.hexdigest()}
   if footer!={'type':'summary',**checks,'amount_scope':'exact whole-invoice candidates for review only; no posting authority'} or rows!=header['source_row_count']:raise PlanRejected('PLAN_COUNTS_MISMATCH')
   source=hashlib.sha256()
   for sheet,number,sha in db.execute('SELECT sheet,row_no,sha FROM refs ORDER BY sheet,row_no'):source.update((canonical([sheet,number,sha])+'\n').encode())
   if source.hexdigest()!=header['source_row_stream_sha256']:raise PlanRejected('SOURCE_COVERAGE_MISMATCH')
   return {'verified':True,'manifest_sha256':manifest_sha,'header':header,'summary':footer,'selected':selected}
  finally:db.close()
