"""Bounded ADR-014 controller exception for native RecordSettlement P-EVD-01."""
from pathlib import Path
from alembic import op
revision='0018_evidence_overrides'
down_revision='0017_proposal_comments'
branch_labels=None
depends_on=None

def predecessor(name):
    text=(Path(__file__).parents[1]/'sql/0015_resimulation.sql').read_text()
    start=text.index('CREATE OR REPLACE FUNCTION lv.'+name+'(')
    return text[start:text.index('$$;',start)+3]

def once(text,old,new):
    if text.count(old)!=1:raise RuntimeError('owning predecessor changed: '+old)
    return text.replace(old,new,1)

def upgrade():
    bind=op.get_bind();bind.exec_driver_sql((Path(__file__).parents[1]/'sql/0018_evidence_overrides.sql').read_text())
    guard=predecessor('guard_proposal_update')
    guard=once(guard," RAISE EXCEPTION 'unsupported proposal transition'", " IF OLD.state='BLOCKED' AND NEW.state='READY_FOR_REVIEW' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;\n RAISE EXCEPTION 'unsupported proposal transition'")
    bind.exec_driver_sql(guard)
    validator=predecessor('validate_submission')
    validator=once(validator,'AND t.user_id=p.created_by)',"AND (t.user_id=p.created_by OR (t.action='overrides' AND EXISTS(SELECT 1 FROM lv.proposal_override ov WHERE ov.tenant_id=p.tenant_id AND ov.proposal_id=p.id AND ov.resulting_version=p.version AND ov.overridden_by=t.user_id AND ov.session_id=t.session_id AND ov.tx_id=pg_current_xact_id()))))")
    validator=once(validator,'AND a.user_id=p.created_by',"AND a.user_id=CASE WHEN act='overrides' THEN (SELECT overridden_by FROM lv.proposal_override WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version) ELSE p.created_by END")
    validator=once(validator,"WHEN act='resimulate' THEN 'PROPOSAL_RESIMULATED' ELSE", "WHEN act='resimulate' THEN 'PROPOSAL_RESIMULATED' WHEN act='overrides' THEN 'PROPOSAL_OVERRIDDEN' ELSE")
    validator=once(validator,"(o->>'aggregate'='CLEAR')", "(o->>'aggregate'='CLEAR' OR lv.override_covers(p))")
    bind.exec_driver_sql(validator)

def downgrade():
    op.execute('ALTER TABLE lv.proposal_override ADD CONSTRAINT downgrade_empty CHECK(false)')
    for name in ['guard_proposal_update','validate_submission']:op.get_bind().exec_driver_sql(predecessor(name))
    op.execute('DROP FUNCTION lv.override_evidence(bytea,uuid,text,text,text,jsonb,jsonb,text,text)')
    op.execute('DROP FUNCTION lv.override_covers(lv.proposal)')
    op.execute('DROP FUNCTION lv.override_json(lv.proposal_override)')
    op.execute('DROP TABLE lv.proposal_override')
    op.execute('DROP FUNCTION lv.validate_evidence_override()')
    op.execute('ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor')
    op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale','resimulate','revalidate'))")
    op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw','resimulate','revalidate') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL))")
    op.execute("DELETE FROM lv.api_idempotency WHERE route='/api/v1/proposals/{proposal_id}/overrides'")
