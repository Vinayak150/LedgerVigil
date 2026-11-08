"""Synchronous native SANDBOX execution. COMMIT precedes every successful response."""
from typing import Annotated,Literal
from uuid import UUID
import logging
import psycopg
from fastapi import APIRouter,Depends,Header,Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict
from psycopg.types.json import Jsonb
from ledgervigil.auth.boundary import RequestContext,require_read_only,session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError,problem_response
from ledgervigil.proposals.routes import raw_body,IDEMPOTENCY_KEY,PROBLEM,NOT_FOUND,READ_ERRORS
from ledgervigil.proposals import validation as v
from ledgervigil.simulation.canonical import canonical,digest
from ledgervigil.reads.paging import check_query
from ledgervigil.reads.representation import render
from .posting_match import PostingScope,PostingMismatch,verify_postings
router=APIRouter(prefix='/api/v1',tags=['native-execution'])
_before_write=None
_after_write=None
_on_defect=None
log=logging.getLogger('ledgervigil.execution')
class NativeExecution(BaseModel):
 model_config=ConfigDict(extra='forbid')
 id:str
 proposal_id:str
 ledger_id:str
 revision:int
 simulation_id:str
 state:Literal['RECONCILED']
 proposal_state:Literal['EXECUTED']
 executed_by:str
 observed_version:int
 resulting_version:int
 commit_seq:str
 settlement_document_id:str
 entry_id:str
 comparison:dict
 accounting_status:Literal['UNREVIEWED']
 data_label:str
