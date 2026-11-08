"""Atomic native SANDBOX RecordSettlement, authenticated human actor, exact pre-commit proof."""
from pathlib import Path
from alembic import op
revision='0021_native_execution'
down_revision='0020_native_rejection'
branch_labels=None
depends_on=None
BASE=Path(__file__).parents[1]/'sql'
def once(s,a,b):
 if s.count(a)!=1:raise RuntimeError('owning predecessor drift: '+a)
 return s.replace(a,b,1)
def get(name):return op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('lv."+name+"()'::regprocedure)").scalar_one()
def upgrade():
 bind=op.get_bind();guard=get('guard_proposal_update');validator=get('validate_submission');reservation=get('validate_reservation');workflow=get('guard_staged_workflow')
 for f in ['0021_execution_schema.sql','0021_execution_fence.sql','0021_native_writer.sql','0021_execution_functions.sql','0021_execute.sql','0021_execution_validation.sql']:bind.exec_driver_sql((BASE/f).read_text())
 guard=once(guard," RAISE EXCEPTION 'unsupported proposal transition'", " IF OLD.state='APPROVED' AND NEW.state='EXECUTED' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;\n RAISE EXCEPTION 'unsupported proposal transition'");bind.exec_driver_sql(guard)
 validator=once(validator," IF p.state='REJECTED' THEN", """ IF p.state='EXECUTED' THEN
 IF NOT EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND resulting_version=p.version AND state='RECONCILED' AND tx_id=pg_current_xact_id()) THEN RAISE EXCEPTION 'executed proposal lacks completion' USING ERRCODE='23514'; END IF;RETURN NULL;END IF;
 IF p.state='REJECTED' THEN""")
 validator=once(validator," IF p.state='STALE' THEN\n", """ IF p.state='STALE' THEN
 IF EXISTS(SELECT 1 FROM lv.native_execution_check WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version AND report->>'status'='STALE' AND tx_id=pg_current_xact_id()) THEN RETURN NULL;END IF;
""");bind.exec_driver_sql(validator)
 reservation=once(reservation," ELSE\n", """ ELSE
 IF w.status='POSTED' AND p.state='EXECUTED' AND EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND resulting_version=p.version AND executed_by=e.user_id AND session_id=e.session_id AND state='RECONCILED' AND tx_id=pg_current_xact_id()) THEN RETURN NULL;END IF;
 IF w.status='VALID' AND p.state='STALE' AND EXISTS(SELECT 1 FROM lv.native_execution_check WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version AND checked_by=e.user_id AND session_id=e.session_id AND report->>'status'='STALE' AND tx_id=pg_current_xact_id()) THEN RETURN NULL;END IF;
""");bind.exec_driver_sql(reservation)
 workflow=once(workflow,'BEGIN\n',"BEGIN\n IF OLD.status='POSTED' OR (NEW.status='POSTED' AND OLD.status<>'RESERVED') THEN RAISE EXCEPTION 'posted statement is terminal' USING ERRCODE='23514'; END IF;\n");bind.exec_driver_sql(workflow)
