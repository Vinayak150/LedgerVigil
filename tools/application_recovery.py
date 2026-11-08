"""Private same-host application recovery acceptance; never a production restore tool."""
from pathlib import Path,PurePosixPath
import hashlib,importlib.metadata,json,os,re,shutil,stat,subprocess,sys
from psycopg.conninfo import make_conninfo
from tools.dev_db import ROOT,env_file
from ledgervigil.execution.proof_export import export_proof
from ledgervigil.execution.proof_verify import verify
from ledgervigil.ingestion.plan_index import IndexReader,MANIFEST,DATABASE
from ledgervigil.ingestion.index_publication import INDEX_MANIFEST_SHA256
from ledgervigil.evaluation.retail import PUBLIC_SHA256

MAX_SOURCE_FILES=2000
MAX_SOURCE_BYTES=64*1024*1024
WORKBOOK='online_retail_II.xlsx'
ASSET_FILES=(MANIFEST,DATABASE)
PACKAGES=('fastapi','uvicorn','sqlalchemy','alembic','psycopg','argon2-cffi','celery','redis')

def digest(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()

def private_json(path,data):
 fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'w') as f:json.dump(data,f,sort_keys=True,indent=2);f.write('\n')

def safe_file(root,relative):
 if not isinstance(relative,str):raise ValueError('RECOVERY_UNSAFE_PATH')
 p=PurePosixPath(relative)
 if p.is_absolute() or str(p)!=relative or not p.parts or any(x in ('.','..') for x in p.parts):raise ValueError('RECOVERY_UNSAFE_PATH')
 allowed_roots={'src','tools','tests','migrations','fixtures','api','docs','web','scripts'}
 allowed_files={'.gitignore','README.md','alembic.ini','compose.yaml','pyproject.toml','requirements.lock','worker-requirements.lock'}
 if (len(p.parts)==1 and relative not in allowed_files) or (len(p.parts)>1 and p.parts[0] not in allowed_roots):raise ValueError('RECOVERY_UNSAFE_PATH')
 if any(x in ('node_modules','__pycache__','.venv','.next','.git') or x.startswith('.env') or x.endswith(('.pyc','.pyo')) for x in p.parts):raise ValueError('RECOVERY_PRIVATE_OR_GENERATED_PATH')
 current=Path(root)
 for part in p.parts:
  current=current/part
  if current.is_symlink():raise ValueError('RECOVERY_SYMLINK_REFUSED')
 if not current.is_file() or not stat.S_ISREG(current.stat().st_mode):raise ValueError('RECOVERY_REGULAR_FILE_REQUIRED')
 return current

def check_source(root,manifest):
 files=manifest.get('files')
 if not isinstance(files,list) or not 1<=len(files)<=MAX_SOURCE_FILES:raise ValueError('RECOVERY_SOURCE_INVENTORY')
 seen=set();total=0
 for row in files:
  name=row['path'];p=safe_file(root,name)
  if name in seen:raise ValueError('RECOVERY_DUPLICATE_PATH')
  seen.add(name);size=p.stat().st_size;total+=size
  if type(row['bytes']) is not int or row['bytes']!=size or total>MAX_SOURCE_BYTES or not re.fullmatch('[0-9a-f]{64}',row['sha256']) or digest(p)!=row['sha256']:raise ValueError('RECOVERY_SOURCE_HASH_MISMATCH')
 needed={'src/ledgervigil/app.py','tools/recovery_probe.py','tools/application_recovery.py','tools/recovery_database.py','requirements.lock','worker-requirements.lock','web/package-lock.json'}
 if not needed<=seen:raise ValueError('RECOVERY_SOURCE_INCOMPLETE')
 return {'files':len(files),'bytes':total,'manifest_sha256':hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()}

def migration_head(root,c):
 from alembic.config import Config
 from alembic.script import ScriptDirectory
 cfg=Config(str(Path(root)/'alembic.ini'));cfg.set_main_option('script_location',str(Path(root)/'migrations'))
 heads=ScriptDirectory.from_config(cfg).get_heads();actual=c.execute('SELECT version_num FROM lv_meta.alembic_version').fetchall()
 if len(heads)!=1 or actual!=[(heads[0],)]:raise ValueError('RECOVERY_SOURCE_MIGRATION_MISMATCH')
 return heads[0]

def copy_private(source,dest,mode=0o600):
 if source.is_symlink() or not source.is_file():raise ValueError('RECOVERY_ASSET_REGULAR_FILE_REQUIRED')
 dest.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
 with source.open('rb') as src:
  fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
  with os.fdopen(fd,'wb') as out:shutil.copyfileobj(src,out)
 os.chmod(dest,mode)
 if digest(source)!=digest(dest):raise ValueError('RECOVERY_COPY_MISMATCH')

def asset_sample(reader):
 first=reader.groups(None,(0,''),3)
 if not first:raise ValueError('RECOVERY_EMPTY_INDEX')
 group=json.loads(first[0][2]);rows=reader.rows(group['group_id'],(0,''),20)
 with reader.connect() as c:counts=(c.execute('SELECT count(*) FROM groups').fetchone()[0],c.execute('SELECT count(*) FROM rows').fetchone()[0])
 if counts!=(reader.manifest['groups'],reader.manifest['rows']):raise ValueError('RECOVERY_INDEX_COUNTS')
 return {'groups':counts[0],'rows':counts[1],'group_sample_sha256':hashlib.sha256(json.dumps(first,separators=(',',':')).encode()).hexdigest(),'row_sample_sha256':hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest(),'sampled_rows':len(rows)}

