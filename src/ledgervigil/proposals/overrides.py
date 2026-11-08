"""ADR-014/Stage 2B: bounded native controller evidence exception; never changes a verdict."""
from typing import Annotated,Literal
from uuid import UUID
import psycopg
from fastapi import APIRouter,Depends,Header,Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel,ConfigDict
from ledgervigil.auth.boundary import RequestContext,require_read_only,session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.reads.ledgers import LIMIT,CURSOR,codec
from ledgervigil.reads.paging import check_query,parse_limit,list_context,page
from ledgervigil.reads.representation import render
from ledgervigil.simulation.canonical import canonical,digest
from .routes import Proposal,raw_body,_respond,_actions,_read_proposal,IDEMPOTENCY_KEY,PROBLEM,READ_ERRORS,NOT_FOUND
from . import validation as v

router=APIRouter(prefix='/api/v1/proposals',tags=['controller-evidence-overrides'])
_before_write=None
_after_write=None

class EvidenceOverride(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['INSUFFICIENT_EVIDENCE_OVERRIDE']
    id:str
    proposal_id:str
    ledger_id:str
    revision:int
    simulation_id:str
    policy_id:Literal['P-EVD-01']
    policy_version:str
    original_outcome:Literal['INSUFFICIENT_EVIDENCE']
    reason:str
    overridden_by:str
    observed_version:int
    resulting_version:int
    freshness:dict
    created_at:str
    applies_to_current_revision:bool

class ApprovalRecord(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['APPROVAL']
    id:str
    proposal_id:str
    ledger_id:str
    revision:int
    simulation_id:str
    approved_by:str
    submitted_by:str
    comment:str|None
    observed_version:int
    resulting_version:int
    check_id:str
    created_at:str
    applies_to_current_revision:bool

class RejectionRecord(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['REJECTION']
    id:str
    proposal_id:str
    ledger_id:str
    revision:int
    simulation_id:str
    rejected_by:str
    submitted_by:str
    comment:str
    observed_version:int
    resulting_version:int
    check_id:str
    created_at:str
    applies_to_current_revision:bool

class ReviewPage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    proposal_id:str
    current_revision:int
    current_version:int
    supported_kinds:list[Literal['INSUFFICIENT_EVIDENCE_OVERRIDE','APPROVAL','REJECTION']]
    unsupported_kinds:list[str]
    items:list[EvidenceOverride|ApprovalRecord|RejectionRecord]
    next_cursor:str|None

BODY={'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False,'required':['policy_ids','reason'],
    'properties':{'policy_ids':{'type':'array','minItems':1,'maxItems':1,'uniqueItems':True,'items':{'enum':['P-EVD-01']}},'reason':{'type':'string','minLength':1,'maxLength':4000}}}}}}

def parse(raw):
    try:
        body=v.parse_body(raw)
        if type(body) is not dict or set(body)!={'policy_ids','reason'} or type(body['reason']) is not str:raise ValueError()
        reason=body['reason']
        if not 1<=len(reason)<=4000 or not reason.strip() or '\x00' in reason:raise ValueError()
        reason.encode('utf-8')
        if body['policy_ids']!=['P-EVD-01']:return body,v.request_hash(raw,body,True),{'status':422,'code':'CAPABILITY_UNAVAILABLE','detail':'only the sole P-EVD-01 insufficiency in native RecordSettlement is supported'}
        return body,v.request_hash(raw,body,True),None
    except (v.Invalid,ValueError,UnicodeError):return None,v.request_hash(raw),{'status':422,'code':'SCHEMA_INVALID','detail':'policy_ids and a non-whitespace UTF-8 reason of 1–4000 characters are required; no other fields are accepted'}

ERRORS={**NOT_FOUND,400:{'description':'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER',**PROBLEM},401:{'description':'UNAUTHENTICATED',**PROBLEM},
    403:{'description':'PERMISSION_DENIED; SEGREGATION_OF_DUTIES (maker, submitter, editors, self-grant, 24-hour review cooling-off)',**PROBLEM},409:{'description':'REQUEST_IN_PROGRESS; INVALID_STATE_TRANSITION; PROPOSAL_STALE (no state change)',**PROBLEM},
    412:{'description':'VERSION_MISMATCH',**PROBLEM},422:{'description':'SCHEMA_INVALID; CAPABILITY_UNAVAILABLE; IDEMPOTENCY_KEY_REUSED; PRECONDITION_FAILED (FAIL_CANNOT_BE_OVERRIDDEN)',**PROBLEM},428:{'description':'VERSION_REQUIRED',**PROBLEM}}

