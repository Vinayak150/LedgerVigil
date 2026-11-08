"""Authenticated asynchronous native integrity exports and session-bound15-minute links."""
import base64,hashlib,hmac,json,time,os
from uuid import UUID
from typing import Annotated
import psycopg
from fastapi import APIRouter,Request,Depends,Header
from fastapi.responses import JSONResponse,Response
from pydantic import BaseModel,ConfigDict
from ledgervigil.auth.boundary import session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.proposals.routes import raw_body
from ledgervigil.proposals.validation import parse_body,Invalid
from ledgervigil.reads.paging import check_query
_before_download=None
_now=time.time
EXPORT_ERRORS={status:{'description':description,'content':{'application/problem+json':{}}} for status,description in {400:'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER',401:'UNAUTHENTICATED',403:'PERMISSION_DENIED; expired or mismatched download link',404:'NOT_FOUND; missing or inaccessible resource',409:'INVALID_STATE_TRANSITION; export not ready',422:'SCHEMA_INVALID; IDEMPOTENCY_KEY_REUSED',500:'INTERNAL; no artifact response on failed audit commit'}.items()}
router=APIRouter(prefix='/api/v1',tags=['native-exports'])
class ExportJob(BaseModel):
 model_config=ConfigDict(extra='forbid')
 id:str;kind:str;state:str;execution_id:str|None;proof_pack_id:str|None=None;format:str='JSON';content_hash:str|None;bytes:int|None;attempts:int;last_error:str|None
class DownloadLink(BaseModel):
 url:str;expires_at:int

def call(request,action,subject,key=None):
 try:
  with session_transaction(request,permission='evidence.export',touch=False) as ctx:
   r=ctx.conn.execute('SELECT lv.native_export_api(%s,%s,%s,%s,%s,%s)',(token_hash(ctx.session_token),action,subject,key,source_key(request),request.state.request_id)).fetchone()[0]
 except psycopg.errors.InsufficientPrivilege as e:
  raise ProblemError(401 if 'session' in (e.diag.message_primary or '') else 403,'UNAUTHENTICATED' if 'session' in (e.diag.message_primary or '') else 'PERMISSION_DENIED') from None
 if r['status'] not in (200,202):raise ProblemError(r['status'],r['code'])
 return r

def claims(request,job,expiry):
 with session_transaction(request,permission='evidence.export',touch=False) as ctx:return {'j':str(job),'t':ctx.tenant_id,'u':ctx.user_id,'s':hmac.digest(request.app.state.auth_settings.cursor_key,b'LV-EXPORT-SESSION-v2\x00'+token_hash(ctx.session_token),'sha256').hex(),'exp':expiry}
def sign(request,c):
 raw=json.dumps(c,sort_keys=True,separators=(',',':')).encode();sig=hmac.digest(request.app.state.auth_settings.cursor_key,b'LV-NATIVE-EXPORT-LINK-v1\x00'+raw,'sha256')
 return base64.urlsafe_b64encode(raw+sig).decode().rstrip('=')
def validate_link(request,job,token):
 try:
  if type(token) is not str or len(token)>2048:raise ValueError()
  decoded=base64.b64decode(token+'='*(-len(token)%4),altchars=b'-_',validate=True);raw,sig=decoded[:-32],decoded[-32:]
  expected=hmac.digest(request.app.state.auth_settings.cursor_key,b'LV-NATIVE-EXPORT-LINK-v1\x00'+raw,'sha256')
  if not hmac.compare_digest(sig,expected):raise ValueError()
  data=json.loads(raw);exp=data['exp'];now=int(_now())
  if type(exp) is not int or not now<exp<=now+900 or data!=claims(request,job,exp):raise ValueError()
 except (ValueError,KeyError,TypeError,UnicodeError):raise ProblemError(403,'PERMISSION_DENIED','Download link is invalid or expired.') from None

