"""Immutable native re-simulation and explicit maker freshness checks (RC-1 additive API)."""
from pathlib import Path
from alembic import op
revision='0015_resimulation'
down_revision='0014_period_staleness'
branch_labels=None
depends_on=None

def upgrade():
 op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0015_resimulation.sql').read_text())
 op.execute('ALTER TABLE lv.proposal_freshness_check ENABLE ROW LEVEL SECURITY')
 op.execute('ALTER TABLE lv.proposal_freshness_check FORCE ROW LEVEL SECURITY')
 op.execute("CREATE POLICY tenant_scope ON lv.proposal_freshness_check USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)")
 op.execute('REVOKE ALL ON lv.proposal_freshness_check FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker')
 op.execute('GRANT SELECT ON lv.proposal_freshness_check TO lv_app')
 for f in ['workflow_resimulate_begin','workflow_revalidate']:op.execute('GRANT EXECUTE ON FUNCTION lv.'+f+'(bytea,uuid,text,text,text,text,text) TO lv_app')

def downgrade():
 op.execute('ALTER TABLE lv.proposal_freshness_check ADD CONSTRAINT downgrade_empty CHECK(false)')
 op.execute("ALTER TABLE lv.proposal_revision ADD CONSTRAINT downgrade_no_resimulation CHECK(change<>'RESIMULATED')")
 for name in ['workflow_resimulate_begin','workflow_revalidate']:op.execute('DROP FUNCTION lv.'+name+'(bytea,uuid,text,text,text,text,text)')
 op.execute('DROP TABLE lv.proposal_freshness_check')
 op.execute('DROP FUNCTION lv.validate_freshness_check()')
 op.execute('DROP FUNCTION lv.proposal_freshness(lv.proposal)')
 def restore(text,name):
  prefix='CREATE OR REPLACE FUNCTION' if 'CREATE OR REPLACE FUNCTION lv.'+name+'(' in text else 'CREATE FUNCTION'
  body=text.split(prefix+' lv.'+name+'(',1)[1].split('$$;',1)[0]
  op.get_bind().exec_driver_sql('CREATE OR REPLACE FUNCTION lv.'+name+'('+body+'$$;')
 base=Path(__file__).parents[1]
 for name in ['guard_proposal_update','validate_submission','validate_reservation']:restore((base/'sql/0014_period_staleness.sql').read_text(),name)
 for name in ['workflow_request','workflow_refused','workflow_finish']:restore((base/'sql/0013_submission.sql').read_text(),name)
 restore((Path(__file__).with_name('0010_proposal_drafts.py')).read_text(),'validate_proposal_revision')
 op.execute('ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor')
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale'))")
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL))")
 op.execute('ALTER TABLE lv.proposal_revision DROP CONSTRAINT proposal_revision_change_check,DROP CONSTRAINT proposal_revision_sequence_kind,DROP CONSTRAINT downgrade_no_resimulation')
 op.execute("ALTER TABLE lv.proposal_revision ADD CONSTRAINT proposal_revision_change_check CHECK(change IN ('CREATED','EDITED'))")
 op.execute("ALTER TABLE lv.proposal_revision ADD CONSTRAINT proposal_revision_check1 CHECK((revision=1 AND previous_revision IS NULL AND change='CREATED') OR (revision>1 AND previous_revision=revision-1 AND change='EDITED'))")
