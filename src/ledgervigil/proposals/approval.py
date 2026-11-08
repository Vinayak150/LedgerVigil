"""Bounded native approval: accepted family 11; no execution endpoint."""
from typing import Annotated
from uuid import UUID
import psycopg
from fastapi import APIRouter,Depends,Header,Request
from psycopg.types.json import Jsonb
from ledgervigil.auth.boundary import session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.simulation.canonical import canonical,digest
from .routes import Proposal,raw_body,_actions,_respond,IDEMPOTENCY_KEY,PROBLEM,NOT_FOUND
from . import validation as v
router=APIRouter(prefix='/api/v1/proposals',tags=['native-approval'])
_before_write=None
_after_write=None
BODY={'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False,'required':['revision'],'properties':{'revision':{'type':'integer','minimum':1},'comment':{'type':['string','null'],'maxLength':4000}}}}}}
ERRORS={**NOT_FOUND,400:{'description':'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER',**PROBLEM},401:{'description':'UNAUTHENTICATED',**PROBLEM},403:{'description':'PERMISSION_DENIED; SEGREGATION_OF_DUTIES (maker/editor/self-grant/24-hour cooling-off)',**PROBLEM},409:{'description':'ALREADY_DECIDED; INVALID_STATE_TRANSITION; REQUEST_IN_PROGRESS; PROPOSAL_STALE (changed dependencies atomically mark STALE)',**PROBLEM},412:{'description':'VERSION_MISMATCH',**PROBLEM},422:{'description':'SCHEMA_INVALID; CAPABILITY_UNAVAILABLE; IDEMPOTENCY_KEY_REUSED',**PROBLEM},428:{'description':'VERSION_REQUIRED',**PROBLEM}}
def parse(raw):
 try:
  b=v.parse_body(raw)
  if type(b) is not dict or 'revision' not in b or set(b)-{'revision','comment'} or type(b['revision']) is not int or not 1<=b['revision']<=2147483647:raise ValueError()
  comment=b.get('comment')
  if comment is not None:
   if type(comment) is not str or len(comment)>4000 or '\0' in comment:raise ValueError()
   comment.encode('utf-8')
  return b,v.request_hash(raw,b,True),None
 except (v.Invalid,ValueError,UnicodeError):return None,v.request_hash(raw),{'status':422,'code':'SCHEMA_INVALID','detail':'revision is required; optional comment is at most 4000 well-formed Unicode characters; no other fields are accepted'}
@router.post('/{proposal_id}/approve',response_model=Proposal,operation_id='approveNativeProposal',responses=ERRORS,openapi_extra={'x-lv-semantic-revalidation':'when needed, locked full native recalculation must preserve economic effects and policy outcomes; original review is immutable','x-lv-permission':'review','x-lv-idempotency':'required','requestBody':BODY,'x-lv-scope':'native SANDBOX only; maker/all editors excluded; all review grants cool for 24 hours; no execution capability'})
def approve(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
 try:
  with session_transaction(request,permission='review',touch=False) as ctx:
   if idempotency_key is None or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):raise ProblemError(400,'IDEMPOTENCY_KEY_REQUIRED','Idempotency-Key must contain 1–64 visible ASCII characters')
   body,body_hash,problem=parse(raw)
   if request.query_params:problem={'status':400,'code':'UNKNOWN_QUERY_PARAMETER','detail':'approval accepts no query parameters'}
   h=digest(canonical({'proposal_id':str(proposal_id),'if_match':if_match,'body_hash':body_hash,'query':[[k,val] for k,val in request.query_params.multi_items()]}))
   if _before_write:_before_write()
   if problem is None and not request.query_params:
    from ledgervigil.proposals.semantic import refresh
    refresh(ctx,request,proposal_id,'approve',if_match,idempotency_key,requested_revision=body['revision'])
   result=ctx.conn.execute('SELECT lv.approve_native(%s,%s,%s,%s,%s,%s,%s,%s,%s)',(token_hash(ctx.session_token),proposal_id,if_match,idempotency_key,h,Jsonb(body) if body else None,Jsonb(problem) if problem else None,source_key(request),request.state.request_id)).fetchone()[0]
   if _after_write:_after_write(ctx)
   if result['status']==200:
       result['_caller_actions']=_actions(ctx,result['body'])
       Proposal.model_validate({**result['body'],'allowed_actions':result['_caller_actions']})
  return _respond(ctx,result,200)
 except psycopg.errors.InsufficientPrivilege as e:
  if 'authenticated session' in (e.diag.message_primary or '') or e.diag.message_primary=='session unavailable':raise ProblemError(401,'UNAUTHENTICATED','session is no longer valid') from None
  raise