@router.post('/executions/{execution_id}/exports',response_model=ExportJob,status_code=202,operation_id='requestNativeProofExport',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'evidence.export','requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False,'required':['format'],'properties':{'format':{'const':'JSON','type':'string'}}}}}}})
def create(execution_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=64)]=None):
 with session_transaction(request,permission='evidence.export',touch=False):
  check_query(request,())
  if key is None:raise ProblemError(400,'IDEMPOTENCY_KEY_REQUIRED')
  try:body=parse_body(raw)
  except Invalid as error:raise ProblemError(error.problem['status'],error.problem['code'],errors=[{'pointer':'','code':error.problem['code']}]) from None
  if body!={'format':'JSON'}:raise ProblemError(422,'SCHEMA_INVALID',errors=[{'pointer':'/format','code':'SCHEMA_INVALID'}])
 r=call(request,'CREATE',execution_id,key);return JSONResponse(r['body'],status_code=202,headers={'Location':'/api/v1/execution-proof-exports/'+r['body']['id']})

@router.get('/execution-proof-exports/{job_id}',response_model=ExportJob,operation_id='getNativeProofExport',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'evidence.export'})
def status(job_id:UUID,request:Request):
 with session_transaction(request,permission='evidence.export',touch=False):check_query(request,())
 return call(request,'STATUS',job_id)['body']

@router.post('/execution-proof-exports/{job_id}/download-link',response_model=DownloadLink,operation_id='issueNativeProofExportLink',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'evidence.export'})
def link(job_id:UUID,request:Request):
 with session_transaction(request,permission='evidence.export',touch=False):check_query(request,())
 call(request,'LINK',job_id);exp=int(_now())+900
 return {'url':'/api/v1/execution-proof-exports/'+str(job_id)+'/download?token='+sign(request,claims(request,job_id,exp)),'expires_at':exp}

@router.get('/execution-proof-exports/{job_id}/download',operation_id='downloadStoredNativeProof',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'evidence.export','x-lv-link-ttl-seconds':900})
def download(job_id:UUID,request:Request):
 with session_transaction(request,permission='evidence.export',touch=False):check_query(request,('token',))
 validate_link(request,job_id,request.query_params.get('token'))
 if _before_download is not None:_before_download()
 info=call(request,'STATUS',job_id)['body']
 if info['proof_pack_id'] is not None:
  from ledgervigil.proofpacks.anchors import GitAnchors
  from ledgervigil.proofpacks.integrity import IntegrityError
  path=os.environ.get('LV_ANCHOR_REPOSITORY')
  if not path:raise ProblemError(503,'DEPENDENCY_UNAVAILABLE','The independent anchor store is unavailable.')
  try:
   with session_transaction(request,permission='evidence.export',touch=False) as ctx:
    store=GitAnchors(path);record,_=store.find(ctx.tenant_id,info['proof_pack_id']);head=store.read(ctx.tenant_id)
    valid=head is not None and ctx.conn.execute('SELECT lv.pack_chain_check(%s,%s,%s)',(token_hash(ctx.session_token),head['chain_seq'],head['chain_hash'])).fetchone()[0]
   if not valid or record['bundle_hash']!='sha256:'+info['content_hash']:raise IntegrityError('External anchor or restored database mismatch')
  except (IntegrityError,OSError):raise ProblemError(503,'DEPENDENCY_UNAVAILABLE','The independent anchor could not verify this export.') from None
 r=call(request,'DOWNLOAD',job_id);data=bytes.fromhex(r['artifact_hex']);digest=r['body']['content_hash']
 if digest!=info['content_hash']:raise ProblemError(500,'INTERNAL','Export changed during verification.')
 if hashlib.sha256(data).hexdigest()!=digest:raise ProblemError(500,'INTERNAL')
 bundle=r['body']['format']=='BUNDLE'
 filename=('ledgervigil-proof-pack-'+r['body']['proof_pack_id']+'.zip') if bundle else ('ledgervigil-execution-'+r['body']['execution_id']+'.json')
 return Response(data,media_type='application/zip' if bundle else 'application/json',headers={'X-Proof-SHA256':digest,'X-Export-Authorization':r['authorization_id'],'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Content-Disposition':'attachment; filename="'+filename+'"','Referrer-Policy':'no-referrer'})
