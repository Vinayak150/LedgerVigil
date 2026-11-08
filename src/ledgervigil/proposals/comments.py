"""Stage 2B Family 11 discussion only. CM-1/2 choices are documented, not approved decisions."""
from typing import Annotated
from uuid import UUID
import psycopg
from fastapi import APIRouter,Depends,Header,Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel,ConfigDict,Field
from ledgervigil.auth.boundary import RequestContext,require_read_only,session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.reads.ledgers import CURSOR,LIMIT,codec
from ledgervigil.reads.paging import check_query,list_context,page,parse_limit
from ledgervigil.reads.representation import render
from ledgervigil.simulation.canonical import canonical,digest
from .routes import raw_body,_read_proposal,IDEMPOTENCY_KEY,READ_ERRORS,NOT_FOUND,PROBLEM
from . import validation as v

router=APIRouter(prefix='/api/v1/proposals',tags=['proposal-discussion'])
_before_write=None
_after_write=None

class Comment(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str
    proposal_id:str
    ledger_id:str
    position:int=Field(ge=1)
    revision:int=Field(ge=1)
    proposal_version:int=Field(ge=1)
    authored_by:str
    body:str=Field(min_length=1,max_length=4000)
    created_at:str

class CommentPage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    proposal_id:str
    current_revision:int
    current_version:int
    items:list[Comment]
    next_cursor:str|None

COMMENT_BODY={'required':True,'content':{'application/json':{'schema':{
    'type':'object','additionalProperties':False,'required':['body'],'properties':{
        'body':{'type':'string','minLength':1,'maxLength':4000,'description':'Plain text, non-whitespace; preserved without trimming. CM-1 bounded representation choice.'}}}}}}

def parse(raw):
    try:
        parsed=v.parse_body(raw)
        if type(parsed) is not dict or set(parsed)!={'body'} or type(parsed['body']) is not str:raise ValueError()
        text=parsed['body']
        if not 1<=len(text)<=4000 or not text.strip() or '\x00' in text:raise ValueError()
        text.encode('utf-8')  # Refuse unpaired escaped surrogates rather than causing a PostgreSQL/JSON encoding failure.
        return text,v.request_hash(raw,parsed,True),None
    except (ValueError,UnicodeError,v.Invalid):
        return None,v.request_hash(raw),{'status':422,'code':'SCHEMA_INVALID','detail':'body must contain 1–4000 characters of non-whitespace UTF-8 text; no other fields are accepted'}

WRITE_ERRORS={**NOT_FOUND,400:{'description':'IDEMPOTENCY_KEY_REQUIRED; UNKNOWN_QUERY_PARAMETER',**PROBLEM},
    401:{'description':'UNAUTHENTICATED',**PROBLEM},403:{'description':'PERMISSION_DENIED (propose or review; CSRF/Origin)',**PROBLEM},
    409:{'description':'REQUEST_IN_PROGRESS',**PROBLEM},412:{'description':'VERSION_MISMATCH (optional If-Match)',**PROBLEM},
    422:{'description':'SCHEMA_INVALID; IDEMPOTENCY_KEY_REUSED',**PROBLEM}}

@router.post('/{proposal_id}/comments',response_model=Comment,status_code=201,operation_id='addProposalComment',responses=WRITE_ERRORS,
    openapi_extra={'x-lv-permission':'propose or review','x-lv-idempotency':'required','x-lv-contract-choices':'CM-1/CM-2 PROPOSED; discussion only', 'requestBody':COMMENT_BODY})
def add_comment(proposal_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],
    idempotency_key:Annotated[str|None,Header(alias='Idempotency-Key',max_length=256)]=None,
    if_match:Annotated[str|None,Header(alias='If-Match',max_length=64,description='Optional CM-2 guard against commenting on an unseen current revision')]=None):
    try:
        with session_transaction(request,touch=False) as ctx:
            if not ctx.permissions.intersection({'propose','review'}):raise ProblemError(403,'PERMISSION_DENIED','requires propose or review')
            if idempotency_key is None or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):raise ProblemError(400,'IDEMPOTENCY_KEY_REQUIRED','Idempotency-Key must contain 1–64 visible ASCII characters')
            text,body_hash,problem=parse(raw)
            if request.query_params:problem={'status':400,'code':'UNKNOWN_QUERY_PARAMETER','detail':'comments accept no query parameters'}
            request_hash=digest(canonical({'proposal_id':str(proposal_id),'body_hash':body_hash,'if_match':if_match,'query':[[k,value] for k,value in request.query_params.multi_items()]}))
            if _before_write:_before_write()
            result=ctx.conn.execute('SELECT lv.add_proposal_comment(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (token_hash(ctx.session_token),proposal_id,idempotency_key,request_hash,text,if_match,Jsonb(problem) if problem else None,source_key(request),request.state.request_id)).fetchone()[0]
            if _after_write:_after_write(ctx)
            if result['status']==201:Comment.model_validate(result['body'])
        # Deferred audit/cache constraints must commit before a successful response leaves the route.
        headers={'Idempotent-Replayed':str(bool(result.get('replayed'))).lower()}
        if result['status']==201:return JSONResponse(result['body'],status_code=201,headers=headers)
        if result.get('retry_after'):headers['Retry-After']=str(result['retry_after'])
        raise ProblemError(result['status'],result['code'],result.get('detail'),headers=headers,
            extra={k:result[k] for k in ('current_version',) if k in result})
    except psycopg.errors.InsufficientPrivilege as e:
        if 'authenticated session' in (e.diag.message_primary or '') or e.diag.message_primary=='session unavailable':
            raise ProblemError(401,'UNAUTHENTICATED','session is no longer valid') from None
        raise

@router.get('/{proposal_id}/comments',response_model=CommentPage,operation_id='listProposalComments',responses={**READ_ERRORS,**NOT_FOUND},
    openapi_extra={'x-lv-permission':'read','x-lv-order':'position ascending, id ascending; separate current reads'})
def list_comments(proposal_id:UUID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))],limit:LIMIT=None,cursor:CURSOR=None):
    check_query(request,('limit','cursor'));head=_read_proposal(ctx,proposal_id);size=parse_limit(limit)
    context=list_context('listProposalComments',ctx.tenant_id,head['ledger_id'],'position.asc,id.asc',{'proposal_id':str(proposal_id)})
    after=codec(request).decode(context,cursor,2) if cursor is not None else ['0','00000000-0000-0000-0000-000000000000']
    try:
        position=int(after[0])
        if str(position)!=after[0] or not 0<=position<=9223372036854775807:raise ValueError()
    except ValueError:raise ProblemError(400,'CURSOR_MISMATCH','invalid comment page position') from None
    found=ctx.conn.execute('SELECT lv.comment_json(c) FROM lv.proposal_comment c WHERE c.proposal_id=%s AND (c.position,c.id)>(%s,%s::uuid) ORDER BY c.position,c.id LIMIT %s',
        (proposal_id,position,after[1],size+1)).fetchall()
    items,next_cursor=page([r[0] for r in found],size,codec(request),context,lambda row:[str(row['position']),row['id']])
    return render(request,CommentPage,{'proposal_id':str(proposal_id),'current_revision':head['revision'],'current_version':head['version'],'items':items,'next_cursor':next_cursor})
