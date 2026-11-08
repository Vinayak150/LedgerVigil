"""Accepted Family 4 account edit; verified administrator, CAS, atomic history/audit."""
from typing import Annotated
from uuid import UUID
import json
import psycopg
from psycopg.types.json import Jsonb
from fastapi import APIRouter,Depends,Header,Request
from fastapi.responses import JSONResponse
from ledgervigil.auth.boundary import session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.problems import ProblemError
from ledgervigil.proposals.routes import raw_body
from ledgervigil.proposals import validation
from ledgervigil.reads.ledgers import Account,PROBLEM
router=APIRouter(prefix='/api/v1',tags=['configuration'])

@router.patch('/accounts/{account_id}',response_model=Account,operation_id='editAccount',responses={s:{'description':d,**PROBLEM} for s,d in {400:'UNKNOWN_QUERY_PARAMETER',401:'UNAUTHENTICATED',403:'PERMISSION_DENIED',404:'NOT_FOUND',409:'INVALID_STATE_TRANSITION; reason ACCOUNT_IN_USE',412:'VERSION_MISMATCH',422:'SCHEMA_INVALID; CAPABILITY_UNAVAILABLE',428:'VERSION_REQUIRED'}.items()},openapi_extra={'x-lv-permission':'administer','x-lv-version-guard':'If-Match required; PATCH is not key-replayed','x-lv-scope':'native SANDBOX account name or deactivation; no posting','requestBody':{'required':True,'content':{'application/json':{'schema':{'oneOf':[{'type':'object','required':['name'],'additionalProperties':False,'properties':{'name':{'type':'string','minLength':1,'maxLength':120,'description':'Must contain a non-whitespace character; no NUL or unpaired surrogate'}}},{'type':'object','required':['active'],'additionalProperties':False,'properties':{'active':{'type':'boolean','const':False}}}]}}}}})
def edit_account(account_id:UUID,request:Request,raw:Annotated[bytes|None,Depends(raw_body)],if_match:Annotated[str|None,Header(alias='If-Match',max_length=64)]=None):
 with session_transaction(request,permission='administer',touch=False) as ctx:
  body=None;problem=None
  if request.query_params:problem={'status':400,'code':'UNKNOWN_QUERY_PARAMETER'}
  else:
   try:body=validation.parse_body(raw)
   except validation.Invalid as e:problem=e.problem
  if problem is None:
   errors=[]
   if type(body) is not dict:errors=[{'pointer':'','code':'object'}]
   else:
    for key in sorted(set(body)-{'name','active'})[:100]:errors.append({'pointer':('' if any(0xD800<=ord(ch)<=0xDFFF for ch in key) else '/'+key.replace('~','~0').replace('/','~1')),'code':'additional_property'})
    if not body or {'name','active'}<=set(body):errors.append({'pointer':'','code':'oneOf'})
    if 'name' in body:
     name=body['name']
     if not(type(name) is str and bool(name.strip()) and len(name)<=120 and '\x00' not in name and not any(0xD800<=ord(ch)<=0xDFFF for ch in name)):errors.append({'pointer':'/name','code':'name'})
    if 'active' in body and body['active'] is not False:errors.append({'pointer':'/active','code':'const'})
   if errors:problem={'status':422,'code':'SCHEMA_INVALID','errors':errors}
  if problem is not None:body=None
  try:result=ctx.conn.execute('SELECT lv.edit_account(%s,%s,%s,%s,%s,%s,%s)',(token_hash(ctx.session_token),account_id,if_match,Jsonb(body) if body is not None else None,Jsonb(problem) if problem is not None else None,source_key(request),request.state.request_id)).fetchone()[0]
  except psycopg.errors.InsufficientPrivilege as e:
   if 'session' in (e.diag.message_primary or ''):raise ProblemError(401,'UNAUTHENTICATED') from None
   raise
 if result['status']!=200:raise ProblemError(result['status'],result['code'],errors=result.get('errors') or ([{'pointer':'','code':'SCHEMA_INVALID'}] if result['status']==422 and result['code']=='SCHEMA_INVALID' else None),extra={k:result[k] for k in ('reason','current_version') if k in result})
 return JSONResponse(result['body'],headers={'ETag':'"'+result['etag']+'"'})
