"""Independent native approval, 24-hour review cooling-off and revision-bound decisions."""
from pathlib import Path
from alembic import op
revision='0019_native_approval'
down_revision='0018_evidence_overrides'
branch_labels=None
depends_on=None
BASE=Path(__file__).parents[1]/'sql'
def extract(file,name):
 text=(BASE/file).read_text();token='FUNCTION lv.'+name+'(';start=text.index(token);start=text.rfind('CREATE',0,start)
 return text[start:text.index('$$;',start)+3].replace('CREATE FUNCTION','CREATE OR REPLACE FUNCTION',1)
def once(text,old,new):
 if text.count(old)!=1:raise RuntimeError('owning predecessor drift: '+old)
 return text.replace(old,new,1)
def previous():
 guard=extract('0015_resimulation.sql','guard_proposal_update');validator=extract('0015_resimulation.sql','validate_submission')
 guard=once(guard," RAISE EXCEPTION 'unsupported proposal transition'", " IF OLD.state='BLOCKED' AND NEW.state='READY_FOR_REVIEW' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;\n RAISE EXCEPTION 'unsupported proposal transition'")
 validator=once(validator,'AND t.user_id=p.created_by)',"AND (t.user_id=p.created_by OR (t.action='overrides' AND EXISTS(SELECT 1 FROM lv.proposal_override ov WHERE ov.tenant_id=p.tenant_id AND ov.proposal_id=p.id AND ov.resulting_version=p.version AND ov.overridden_by=t.user_id AND ov.session_id=t.session_id AND ov.tx_id=pg_current_xact_id()))))")
 validator=once(validator,'AND a.user_id=p.created_by',"AND a.user_id=CASE WHEN act='overrides' THEN (SELECT overridden_by FROM lv.proposal_override WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version) ELSE p.created_by END")
 validator=once(validator,"WHEN act='resimulate' THEN 'PROPOSAL_RESIMULATED' ELSE", "WHEN act='resimulate' THEN 'PROPOSAL_RESIMULATED' WHEN act='overrides' THEN 'PROPOSAL_OVERRIDDEN' ELSE")
 validator=once(validator,"(o->>'aggregate'='CLEAR')", "(o->>'aggregate'='CLEAR' OR lv.override_covers(p))")
 return guard,validator

