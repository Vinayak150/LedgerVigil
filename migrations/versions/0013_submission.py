"""Atomic native SANDBOX settlement submission, pinned artifacts, reservations and maker withdrawal."""
from pathlib import Path
import importlib.util
from alembic import op
revision='0013_submission'
down_revision='0012_account_versions'
branch_labels=None
depends_on=None
TABLES=('ledger_snapshot','proposal_read_set','verdict','proposal_transition','staged_workflow','reservation_event')
FUNCTIONS=('workflow_request(bytea,text,text,text,text,text)',
           'workflow_begin(bytea,uuid,text,text,text,text,text)',
           'workflow_finish(bytea,uuid,text,text,text,text,text,text)',
           'workflow_withdraw(bytea,uuid,text,text,text,text,text)',
           'workflow_refused(bytea,text,text,text,jsonb,text,text)')

def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0013_submission.sql').read_text())
    for table in TABLES:
        op.execute(f'ALTER TABLE lv.{table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE lv.{table} FORCE ROW LEVEL SECURITY')
        op.execute(f"CREATE POLICY tenant_scope ON lv.{table} USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)")
        op.execute(f'REVOKE ALL ON lv.{table} FROM PUBLIC,lv_app,lv_seed,lv_ingest')
        op.execute(f'GRANT SELECT ON lv.{table} TO lv_app')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC')
    for signature in FUNCTIONS:op.execute('GRANT EXECUTE ON FUNCTION lv.'+signature+' TO lv_app')

def downgrade():
    # Refuse destructive conversion of submitted/withdrawn proposals even in a disposable database.
    bind=op.get_bind()
    bind.exec_driver_sql("ALTER TABLE lv.proposal ADD CONSTRAINT proposal_downgrade_drafts_only CHECK(state='DRAFT')")
    for signature in FUNCTIONS:op.execute('DROP FUNCTION lv.'+signature)
    for table,trigger in [('proposal','workflow_complete'),('proposal_transition','workflow_complete'),('staged_workflow','reservation_complete'),('staged_workflow','workflow_guard')]:
        op.execute(f'DROP TRIGGER {trigger} ON lv.{table}')
    op.execute('DROP FUNCTION lv.validate_submission()')
    op.execute('DROP FUNCTION lv.validate_reservation()')
    op.execute('DROP FUNCTION lv.guard_staged_workflow()')
    op.execute('DROP FUNCTION lv.workflow_final_session(bytea,text,text)')
    op.execute('DROP FUNCTION lv.workflow_cache(lv_identity.session,text,text,text,jsonb)')
    op.execute('DROP FUNCTION lv.workflow_simulation_json(uuid)')
    # Restore the exact owning predecessor definitions before removing their extended row columns.
    spec=importlib.util.spec_from_file_location('draft_predecessor',Path(__file__).with_name('0010_proposal_drafts.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for name in ('guard_proposal_update','proposal_json'):
        text=module.UPGRADE.split('CREATE FUNCTION lv.'+name+'(',1)[1].split('$$;',1)[0]
        bind.exec_driver_sql('CREATE OR REPLACE FUNCTION lv.'+name+'('+text+'$$;')
    op.execute('ALTER TABLE lv.proposal DROP CONSTRAINT submission_snapshot_fkey, DROP CONSTRAINT submission_run_fkey, DROP CONSTRAINT proposal_workflow_state')
    op.execute('ALTER TABLE lv.proposal RENAME CONSTRAINT proposal_downgrade_drafts_only TO proposal_state_check')
    op.execute('ALTER TABLE lv.proposal DROP COLUMN snapshot_id, DROP COLUMN latest_simulation_id, DROP COLUMN submitted_by')
    for table in reversed(TABLES):op.execute('DROP TABLE lv.'+table)
