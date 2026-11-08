"""Offline reader and real PostgreSQL staging safeguards, separate from real-data metrics."""
from dataclasses import replace
from decimal import localcontext,Context
from pathlib import Path
import hashlib,zipfile
import pytest
from ledgervigil.evaluation.retail import SourceRow,assess,Workbook,WorkbookRejected,HEADERS,SHEETS,NS,file_hash
from ledgervigil.evaluation import staging
from tools.dev_db import connection
from uuid import uuid4


def row(**changes):
 values=['489434','85048','Public-source product','12','40148.322916666664','6.95','13085','United Kingdom']
 keys={'invoice':0,'quantity':3,'price':5,'customer':6,'country':7,'description':2,'date':4}
 for k,v in changes.items():values[keys[k]]=v
 return SourceRow(SHEETS[0][0],2,tuple(values))


def test_exact_money_ignores_process_decimal_context():
 with localcontext(Context(prec=3)):
  result=assess(row(quantity='13'));assert result['candidate'] and result['unit_minor']==695 and result['extended_minor']==9035

@pytest.mark.parametrize('changes,reason',[
 ({'price':'0.001'},'INVALID_OR_SUB_MINOR_MONEY'),({'price':'NaN'},'INVALID_OR_SUB_MINOR_MONEY'),
 ({'price':'Infinity'},'INVALID_OR_SUB_MINOR_MONEY'),({'price':'0'},'NONPOSITIVE_UNIT_PRICE'),
 ({'quantity':'-12'},'NONPOSITIVE_QUANTITY'),({'quantity':'1.2'},'INVALID_QUANTITY'),
 ({'customer':None},'MISSING_CUSTOMER_ID'),({'invoice':'C489434'},'CANCELLATION_REQUIRES_ORIGINAL_LINK'),
 ({'invoice':'A489434'},'NON_SALE_INVOICE_CODE'),({'date':'60'},'INVALID_DATE'),({'description':''},'MISSING_DESCRIPTION')])
def test_uncertain_or_unsupported_rows_are_retained_as_quarantine(changes,reason):
 source=row(**changes);result=assess(source);assert not result['candidate'] and reason in result['reasons'];assert source.values[5]==changes.get('price','6.95')


def test_same_values_different_coordinates_have_same_content_hash():
 a=row();b=replace(a,sheet=SHEETS[1][0],row=99);assert a.sha256==b.sha256 and a.payload==b.payload


def workbook(tmp_path,*,headers=HEADERS,formula=False,doctype=False):
 # Small OOXML fixture only. It is not a transformed public dataset or delivered workbook.
 import xml.sax.saxutils as xml
 p=tmp_path/'fixture.xlsx'
 with zipfile.ZipFile(p,'w',zipfile.ZIP_DEFLATED) as z:
  z.writestr('xl/workbook.xml','<workbook xmlns="'+NS[1:-1]+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{s}" r:id="rId{i}"/>' for i,(s,_) in enumerate(SHEETS,1))+'</sheets></workbook>')
  z.writestr('xl/_rels/workbook.xml.rels','<Relationships>'+''.join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in (1,2))+'</Relationships>')
  z.writestr('xl/sharedStrings.xml','<sst xmlns="'+NS[1:-1]+'"/>')
  for _,path in SHEETS:
   def cells(values,n):return ''.join(f'<c r="{chr(65+i)}{n}" t="inlineStr">'+('<f>NOW()</f>' if formula and n==2 and i==5 else '')+f'<is><t>{xml.escape(v or "")}</t></is></c>' for i,v in enumerate(values))
   body='<worksheet xmlns="'+NS[1:-1]+'"><sheetData><row r="1">'+cells(headers,1)+'</row><row r="2">'+cells(row().values,2)+'</row></sheetData></worksheet>'
   z.writestr(path,('<!DOCTYPE worksheet [<!ENTITY a "bad">]>' if doctype else '')+body)
 return p


def test_original_source_hash_and_both_sheets_are_required(tmp_path):
 p=workbook(tmp_path);original=p.read_bytes();rows=list(Workbook(p,file_hash(p)).rows());assert len(rows)==2 and {r.sheet for r in rows}=={s for s,_ in SHEETS};assert p.read_bytes()==original
 with pytest.raises(WorkbookRejected,match='SOURCE_HASH_MISMATCH'):Workbook(p,'0'*64)


def test_wrong_headers_fail_instead_of_guessing_a_mapping(tmp_path):
 p=workbook(tmp_path,headers=('InvoiceNo',)+HEADERS[1:])
 with pytest.raises(WorkbookRejected,match='columns'):list(Workbook(p,file_hash(p)).rows())


def test_formula_cached_value_is_never_treated_as_raw_observation(tmp_path):
 p=workbook(tmp_path,formula=True);rows=list(Workbook(p,file_hash(p)).rows());assert all('FORMULA_CELL' in assess(r)['reasons'] for r in rows)


