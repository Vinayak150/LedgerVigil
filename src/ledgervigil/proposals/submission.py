"""Bounded native submission/withdrawal: one runtime connection, one atomic workflow transaction."""
import json
import logging
from typing import Annotated
from uuid import UUID
import psycopg
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict
from ledgervigil.auth.boundary import session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.simulation import bundle,engine
from ledgervigil.simulation.canonical import canonical,digest
from ledgervigil.simulation.routes import SimulationRun
from .routes import Proposal,raw_body,_actions,_respond,IDEMPOTENCY_KEY,PROBLEM
from . import validation as validation

router=APIRouter(prefix='/api/v1',tags=['proposal-workflow'])
log=logging.getLogger('ledgervigil.submission')
_after_begin=None
_before_finish=None
_after_finish=None

class SubmissionResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    proposal:Proposal
    simulation:SimulationRun

class FreshnessResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    proposal:Proposal
    freshness:dict
    check_id:str

class Refused(Exception):
    def __init__(self,problem):self.problem=problem


def _request_hash(action,pid,match,raw):
    problem=None
    if raw==b'':body={}
    else:
        try:body=validation.parse_body(raw)
        except validation.Invalid as e:body=None;problem=e.problem
    if problem is None and body!={}:
        problem={'status':422,'code':'SCHEMA_INVALID','detail':'this action accepts an empty body or an empty JSON object'}
    h=validation.request_hash(raw,body,problem is None)
    return digest(canonical({'method':'POST','action':action,'proposal_id':str(pid),'if_match':match,'body_hash':h})),problem


def _workflow(action,pid,request,raw,key,match):
    try:
        with session_transaction(request,permission='propose',touch=False) as ctx:
            if key is None or not IDEMPOTENCY_KEY.fullmatch(key):
                raise ProblemError(400,'IDEMPOTENCY_KEY_REQUIRED','Idempotency-Key must contain 1–64 visible ASCII characters')
            h,problem=_request_hash(action,pid,match,raw)
            h=digest(canonical({'request':h,'query':[[k,v] for k,v in request.query_params.multi_items()]}))
            args=(token_hash(ctx.session_token),source_key(request),request.state.request_id)
            result=ctx.conn.execute('SELECT lv.workflow_request(%s,%s,%s,%s,%s,%s)',(args[0],action,key,h,*args[1:])).fetchone()[0]
            if result.get('ready'):
                try:
                    with ctx.conn.transaction():
                        if problem:raise Refused(problem)
                        if request.query_params:raise Refused({'status':400,'code':'UNKNOWN_QUERY_PARAMETER','detail':'this action accepts no query parameters'})
                        if action in ('submit','resimulate'):
                            begin='workflow_begin' if action=='submit' else 'workflow_resimulate_begin'
                            begun=ctx.conn.execute('SELECT lv.'+begin+'(%s,%s,%s,%s,%s,%s,%s)',(args[0],pid,match,key,h,*args[1:])).fetchone()[0]
                            if _after_begin is not None:_after_begin()
                            captured=bundle.capture_submitted(ctx,pid,begun['revision'])
                            outcome=engine.evaluate(captured)
                            if outcome['status']=='REJECTED':raise Refused(outcome['rejection'])
                            if _before_finish is not None:_before_finish()
                            inp,out=canonical(captured),canonical(outcome)
                            result=ctx.conn.execute('SELECT lv.workflow_finish(%s,%s,%s,%s,%s,%s,%s,%s)',(args[0],pid,inp,digest(inp),out,digest(out),*args[1:])).fetchone()[0]
                        elif action=='revalidate':
                            result=ctx.conn.execute('SELECT lv.workflow_revalidate(%s,%s,%s,%s,%s,%s,%s)',(args[0],pid,match,key,h,*args[1:])).fetchone()[0]
                        else:
                            result=ctx.conn.execute('SELECT lv.workflow_withdraw(%s,%s,%s,%s,%s,%s,%s)',(args[0],pid,match,key,h,*args[1:])).fetchone()[0]
                        if _after_finish is not None:_after_finish(ctx)
                except (Refused,bundle.CaptureRefused) as e:
                    p=e.problem if isinstance(e,Refused) else {'status':e.status,'code':e.code,'detail':e.detail}
                    if p['status']>=500:raise ProblemError(p['status'],p['code'],p['detail']) from None
                    result=ctx.conn.execute('SELECT lv.workflow_refused(%s,%s,%s,%s,%s,%s,%s)',(args[0],action,key,h,Jsonb(p),*args[1:])).fetchone()[0]
                except psycopg.Error as e:
                    if e.sqlstate!='LVR02':raise
                    p=json.loads(e.diag.message_detail)
                    result=ctx.conn.execute('SELECT lv.workflow_refused(%s,%s,%s,%s,%s,%s,%s)',(args[0],action,key,h,Jsonb(p),*args[1:])).fetchone()[0]
            if result.get('status')==200:
                body=result['body']
                if action in ('submit','resimulate'):
                    body['proposal']['allowed_actions']=_actions(ctx,body['proposal']);SubmissionResult.model_validate(body)
                    response=JSONResponse(body,headers={'ETag':'"'+result['etag']+'"','Idempotent-Replayed':str(bool(result.get('replayed'))).lower()})
                elif action=='revalidate':
                    body['proposal']['allowed_actions']=_actions(ctx,body['proposal']);FreshnessResult.model_validate(body)
                    response=JSONResponse(body,headers={'ETag':'"'+result['etag']+'"','Idempotent-Replayed':str(bool(result.get('replayed'))).lower()})
                else:response=_respond(ctx,result,200)
        # The transaction, including deferred validators, committed before any success is returned.
        if result.get('status')==200:return response
        return _respond(ctx,result,200)
    except psycopg.errors.InsufficientPrivilege as e:
        if 'authenticated session' in (e.diag.message_primary or '') or e.diag.message_primary=='session unavailable':
            raise ProblemError(401,'UNAUTHENTICATED','session is no longer valid') from None
        raise

