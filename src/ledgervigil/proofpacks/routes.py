import json,os
import psycopg
from uuid import UUID
from typing import Annotated
from fastapi import APIRouter,Request,Header,Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict
from ledgervigil.auth.boundary import session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.proposals.routes import raw_body
from ledgervigil.proposals.validation import parse_body,Invalid
from ledgervigil.exports.routes import EXPORT_ERRORS
from ledgervigil.reads.paging import check_query
from .integrity import verify,canonical,IntegrityError
from .anchors import GitAnchors
router=APIRouter(prefix='/api/v1',tags=['proof-packs'])

class PackMetadata(BaseModel):
 model_config=ConfigDict(extra='forbid')
 id:str;proposal_id:str;revision:int;ledger_id:str;state:str;content_hash:str|None;chain_hash:str|None;chain_seq:int|None;anchor_commit:str|None;chain_head_exported_at:str|None;last_error:str|None
class PackBuildJob(BaseModel):
 id:str;kind:str;state:str
class PackRequest(BaseModel):
 proof_pack:PackMetadata;job:PackBuildJob
class PackExportJob(BaseModel):
 id:str;kind:str;state:str;proof_pack_id:str


def call(request,action,pid,rev=None,key=None):
 try:
  with session_transaction(request,permission='evidence.export' if action=='EXPORT' else 'read',touch=False) as ctx:
   check_query(request,())
   if action=='CREATE':q='SELECT lv.pack_request(%s,%s,%s,%s,%s,%s)';args=(token_hash(ctx.session_token),pid,rev,key,source_key(request),request.state.request_id)
   elif action=='EXPORT':q='SELECT lv.pack_export_request(%s,%s,%s,%s,%s)';args=(token_hash(ctx.session_token),pid,key,source_key(request),request.state.request_id)
   else:q='SELECT lv.pack_status(%s,%s,%s,%s)';args=(token_hash(ctx.session_token),pid,source_key(request),request.state.request_id)
   r=ctx.conn.execute(q,args).fetchone()[0]
 except psycopg.errors.InsufficientPrivilege as e:
  missing='session' in (e.diag.message_primary or '')
  raise ProblemError(401 if missing else 403,'UNAUTHENTICATED' if missing else 'PERMISSION_DENIED') from None
 if r['status'] not in (200,202):raise ProblemError(r['status'],r['code'])
 return r

def pack(r):
 if r['content'] is None:raise ProblemError(409,'INVALID_STATE_TRANSITION','Proof pack is not sealed.')
 return {'manifest_schema':'lv-proofpack/1','content':json.loads(r['content']),'content_hash':r['body']['content_hash'],'chain':{'previous_chain_head':r['previous_chain_head'],'chain_hash':r['body']['chain_hash']},'sealed_at':r['sealed_at']}

def body(request,raw,permission):
 with session_transaction(request,permission=permission,touch=False):
  if request.query_params:raise ProblemError(400,'UNKNOWN_QUERY_PARAMETER')
  try:return parse_body(raw)
  except Invalid as e:raise ProblemError(e.problem['status'],e.problem['code'],errors=[{'pointer':'','code':e.problem['code']}]) from None

def key_required(key):
 if key is None:raise ProblemError(400,'IDEMPOTENCY_KEY_REQUIRED')

@router.post('/proposals/{proposal_id}/proof-packs',status_code=202,response_model=PackRequest,operation_id='requestProofPack',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'read','requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False,'required':['revision'],'properties':{'revision':{'type':'integer','minimum':1}}}}}}})
def create(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=64)]=None):
 b=body(request,raw,'read');key_required(key)
 if type(b) is not dict or set(b)!={'revision'} or type(b['revision']) is not int or b['revision']<1:raise ProblemError(422,'SCHEMA_INVALID',errors=[{'pointer':'/revision','code':'SCHEMA_INVALID'}])
 r=call(request,'CREATE',proposal_id,b['revision'],key);return JSONResponse({'proof_pack':r['body'],'job':{'id':r['body']['id'],'kind':'PROOF_PACK_BUILD','state':'DONE' if r['body']['anchor_commit'] else 'QUEUED'}},status_code=202)

@router.get('/proof-packs/{pack_id}',response_model=PackMetadata,operation_id='getProofPack',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'read'})
def get(pack_id:UUID,request:Request):return call(request,'STATUS',pack_id)['body']

@router.get('/proof-packs/{pack_id}/manifest',operation_id='getProofPackManifest',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'read','x-lv-scope':'manifest metadata; full canonical content is an audited export'})
def manifest(pack_id:UUID,request:Request):
 r=call(request,'STATUS',pack_id);p=pack(r);verify(p)
 return {k:v for k,v in p.items() if k!='content'}|{'proposal':p['content']['proposal'],'ledger':p['content']['ledger'],'accounting_review_status':'UNREVIEWED'}

@router.get('/proof-packs/{pack_id}/verify',operation_id='verifyProofPack',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'read'})
def check(pack_id:UUID,request:Request):
 r=call(request,'STATUS',pack_id);p=pack(r)
 try:h=verify(p)
 except IntegrityError:raise ProblemError(500,'INTERNAL','Proof pack integrity failed.') from None
 result={'stored_hash':r['body']['content_hash'],'computed_hash':h,'chain_ok':False,'chain_head_exported_at':r['body']['chain_head_exported_at'],'external_anchor':'NOT_VERIFIED'}
 path=os.environ.get('LV_ANCHOR_REPOSITORY')
 if not path:return result
 if not r['body']['anchor_commit']:return result|{'external_anchor':'NOT_EXPORTED'}
 try:
  store=GitAnchors(path);record,oid=store.find(p['content']['tenant_id'],str(pack_id));head=store.read(p['content']['tenant_id'])
  if record['content_hash']!=h or record['chain_hash']!=p['chain']['chain_hash'] or oid!=r['body']['anchor_commit']:return result|{'external_anchor':'ROLLBACK_OR_DIVERGENCE'}
  if head is None:return result
  with session_transaction(request,permission='read',touch=False) as ctx:
   facts=ctx.conn.execute('SELECT lv.pack_chain_check(%s,%s,%s)',(token_hash(ctx.session_token),head['chain_seq'],head['chain_hash'])).fetchone()[0]
  result.update(chain_ok=facts,external_anchor='LOCAL_GIT_VERIFIED' if facts else 'ROLLBACK_OR_DIVERGENCE')
 except (IntegrityError,OSError):result['external_anchor']='ANCHOR_UNAVAILABLE'
 return result

@router.post('/proof-packs/{pack_id}/exports',status_code=202,response_model=PackExportJob,operation_id='requestProofPackExport',responses=EXPORT_ERRORS,openapi_extra={'x-lv-permission':'evidence.export','requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False,'required':['format'],'properties':{'format':{'type':'string','const':'BUNDLE'}}}}}}})
def export(pack_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=64)]=None):
 b=body(request,raw,'evidence.export');key_required(key)
 if b!={'format':'BUNDLE'}:raise ProblemError(422,'SCHEMA_INVALID',errors=[{'pointer':'/format','code':'SCHEMA_INVALID'}])
 r=call(request,'EXPORT',pack_id,key=key);return JSONResponse(r['body'],status_code=202)