def downgrade():
 for table in ['native_execution','native_execution_check','native_execution_incident']:op.execute('ALTER TABLE lv.'+table+' ADD CONSTRAINT downgrade_empty CHECK(false)')
 bind=op.get_bind();bind.exec_driver_sql((BASE/'0021_predecessors.sql').read_text())
 for name,table in [('execution_guard','commit_batch'),('execution_batch_complete','commit_batch'),('execution_fence','source_file'),('execution_fence','import_batch'),('execution_fence','source_record'),('execution_fence','staged_document'),('execution_fence','staged_statement_item'),('native_notification_immutable','outbox')]:op.execute('DROP TRIGGER '+name+' ON lv.'+table)
 op.execute('ALTER TABLE lv.outbox DROP CONSTRAINT outbox_subject,DROP CONSTRAINT outbox_native_execution,DROP COLUMN native_execution_id,DROP COLUMN native_payload,DROP COLUMN native_tx_id,ALTER COLUMN work_item_id SET NOT NULL')
 for name,args in [('execution_available','uuid,uuid'),('execute_native','bytea,uuid,text,text,text,jsonb,jsonb,text,text'),('record_execution_incident','bytea,uuid,text,text'),('post_native_settlement','uuid'),('native_posting_match','lv.native_execution'),('execution_posting_proof','lv.native_execution'),('native_execution_json','lv.native_execution'),('executor_blocker','lv.proposal,uuid'),('validate_settlement_execution','uuid,uuid,bigint,uuid,text'),('settlement_execution_event','uuid,uuid,bigint')]:op.execute('DROP FUNCTION lv.'+name+'('+args+')')
 for name in ['native_execution','native_execution_check','native_execution_incident']:op.execute('DROP TABLE lv.'+name)
 for name in ['guard_execution_history','validate_native_execution','validate_execution_check','guard_execution_batch','fence_ingestion']:op.execute('DROP FUNCTION lv.'+name+'()')
 op.execute('DROP FUNCTION lv.ingestion_ledger(uuid)')
 op.execute('ALTER TABLE lv.source_file DROP COLUMN ingestion_xid')
 op.execute('ALTER TABLE lv.import_batch DROP COLUMN ingestion_xid')
 for table,constraint in [('document_version','document_version_actor'),('journal_entry','journal_entry_actor')]:
  op.execute('ALTER TABLE lv.'+table+' DROP CONSTRAINT '+constraint)
  if table=='document_version':op.execute('ALTER TABLE lv.document_version DROP CONSTRAINT document_version_origin_actor')
  op.execute('ALTER TABLE lv.'+table+' DROP COLUMN created_by_user,ALTER COLUMN created_by SET NOT NULL')
 op.execute('ALTER TABLE lv.proposal DROP CONSTRAINT proposal_workflow_state')
 op.execute("ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','APPROVED','REJECTED','STALE','WITHDRAWN'))")
 op.execute('ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor')
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale','resimulate','revalidate','overrides','approve','reject'))")
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw','resimulate','revalidate','overrides','approve','reject') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL))")
 op.execute('ALTER TABLE lv.staged_workflow DROP CONSTRAINT staged_workflow_status_check,DROP CONSTRAINT staged_workflow_check')
 op.execute("ALTER TABLE lv.staged_workflow ADD CONSTRAINT staged_workflow_status_check CHECK(status IN ('VALID','RESERVED'))")
 op.execute("ALTER TABLE lv.staged_workflow ADD CONSTRAINT staged_workflow_check CHECK((status='RESERVED' AND proposal_id IS NOT NULL AND revision IS NOT NULL) OR (status='VALID' AND proposal_id IS NULL AND revision IS NULL))")
 op.execute('ALTER TABLE lv.reservation_event DROP CONSTRAINT reservation_event_event_check')
 op.execute("ALTER TABLE lv.reservation_event ADD CONSTRAINT reservation_event_event_check CHECK(event IN ('RESERVED','RELEASED'))")
 op.execute('DROP TRIGGER seed_guard ON lv.commit_batch');op.execute('DROP TRIGGER seed_commit_complete ON lv.commit_batch')
 op.execute("CREATE TRIGGER seed_guard BEFORE INSERT ON lv.commit_batch FOR EACH ROW WHEN(NEW.kind::text<>'PERIOD_CLOSE') EXECUTE FUNCTION lv.guard_seed_batch()")
 op.execute("CREATE CONSTRAINT TRIGGER seed_commit_complete AFTER INSERT ON lv.commit_batch DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(NEW.kind::text<>'PERIOD_CLOSE') EXECUTE FUNCTION lv.validate_seed_batch()")
 op.execute("DELETE FROM lv.api_idempotency WHERE route='/api/v1/proposals/{proposal_id}/execute'")