BODY={'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False,'required':['revision'],'properties':{'revision':{'type':'integer','minimum':1}}}}}}
def parse(raw):
 try:
  b=v.parse_body(raw)
  if type(b) is not dict or set(b)!={'revision'} or type(b['revision']) is not int or not 1<=b['revision']<=2147483647:raise ValueError()
  return b,v.request_hash(raw,b,True),None
 except (v.Invalid,ValueError,UnicodeError):return None,v.request_hash(raw),{'status':422,'code':'SCHEMA_INVALID','detail':'only an exact positive integer revision is accepted'}
@router.post('/proposals/{proposal_id}/execute',response_model=NativeExecution,operation_id='executeNativeProposal',responses={**NOT_FOUND,400:{'description':'Idempotency key/query invalid',**PROBLEM},401:{'description':'Session required',**PROBLEM},403:{'description':'Execute permission, maker, self-grant and 24-hour cooling checks',**PROBLEM},409:{'description':'PROPOSAL_STALE; INVALID_STATE_TRANSITION; REQUEST_IN_PROGRESS',**PROBLEM},412:{'description':'VERSION_MISMATCH',**PROBLEM},422:{'description':'SCHEMA_INVALID; CAPABILITY_UNAVAILABLE; IDEMPOTENCY_KEY_REUSED',**PROBLEM},428:{'description':'VERSION_REQUIRED',**PROBLEM},500:{'description':'Non-retryable defect or unconfirmed commit; read back and retain original request identity',**PROBLEM}},openapi_extra={'requestBody':BODY,'x-lv-permission':'execute','x-lv-idempotency':'required','x-lv-scope':'synchronous single native SANDBOX RecordSettlement transaction; no provider or BOOKS writes'})
def execute(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
 commit_phase=False
 try:
  with session_transaction(request,permission='execute',touch=False) as ctx:
   if idempotency_key is None or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):raise ProblemError(400,'IDEMPOTENCY_KEY_REQUIRED','Idempotency-Key must contain 1–64 visible ASCII characters')
   body,bhash,problem=parse(raw)
   if request.query_params:problem={'status':400,'code':'UNKNOWN_QUERY_PARAMETER'}
   rhash=digest(canonical({'proposal_id':str(proposal_id),'if_match':if_match,'body_hash':bhash,'query':list(request.query_params.multi_items())}))
   if _before_write:_before_write()
   result=ctx.conn.execute('SELECT lv.execute_native(%s,%s,%s,%s,%s,%s,%s,%s,%s)',(token_hash(ctx.session_token),proposal_id,if_match,idempotency_key,rhash,Jsonb(body) if body else None,Jsonb(problem) if problem else None,source_key(request),request.state.request_id)).fetchone()[0]
   if result['status']==200:
    NativeExecution.model_validate(result['body'])
    # For a new effect independently rerun the pure gate before COMMIT. Historical
    # replay is its immutable receipt, not a revalidation of today's ledger.
    if not result.get('replayed'):
     proof=ctx.conn.execute('SELECT lv.execution_posting_proof(e) FROM lv.native_execution e WHERE id=%s',(result['body']['id'],)).fetchone()[0]
     match=verify_postings(PostingScope(**proof['scope']),proof['predicted'],PostingScope(**proof['scope']),proof['derived'])
     if match!=result['body']['comparison']:raise PostingMismatch('DATABASE_AND_APPLICATION_PROOFS_DIFFER')
   if _after_write:_after_write(ctx)
   commit_phase=True
  headers={'Idempotent-Replayed':'true'} if result.get('replayed') else {}
  if result['status']==200:return JSONResponse(result['body'],headers={**headers,'ETag':'"'+result['etag']+'"'})
  if result.get('retry_after'):headers['Retry-After']=str(result['retry_after'])
  raise ProblemError(result['status'],result['code'],headers=headers,extra={k:result[k] for k in ('state','reason','current_version') if k in result})
 except ProblemError:
  raise
 except Exception as error:
  if isinstance(error,psycopg.errors.InsufficientPrivilege) and 'session' in (error.diag.message_primary or ''):raise ProblemError(401,'UNAUTHENTICATED') from None
  if _on_defect:_on_defect(error)
  # A lost COMMIT acknowledgement can follow a durable commit. Never call that
  # a rollback or encourage a new key. The original atomic receipt resolves it.
  unknown=isinstance(error,(psycopg.OperationalError,psycopg.InterfaceError)) or (commit_phase and not isinstance(error,psycopg.IntegrityError))
  if unknown:
   log.error('native execution outcome unconfirmed; request_id=%s',request.state.request_id)
   return problem_response(request,500,'INTERNAL','Execution outcome is unconfirmed. Read the proposal and retry only the original request with its original Idempotency-Key and If-Match.',retryable=False,extra={'reason':'NATIVE_EXECUTION_OUTCOME_UNKNOWN'})
  # The acting transaction has already rolled back. The small incident transaction
  # cannot make or retry a financial write; never expose SQL/request contents.
  incident=None
  try:
   with session_transaction(request,permission='execute',touch=False) as ctx:
    row=ctx.conn.execute('SELECT lv.record_execution_incident(%s,%s,%s,%s)',(token_hash(ctx.session_token),proposal_id,source_key(request),request.state.request_id)).fetchone();incident=str(row[0]) if row and row[0] else None
  except Exception:log.error('native execution incident persistence failed; request_id=%s',request.state.request_id)
  return problem_response(request,500,'INTERNAL','Native execution verification failed; no financial changes committed.',retryable=False,extra={'reason':'NATIVE_EXECUTION_INVARIANT_FAILURE'})
@router.get('/executions/{execution_id}',response_model=NativeExecution,operation_id='readNativeExecution',responses={**READ_ERRORS,**NOT_FOUND},openapi_extra={'x-lv-permission':'read'})
def read_execution(execution_id:UUID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))]):
 check_query(request,());row=ctx.conn.execute("SELECT lv.native_execution_json(e) FROM lv.native_execution e WHERE id=%s AND state='RECONCILED'",(execution_id,)).fetchone()
 if not row:raise ProblemError(404,'NOT_FOUND')
 return render(request,NativeExecution,row[0])

@router.get('/proposals/{proposal_id}/execution',response_model=NativeExecution,operation_id='readNativeProposalExecution',responses={**READ_ERRORS,**NOT_FOUND},openapi_extra={'x-lv-permission':'read','x-lv-scope':'immutable current-revision receipt; no financial writes or cache dependency'})
def read_proposal_execution(proposal_id:UUID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))]):
 check_query(request,())
 row=ctx.conn.execute("SELECT lv.native_execution_json(e) FROM lv.native_execution e JOIN lv.proposal p ON(p.tenant_id,p.ledger_id,p.id,p.revision)=(e.tenant_id,e.ledger_id,e.proposal_id,e.revision) WHERE p.id=%s AND p.state='EXECUTED' AND e.state='RECONCILED'",(proposal_id,)).fetchone()
 if not row:raise ProblemError(404,'NOT_FOUND')
 return render(request,NativeExecution,row[0])
