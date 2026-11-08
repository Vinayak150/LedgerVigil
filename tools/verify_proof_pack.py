"""Offline standard-library verifier for a JSON+HTML proof bundle and optional independent Git anchor."""
from pathlib import Path
import argparse,json,sys,zipfile,io
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ledgervigil.proofpacks.integrity import verify,canonical,readable,digest,IntegrityError
from ledgervigil.proofpacks.anchors import GitAnchors

def check(data,repository=None):
 if not 0<len(data)<=16777216:raise IntegrityError('Bundle size out of range')
 with zipfile.ZipFile(io.BytesIO(data)) as z:
  entries=z.infolist()
  if sorted(x.filename for x in entries)!=['manifest.json','report.html'] or sum(x.file_size for x in entries)>67108864:raise IntegrityError('Invalid or oversized archive entries')
  raw=z.read('manifest.json');pack=json.loads(raw)
  if raw!=canonical(pack)+b'\n':raise IntegrityError('Manifest is not canonical')
  content_hash=verify(pack)
  if z.read('report.html')!=readable(pack):raise IntegrityError('HTML differs from the canonical manifest')
 result={'valid':True,'content_hash':content_hash,'chain_hash':pack['chain']['chain_hash'],'accounting':'UNREVIEWED','signature':'NONE','external_anchor':'NOT_CHECKED'}
 if repository:
  content=pack['content'];record,oid=GitAnchors(repository).find(content['tenant_id'],content['proof_pack_id'])
  if record['content_hash']!=content_hash or record['chain_hash']!=pack['chain']['chain_hash'] or record['bundle_hash']!=digest(data):raise IntegrityError('Bundle does not match the external anchor')
  result.update(external_anchor='LOCAL_GIT_VERIFIED',anchor_commit=oid)
 return result

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('bundle',type=Path);p.add_argument('--anchor-repository',type=Path);a=p.parse_args()
 try:print(json.dumps(check(a.bundle.read_bytes(),a.anchor_repository),sort_keys=True))
 except (ValueError,OSError,zipfile.BadZipFile) as e:print(json.dumps({'valid':False,'error':str(e)}));raise SystemExit(1)
