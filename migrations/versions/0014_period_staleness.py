"""Fenced native period-close staleness consumer; no approval or execution."""
from pathlib import Path
from alembic import op
revision='0014_period_staleness'
down_revision='0013_submission'
branch_labels=None
depends_on=None
FUNCTIONS=('maintenance_due(integer)','maintenance_dispatched(uuid,uuid)','maintenance_claim(uuid,uuid,text)','maintenance_step(uuid,uuid,text,bigint)','maintenance_fail(uuid,uuid,text,bigint,text)','maintenance_sweep(integer)','maintenance_health()')

def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0014_period_staleness.sql').read_text())
    op.execute('ALTER TABLE lv.maintenance_event ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE lv.maintenance_event FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY tenant_scope ON lv.maintenance_event USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)")
    op.execute('REVOKE ALL ON lv.maintenance_event FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker')
    op.execute('GRANT SELECT ON lv.maintenance_event TO lv_app')
    op.execute('GRANT USAGE ON SCHEMA lv TO lv_worker')
    for f in FUNCTIONS:op.execute('GRANT EXECUTE ON FUNCTION lv.'+f+' TO lv_worker')

def downgrade():
    # Do not discard system actor/lease/progress history.
    op.execute("ALTER TABLE lv.maintenance_event ADD CONSTRAINT downgrade_empty CHECK(false)")
    op.execute("ALTER TABLE lv.proposal ADD CONSTRAINT downgrade_no_stale CHECK(state<>'STALE')")
    for f in FUNCTIONS:op.execute('DROP FUNCTION lv.'+f)
    for f in ('maintenance_retry(lv.work_item,text,text)','maintenance_enqueue(lv.work_item,integer)','maintenance_context(uuid)'):op.execute('DROP FUNCTION lv.'+f)
    text=(Path(__file__).parents[1]/'sql/0013_submission.sql').read_text()
    for name in ('guard_proposal_update','workflow_withdraw','validate_submission','validate_reservation'):
        prefix='CREATE OR REPLACE FUNCTION' if 'CREATE OR REPLACE FUNCTION lv.'+name+'(' in text else 'CREATE FUNCTION'
        body=text.split(prefix+' lv.'+name+'(',1)[1].split('$$;',1)[0]
        op.get_bind().exec_driver_sql('CREATE OR REPLACE FUNCTION lv.'+name+'('+body+'$$;')
    op.execute('DROP TABLE lv.maintenance_event')
    op.execute('DROP POLICY maintenance_queue_lock ON lv.work_item')
    op.execute('DROP POLICY maintenance_queue_lock ON lv.outbox')
    op.execute('DROP POLICY maintenance_queue_read ON lv.work_item')
    op.execute('DROP POLICY maintenance_queue_read ON lv.outbox')
    op.execute('ALTER TABLE lv.work_item DROP COLUMN scan_cursor,DROP COLUMN scanned_count,DROP COLUMN stale_count,DROP COLUMN delivery_generation')
    op.execute('ALTER TABLE lv.proposal_transition DROP CONSTRAINT transition_worker_fkey,DROP CONSTRAINT transition_actor,DROP CONSTRAINT proposal_transition_action_check,DROP COLUMN worker_job_id,DROP COLUMN worker_fencing_token,ALTER COLUMN user_id SET NOT NULL,ALTER COLUMN session_id SET NOT NULL')
    op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw'))")
    op.execute('ALTER TABLE lv.reservation_event DROP CONSTRAINT reservation_worker_fkey,DROP CONSTRAINT reservation_actor,DROP COLUMN worker_job_id,DROP COLUMN worker_fencing_token,ALTER COLUMN user_id SET NOT NULL,ALTER COLUMN session_id SET NOT NULL')
    op.execute('ALTER TABLE lv.proposal DROP CONSTRAINT downgrade_no_stale,DROP CONSTRAINT proposal_workflow_state')
    op.execute("ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','WITHDRAWN'))")