@router.post('/{proposal_id}/overrides',response_model=Proposal,operation_id='overrideProposalEvidence',responses=ERRORS,
    openapi_extra={'x-lv-semantic-revalidation':'when needed, locked full native recalculation must preserve economic effects and policy outcomes; original review is immutable','x-lv-permission':'review','x-lv-role':'CONTROLLER','x-lv-idempotency':'required','requestBody':BODY,
        'x-lv-scope':'native SANDBOX RecordSettlement; sole P-EVD-01 insufficiency; no approval/execution; OV-1/OV-2 bounded choices PROPOSED'})
def override(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],
    idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,
    if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
    try:
        with session_transaction(request,permission='review',touch=False) as ctx:
            if 'CONTROLLER' not in ctx.session['roles']:raise ProblemError(403,'PERMISSION_DENIED','controller role required')
            if idempotency_key is None or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):raise ProblemError(400,'IDEMPOTENCY_KEY_REQUIRED','Idempotency-Key must contain 1–64 visible ASCII characters')
            body,body_hash,problem=parse(raw)
            if request.query_params:problem={'status':400,'code':'UNKNOWN_QUERY_PARAMETER','detail':'overrides accept no query parameters'}
            h=digest(canonical({'proposal_id':str(proposal_id),'if_match':if_match,'body_hash':body_hash,'query':[[k,val] for k,val in request.query_params.multi_items()]}))
            if _before_write:_before_write()
            if problem is None and not request.query_params:
             from ledgervigil.proposals.semantic import refresh
             refresh(ctx,request,proposal_id,'override',if_match,idempotency_key)
            result=ctx.conn.execute('SELECT lv.override_evidence(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (token_hash(ctx.session_token),proposal_id,if_match,idempotency_key,h,Jsonb(body) if body else None,Jsonb(problem) if problem else None,source_key(request),request.state.request_id)).fetchone()[0]
            if _after_write:_after_write(ctx)
            if result['status']==200:
                result['_caller_actions']=_actions(ctx,result['body'])
                Proposal.model_validate({**result['body'],'allowed_actions':result['_caller_actions']})
        return _respond(ctx,result,200)
    except psycopg.errors.InsufficientPrivilege as e:
        if 'authenticated session' in (e.diag.message_primary or '') or e.diag.message_primary=='session unavailable':raise ProblemError(401,'UNAUTHENTICATED','session is no longer valid') from None
        raise

@router.get('/{proposal_id}/reviews',response_model=ReviewPage,operation_id='listProposalReviews',responses={**READ_ERRORS,**NOT_FOUND},
    openapi_extra={'x-lv-permission':'read','x-lv-scope':'immutable approval, rejection and override records','x-lv-order':'resulting_version ascending, id ascending'})
def reviews(proposal_id:UUID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))],limit:LIMIT=None,cursor:CURSOR=None):
    check_query(request,('limit','cursor'));head=_read_proposal(ctx,proposal_id);size=parse_limit(limit)
    context=list_context('listProposalReviews',ctx.tenant_id,head['ledger_id'],'resulting_version.asc,id.asc',{'proposal_id':str(proposal_id)})
    pos=codec(request).decode(context,cursor,2) if cursor is not None else ['0','00000000-0000-0000-0000-000000000000']
    try:
        version=int(pos[0])
        if str(version)!=pos[0] or not 0<=version<=2147483647:raise ValueError()
    except ValueError:raise ProblemError(400,'CURSOR_MISMATCH','invalid review page position') from None
    found=ctx.conn.execute('SELECT body FROM (SELECT o.proposal_id,o.resulting_version,o.id,lv.override_json(o) AS body FROM lv.proposal_override o UNION ALL SELECT d.proposal_id,d.resulting_version,d.id,lv.decision_json(d) AS body FROM lv.proposal_decision d) history WHERE proposal_id=%s AND (resulting_version,id)>(%s,%s::uuid) ORDER BY resulting_version,id LIMIT %s',(proposal_id,version,pos[1],size+1)).fetchall()
    rows=[{**r[0],'applies_to_current_revision':r[0]['revision']==head['revision'] and r[0]['simulation_id']==head['latest_simulation_id']} for r in found]
    items,next_cursor=page(rows,size,codec(request),context,lambda row:[str(row['resulting_version']),row['id']])
    return render(request,ReviewPage,{'proposal_id':str(proposal_id),'current_revision':head['revision'],'current_version':head['version'],
        'supported_kinds':['INSUFFICIENT_EVIDENCE_OVERRIDE','APPROVAL','REJECTION'],'unsupported_kinds':[],'items':items,'next_cursor':next_cursor})