def test_xml_entity_declarations_fail_closed(tmp_path):
 p=workbook(tmp_path,doctype=True)
 with pytest.raises(WorkbookRejected,match='declarations'):list(Workbook(p,file_hash(p)).rows())

@pytest.fixture
def evaluated(database):
 with connection(database=database,autocommit=True) as c:
  staging.setup(c)
  try:yield c
  finally:c.execute('DROP SCHEMA lv_eval CASCADE')


def test_interruption_rollback_and_same_source_replay(evaluated):
 c=evaluated;source=[row(),replace(row(),row=3)];sha='a'*64;ledger=uuid4()
 with pytest.raises(RuntimeError,match='INTERRUPTION'):staging.ingest(c,iter(source),sha,ledger,fault_after=1)
 assert c.execute('SELECT count(*) FROM lv_eval.run').fetchone()==(0,) and c.execute('SELECT count(*) FROM lv_eval.retail_line').fetchone()==(0,)
 first=staging.ingest(c,iter(source),sha,ledger)
 def forbidden():raise AssertionError('source re-read');yield
 again=staging.ingest(c,forbidden(),sha,ledger)
 assert again['idempotent_replayed'] and again['run_id']==first['run_id'] and first['rows']==2
 assert first['summary']['additional_duplicate_content_rows_retained']==1


def test_invoice_group_with_one_bad_line_is_held_whole(evaluated):
 c=evaluated;source=[row(),replace(row(customer=None),row=3)]
 r=staging.ingest(c,source,'b'*64,uuid4());s=r['summary'];assert s['candidate_rows']==1 and s['quarantined_rows']==1 and s['held_invoice_groups']==1 and s['complete_positive_consistent_invoice_groups']==0


def test_cross_customer_invoice_group_is_not_silently_merged(evaluated):
 r=staging.ingest(evaluated,[row(),replace(row(customer='13086'),row=3)],'c'*64,uuid4());assert r['summary']['held_invoice_groups']==1


def test_independent_money_check_catches_bad_kernel_result_and_rolls_back(evaluated,monkeypatch):
 original=staging.assess
 def broken(source):r=original(source);r['extended_minor']+=1;return r
 monkeypatch.setattr(staging,'assess',broken)
 with pytest.raises(AssertionError,match='INDEPENDENT_MONEY'):staging.ingest(evaluated,[row()],'d'*64,uuid4())
 assert evaluated.execute('SELECT count(*) FROM lv_eval.retail_line').fetchone()==(0,)


def test_empty_source_is_not_a_successful_evaluation(evaluated):
 with pytest.raises(ValueError,match='EMPTY_SOURCE'):staging.ingest(evaluated,[],'e'*64,uuid4())


def test_same_file_on_another_evaluation_ledger_is_a_distinct_run(evaluated):
 a=staging.ingest(evaluated,[row()],'f'*64,uuid4());b=staging.ingest(evaluated,[row()],'f'*64,uuid4());assert not b['idempotent_replayed'] and a['run_id']!=b['run_id']


def test_runtime_roles_cannot_read_evaluation_staging(evaluated):
 for role in ['lv_app','lv_seed','lv_ingest','lv_worker']:
  assert evaluated.execute("SELECT has_schema_privilege(%s,'lv_eval','USAGE'),has_table_privilege(%s,'lv_eval.retail_line','SELECT')",(role,role)).fetchone()==(False,False)


def test_half_cent_source_is_never_rounded_into_a_candidate():
 r=assess(row(price='0.005'));assert not r['candidate'] and 'INVALID_OR_SUB_MINOR_MONEY' in r['reasons']


def test_near_penny_diagnostic_never_changes_acceptance(evaluated):
 values=[replace(row(price='2.0999999999999996'),row=2),replace(row(price='0.005'),row=3),replace(row(price='2.10'),row=4)]
 r=staging.ingest(evaluated,values,'1'*64,uuid4());d=r['summary']['price_precision_diagnostic']
 assert d['stored_numeric_prices_not_whole_pence']==2 and d['within_one_billionth_penny_of_whole_penny']==1 and d['larger_sub_penny_difference']==1 and not d['acceptance_policy_changed']
 assert r['summary']['candidate_rows']==1 and r['summary']['held_invoice_groups']==1


def test_a_different_workbook_cannot_claim_public_dataset_identity(tmp_path):
 from tools.evaluate_retail import pinned_workbook
 p=workbook(tmp_path)
 with pytest.raises(WorkbookRejected,match='SOURCE_NOT_PINNED'):pinned_workbook(p,file_hash(p))


def test_controlled_fixture_keeps_synthetic_provenance(evaluated):
 r=staging.ingest(evaluated,[row()],'2'*64,uuid4());assert r['provenance']['classification']=='SYNTHETIC_TEST' and 'doi' not in r['provenance']