def maintenance_inventory(c):
 return {'jobs':[list(r) for r in c.execute('SELECT state,count(*) FROM lv.work_item GROUP BY state ORDER BY state')],
 'delivery':[list(r) for r in c.execute("SELECT status,count(*) FROM lv.outbox WHERE work_item_id IS NOT NULL GROUP BY status ORDER BY status")],
 'native_completions':[list(r) for r in c.execute("SELECT status,count(*) FROM lv.outbox WHERE native_execution_id IS NOT NULL GROUP BY status ORDER BY status")]}

class ApplicationAcceptance:
 def __init__(self,source_manifest,assets,*,source_root=ROOT):
  self.source=Path(source_root);self.manifest=json.loads(Path(source_manifest).read_text());self.assets=Path(assets);self.bundle=None
 def capture(self,c,output):
  # Called inside the exact database snapshot imported by pg_dump.
  self.bundle=Path(output)/'application';self.bundle.mkdir(mode=0o700)
  source_info=check_source(self.source,self.manifest);head=migration_head(self.source,c);app=self.bundle/'app';app.mkdir(mode=0o700)
  modes={r['path']:0o700 if safe_file(self.source,r['path']).stat().st_mode&stat.S_IXUSR else 0o600 for r in self.manifest['files']}
  for r in self.manifest['files']:copy_private(safe_file(self.source,r['path']),app/r['path'],modes[r['path']])
  if check_source(app,self.manifest)!=source_info or check_source(self.source,self.manifest)!=source_info:raise ValueError('RECOVERY_SOURCE_CHANGED')
  if self.assets.is_symlink() or (self.assets/'index-v1').is_symlink():raise ValueError('RECOVERY_SYMLINK_REFUSED')
  original=IndexReader(self.assets/'index-v1',INDEX_MANIFEST_SHA256);sample=asset_sample(original)
  asset_dir=self.bundle/'data/public/uci-online-retail-ii';index=asset_dir/'index-v1'
  for name in ASSET_FILES:copy_private(self.assets/'index-v1'/name,index/name,0o400)
  if digest(self.assets/WORKBOOK)!=PUBLIC_SHA256:raise ValueError('RECOVERY_WORKBOOK_HASH_MISMATCH')
  copy_private(self.assets/WORKBOOK,asset_dir/WORKBOOK,0o400)
  copy_private(self.assets/'PROVENANCE.json',asset_dir/'PROVENANCE.json',0o400)
  if asset_sample(IndexReader(index,INDEX_MANIFEST_SHA256))!=sample:raise ValueError('RECOVERY_ASSET_COPY_MISMATCH')
  references=c.execute('SELECT tenant_id,id FROM lv.native_execution ORDER BY tenant_id,id LIMIT 26').fetchall()
  if len(references)>25:raise ValueError('RECOVERY_PROOF_LIMIT')
  proofs=[];proofdir=self.bundle/'proofs';proofdir.mkdir(mode=0o700)
  for tenant,eid in references:
   data=export_proof(c,eid);result=verify(data);path=proofdir/(str(eid)+'.json');fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
   with os.fdopen(fd,'wb') as f:f.write(data)
   proofs.append({'tenant_id':str(tenant),'execution_id':str(eid),'sha256':result['file_sha256']})
  runtime={'python':sys.version.split()[0],'dependencies':{p:importlib.metadata.version(p) for p in PACKAGES},
    'database':'loopback:55432; existing restricted roles and passwords required',
    'broker':'NOT_BACKED_UP; PostgreSQL is durable work authority; isolated broker recovery tested separately',
    'cookie_mode':'explicit local-http for loopback demo; secure default retained',
    'cursor_key':'NOT_BACKED_UP; ephemeral default invalidates old cursors after restart',
    'secrets':'NOT_BACKED_UP; existing private .env and .env.roles prerequisites',
    'processes':'NOT_RESTORED; API/Next/worker/scheduler require deliberate restart',
    'frontend':'source and lockfile backed up; node_modules/build artifacts not backed up or rebuilt by this drill',
    'native_completion_consumer':'NOT_IMPLEMENTED'}
  state={'source':source_info,'migration_head':head,'source_file_modes':modes,'assets':sample,'proofs':proofs,'maintenance':maintenance_inventory(c),'runtime':runtime,
    'asset_files':[{'path':str(p.relative_to(self.bundle)),'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(asset_dir.rglob('*')) if p.is_file()]}
  private_json(self.bundle/'SOURCE.json',self.manifest);private_json(self.bundle/'CAPTURE.json',state)
  return {'source_files':source_info['files'],'index_groups':sample['groups'],'index_rows':sample['rows'],'execution_proofs_captured':len(proofs)}
 def verify_restored(self,database):
  from tools.verify_recovery import restore_target
  restore_target(database);app=self.bundle/'app'
  check_source(app,self.manifest)
  env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','LANG','LC_ALL','SYSTEMROOT')}
  env.update(PYTHONPATH=str(app/'src')+os.pathsep+str(app),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
  for role,key in [('ADMIN','POSTGRES_PASSWORD'),('APP','APP_PASSWORD'),('WORKER','WORKER_PASSWORD')]:
   password=env_file('.env' if role=='ADMIN' else '.env.roles')[key]
   env['LV_RECOVERY_'+role+'_DSN']=make_conninfo(host='127.0.0.1',port=55432,dbname=database,user='postgres' if role=='ADMIN' else 'lv_'+role.lower(),password=password)
  child=subprocess.run([sys.executable,str(app/'tools/recovery_probe.py'),str(self.bundle)],cwd=app,env=env,capture_output=True,text=True,timeout=180)
  if child.returncode:raise ValueError('RECOVERY_APPLICATION_PROBE_FAILED')
  result=json.loads(child.stdout)
  if result.get('status')!='PASS':raise ValueError('RECOVERY_APPLICATION_PROBE_FAILED')
  check_source(app,self.manifest);private_json(self.bundle/'ACCEPTANCE.json',result)
  return result
