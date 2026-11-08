"""Rebuildable immutable public-plan index. No financial authority or tenant data."""
from pathlib import Path
from uuid import UUID
import gzip,hashlib,json,os,shutil,sqlite3,zlib
from contextlib import contextmanager
from urllib.parse import quote
from ledgervigil.evaluation.admission import verify_plan,canonical,FILE
from ledgervigil.evaluation.retail import file_hash

PROFILE='lv-public-plan-index/1'
DATABASE='index.sqlite'
MANIFEST='INDEX_MANIFEST.json'
PUBLIC_PLAN='3c7dbcb169405d9d0f8abcff095c9f30df46ca6b1b38fb31f894ed1657ea0523'
_before_publish=None
class IndexUnavailable(ValueError):pass

def page_uuid(value):return str(UUID(bytes=hashlib.sha256(value.encode()).digest()[:16]))

def build_index(plan,destination):
 plan=Path(plan);destination=Path(destination)
 if destination.exists():raise FileExistsError(str(destination))
 manifest_bytes=(plan/'MANIFEST.json').read_bytes()
 if hashlib.sha256(manifest_bytes).hexdigest()!=PUBLIC_PLAN:raise IndexUnavailable('PLAN_MANIFEST_MISMATCH')
 source_manifest=json.loads(manifest_bytes)
 checked=verify_plan(plan,expected_manifest_sha256=PUBLIC_PLAN)
 if checked['header']['classification']!='PUBLIC_OBSERVATIONAL':raise IndexUnavailable('PUBLIC_PLAN_REQUIRED')
 destination.mkdir(mode=0o700,parents=False,exist_ok=False);complete=False;db=None
 try:
  db=sqlite3.connect(destination/DATABASE)
  db.executescript('''CREATE TABLE groups(group_id TEXT NOT NULL UNIQUE,ordinal INTEGER PRIMARY KEY,page_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,metadata TEXT NOT NULL);
CREATE UNIQUE INDEX group_page ON groups(ordinal,page_id);
CREATE INDEX group_status_page ON groups(status,ordinal,page_id);
CREATE TABLE rows(group_ordinal INTEGER NOT NULL REFERENCES groups(ordinal),ordinal INTEGER NOT NULL,sheet_no INTEGER NOT NULL,row_no INTEGER NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(group_ordinal,ordinal),UNIQUE(sheet_no,row_no)) WITHOUT ROWID;''')
  digest=hashlib.sha256();groups=rows=0
  with db:
   with gzip.open(plan/FILE,'rb') as f:
    for raw in f:
     digest.update(raw);record=json.loads(raw)
     if record.get('type')!='invoice_group':continue
     body=record['payload'];gid=body['group_id'];groups+=1
     metadata={k:v for k,v in body.items() if k!='source_rows'};metadata['payload_sha256']=record['payload_sha256']
     db.execute('INSERT INTO groups VALUES(?,?,?,?,?)',(gid,groups,page_uuid(gid),body['status'],canonical(metadata)))
     for ordinal,row in enumerate(body['source_rows'],1):
      rows+=1;db.execute('INSERT INTO rows VALUES(?,?,?,?,?)',(groups,ordinal,0 if row['sheet']=='Year 2009-2010' else 1,row['row'],zlib.compress(canonical(row).encode())))
  if digest.hexdigest()!=source_manifest['uncompressed_sha256'] or groups!=checked['summary']['groups'] or rows!=checked['header']['source_row_count']:raise IndexUnavailable('INDEX_SOURCE_MISMATCH')
  db.execute('ANALYZE');db.commit();db.close();db=None
  output={'profile':PROFILE,'source_manifest_sha256':PUBLIC_PLAN,'source_row_stream_sha256':checked['header']['source_row_stream_sha256'],'groups':groups,'rows':rows,'database':DATABASE,'database_bytes':(destination/DATABASE).stat().st_size,'database_sha256':file_hash(destination/DATABASE),'sqlite_version':sqlite3.sqlite_version,'status_counts':checked['summary']['status_counts']}
  if _before_publish:_before_publish()
  with (destination/MANIFEST).open('x') as f:f.write(json.dumps(output,indent=2)+'\n');f.flush();os.fsync(f.fileno())
  os.chmod(destination/DATABASE,0o444);os.chmod(destination/MANIFEST,0o444);complete=True
  return {'manifest_sha256':file_hash(destination/MANIFEST),**output}
 finally:
  if db:db.close()
  if not complete:shutil.rmtree(destination)

class IndexReader:
 def __init__(self,directory,expected_manifest):
  self.directory=Path(directory);data=(self.directory/MANIFEST).read_bytes()
  if hashlib.sha256(data).hexdigest()!=expected_manifest:raise IndexUnavailable('INDEX_MANIFEST_MISMATCH')
  self.manifest=json.loads(data);self.path=self.directory/DATABASE
  if self.manifest.get('profile')!=PROFILE or self.manifest.get('source_manifest_sha256')!=PUBLIC_PLAN or self.manifest.get('database')!=DATABASE:raise IndexUnavailable('INDEX_SCOPE_MISMATCH')
  stat=self.path.stat()
  if stat.st_mode&0o222 or stat.st_size!=self.manifest['database_bytes'] or file_hash(self.path)!=self.manifest['database_sha256']:raise IndexUnavailable('INDEX_INTEGRITY_MISMATCH')
  self.identity=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
 @contextmanager
 def connect(self):
  s=self.path.stat()
  if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)!=self.identity or s.st_mode&0o222:raise IndexUnavailable('INDEX_CHANGED')
  db=sqlite3.connect('file:'+quote(str(self.path.resolve()),safe='/')+'?mode=ro&immutable=1',uri=True);db.execute('PRAGMA query_only=ON')
  try:yield db
  finally:db.close()
 def group_query(self,status,position,limit):
  query='SELECT ordinal,page_id,metadata FROM groups'+(' INDEXED BY group_status_page' if status else '')+' WHERE (ordinal,page_id)>(?,?)';args=list(position)
  if status:query+=' AND status=?';args.append(status)
  query+=' ORDER BY ordinal,page_id LIMIT ?';args.append(limit)
  return query,args
 def groups(self,status,position,limit):
  query,args=self.group_query(status,position,limit)
  with self.connect() as db:return db.execute(query,args).fetchall()
 def group(self,gid):
  with self.connect() as db:
   row=db.execute('SELECT metadata FROM groups WHERE group_id=?',(gid,)).fetchone()
  return json.loads(row[0]) if row else None
 def rows(self,gid,position,limit):
  with self.connect() as db:found=db.execute('SELECT ordinal,payload FROM rows WHERE group_ordinal=(SELECT ordinal FROM groups WHERE group_id=?) AND ordinal>? ORDER BY ordinal LIMIT ?',(gid,position[0],limit)).fetchall()
  return [(ordinal,page_uuid(gid+':'+str(ordinal)),zlib.decompress(payload).decode()) for ordinal,payload in found]
