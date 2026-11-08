"""Whole-invoice holds and immutable replayable plans, with real PostgreSQL staging."""
from dataclasses import replace
from uuid import uuid4
from pathlib import Path
import json,gzip,hashlib,sqlite3
import pytest
from ledgervigil.evaluation import admission as plan,staging
from ledgervigil.evaluation.retail import SHEETS,canonical,file_hash
from test_retail_evaluation import row,evaluated  # noqa:F401


def line(source):return {'sheet':source.sheet,'row':source.row,'raw':{'values':list(source.values),'cell_issues':list(source.issues)},'sha256':source.sha256}


def group(rows):return plan.group_record('a'*64,[line(r) for r in rows])['payload']


def test_clean_exact_invoice_is_only_a_candidate_for_review():
 a=row();b=replace(row(quantity='2',price='1.25'),row=3);result=group([a,b]);assert result['status']=='CANDIDATE_REVIEW' and result['amount']=={'amount':'85.90','currency':'GBP'} and result['posting_eligibility']=='UNAVAILABLE' and result['accounting_status']=='UNREVIEWED'

@pytest.mark.parametrize('change,reason',[
 ({'price':'2.0999999999999996'},'LINE_INVALID_OR_SUB_MINOR_MONEY'),
 ({'price':'0.005'},'LINE_INVALID_OR_SUB_MINOR_MONEY'),
 ({'customer':None},'LINE_MISSING_CUSTOMER_ID'),
 ({'customer':'13086'},'CUSTOMER_CONFLICT'),
 ({'country':'France'},'COUNTRY_CONFLICT'),
 ({'date':'40148.5'},'SOURCE_TIMESTAMP_CONFLICT'),
])
def test_one_incompatible_line_holds_every_line_and_no_partial_amount(change,reason):
 r=group([row(),replace(row(**change),row=3)]);assert r['status']=='HOLD' and reason in r['hold_reasons'] and r['amount'] is None and len(r['source_rows'])==2


def test_repeated_content_is_retained_and_holds_whole_invoice():
 r=group([row(),replace(row(),row=3)]);assert r['status']=='HOLD' and r['source_line_count']==2 and 'DUPLICATE_CONTENT_UNRESOLVED' in r['hold_reasons'] and r['amount'] is None


def test_cross_sheet_identity_is_held_without_silent_deduplication():
 r=group([row(),replace(row(),sheet=SHEETS[1][0])]);assert r['status']=='HOLD' and 'CROSS_SHEET_INVOICE_ID_UNRESOLVED' in r['hold_reasons'] and len(r['source_rows'])==2


def test_invoice_aggregate_overflow_is_held_even_with_valid_lines():
 r=group([row(quantity='1',price='9999999999999999.99'),replace(row(quantity='1',price='0.01'),row=3)]);assert 'INVOICE_AMOUNT_RANGE' in r['hold_reasons'] and r['amount'] is None


def stage(c,rows):return staging.ingest(c,rows,'a'*64,uuid4())


def test_export_verify_selection_and_deterministic_bytes(evaluated,tmp_path):
 c=evaluated;r=stage(c,[row(),replace(row(quantity='2'),row=3)])
 a=plan.export_plan(c,r['run_id'],tmp_path/'one');b=plan.export_plan(c,r['run_id'],tmp_path/'two')
 assert a==b and (tmp_path/'one'/plan.FILE).read_bytes()==(tmp_path/'two'/plan.FILE).read_bytes()
 verified=plan.verify_plan(tmp_path/'one',select_invoice='489434',expected_manifest_sha256=file_hash(tmp_path/'one/MANIFEST.json'))
 assert verified['verified'] and verified['selected']['source_line_count']==2 and verified['summary']['source_rows']==2 and verified['header']['classification']=='SYNTHETIC_TEST'
 assert c.execute('SELECT count(*) FROM lv_eval.run').fetchone()==(1,) and c.execute('SELECT count(*) FROM lv_eval.retail_line').fetchone()==(2,)


def test_missing_invoice_rows_remain_distinct_orphans(evaluated,tmp_path):
 rows=[row(invoice=None),replace(row(invoice=None),row=3)];r=stage(evaluated,rows);m=plan.export_plan(evaluated,r['run_id'],tmp_path/'plan');assert m['summary']['groups']==2 and m['summary']['status_counts']=={'HOLD':2};assert plan.verify_plan(tmp_path/'plan')['verified']

@pytest.mark.parametrize('point',['_after_group','_before_manifest'])
def test_export_failure_leaves_no_complete_or_partial_destination(evaluated,tmp_path,monkeypatch,point):
 r=stage(evaluated,[row()]);destination=tmp_path/'plan'
 def fail(*_):raise RuntimeError('injected export failure')
 monkeypatch.setattr(plan,point,fail)
 with pytest.raises(RuntimeError,match='injected'):plan.export_plan(evaluated,r['run_id'],destination)
 assert not destination.exists()
 assert evaluated.execute('SELECT count(*) FROM lv_eval.retail_line').fetchone()==(1,)


def test_export_does_not_replace_an_existing_review(evaluated,tmp_path):
 r=stage(evaluated,[row()]);destination=tmp_path/'existing';destination.mkdir();(destination/'keep').write_text('accepted review')
 with pytest.raises(FileExistsError):plan.export_plan(evaluated,r['run_id'],destination)
 assert (destination/'keep').read_text()=='accepted review'

