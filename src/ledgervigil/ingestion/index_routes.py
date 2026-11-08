"""SR-2 PROPOSED: tenant-authorized paging over a sealed public observation index."""
from functools import lru_cache
from typing import Annotated,Literal
from uuid import UUID
import json,sqlite3
from fastapi import APIRouter,Depends,Request,Path
from pydantic import BaseModel,ConfigDict
from ledgervigil.auth.boundary import require_read_only,RequestContext
from ledgervigil.auth.problems import ProblemError
from ledgervigil.reads.ledgers import LIMIT,CURSOR
from ledgervigil.reads.paging import check_query,parse_limit,CursorCodec,list_context
from ledgervigil.reads.representation import render
from .registry import body as registration,MANIFEST,ERRORS
from .plan_index import IndexReader,IndexUnavailable
from . import index_publication

router=APIRouter(prefix='/api/v1/source-plan-registrations',tags=['public-source-inspection'],responses={**ERRORS,503:{'description':'SOURCE_INDEX_UNAVAILABLE'}})
GROUP_ID=Annotated[str,Path(pattern=r'^[0-9a-f]{64}$')]
class Strict(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class Amount(Strict):
 amount:str
 currency:Literal['GBP']
class Group(Strict):
 group_id:str
 source_identity:list[str|int]
 status:Literal['HOLD','CANDIDATE_REVIEW']
 posting_eligibility:Literal['UNAVAILABLE']
 accounting_status:Literal['UNREVIEWED']
 hold_reasons:list[str]
 source_line_count:int
 amount:Amount|None
 payload_sha256:str
class GroupPage(Strict):
 registration_id:str
 items:list[Group]
 next_cursor:str|None
class Raw(Strict):
 values:list[str|None]
 cell_issues:list[str]
class SourceRow(Strict):
 sheet:str
 row:int
 raw:Raw
 sha256:str
class RowPage(Strict):
 registration_id:str
 group_id:str
 items:list[SourceRow]
 next_cursor:str|None
 posting_eligibility:Literal['UNAVAILABLE']='UNAVAILABLE'

@lru_cache(maxsize=1)
def published_index():
 if not index_publication.INDEX_MANIFEST_SHA256:raise IndexUnavailable('index not published')
 return IndexReader(index_publication.INDEX_DIRECTORY,index_publication.INDEX_MANIFEST_SHA256)

def access(ctx,request,rid):
 registered=registration(ctx,rid)   # Auth/RLS resolution always precedes artifact access.
 if registered['manifest_sha256']!=MANIFEST:raise ProblemError(503,'SOURCE_INDEX_UNAVAILABLE','index is unavailable for this snapshot')
 try:return getattr(request.app.state,'source_plan_index',None) or published_index()
 except (IndexUnavailable,OSError,ValueError,sqlite3.Error):raise ProblemError(503,'SOURCE_INDEX_UNAVAILABLE','verified source index is unavailable') from None

def guarded(operation):
 try:return operation()
 except (IndexUnavailable,OSError,sqlite3.Error):raise ProblemError(503,'SOURCE_INDEX_UNAVAILABLE','verified source index is unavailable') from None

def paging(ctx,request,rid,operation,filters):
 size=parse_limit(request.query_params.get('limit'));codec=CursorCodec(request.app.state.auth_settings.cursor_key)
 context=list_context(operation,ctx.tenant_id,str(rid),'ordinal.asc,page_id.asc',{'manifest':MANIFEST,**filters})
 pos=codec.decode(context,request.query_params['cursor'],2) if 'cursor' in request.query_params else ['0','00000000-0000-0000-0000-000000000000']
 try:
  number=int(pos[0])
  if str(number)!=pos[0] or not 0<=number<=1067371:raise ValueError()
 except ValueError:raise ProblemError(400,'CURSOR_MISMATCH','invalid position') from None
 return size,codec,context,(number,pos[1])

def finish(rows,size,codec,context,budget=1048576):
 chosen=[];weight=0
 for r in rows[:size]:
  cost=len(r[2].encode())
  if chosen and weight+cost>budget:break
  chosen.append(r);weight+=cost
 more=len(chosen)<len(rows)
 cursor=codec.encode(context,[str(chosen[-1][0]),chosen[-1][1]]) if chosen and more else None
 return [json.loads(r[2]) for r in chosen],cursor

@router.get('/{registration_id}/groups',response_model=GroupPage,operation_id='listSourcePlanGroups',openapi_extra={'x-lv-permission':'read','x-lv-contract-extension':'SR-2 PROPOSED derived public observation index'})
def groups(registration_id:UUID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))],limit:LIMIT=None,cursor:CURSOR=None,status:Literal['HOLD','CANDIDATE_REVIEW']|None=None):
 check_query(request,('limit','cursor','status'));reader=access(ctx,request,registration_id)
 size,codec,context,pos=paging(ctx,request,registration_id,'source.groups',{'status':status})
 rows=guarded(lambda:reader.groups(status,pos,size+1));items,next_cursor=finish(rows,size,codec,context)
 return render(request,GroupPage,{'registration_id':str(registration_id),'items':items,'next_cursor':next_cursor})

@router.get('/{registration_id}/groups/{group_id}',response_model=Group,operation_id='getSourcePlanGroup',openapi_extra={'x-lv-permission':'read','x-lv-contract-extension':'SR-2 PROPOSED'})
def group(registration_id:UUID,group_id:GROUP_ID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))]):
 check_query(request,());reader=access(ctx,request,registration_id);value=guarded(lambda:reader.group(group_id))
 if value is None:raise ProblemError(404,'NOT_FOUND','source group not found')
 return render(request,Group,value)

@router.get('/{registration_id}/groups/{group_id}/rows',response_model=RowPage,operation_id='listSourcePlanRows',openapi_extra={'x-lv-permission':'read','x-lv-contract-extension':'SR-2 PROPOSED'})
def rows(registration_id:UUID,group_id:GROUP_ID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))],limit:LIMIT=None,cursor:CURSOR=None):
 check_query(request,('limit','cursor'));reader=access(ctx,request,registration_id)
 if guarded(lambda:reader.group(group_id)) is None:raise ProblemError(404,'NOT_FOUND','source group not found')
 size,codec,context,pos=paging(ctx,request,registration_id,'source.rows',{'group':group_id})
 found=guarded(lambda:reader.rows(group_id,pos,size+1));items,next_cursor=finish(found,size,codec,context)
 return render(request,RowPage,{'registration_id':str(registration_id),'group_id':group_id,'items':items,'next_cursor':next_cursor,'posting_eligibility':'UNAVAILABLE'})
