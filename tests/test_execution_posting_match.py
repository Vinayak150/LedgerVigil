from dataclasses import replace
from copy import deepcopy
import pytest
from tools.dev_db import connection
from ledgervigil.execution.posting_match import PostingScope,PostingMismatch,verify_postings
from test_processor_settlements import cfg,paid,seed,settled
from test_proposal_drafts import fingerprint

S=PostingScope('tenant','ledger','proposal',1,'simulation','INR',2,'2026-10-14','sp1-posting','1.0')
def lines(bank='bank',fee='fee',clearing='clearing'):
 return [dict(account_id=a,party_id=None,side=s,amount=v,currency='INR') for a,s,v in [(bank,'D','49000.00'),(fee,'D','1000.00'),(clearing,'C','50000.00')]]
def test_exact_match_ignores_row_order_but_never_authorizes():
 r=verify_postings(S,lines(),S,list(reversed(lines())));assert r['comparison']=='MATCHED' and r['predicted_hash']==r['derived_hash'] and not r['authorizes_execution'] and r['accounting_status']=='UNREVIEWED'
@pytest.mark.parametrize('change',[{'ledger_id':'other'},{'tenant_id':'other'},{'proposal_id':'other'},{'revision':2},{'simulation_id':'other'},{'effective_date':'2026-10-15'},{'posting_rule_version':'2.0'}])
def test_scope_and_rule_changes_refused(change):
 with pytest.raises(PostingMismatch):verify_postings(S,lines(),replace(S,**change),lines())
def test_balanced_wrong_sale_and_wrong_party_are_refused():
 for key,value in [('account_id','revenue'),('party_id','foreign-party')]:
  wrong=lines();wrong[-1][key]=value
  with pytest.raises(PostingMismatch,match='DERIVED_POSTINGS_DIFFER'):verify_postings(S,lines(),S,wrong)
@pytest.mark.parametrize('amount',[49000.0,'49000.000','NaN','Infinity','4.9e4','049000.00','-49000.00',True])
def test_non_exact_money_refused(amount):
 wrong=lines();wrong[0]['amount']=amount
 with pytest.raises(PostingMismatch):verify_postings(S,lines(),S,wrong)
def test_split_lines_missing_lines_and_extra_fields_are_refused():
 for wrong in [[],lines()[:-1],lines()+[lines()[0]], [{**lines()[0],'trusted':True},*lines()[1:]]]:
  with pytest.raises(PostingMismatch):verify_postings(S,lines(),S,wrong)
def test_actual_postgresql_settlement_lines_match_independent_oc01_and_detect_balanced_mutation(database,settled):
 config,doc,body,result=settled;before=fingerprint(database,set())
 with connection(database=database) as db:
  rows=db.execute('SELECT account_id::text,party_id::text,side::text,amount FROM lv.journal_line WHERE entry_id=%s ORDER BY line_no',(result['entry_id'],)).fetchall()
 actual=[dict(account_id=a,party_id=p,side=s,amount=f'{v:.2f}',currency='INR') for a,p,s,v in rows]
 chart=config['chart'];expected=lines(chart['BANK_OPERATING'],chart['PROCESSOR_FEE_EXPENSE'],chart['PROCESSOR_CLEARING'])
 scope=replace(S,tenant_id=str(config['tenant']),ledger_id=str(config['ledger']))
 assert verify_postings(scope,expected,scope,actual)['comparison']=='MATCHED'
 wrong=deepcopy(actual);wrong[-1]['account_id']=chart['REVENUE_DEFAULT']
 with pytest.raises(PostingMismatch,match='DERIVED_POSTINGS_DIFFER'):verify_postings(scope,expected,scope,wrong)
 assert fingerprint(database,set())==before

def test_balanced_redistribution_between_bank_and_fee_is_not_a_match():
 wrong=lines();wrong[0]['amount']='48000.00';wrong[1]['amount']='2000.00'
 with pytest.raises(PostingMismatch,match='DERIVED_POSTINGS_DIFFER'):verify_postings(S,lines(),S,wrong)
