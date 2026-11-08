"""Read-only, bounded, deterministic native integrity export; never dispatches an event."""
from datetime import date,datetime,timezone
from decimal import Decimal
from uuid import UUID
from fastapi import Depends,Request
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict
from typing import Annotated,Any,Literal
from ledgervigil.auth.boundary import session_transaction,source_key
from ledgervigil.auth.crypto import token_hash
import psycopg
from ledgervigil.auth.problems import ProblemError
from ledgervigil.proposals.routes import READ_ERRORS,NOT_FOUND,PROBLEM
from ledgervigil.reads.paging import check_query
from .routes import router
from .proof_verify import FIELDS,FORMAT,MAX_BYTES,MAX_HISTORY,ProofInvalid,canon,seal,sha,verify

class ProofScope(BaseModel):
 model_config=ConfigDict(extra='forbid')
 tenant_id:str;ledger_id:str;proposal_id:str;revision:int;execution_id:str;simulation_id:str;commit_seq:str;currency:str;exponent:int
class ProofManifestEntry(BaseModel):
 model_config=ConfigDict(extra='forbid')
 name:str;bytes:int;sha256:str
class ExecutionProof(BaseModel):
 model_config=ConfigDict(extra='forbid')
 format:Literal['ledgervigil.native-execution-proof/1'];scope:ProofScope;claims:dict[str,str];manifest:list[ProofManifestEntry];manifest_sha256:str;artifacts:dict[str,Any]

def value(v):
 if isinstance(v,UUID):return str(v)
 if isinstance(v,datetime):return v.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
 if isinstance(v,date):return v.isoformat()
 if isinstance(v,Decimal):return format(v,'f')
 if type(v) is dict:return {k:value(x) for k,x in v.items()}
 if type(v) is list:return [value(x) for x in v]
 return v

def records(conn,table,kind,where,args,limit=2001):
 # All identifiers/conditions here are source constants; only values use parameters.
 fields=FIELDS[kind];cursor=conn.execute('SELECT '+','.join(fields)+' FROM lv.'+table+' WHERE '+where+' LIMIT '+str(limit),args);result=[]
 for row in cursor:
  d={k:value(v) for k,v in zip(fields,row)}
  for k in ('seq','entry_no','snapshot_seq'):
   if k in d:d[k]=str(d[k])
  result.append(d)
 return result

def one(conn,table,kind,where,args):
 rows=records(conn,table,kind,where,args,2)
 if len(rows)!=1:raise ProofInvalid('MISSING_OR_AMBIGUOUS_'+kind.upper())
 return rows[0]