ERRORS={400:{'description':'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER',**PROBLEM},401:{'description':'UNAUTHENTICATED',**PROBLEM},403:{'description':'PERMISSION_DENIED (maker/propose/CSRF/Origin)',**PROBLEM},404:{'description':'NOT_FOUND (absent/other tenant)',**PROBLEM},409:{'description':'REQUEST_IN_PROGRESS; INVALID_STATE_TRANSITION',**PROBLEM},412:{'description':'VERSION_MISMATCH',**PROBLEM},422:{'description':'SCHEMA_INVALID; PRECONDITION_FAILED (STAGED_DOCUMENT_RESERVED, STATEMENT_IDENTITY_MISMATCH); CAPABILITY_UNAVAILABLE; UNSUPPORTED_BY_PROFILE; IDEMPOTENCY_KEY_REUSED',**PROBLEM},428:{'description':'VERSION_REQUIRED',**PROBLEM}}

@router.post('/proposals/{proposal_id}/submit',response_model=SubmissionResult,operation_id='submitProposal',responses=ERRORS,openapi_extra={'x-lv-permission':'propose','x-lv-resource-guard':'maker','x-lv-idempotency':'required','x-lv-scope':'native SANDBOX, one RecordSettlement, at most 200 items; no worker'})
def submit_proposal(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],
                    idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,
                    if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
    return _workflow('submit',proposal_id,request,raw,idempotency_key,if_match)

@router.post('/proposals/{proposal_id}/withdraw',response_model=Proposal,operation_id='withdrawProposal',responses=ERRORS,openapi_extra={'x-lv-permission':'propose','x-lv-resource-guard':'maker','x-lv-idempotency':'required','x-lv-states':['DRAFT','BLOCKED','READY_FOR_REVIEW','STALE','APPROVED'],'x-lv-contract':'Stage 2A 10.4 maker withdrawal; atomic reservation release; no posting'})
def withdraw_proposal(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],
                      idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,
                      if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
    return _workflow('withdraw',proposal_id,request,raw,idempotency_key,if_match)


@router.post('/proposals/{proposal_id}/resimulate',response_model=SubmissionResult,operation_id='resimulateProposal',responses=ERRORS,openapi_extra={'x-lv-permission':'propose','x-lv-resource-guard':'maker','x-lv-idempotency':'required','x-lv-scope':'unchanged-content new revision from STALE; native SANDBOX only'})
def resimulate_proposal(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],
                       idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,
                       if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
    return _workflow('resimulate',proposal_id,request,raw,idempotency_key,if_match)

@router.post('/proposals/{proposal_id}/revalidate',response_model=FreshnessResult,operation_id='revalidateProposal',responses=ERRORS,openapi_extra={'x-lv-permission':'propose','x-lv-resource-guard':'maker','x-lv-idempotency':'required','x-lv-contract-extension':'RC-1 PROPOSED additive native API; no approval authority'})
def revalidate_proposal(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],
                       idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,
                       if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
    return _workflow('revalidate',proposal_id,request,raw,idempotency_key,if_match)
