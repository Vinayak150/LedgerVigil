"""Shared revision decision: native rejection, immutable reason and atomic claim release."""
from pathlib import Path
from alembic import op
revision='0020_native_rejection'
down_revision='0019_native_approval'
branch_labels=None
depends_on=None
BASE=Path(__file__).parents[1]/'sql'
def once(s,a,b):
 if s.count(a)!=1:raise RuntimeError('owning predecessor drift: '+a)
 return s.replace(a,b,1)
def function(name):
 return op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('lv."+name+"()'::regprocedure)").scalar_one()
def upgrade():
 bind=op.get_bind();guard=function('guard_proposal_update');validator=function('validate_submission');reservation=function('validate_reservation')
 bind.exec_driver_sql((BASE/'0020_native_rejection.sql').read_text())
 guard=once(guard,"OLD.state='READY_FOR_REVIEW' AND NEW.state='APPROVED'", "OLD.state='READY_FOR_REVIEW' AND NEW.state IN ('APPROVED','REJECTED')")
 bind.exec_driver_sql(guard)
 validator=once(validator," IF p.state='STALE' THEN\n", """ IF p.state='REJECTED' THEN
 IF NOT EXISTS(SELECT 1 FROM lv.proposal_decision d JOIN lv.approval_check c ON (c.tenant_id,c.id)=(d.tenant_id,d.check_id)
 WHERE d.tenant_id=p.tenant_id AND d.proposal_id=p.id AND d.revision=p.revision AND d.resulting_version=p.version AND d.kind='REJECTED' AND d.tx_id=pg_current_xact_id() AND c.tx_id=pg_current_xact_id() AND c.decision_action='reject' AND c.report->>'status'='FRESH')
 OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'rejection lacks decision or reservation release' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
 IF p.state='STALE' THEN
""")
 validator=once(validator,"t.action='approve' AND t.user_id=c.checked_by", "t.action=c.decision_action AND t.user_id=c.checked_by")
 bind.exec_driver_sql(validator)
 reservation=once(reservation,"t.action='approve' AND t.tx_id", "t.action=c.decision_action AND t.tx_id")
 reservation=once(reservation," ELSE\n", """ ELSE
 IF w.status='VALID' AND p.state='REJECTED' AND EXISTS(SELECT 1 FROM lv.proposal_decision d JOIN lv.approval_check c ON (c.tenant_id,c.id)=(d.tenant_id,d.check_id)
 JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.version)=(d.tenant_id,d.proposal_id,d.resulting_version)
 WHERE d.tenant_id=p.tenant_id AND d.proposal_id=p.id AND d.revision=p.revision AND d.resulting_version=p.version AND d.kind='REJECTED' AND d.approved_by=e.user_id AND d.session_id=e.session_id
 AND d.tx_id=pg_current_xact_id() AND c.tx_id=pg_current_xact_id() AND c.decision_action='reject' AND c.report->>'status'='FRESH' AND t.tx_id=pg_current_xact_id() AND t.action='reject') THEN RETURN NULL; END IF;
""")
 bind.exec_driver_sql(reservation)
def downgrade():
 op.execute("ALTER TABLE lv.approval_check ADD CONSTRAINT downgrade_no_reject CHECK(decision_action<>'reject')")
 op.execute("ALTER TABLE lv.proposal_decision ADD CONSTRAINT downgrade_no_reject CHECK(kind<>'REJECTED')")
 op.execute("ALTER TABLE lv.proposal ADD CONSTRAINT downgrade_no_reject CHECK(state<>'REJECTED')")
 op.get_bind().exec_driver_sql((BASE/'0020_review_predecessors.sql').read_text())
 op.execute('DROP FUNCTION lv.reject_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text)')
 op.execute('ALTER TABLE lv.approval_check DROP COLUMN decision_action')
 op.execute('ALTER TABLE lv.proposal_decision DROP CONSTRAINT downgrade_no_reject,DROP CONSTRAINT rejection_reason,DROP CONSTRAINT proposal_decision_kind_check')
 op.execute("ALTER TABLE lv.proposal_decision ADD CONSTRAINT proposal_decision_kind_check CHECK(kind='APPROVED')")
 op.execute('DROP FUNCTION lv.rejection_reason_valid(text)')
 op.execute('ALTER TABLE lv.proposal DROP CONSTRAINT downgrade_no_reject,DROP CONSTRAINT proposal_workflow_state')
 op.execute("ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','APPROVED','STALE','WITHDRAWN'))")
 op.execute('ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor')
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale','resimulate','revalidate','overrides','approve'))")
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw','resimulate','revalidate','overrides','approve') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL))")
 op.execute("DELETE FROM lv.api_idempotency WHERE route='/api/v1/proposals/{proposal_id}/reject'")