def upgrade():
 bind=op.get_bind();bind.exec_driver_sql((BASE/'0019_native_approval.sql').read_text());guard,validator=previous()
 guard=once(guard,"OLD.state IN ('BLOCKED','READY_FOR_REVIEW') AND NEW.state='STALE'","OLD.state IN ('BLOCKED','READY_FOR_REVIEW','APPROVED') AND NEW.state='STALE'")
 guard=once(guard," RAISE EXCEPTION 'unsupported proposal transition'"," IF OLD.state='READY_FOR_REVIEW' AND NEW.state='APPROVED' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;\n RAISE EXCEPTION 'unsupported proposal transition'")
 bind.exec_driver_sql(guard)
 stale=""" IF EXISTS(SELECT 1 FROM lv.approval_check c JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.revision,t.version)=(c.tenant_id,c.proposal_id,c.revision,c.resulting_version)
 WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.report->>'status'='STALE' AND c.tx_id=pg_current_xact_id() AND t.tx_id=pg_current_xact_id() AND t.action='approve' AND t.user_id=c.checked_by AND t.session_id=c.session_id)
 THEN
 IF EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'approval stale left reservations' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
"""
 validator=once(validator," IF p.state='STALE' THEN\n"," IF p.state='STALE' THEN\n"+stale)
 validator=once(validator,"AND (t.user_id=p.created_by OR (t.action='overrides'", "AND (t.user_id=p.created_by OR (t.action='approve' AND EXISTS(SELECT 1 FROM lv.proposal_decision d WHERE d.tenant_id=p.tenant_id AND d.proposal_id=p.id AND d.resulting_version=p.version AND d.approved_by=t.user_id AND d.session_id=t.session_id AND d.tx_id=pg_current_xact_id())) OR (t.action='overrides'")
 validator=once(validator,"CASE WHEN act='overrides' THEN (SELECT overridden_by", "CASE WHEN act='approve' THEN (SELECT approved_by FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version) WHEN act='overrides' THEN (SELECT overridden_by")
 validator=once(validator,"WHEN act='overrides' THEN 'PROPOSAL_OVERRIDDEN' ELSE", "WHEN act='overrides' THEN 'PROPOSAL_OVERRIDDEN' WHEN act='approve' THEN 'PROPOSAL_APPROVED' ELSE")
 validator=once(validator,"(p.state='READY_FOR_REVIEW') IS DISTINCT", "(p.state IN ('READY_FOR_REVIEW','APPROVED')) IS DISTINCT")
 bind.exec_driver_sql(validator)
 covers=extract('0018_evidence_overrides.sql','override_covers');covers=once(covers,'o.resulting_version=p.version',"o.resulting_version=CASE WHEN p.state='APPROVED' THEN p.version-1 ELSE p.version END");bind.exec_driver_sql(covers)
 reservation=extract('0015_resimulation.sql','validate_reservation')
 reservation=once(reservation,' ELSE\n IF w.status',""" ELSE
 IF w.status='VALID' AND p.state='STALE' AND EXISTS(SELECT 1 FROM lv.approval_check c JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.version)=(c.tenant_id,c.proposal_id,c.resulting_version)
 WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.report->>'status'='STALE' AND c.checked_by=e.user_id AND c.session_id=e.session_id AND c.tx_id=pg_current_xact_id() AND t.action='approve' AND t.tx_id=pg_current_xact_id()) THEN RETURN NULL; END IF;
 IF w.status""")
 reservation=once(reservation,"p.state NOT IN ('BLOCKED','READY_FOR_REVIEW')", "p.state NOT IN ('BLOCKED','READY_FOR_REVIEW','APPROVED')");bind.exec_driver_sql(reservation)
 for file,name in [('0014_period_staleness.sql','maintenance_step'),('0015_resimulation.sql','workflow_revalidate')]:
  text=extract(file,name);text=once(text,"('BLOCKED','READY_FOR_REVIEW')","('BLOCKED','READY_FOR_REVIEW','APPROVED')");bind.exec_driver_sql(text)

def downgrade():
 op.execute('ALTER TABLE lv.approval_check ADD CONSTRAINT downgrade_empty CHECK(false)')
 op.execute('ALTER TABLE lv.proposal_decision ADD CONSTRAINT downgrade_empty CHECK(false)')
 op.execute("ALTER TABLE lv.proposal ADD CONSTRAINT downgrade_no_approved CHECK(state<>'APPROVED')")
 for text in previous():op.get_bind().exec_driver_sql(text)
 for file,name in [('0018_evidence_overrides.sql','override_covers'),('0015_resimulation.sql','validate_reservation'),('0014_period_staleness.sql','maintenance_step'),('0015_resimulation.sql','workflow_revalidate')]:op.get_bind().exec_driver_sql(extract(file,name))
 for name,args in [('approve_native','bytea,uuid,text,text,text,jsonb,jsonb,text,text'),('approval_available','uuid,uuid'),('review_blocker','lv.proposal,uuid'),('decision_json','lv.proposal_decision')]:op.execute('DROP FUNCTION lv.'+name+'('+args+')')
 op.execute('DROP TABLE lv.proposal_decision');op.execute('DROP TABLE lv.approval_check')
 for name in ['validate_approval_check','validate_proposal_decision']:op.execute('DROP FUNCTION lv.'+name+'()')
 op.execute('ALTER TABLE lv.proposal DROP CONSTRAINT proposal_maker_identity,DROP CONSTRAINT proposal_workflow_state,DROP CONSTRAINT downgrade_no_approved')
 op.execute("ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','STALE','WITHDRAWN'))")
 op.execute('ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor')
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale','resimulate','revalidate','overrides'))")
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw','resimulate','revalidate','overrides') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL))")
 op.execute("DELETE FROM lv.api_idempotency WHERE route='/api/v1/proposals/{proposal_id}/approve'")