def export_proof(conn,eid):
 row=conn.execute("SELECT e.tenant_id,e.ledger_id,e.proposal_id,e.revision,e.simulation_id,e.commit_seq,e.approval_id,e.check_id,lv.native_execution_json(e),l.currency,l.currency_exponent FROM lv.native_execution e JOIN lv.ledger l ON(l.tenant_id,l.id)=(e.tenant_id,e.ledger_id) WHERE e.id=%s AND e.state='RECONCILED'",(eid,)).fetchone()
 if row is None:raise ProblemError(404,'NOT_FOUND')
 t,l,p,rev,sim,seq,approval,check,receipt,currency,exp=row
 if seq>MAX_HISTORY:raise ProblemError(422,'CAPABILITY_UNAVAILABLE',extra={'reason':'PROOF_EXPORT_LIMIT'})
 args=(t,l)
 # Reject oversized source inputs before materializing the response.
 sizes=conn.execute("SELECT (SELECT coalesce(sum(octet_length(b.canonical_content)+octet_length(e.canonical_content)),0) FROM lv.commit_batch b JOIN lv.ledger_event e USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=%s AND b.ledger_id=%s AND b.seq<=%s),(SELECT octet_length(input_canonical)+octet_length(outcome_canonical) FROM lv.simulation_run WHERE tenant_id=%s AND id=%s),(SELECT coalesce(sum(octet_length(financial_fields::text)+octet_length(original_request::text)+octet_length(informational_fields::text)),0) FROM lv.document_version WHERE tenant_id=%s AND ledger_id=%s AND (seq=%s OR (document_id,version) IN (SELECT document_id,previous_version FROM lv.document_version WHERE tenant_id=%s AND ledger_id=%s AND seq=%s)))",(t,l,seq,t,sim,t,l,seq,t,l,seq)).fetchone()
 if any(v is None or v>MAX_BYTES//4 for v in sizes):raise ProblemError(422,'CAPABILITY_UNAVAILABLE',extra={'reason':'PROOF_EXPORT_LIMIT'})
 scope={'tenant_id':str(t),'ledger_id':str(l),'proposal_id':str(p),'revision':rev,'execution_id':str(eid),'simulation_id':str(sim),'commit_seq':str(seq),'currency':currency,'exponent':exp}
 cond='tenant_id=%s AND ledger_id=%s AND proposal_id=%s AND revision=%s';pa=(t,l,p,rev)
 revision=one(conn,'proposal_revision','revision',cond,pa)
 identity=one(conn,'proposal','identity','tenant_id=%s AND ledger_id=%s AND id=%s',(t,l,p))
 editors=sorted({str(r[0]) for r in conn.execute('SELECT authored_by FROM lv.proposal_revision WHERE tenant_id=%s AND ledger_id=%s AND proposal_id=%s AND revision<=%s',pa)})
 decision=one(conn,'proposal_decision','decision','tenant_id=%s AND id=%s',(t,approval))
 ac=one(conn,'approval_check','approval_check','tenant_id=%s AND id=%s',(t,decision['check_id']))
 overrides=records(conn,'proposal_override','override',cond+' AND simulation_id=%s ORDER BY policy_id',(*pa,sim),2)
 simulation=one(conn,'simulation_run','simulation','tenant_id=%s AND id=%s',(t,sim))
 ec=one(conn,'native_execution_check','check','tenant_id=%s AND id=%s',(t,check))
 transition=one(conn,'proposal_transition','transition',cond+" AND version=%s AND action='execute' AND phase=0",(*pa,receipt['resulting_version']))
 documents=records(conn,'document_version','document','tenant_id=%s AND ledger_id=%s AND (seq=%s OR (document_id,version) IN (SELECT document_id,previous_version FROM lv.document_version WHERE tenant_id=%s AND ledger_id=%s AND seq=%s)) ORDER BY document_id,version',(t,l,seq,t,l,seq))
 entry=one(conn,'journal_entry','entry','tenant_id=%s AND ledger_id=%s AND id=%s',(t,l,receipt['entry_id']))
 lines=records(conn,'journal_line','line','tenant_id=%s AND ledger_id=%s AND entry_id=%s ORDER BY line_no',(t,l,receipt['entry_id']),4)
 inclusions=records(conn,'settlement_inclusion','inclusion','tenant_id=%s AND ledger_id=%s AND settlement_document_id=%s AND settlement_version=1 ORDER BY line_no',(t,l,receipt['settlement_document_id']),1001)
 history=[{'seq':str(n),'batch_canonical':bc,'batch_previous_hash':bp,'batch_hash':bh,'event_canonical':ec,'event_previous_hash':ep,'event_hash':eh} for n,bc,bp,bh,ec,ep,eh in conn.execute('SELECT b.seq,b.canonical_content,b.previous_hash,b.hash,e.canonical_content,e.previous_hash,e.hash FROM lv.commit_batch b JOIN lv.ledger_event e USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=%s AND b.ledger_id=%s AND b.seq<=%s ORDER BY b.seq LIMIT %s',(t,l,seq,MAX_HISTORY))]
 artifacts={'revision.json':revision,'approval.json':{'decision':decision,'check':ac,'overrides':overrides},'simulation.json':simulation,'execution.json':{'receipt':receipt,'approval_id':str(approval),'check':ec,'transition':transition,'identity':identity,'editors':editors},'documents.json':documents,'journal.json':{'entry':entry,'lines':lines},'inclusions.json':inclusions,'history.json':history}
 bundle=seal(scope,artifacts);ExecutionProof.model_validate(bundle);data=(canon(bundle)+'\n').encode()
 if len(data)>MAX_BYTES:raise ProblemError(422,'CAPABILITY_UNAVAILABLE',extra={'reason':'PROOF_EXPORT_LIMIT'})
 verify(data) # Never serve an internally inconsistent proof as a successful download.
 return data

_before_release=None  # deterministic test seam; never set in production

@router.get('/executions/{execution_id}/proof',status_code=410,deprecated=True,operation_id='downloadNativeExecutionProof',responses={**READ_ERRORS,410:{'description':'Synchronous proof export retired; request an authorized durable export job',**PROBLEM}},openapi_extra={'x-lv-permission':'evidence.export','x-lv-scope':'RETIRED; use authenticated durable export jobs'})
def download_proof(execution_id:UUID,request:Request):
 with session_transaction(request,permission='evidence.export',touch=False):
  check_query(request,())
 raise ProblemError(410,'CAPABILITY_UNAVAILABLE','Synchronous proof exports have been retired. Request an export job at /api/v1/executions/{execution_id}/exports.')