@pytest.mark.parametrize('mutation',["UPDATE lv_eval.retail_line SET candidate=false,reasons=ARRAY['MISSING_CUSTOMER_ID']", "UPDATE lv_eval.retail_line SET raw=jsonb_set(raw,'{values,5}','\"9.99\"')", "DELETE FROM lv_eval.retail_line"])
def test_staging_mutation_or_missing_source_cannot_be_exported(evaluated,tmp_path,mutation):
 r=stage(evaluated,[row()]);evaluated.execute(mutation)
 with pytest.raises(plan.PlanRejected):plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 assert not (tmp_path/'plan').exists()


def test_wrong_manifest_anchor_or_modified_compressed_bytes_are_refused(evaluated,tmp_path):
 r=stage(evaluated,[row()]);plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 with pytest.raises(plan.PlanRejected,match='MANIFEST_HASH'):plan.verify_plan(tmp_path/'plan',expected_manifest_sha256='0'*64)
 with (tmp_path/'plan'/plan.FILE).open('ab') as f:f.write(b'changed')
 with pytest.raises(plan.PlanRejected,match='PLAN_HASH'):plan.verify_plan(tmp_path/'plan')


def rewrite(directory,mutate):
 m=json.loads((directory/'MANIFEST.json').read_text());values=[json.loads(v) for v in gzip.decompress((directory/plan.FILE).read_bytes()).splitlines()];mutate(values)
 raw=''.join(canonical(v)+'\n' for v in values).encode();data=gzip.compress(raw,mtime=0)
 (directory/plan.FILE).write_bytes(data);m.update(compressed_bytes=len(data),compressed_sha256=hashlib.sha256(data).hexdigest(),uncompressed_sha256=hashlib.sha256(raw).hexdigest(),header=values[0],summary=values[-1]);(directory/'MANIFEST.json').write_text(json.dumps(m))


def test_wrong_invoice_amount_is_refused_even_with_updated_container_hashes(evaluated,tmp_path):
 r=stage(evaluated,[row()]);plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 def alter(v):v[1]['payload']['amount']['amount']='0.01';v[1]['payload_sha256']=plan.hash_value(v[1]['payload'])
 rewrite(tmp_path/'plan',alter)
 with pytest.raises(plan.PlanRejected,match='INVOICE_PLAN_MISMATCH'):plan.verify_plan(tmp_path/'plan')


def test_duplicate_source_reference_is_refused_even_with_new_hashes(evaluated,tmp_path):
 r=stage(evaluated,[row()]);plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 def duplicate(v):v.insert(2,v[1])
 rewrite(tmp_path/'plan',duplicate)
 with pytest.raises(plan.PlanRejected,match='DUPLICATE_SOURCE_REFERENCE'):plan.verify_plan(tmp_path/'plan')


def test_header_cannot_enable_posting_or_approve_data_policy(evaluated,tmp_path):
 r=stage(evaluated,[row()]);plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 rewrite(tmp_path/'plan',lambda values:values[0]['policy_status'].update({'DATA-1':'ACCEPTED'}))
 with pytest.raises(plan.PlanRejected,match='HEADER_SCOPE'):plan.verify_plan(tmp_path/'plan')


def test_group_budget_refuses_without_partial_export(evaluated,tmp_path,monkeypatch):
 r=stage(evaluated,[row(),replace(row(quantity='2'),row=3)]);monkeypatch.setattr(plan,'MAX_GROUP_ROWS',1)
 with pytest.raises(plan.PlanRejected,match='GROUP_BUDGET'):plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 assert not(tmp_path/'plan').exists()


def test_source_coverage_catches_changed_observation_with_recomputed_payloads(evaluated,tmp_path):
 r=stage(evaluated,[row()]);plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 def alter(values):
  body=values[1]['payload'];body['source_rows'][0]['raw']['values'][5]='1.00';body['source_rows'][0]['sha256']=plan.hash_value(body['source_rows'][0]['raw'])
  values[1]=plan.group_record('a'*64,body['source_rows'])
  values[-1]['candidate_review_amount_minor_sum']='1200';values[-1]['group_chain_sha256']=hashlib.sha256((values[1]['payload_sha256']+'\n').encode()).hexdigest()
 rewrite(tmp_path/'plan',alter)
 with pytest.raises(plan.PlanRejected,match='SOURCE_COVERAGE_MISMATCH'):plan.verify_plan(tmp_path/'plan')


def test_summary_never_counts_partial_held_invoice_amounts(evaluated,tmp_path):
 rows=[row(),replace(row(invoice='489435'),row=3),replace(row(invoice='489435',customer=None),row=4)]
 r=stage(evaluated,rows);m=plan.export_plan(evaluated,r['run_id'],tmp_path/'plan');assert m['summary']['status_counts']=={'CANDIDATE_REVIEW':1,'HOLD':1} and m['summary']['candidate_review_amount_minor_sum']=='8340'
 assert plan.verify_plan(tmp_path/'plan')['verified']


def test_completed_export_cannot_be_replaced(evaluated,tmp_path):
 # Exclusive directory creation is exercised by an existing completed export, including byte preservation.
 r=stage(evaluated,[row()]);destination=tmp_path/'plan';plan.export_plan(evaluated,r['run_id'],destination);original=(destination/plan.FILE).read_bytes()
 with pytest.raises(FileExistsError):plan.export_plan(evaluated,r['run_id'],destination)
 assert (destination/plan.FILE).read_bytes()==original


def test_corrupted_plan_is_refused_before_returning_a_matching_invoice(evaluated,tmp_path):
 r=stage(evaluated,[row()]);plan.export_plan(evaluated,r['run_id'],tmp_path/'plan')
 rewrite(tmp_path/'plan',lambda values:values[-1].update({'source_rows':99}))
 with pytest.raises(plan.PlanRejected,match='PLAN_COUNTS_MISMATCH'):plan.verify_plan(tmp_path/'plan',select_invoice='489434')
