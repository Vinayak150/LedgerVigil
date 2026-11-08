"""RFC8785 canonicalization for the pack's exact JSON profile (no floating money)."""
import hashlib,json,html,io,zipfile,re
from datetime import datetime
from uuid import UUID
ZERO='sha256:'+'0'*64
class IntegrityError(ValueError):pass

def canonical(value):
 def render(v,depth=0):
  if depth>64:raise IntegrityError("JSON nesting exceeds64")
  if v is None:return 'null'
  if type(v) is bool:return 'true' if v else 'false'
  if type(v) is str:
   v.encode('utf-16-be');return json.dumps(v,ensure_ascii=False,separators=(',',':'))
  if type(v) is int and abs(v)<=9007199254740991:return str(v)
  if type(v) is list:return '['+','.join(render(x,depth+1) for x in v)+']'
  if type(v) is dict and all(type(k) is str for k in v):return '{'+','.join(render(k)+':'+render(v[k],depth+1) for k in sorted(v,key=lambda k:k.encode('utf-16-be'))) +'}'
  raise IntegrityError('Unsupported JSON number/type; amounts and large sequences must be strings')
 try:return render(value).encode('utf-8')
 except (ValueError,UnicodeError,RecursionError) as e:raise IntegrityError('Invalid canonical JSON') from e

def digest(data):return 'sha256:'+hashlib.sha256(data).hexdigest()
def chain(content_hash,previous):return digest((content_hash+previous).encode('ascii'))
def seal(content,previous,at):
 h=digest(canonical(content));return {'manifest_schema':'lv-proofpack/1','content':content,'content_hash':h,'chain':{'previous_chain_head':previous,'chain_hash':chain(h,previous)},'sealed_at':at}
def verify(pack):
 try:
  if set(pack)!=set(('manifest_schema','content','content_hash','chain','sealed_at')) or pack['manifest_schema']!='lv-proofpack/1':raise IntegrityError('Invalid manifest schema')
  if type(pack['sealed_at']) is not str or datetime.fromisoformat(pack['sealed_at'].replace('Z','+00:00')).tzinfo is None:raise IntegrityError('Invalid seal timestamp')
  content=pack['content']
  for field in ('tenant_id','proof_pack_id'):
   if str(UUID(content[field]))!=content[field]:raise IntegrityError('Invalid pack scope')
  if type(content['proposal']['revision']) is not int or content['proposal']['revision']<1:raise IntegrityError('Invalid revision')
  h=digest(canonical(content));previous=pack['chain']['previous_chain_head']
  if not re.fullmatch(r'sha256:[0-9a-f]{64}',previous):raise IntegrityError('Invalid chain head')
  if h!=pack['content_hash'] or chain(h,previous)!=pack['chain']['chain_hash']:raise IntegrityError('Pack hash or chain mismatch')
  if content['accounting_review_status']!='UNREVIEWED':raise IntegrityError('Unsupported accounting claim')
  return h
 except (KeyError,TypeError,ValueError,AttributeError) as exc:
  if isinstance(exc,IntegrityError):raise
  raise IntegrityError('Malformed proof pack') from None

def readable(pack):
 verify(pack);pack=json.loads(canonical(pack));content=pack['content']
 def text(value):return html.escape(str(value),quote=True)
 def section(title,value):
  return '<section><h2>'+text(title)+'</h2><pre>'+html.escape(json.dumps(value,ensure_ascii=False,indent=2))+'</pre></section>'
 rows=[('Proposal',content['proposal']['id']),('Revision',content['proposal']['revision']),('Ledger',content['ledger']['id']),('Sealed at',pack['sealed_at']),('Content SHA-256',pack['content_hash']),('Chain SHA-256',pack['chain']['chain_hash'])]
 summary='<dl>'+''.join('<div><dt>'+text(k)+'</dt><dd>'+text(v)+'</dd></div>' for k,v in rows)+'</dl>'
 style='body{margin:0;background:#f4f6f5;color:#172c25;font:16px/1.6 system-ui,sans-serif}main{max-width:1040px;margin:auto;padding:40px 24px}h1{font-size:32px;line-height:1.2}h2{font-size:20px}section,header{background:white;border:1px solid #d0dcd6;border-radius:12px;padding:24px;margin-bottom:20px}small,dt{color:#465e53}dl div{padding:10px 0;border-bottom:1px solid #e4eae6}dt{font-weight:600}dd{margin:4px 0 0;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.7 ui-monospace,monospace}.status{display:inline-block;padding:4px 12px;background:#fff0cd;color:#624300;border-radius:6px;font-weight:700}@media(max-width:600px){main{padding:16px}section,header{padding:18px}h1{font-size:26px}}@media print{body{background:white}main{padding:0}section,header{break-inside:avoid}}'
 report='<header><small>LEDGERVIGIL · NATIVE EVIDENCE</small><h1>Proof pack</h1><p class="status">SANDBOX · accounting UNREVIEWED</p><p>This report presents the sealed revision and its recorded evidence. Integrity checks do not establish accounting accuracy or a digital signature.</p>'+summary+'</header>'
 for title,key in [('Proposed operations','operations'),('Evidence references','evidence'),('Recorded decisions','decisions'),('Simulation','simulation'),('Execution receipt','execution')]:report+=section(title,content.get(key))
 report+='<section><h2>Independent verification</h2><p>Use the offline verifier and a separately trusted Git anchor repository to verify this bundle. An included hash alone does not establish independent authenticity.</p><details><summary>Full canonical manifest data</summary><pre>'+html.escape(json.dumps(pack,ensure_ascii=False,indent=2))+'</pre></details></section>'
 return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LedgerVigil proof pack</title><style>'+style+'</style></head><body><main>'+report+'</main></body></html>').encode()

def archive(pack):
 verify(pack);rendered=readable(pack);manifest=canonical(pack)+b'\n'
 if len(rendered)+len(manifest)>67108864:raise IntegrityError('Expanded proof pack exceeds64MiB')
 buf=io.BytesIO()
 with zipfile.ZipFile(buf,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
  for name,data in [('manifest.json',manifest),('report.html',rendered)]:
   entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED;entry.external_attr=0o100644<<16;z.writestr(entry,data,compresslevel=9)
 data=buf.getvalue()
 if len(data)>16777216:raise IntegrityError('Compressed proof pack exceeds16MiB')
 return data
