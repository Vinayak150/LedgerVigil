"""Explicitly provisioned independent Git anchors. Readers never write to the store."""
from pathlib import Path
from contextlib import contextmanager
from uuid import UUID
import fcntl,json,os,subprocess
from .integrity import canonical,IntegrityError,ZERO

class GitAnchors:
 def __init__(self,path):self.root=Path(path).resolve()
 def git(self,*args):
  try:
   return subprocess.run(['git','-c','safe.directory='+str(self.root),'-c','core.fsync=committed','-c','user.name=LedgerVigil Anchor','-c','user.email=anchor@ledgervigil.invalid','-c','commit.gpgsign=false',*args],cwd=self.root,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout.decode().strip()
  except (OSError,subprocess.CalledProcessError,UnicodeError) as exc:raise IntegrityError('External anchor repository is unavailable or corrupt') from exc
 def validate(self):
  if not (self.root/'.git').is_dir():raise IntegrityError('External anchor repository is unavailable; operator initialization is required')
  if self.git('rev-parse','--show-object-format')!='sha256' or self.git('rev-parse','--show-toplevel')!=str(self.root):raise IntegrityError('Invalid independent anchor repository')
 def initialize(self):
  """Operator-only creation; refuse an existing directory to prevent replacing lost history."""
  self.root.mkdir(mode=0o700,parents=False,exist_ok=False)
  self.git('init','--object-format=sha256');self.validate()
  return self
 def head(self):
  self.validate()
  # A deliberately provisioned empty repository has no refs yet. Invalid objects raise.
  refs=self.git('show-ref','--head') if self.git('for-each-ref','--format=%(refname)') else ''
  return self.git('rev-parse','--verify','HEAD') if refs else None
 @contextmanager
 def locked(self):
  self.validate()
  with (self.root/'anchor.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX);self.validate();yield
 def _read(self,tenant,head):
  name=str(UUID(str(tenant)))+'.json'
  if head is None or not self.git('ls-tree',head,'--','heads/'+name):return None
  try:return json.loads(self.git('show',head+':heads/'+name))
  except (ValueError,TypeError) as exc:raise IntegrityError('Malformed committed anchor') from exc
 def read(self,tenant):return self._read(tenant,self.head())
 def append(self,record):
  tenant=str(UUID(record['tenant_id']))
  with self.locked():
   head=self.head();old=self._read(tenant,head)
   if old==record:return self.git('log','-1','--format=%H',head,'--','heads/'+tenant+'.json')
   if (old['chain_hash'] if old else ZERO)!=record['previous_chain_head']:raise IntegrityError('External anchor fork or database rollback detected')
   if record['chain_seq']!=(old['chain_seq']+1 if old else 1):raise IntegrityError('External anchor sequence gap')
   p=self.root/'heads'/(tenant+'.json');p.parent.mkdir(mode=0o700,exist_ok=True);tmp=p.with_suffix('.tmp')
   with tmp.open('wb') as f:f.write(canonical(record)+b'\n');f.flush();os.fsync(f.fileno())
   os.replace(tmp,p);self.git('add','--','heads/'+p.name);self.git('commit','-m','Anchor '+tenant+' '+str(record['chain_seq']))
   return self.git('rev-parse','HEAD')
 def heads(self):
  head=self.head()
  if head is None:return []
  names=self.git('ls-tree','--name-only',head,'heads/').splitlines()
  try:return [json.loads(self.git('show',head+':'+name)) for name in names]
  except (ValueError,TypeError) as exc:raise IntegrityError('Malformed committed anchor') from exc
 def find(self,tenant,pack_id):
  head=self.head();name=str(UUID(str(tenant)))+'.json'
  if head is not None:
   for oid in self.git('log','--format=%H',head,'--','heads/'+name).splitlines():
    try:record=json.loads(self.git('show',oid+':heads/'+name))
    except (ValueError,TypeError) as exc:raise IntegrityError('Malformed committed anchor') from exc
    if record['pack_id']==str(pack_id):return record,oid
  raise IntegrityError('Proof pack is absent from the trusted Git history')
