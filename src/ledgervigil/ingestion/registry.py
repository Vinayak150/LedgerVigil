"""SR-1 proposed additive registry API; inspection metadata only, no upload/posting."""
import hashlib,json
import psycopg
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter,Request,Depends,Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from ledgervigil.auth.boundary import session_transaction,require_read_only,RequestContext,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.reads.paging import check_query
from ledgervigil.reads.representation import render
ERRORS={status:{'description':description,'content':{'application/problem+json':{}}} for status,description in {400:'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER',401:'UNAUTHENTICATED',403:'PERMISSION_DENIED',404:'NOT_FOUND',409:'REQUEST_IN_PROGRESS',422:'SCHEMA_INVALID; IDEMPOTENCY_KEY_REUSED; CAPABILITY_UNAVAILABLE; UNVERIFIED_SOURCE_PLAN'}.items()}
router=APIRouter(prefix='/api/v1/source-plan-registrations',tags=['source-plan-registry'],responses=ERRORS)
MANIFEST='3c7dbcb169405d9d0f8abcff095c9f30df46ca6b1b38fb31f894ed1657ea0523'
class Register(BaseModel):
 model_config=ConfigDict(extra='forbid')
 ledger_id:UUID
 manifest_sha256:str=Field(pattern='^[0-9a-f]{64}$')
class Registration(BaseModel):
 model_config=ConfigDict(extra='forbid')
 id:str
 ledger_id:str
 manifest_sha256:str
 created_by:str
 created_at:str
 classification:str='PUBLIC_OBSERVATIONAL'
 source_rows:int=1067371
 invoice_groups:int=53628
 candidate_review:int=12696
 held:int=40932
 accounting_status:str='UNREVIEWED'
 posting_eligibility:str='UNAVAILABLE'
 scope:str='Public observational snapshot; financial source admission unavailable'

def body(ctx,pid):
 r=ctx.conn.execute('SELECT id::text,ledger_id::text,manifest_sha256,created_by::text,created_at::text FROM lv.source_plan_registration WHERE id=%s',(pid,)).fetchone()
 if not r:raise ProblemError(404,'NOT_FOUND','source plan registration not found')
 from datetime import datetime
 data=dict(zip(('id','ledger_id','manifest_sha256','created_by','created_at'),r));data['created_at']=datetime.fromisoformat(data['created_at']).isoformat().replace('+00:00','Z')
 return Registration(**data).model_dump()


@router.post('',response_model=Registration,openapi_extra={'x-lv-permission':'source.import','x-lv-contract-extension':'SR-1 PROPOSED; metadata registry only'})
def register(payload:Register,request:Request,idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None):
 with session_transaction(request,permission='source.import',touch=False) as ctx:
  check_query(request,())
  raw=json.dumps(payload.model_dump(mode='json'),sort_keys=True,separators=(',',':'));digest=hashlib.sha256(raw.encode()).hexdigest()
  try:
   result=ctx.conn.execute('SELECT lv.register_source_plan(%s,%s,%s,%s,%s,%s,%s)',(token_hash(ctx.session_token),payload.ledger_id,payload.manifest_sha256,idempotency_key,digest,source_key(request),request.state.request_id)).fetchone()[0]
  except psycopg.errors.InsufficientPrivilege as exc:
   if 'authenticated session' in (exc.diag.message_primary or '') or exc.diag.message_primary=='session unavailable':raise ProblemError(401,'UNAUTHENTICATED','session is no longer valid') from None
   raise
  if result['status']!=200:raise ProblemError(result['status'],result['code'],'source plan registration refused')
  value=body(ctx,result['id'])
 return JSONResponse(value,headers={'Idempotent-Replayed':str(bool(result.get('replayed'))).lower()})

@router.get('/{registration_id}',response_model=Registration,openapi_extra={'x-lv-permission':'read'})
def inspect(registration_id:UUID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))]):
 check_query(request,());return render(request,Registration,body(ctx,registration_id))
