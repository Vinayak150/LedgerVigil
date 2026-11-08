"""Offline native execution proof verifier. Python standard library only.

Internal consistency is not a digital signature, external anchor or accounting review.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from uuid import UUID
from datetime import datetime,timedelta

FORMAT='ledgervigil.native-execution-proof/1'
SEMANTIC_FORMAT='ledgervigil.native-execution-proof/2'
MAX_BYTES=16*1024*1024
MAX_HISTORY=2000
NAMES=('approval.json','documents.json','execution.json','history.json','inclusions.json','journal.json','revision.json','simulation.json')
CLAIMS={'integrity':'INTERNAL_CONSISTENCY_ONLY','accounting':'UNREVIEWED','signature':'NONE','external_anchor':'NONE','source_file_contents':'NOT_INCLUDED','current_authorization':'NOT_ATTESTED','historical_role_grants':'NOT_FULLY_RECONSTRUCTED'}
C=('tenant_id','ledger_id')
FIELDS={
'identity':C+('id','revision','version','state','created_by','submitted_by'),
'revision':C+('proposal_id','revision','previous_revision','change','authored_by','target_adapter','operations','evidence_refs','rationale','content_hash'),
'decision':C+('id','proposal_id','revision','kind','simulation_id','check_id','submitted_by','approved_by','observed_version','resulting_version','comment'),
'approval_check':C+('id','proposal_id','revision','simulation_id','observed_version','resulting_version','checked_by','report','review_granted_at','checked_at','decision_action'),
'override':C+('id','proposal_id','revision','simulation_id','policy_id','policy_version','reason','overridden_by','observed_version','resulting_version','freshness'),
'simulation':C+('id','proposal_id','revision','status','simulator_version','snapshot_seq','input_canonical','input_hash','outcome_canonical','result_hash','created_by'),
'check':C+('id','proposal_id','revision','simulation_id','observed_version','resulting_version','checked_by','report'),
'transition':C+('proposal_id','revision','version','phase','from_state','to_state','user_id','action'),
'document':C+('document_id','version','id','previous_version','seq','ordinal','financial_fields','financial_fingerprint','informational_fields','original_request','created_by','created_by_user','reason','origin','provenance'),
'entry':C+('id','entry_no','seq','kind','effective_date','period_id','currency','currency_exponent','source_document_id','source_document_version','posting_rule_id','posting_rule_version','provenance','created_by','created_by_user','description'),
'line':C+('id','entry_id','line_no','seq','account_id','requires_party','party_id','document_id','side','amount','currency','currency_exponent'),
'inclusion':C+('settlement_document_id','settlement_version','line_no','payment_document_id','payment_version_before','payment_version_after','kind','amount','seq'),
}
class ProofInvalid(ValueError):pass
def require(test,code):
 if not test:raise ProofInvalid(code)
def _check(v,depth=0):
 require(depth<=64,'TOO_DEEP')
 if v is None or type(v) in (str,bool):return
 if type(v) is int:require(abs(v)<2**63,'INTEGER_RANGE');return
 if type(v) is list:
  for x in v:_check(x,depth+1)
 elif type(v) is dict:
  require(all(type(k) is str for k in v),'NON_STRING_KEY')
  for x in v.values():_check(x,depth+1)
 else:raise ProofInvalid('UNSUPPORTED_JSON_TYPE')
def canon(v,ascii=True):
 _check(v)
 try:return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=ascii,allow_nan=False)
 except (ValueError,UnicodeError):raise ProofInvalid('INVALID_JSON') from None
def sha(v):return hashlib.sha256(v).hexdigest()
def _pairs(pairs):
 result={}
 for k,v in pairs:
  require(k not in result,'DUPLICATE_JSON_KEY');result[k]=v
 return result
def _bad(_):raise ProofInvalid('NON_EXACT_JSON_NUMBER')
def parse(text):
 try:
  v=json.loads(text,object_pairs_hook=_pairs,parse_float=_bad,parse_constant=_bad);_check(v);return v
 except (ValueError,RecursionError,UnicodeError) as e:
  if isinstance(e,ProofInvalid):raise
  raise ProofInvalid('INVALID_JSON') from None
def exact(v,keys):require(type(v) is dict and set(v)==set(keys),'UNEXPECTED_FIELDS')
def seal(scope,artifacts):
 manifest=[{'name':n,'bytes':len(canon(artifacts[n]).encode()),'sha256':sha(canon(artifacts[n]).encode())} for n in NAMES]
 core={'format':SEMANTIC_FORMAT if 'semantic_rechecks' in artifacts.get('simulation.json',{}) else FORMAT,'scope':scope,'claims':dict(CLAIMS),'manifest':manifest}
 return {**core,'manifest_sha256':sha(canon(core).encode()),'artifacts':artifacts}
def _integer(v):return type(v) is int and v>0
def _seq(v):return type(v) is str and re.fullmatch(r'[1-9][0-9]{0,18}',v) and int(v)<2**63
def _money(s,exp,stored=False):
 scale=4 if stored else exp
 require(type(s) is str and re.fullmatch(r'(?:0|[1-9][0-9]{0,15})'+(r'\.[0-9]{'+str(scale)+'}' if scale else ''),s),'NON_EXACT_MONEY')
 units=int(s.replace('.',''))
 if stored:
  factor=10**(4-exp);require(units%factor==0,'EXCESS_MONEY_SCALE');units//=factor
 return units
def _uuid(v):
 try:return type(v) is str and str(UUID(v))==v
 except (ValueError,AttributeError):return False

def _scoped(v,s,kind):
 exact(v,(*FIELDS[kind],'semantic_rechecks') if kind=='simulation' and 'semantic_rechecks' in v else FIELDS[kind])
 for k in ('revision','previous_revision','version','previous_version','observed_version','resulting_version','line_no','ordinal','source_document_version','settlement_version','payment_version_before','payment_version_after'):
  if k in v:require(v[k] is None and k in ('previous_version','previous_revision') or _integer(v[k]),'INVALID_VERSION_OR_ORDINAL')
 for k in ('phase','currency_exponent'):
  if k in v:require(type(v[k]) is int and 0<=v[k]<=4,'INVALID_INTEGER')
 for k in ('id','tenant_id','ledger_id','proposal_id','simulation_id','check_id','authored_by','submitted_by','approved_by','checked_by','overridden_by','document_id','created_by','created_by_user','period_id','source_document_id','entry_id','account_id','party_id','settlement_document_id','payment_document_id','user_id'):
  if k in v:require(v[k] is None and k in ('created_by','created_by_user','party_id') or _uuid(v[k]),'INVALID_RECORD_ID')
 require(v['tenant_id']==s['tenant_id'] and v['ledger_id']==s['ledger_id'],'TENANT_OR_LEDGER_MISMATCH')
 if 'proposal_id' in v:require(v['proposal_id']==s['proposal_id'] and v['revision']==s['revision'],'PROPOSAL_OR_REVISION_MISMATCH')
def _fresh(c,seq):
 f=c['report'];require(f.get('status')=='FRESH' and f.get('changes')==[] and f.get('pinned_seq')==seq and f.get('observed_seq')==seq,'FRESHNESS_MISMATCH')
def verify(data,expected_sha256=None):
 """Verify exact bytes, manifest, scope, links, chains and native amounts independently."""
 try:return _verify(data,expected_sha256)
 except (KeyError,TypeError,IndexError,OverflowError,RecursionError,UnicodeError,AttributeError):raise ProofInvalid('MALFORMED_PROOF') from None
def _verify(data,expected_sha256):
 require(type(data) is bytes and 0<len(data)<=MAX_BYTES,'PROOF_SIZE_LIMIT');file_hash=sha(data)
 if expected_sha256 is not None:require(type(expected_sha256) is str and re.fullmatch('[0-9a-f]{64}',expected_sha256) and expected_sha256==file_hash,'EXPECTED_DIGEST_MISMATCH')
 b=parse(data);exact(b,('format','scope','claims','manifest','manifest_sha256','artifacts'));require(b['format'] in (FORMAT,SEMANTIC_FORMAT) and b['claims']==CLAIMS,'UNSUPPORTED_FORMAT_OR_CLAIMS')
 s=b['scope'];exact(s,('tenant_id','ledger_id','proposal_id','revision','execution_id','simulation_id','commit_seq','currency','exponent'))
 require(all(type(s[k]) is str and s[k] for k in ('tenant_id','ledger_id','proposal_id','execution_id','simulation_id','currency')) and _integer(s['revision']) and _seq(s['commit_seq']) and type(s['exponent']) is int and 0<=s['exponent']<=4,'INVALID_SCOPE')
 require(all(_uuid(s[k]) for k in ('tenant_id','ledger_id','proposal_id','execution_id','simulation_id')),'INVALID_SCOPE_ID')
 a=b['artifacts'];exact(a,NAMES);expected=seal(s,a);require(b['manifest']==expected['manifest'] and b['manifest_sha256']==expected['manifest_sha256'],'MANIFEST_MISMATCH')
 require(data==(canon(b)+'\n').encode(),'NONCANONICAL_PROOF')
 r=a['revision.json'];_scoped(r,s,'revision');require(r['target_adapter']=='NATIVE' and len(r['operations'])==1,'UNSUPPORTED_OPERATION')
 revbody={k:r[k] for k in ('target_adapter','operations','evidence_refs','rationale')};require(sha(canon(revbody,False).encode())==r['content_hash'],'REVISION_HASH_MISMATCH')
 op=r['operations'][0];require(op['type']=='RecordSettlement' and op['schema_version']==1 and 1<=len(op['items'])<=1000,'UNSUPPORTED_OPERATION')
 sim=a['simulation.json'];_scoped(sim,s,'simulation');require(sim['id']==s['simulation_id'] and sim['status']=='COMPLETED' and sim['simulator_version']=='lv-native-recordsettlement/0.3.0','SIMULATION_MISMATCH')
 for key,h in [('input_canonical','input_hash'),('outcome_canonical','result_hash')]:require(type(sim[key]) is str and sha(sim[key].encode())==sim[h],'SIMULATION_HASH_MISMATCH')
 inp=parse(sim['input_canonical']);out=parse(sim['outcome_canonical']);require(canon(inp)==sim['input_canonical'] and canon(out)==sim['outcome_canonical'],'NONCANONICAL_SIMULATION')
 require(inp['tenant_id']==s['tenant_id'] and inp['ledger']['id']==s['ledger_id'] and inp['ledger']['purpose']=='SANDBOX' and inp['ledger']['source']=='NATIVE' and inp['ledger']['currency']==s['currency'] and inp['ledger']['currency_exponent']==s['exponent'],'CAPTURE_SCOPE_MISMATCH')
 facts=inp['facts'];require(facts['tenant_id']==s['tenant_id'] and facts['ledger_id']==s['ledger_id'] and facts['currency']==s['currency'] and facts['exponent']==s['exponent'] and facts['purpose']=='SANDBOX' and facts['source_kind']=='NATIVE' and inp['snapshot']['ledger_id']==s['ledger_id'],'CAPTURE_FACT_SCOPE_MISMATCH')
 require(inp['proposal']['id']==s['proposal_id'] and inp['proposal']['revision']==str(s['revision']) and inp['proposal']['content_hash']==r['content_hash'] and inp['proposal']['operations']==r['operations'] and inp['proposal']['evidence_refs']==r['evidence_refs'],'CAPTURE_REVISION_MISMATCH')
 require(out['input_hash']==sim['input_hash'] and out['status']=='COMPLETED' and out['accounting_status']=='UNREVIEWED' and out['simulator_version']==sim['simulator_version'] and out['aggregate'] in ('CLEAR','BLOCKED_INSUFFICIENT'),'OUTCOME_MISMATCH')
 seq=int(s['commit_seq']);original_seq=int(sim['snapshot_seq']);require(1<seq<=MAX_HISTORY and 0<original_seq<=seq-1 and inp['snapshot']['seq']==str(original_seq) and out['snapshot_seq']==str(original_seq),'CAPTURE_SEQUENCE_MISMATCH');rechecks=_semantic_certificates(sim,s,inp,out,seq);require(original_seq==seq-1 or any(q['seq']==str(seq-1) for q in rechecks.values()),'MISSING_SEMANTIC_RECHECK')
 approval=a['approval.json'];exact(approval,('decision','check','overrides'));d,ac=approval['decision'],approval['check'];_scoped(d,s,'decision');_scoped(ac,s,'approval_check')
 require(d['kind']=='APPROVED' and d['simulation_id']==s['simulation_id'] and d['check_id']==ac['id'] and d['approved_by']==ac['checked_by'] and d['approved_by']!=d['submitted_by'] and d['observed_version']+1==d['resulting_version'] and ac['observed_version']==d['observed_version'] and ac['resulting_version']==d['resulting_version'] and ac['simulation_id']==s['simulation_id'] and ac['decision_action']=='approve','APPROVAL_MISMATCH');_fresh_semantic(ac,original_seq,rechecks)
 try:
  granted=datetime.fromisoformat(ac['review_granted_at'].replace('Z','+00:00'));checked=datetime.fromisoformat(ac['checked_at'].replace('Z','+00:00'));require(granted.tzinfo is not None and checked.tzinfo is not None and checked-granted>=timedelta(hours=24),'REVIEW_GRANT_INTERVAL_MISMATCH')
 except ValueError as e:
  if isinstance(e,ProofInvalid):raise
  raise ProofInvalid('INVALID_REVIEW_TIME') from None
 require(type(approval['overrides']) is list and len(approval['overrides'])<=1,'OVERRIDE_MISMATCH')
 for ov in approval['overrides']:
  _scoped(ov,s,'override');require(ov['simulation_id']==s['simulation_id'] and ov['policy_id']=='P-EVD-01' and ov['reason'].strip() and ov['overridden_by']!=d['submitted_by'] and ov['resulting_version']==d['observed_version'] and ov['freshness'].get('status')=='FRESH','OVERRIDE_MISMATCH')
 if out['aggregate']=='BLOCKED_INSUFFICIENT':require(len(approval['overrides'])==1,'MISSING_EVIDENCE_EXCEPTION')
 ex=a['execution.json'];exact(ex,('receipt','approval_id','check','transition','identity','editors'));identity=ex['identity'];_scoped(identity,s,'identity');require(identity['id']==s['proposal_id'] and identity['revision']==s['revision'] and identity['state']=='EXECUTED' and identity['created_by']==d['submitted_by'] and _uuid(identity['submitted_by']),'PROPOSAL_IDENTITY_MISMATCH');require(type(ex['editors']) is list and ex['editors']==sorted(set(ex['editors'])) and all(_uuid(x) for x in ex['editors']) and identity['created_by'] in ex['editors'] and r['authored_by'] in ex['editors'] and d['approved_by'] not in ex['editors'],'REVIEWER_EDITOR_MISMATCH');receipt=ex['receipt'];check=ex['check'];tr=ex['transition'];_scoped(check,s,'check');_scoped(tr,s,'transition')
 require(receipt['id']==s['execution_id'] and receipt['proposal_id']==s['proposal_id'] and receipt['ledger_id']==s['ledger_id'] and receipt['revision']==s['revision'] and receipt['simulation_id']==s['simulation_id'] and receipt['state']=='RECONCILED' and receipt['proposal_state']=='EXECUTED' and receipt['commit_seq']==s['commit_seq'] and receipt['accounting_status']=='UNREVIEWED' and receipt['data_label']=='SANDBOX — not accounting records','RECEIPT_MISMATCH')
 exact(receipt,('id','proposal_id','ledger_id','revision','simulation_id','state','proposal_state','executed_by','observed_version','resulting_version','commit_seq','settlement_document_id','entry_id','comparison','accounting_status','data_label'));actor=receipt['executed_by'];require(_uuid(actor),'INVALID_ACTOR');require(actor not in (identity['created_by'],identity['submitted_by']) and identity['version']==receipt['resulting_version'] and ex['approval_id']==d['id'] and receipt['observed_version']==d['resulting_version'] and receipt['resulting_version']==receipt['observed_version']+1 and check['simulation_id']==s['simulation_id'] and check['checked_by']==actor and check['observed_version']==receipt['observed_version'] and check['resulting_version']==receipt['resulting_version'],'EXECUTION_LINK_MISMATCH');_fresh_semantic(check,seq-1,rechecks)
 if original_seq<seq-1:require(check['report'].get('semantic_recheck_id') in rechecks and rechecks[check['report']['semantic_recheck_id']]['equivalent'],'MISSING_EXECUTION_RECHECK')
 require(tr['action']=='execute' and tr['from_state']=='APPROVED' and tr['to_state']=='EXECUTED' and tr['user_id']==actor and tr['version']==receipt['resulting_version'] and tr['phase']==0,'TRANSITION_MISMATCH')
 hist=a['history.json'];require(type(hist) is list and len(hist)==seq,'HISTORY_GAP');bp=ep='0'*64;last=None;entry_ids=set();doc_ids=set();prior_docs={}
 for n,h in enumerate(hist,1):
  exact(h,('seq','batch_canonical','batch_previous_hash','batch_hash','event_canonical','event_previous_hash','event_hash'));require(h['seq']==str(n),'HISTORY_GAP')
  for prefix,prior in [('batch',bp),('event',ep)]:
   text=h[prefix+'_canonical'];payload=parse(text);require(canon(payload,False)==text and payload['tenant_id']==s['tenant_id'] and payload['ledger_id']==s['ledger_id'] and payload['seq']==str(n),'HISTORY_SCOPE_OR_CANONICAL_MISMATCH');require(h[prefix+'_previous_hash']==prior and sha((text+prior).encode())==h[prefix+'_hash'],'HISTORY_HASH_MISMATCH')
  batch=parse(h['batch_canonical']);event=parse(h['event_canonical']);require(batch['kind']==event['kind'] and event['kind'] in ('SEED','EXECUTION','PERIOD_CLOSE'),'HISTORY_KIND_MISMATCH')
  if event['kind']!='PERIOD_CLOSE':
   require(event['entry_id'] not in entry_ids and event['document_id'] not in doc_ids and event['entry_no']==str(len(entry_ids)+1),'HISTORY_ENTRY_MISMATCH');entry_ids.add(event['entry_id']);doc_ids.add(event['document_id']);prior_docs[(event['document_id'],event['document_version'])]=event
   lines=event.get('postings',event['financial_fields'])['lines'];balance=0;require(len(lines)>=2,'HISTORY_POSTING_MISMATCH')
   for index,line in enumerate(lines,1):
    require(line['line_no']==str(index) and line['side'] in ('D','C'),'HISTORY_POSTING_MISMATCH');units=_money(line['amount'],4);require(units>0,'ZERO_LINE');balance+=units if line['side']=='D' else -units
   require(balance==0,'HISTORY_UNBALANCED')
  bp=h['batch_hash'];ep=h['event_hash'];last=event
 require(inp['snapshot']['batch_head']==hist[original_seq-1]['batch_hash'] and inp['snapshot']['event_head']==hist[original_seq-1]['event_hash'],'CAPTURE_HEAD_MISMATCH')
 for q in rechecks.values():
  captured=parse(q['input_canonical']);head=hist[int(q['seq'])-1]
  require(captured['snapshot']['batch_head']==head['batch_hash'] and captured['snapshot']['event_head']==head['event_hash'] and str(captured['facts']['seq'])==q['seq'] and captured['facts']['batch_head']==head['batch_hash'] and captured['facts']['event_head']==head['event_hash'],'SEMANTIC_RECHECK_HEAD_MISMATCH')
 docs=a['documents.json'];require(type(docs) is list and len(docs)==1+2*len(op['items']),'DOCUMENT_SET_MISMATCH');versions={}
 for v in docs:
  _scoped(v,s,'document');key=(v['document_id'],v['version']);require(key not in versions,'DUPLICATE_DOCUMENT_VERSION');versions[key]=v;require(sha(canon(v['financial_fields'],False).encode())==v['financial_fingerprint'],'DOCUMENT_FINGERPRINT_MISMATCH')
 settle=versions[(receipt['settlement_document_id'],1)];f=settle['financial_fields'];require(settle['origin']=='EXECUTION' and settle['created_by'] is None and settle['created_by_user']==actor and settle['seq']==s['commit_seq'] and settle['ordinal']==1 and f['document_type']=='SETTLEMENT' and f['currency']==s['currency'],'SETTLEMENT_VERSION_MISMATCH')
 for key in ('processor_ref','payout_id','payout_date'):require(f[key]==op[key],'APPROVED_OPERATION_MISMATCH')
 expected_request={'processor_ref':op['processor_ref'],'payout_id':op['payout_id'],'payout_date':op['payout_date'],'currency':s['currency'],'gross':op['gross']['amount'],'fee':op['fee']['amount'],'net':op['net']['amount'],'description':'Approved native SANDBOX settlement','items':[{'document_id':x['document_id'],'kind':x['kind'],'amount':x['amount']['amount']} for x in op['items']]}
 require(settle['original_request']==expected_request,'APPROVED_REQUEST_MISMATCH')
 bindings={v['role']:v['account_id'] for v in inp['facts']['bindings']}
 amounts={k:_money(op[k]['amount'],s['exponent']) for k in ('gross','fee','net')};require(all(op[k]['currency']==s['currency'] and _money(f[k],s['exponent'])==amounts[k] for k in amounts) and amounts['gross']>0 and amounts['gross']==amounts['fee']+amounts['net'],'SETTLEMENT_AMOUNT_MISMATCH')
 inc=a['inclusions.json'];require(type(inc) is list and len(inc)==len(op['items'])==len(f['items'])==len(last['payment_versions']),'INCLUSION_SET_MISMATCH');total=0;seen=set()
 for index,(item,si,incrow,pv) in enumerate(zip(op['items'],f['items'],inc,last['payment_versions']),1):
  _scoped(incrow,s,'inclusion');pid=item['document_id'];require(pid not in seen,'DUPLICATE_PAYMENT');seen.add(pid);units=_money(item['amount']['amount'],s['exponent']);require(units>0,'NON_POSITIVE_PAYMENT');total+=units
  require(item['kind']==si['kind']==incrow['kind']=='PAYMENT' and item['amount']['currency']==s['currency'] and si['document_id']==incrow['payment_document_id']==pv['document_id']==pid and si['line_no']==str(index) and incrow['line_no']==index and _money(si['amount'],s['exponent'])==_money(incrow['amount'],s['exponent'],True)==units,'INCLUSION_MISMATCH')
  before=versions[(pid,incrow['payment_version_before'])];after=versions[(pid,incrow['payment_version_after'])];require(after['version']==before['version']+1 and after['previous_version']==before['version'] and after['seq']==s['commit_seq'] and int(before['seq'])<seq and after['ordinal']==index+1 and after['created_by_user']==actor and after['created_by'] is None and after['origin']=='EXECUTION','PAYMENT_VERSION_MISMATCH')
  require(after['financial_fields']=={**before['financial_fields'],'status':'SETTLED','settlement_document_id':settle['document_id'],'settlement_version':'1'} and before['financial_fields'].get('status')!='SETTLED' and before['financial_fields']['processor_ref']==f['processor_ref'] and before['financial_fields']['currency']==s['currency'] and _money(before['financial_fields']['amount'],s['exponent'])==units,'PAYMENT_FACT_MISMATCH')
  require(after['original_request']==settle['original_request'] and after['informational_fields']==before['informational_fields'],'PAYMENT_INFORMATION_MISMATCH')
  prior=prior_docs.get((pid,str(before['version'])))
  require(prior is not None and prior['seq']==before['seq'] and prior['financial_fields']==before['financial_fields'],'PAYMENT_HISTORY_MISMATCH')
  prior_lines=[(x['account_id'],x['party_id'],x['side'],_money(x['amount'],s['exponent'],True)) for x in prior['postings']['lines']]
  require(prior_lines==[(bindings['PROCESSOR_CLEARING'],None,'D',units),(bindings['AR_CONTROL'],before['financial_fields']['party_id'],'C',units)],'PAYMENT_ORIGINAL_POSTING_MISMATCH')
  require(incrow['settlement_document_id']==settle['document_id'] and incrow['settlement_version']==1 and incrow['seq']==s['commit_seq'] and pv['version_before']==si['payment_version']==str(before['version']) and pv['version_after']==str(after['version']) and pv['financial_fingerprint_after']==after['financial_fingerprint'],'PAYMENT_EVENT_MISMATCH')
 require(total==amounts['gross'],'GROSS_ITEMS_MISMATCH')
 journal=a['journal.json'];exact(journal,('entry','lines'));entry=journal['entry'];_scoped(entry,s,'entry');require(entry['id']==receipt['entry_id'] and entry['seq']==s['commit_seq'] and entry['source_document_id']==settle['document_id'] and entry['source_document_version']==1 and entry['currency']==s['currency'] and entry['currency_exponent']==s['exponent'] and entry['posting_rule_id']=='sp1-posting' and entry['posting_rule_version']=='1.0' and entry['effective_date']==f['payout_date'] and entry['created_by_user']==actor and entry['created_by'] is None and entry['kind']=='DOCUMENT_POSTING' and entry['provenance']=='SYNTHETIC','JOURNAL_ENTRY_MISMATCH')
 bindings={v['role']:v['account_id'] for v in inp['facts']['bindings']};wanted=[]
 for role,side,key in [('BANK_OPERATING','D','net'),('PROCESSOR_FEE_EXPENSE','D','fee'),('PROCESSOR_CLEARING','C','gross')]:
  if amounts[key]:wanted.append((bindings[role],None,side,amounts[key]))
 actual=[];event_lines=[]
 for index,line in enumerate(journal['lines'],1):
  _scoped(line,s,'line');require(line['entry_id']==entry['id'] and line['line_no']==index and line['seq']==s['commit_seq'] and line['currency']==s['currency'] and line['currency_exponent']==s['exponent'] and line['document_id']==settle['document_id'],'JOURNAL_LINE_MISMATCH');actual.append((line['account_id'],line['party_id'],line['side'],_money(line['amount'],s['exponent'],True)));event_lines.append({k:str(line[k]) if k=='line_no' else line[k] for k in ('account_id','party_id','side','amount','line_no')})
 require(actual==wanted,'DERIVED_JOURNAL_MISMATCH')
 predicted=[(v['account_id'],v['party_id'],v['side'],_money(v['amount']['amount'],s['exponent'])) for v in out['predicted_postings']];require(predicted==wanted,'PREDICTED_POSTING_MISMATCH')
 normal=sorted([[ac,pa,side,(str(units) if not s['exponent'] else f'{units//10**s["exponent"]}.{units%10**s["exponent"]:0{s["exponent"]}d}'),s['currency']] for ac,pa,side,units in wanted],key=lambda v:(v[0],v[1] or '',v[2]))
 scope={'tenant_id':s['tenant_id'],'ledger_id':s['ledger_id'],'proposal_id':s['proposal_id'],'revision':s['revision'],'simulation_id':s['simulation_id'],'currency':s['currency'],'exponent':s['exponent'],'effective_date':entry['effective_date'],'posting_rule':entry['posting_rule_id'],'posting_rule_version':entry['posting_rule_version']}
 match=sha(canon({'format':'lv-native-posting-match/1','scope':scope,'lines':normal},False).encode());require(receipt['comparison']=={'format':'lv-native-posting-match/1','comparison':'MATCHED','predicted_hash':match,'derived_hash':match,'line_count':len(wanted),'accounting_status':'UNREVIEWED','authorizes_execution':False},'POSTING_PROOF_MISMATCH')
 require(last['kind']=='EXECUTION' and last['created_by_user']==actor and last['document_id']==settle['document_id'] and last['entry_id']==entry['id'] and last['entry_no']==entry['entry_no'] and last['financial_fields']==f and last['postings']['lines']==event_lines and last['posting_rule']=='sp1-posting/1.0' and last['postings']['currency']==s['currency'] and last['postings']['effective_date']==entry['effective_date'] and last['provenance']=='SYNTHETIC' and last['document_type']=='SETTLEMENT' and last['document_version']=='1','EXECUTION_EVENT_MISMATCH')
 batch=parse(hist[-1]['batch_canonical']);require(batch['created_by_user']==actor and batch['request_hash']==sha(canon(settle['original_request'],False).encode()),'COMMIT_REQUEST_MISMATCH')
 return {'valid':True,'format':b['format'],'file_sha256':file_hash,'manifest_sha256':b['manifest_sha256'],'execution_id':s['execution_id'],'commit_seq':s['commit_seq'],'event_head':ep,'batch_head':bp,'integrity':'INTERNAL_CONSISTENCY_VERIFIED','supplied_digest':'MATCHED' if expected_sha256 else 'NOT_SUPPLIED','authenticity':'NOT_ESTABLISHED','accounting':'UNREVIEWED','external_anchor':'NONE'}

def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('file');parser.add_argument('--expected-sha256');args=parser.parse_args(argv)
 try:
  p=Path(args.file);require(p.stat().st_size<=MAX_BYTES,'PROOF_SIZE_LIMIT');result=verify(p.read_bytes(),args.expected_sha256)
 except (OSError,ProofInvalid) as error:
  print(json.dumps({'valid':False,'reason':str(error) if isinstance(error,ProofInvalid) else 'FILE_UNAVAILABLE'}));return 1
 print(json.dumps(result,sort_keys=True));return 0


def _semantic_projection(outcome):
 # Standard-library-only counterpart of the online comparator; offline tools remain standalone.
 import copy
 if outcome.get('status')!='COMPLETED':return None
 o=copy.deepcopy(outcome);seq=o.pop('snapshot_seq');o.pop('input_hash')
 for x in o['account_deltas']:x.pop('before');x.pop('after')
 for x in o['verdicts']:
  if x['policy_id']=='P-CLR-02':x['facts'].pop('clearing_balance_after')
 for k in ('input_hash','snapshot_hash','envelope_sha256'):o['calculation']['kernel'].pop(k)
 for x in o['calculation']['evidence_observations']:
  if x['id']=='E-DUP-01':
   x['reason']=x['reason'].replace('snapshot seq '+seq,'snapshot seq CAPTURE');x['values']['scope']=x['values']['scope'].replace('snapshot seq '+seq,'snapshot seq CAPTURE');x['trace'].pop('batch_head');x['trace'].pop('snapshot_seq')
 return o

def _semantic_certificates(sim,s,original,out,commit_seq):
 rows=sim.get('semantic_rechecks',[]);require(type(rows) is list and len(rows)<=2000,'SEMANTIC_RECHECK_LIMIT');result={}
 for q in rows:
  require(_uuid(q['id']) and q['id'] not in result and _uuid(q['actor_id']) and q['proposal_id']==s['proposal_id'] and q['simulation_id']==s['simulation_id'] and q['revision']==s['revision'] and q['action'] in ('approve','override','execute','revalidate') and _seq(q['seq']) and int(q['seq'])<commit_seq and type(q['equivalent']) is bool,'SEMANTIC_RECHECK_SCOPE')
  for key,h in [('input_canonical','input_hash'),('outcome_canonical','result_hash')]:require(sha(q[key].encode())==q[h],'SEMANTIC_RECHECK_HASH')
  b=parse(q['input_canonical']);o=parse(q['outcome_canonical']);require(canon(b)==q['input_canonical'] and canon(o)==q['outcome_canonical'] and o['input_hash']==q['input_hash'],'SEMANTIC_RECHECK_CANONICAL')
  require(b['tenant_id']==s['tenant_id'] and b['ledger']==original['ledger'] and b['versions']==original['versions'] and b['proposal']['id']==s['proposal_id'] and b['proposal']['revision']==str(s['revision']) and b['proposal']['operations']==original['proposal']['operations'] and b['proposal']['evidence_refs']==original['proposal']['evidence_refs'] and b['pinned_sources']==original['pinned_sources'] and b['snapshot']['seq']==q['seq'],'SEMANTIC_RECHECK_INPUTS')
  equivalent=o.get('status')=='COMPLETED' and _semantic_projection(o)==_semantic_projection(out);require(equivalent==q['equivalent'],'SEMANTIC_RECHECK_COMPARISON');result[q['id']]=q
 return result

def _fresh_semantic(check,expected,rechecks):
 report=check['report'];rid=report.get('semantic_recheck_id')
 if rid is None:_fresh(check,expected);return
 q=rechecks.get(rid);require(q is not None and q['equivalent'] is True,'MISSING_SEMANTIC_RECHECK')
 # Approval can occur at a later safe capture; execution must bind its immediately preceding sequence.
 seq=int(q['seq']);require(seq>=expected and report.get('pinned_seq')==seq and report.get('observed_seq')==seq,'SEMANTIC_RECHECK_SEQUENCE');_fresh(check,seq)

if __name__=='__main__':raise SystemExit(main())
