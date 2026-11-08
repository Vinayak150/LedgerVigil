from pathlib import Path
from alembic import op
revision='0025_export_jobs'
down_revision='0024_proof_export_authority'
branch_labels=None
depends_on=None

def upgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0025_export_jobs.sql').read_text())
def downgrade():
 op.execute('ALTER TABLE lv.native_export_job ADD CONSTRAINT no_export_job_downgrade CHECK(false)')
 for signature in ['native_export_api(bytea,text,uuid,text,text,text)','native_export_due(integer)','native_export_dispatch(uuid,uuid)','native_export_claim(uuid,uuid,text)','native_export_snapshot(uuid,uuid,text,bigint)','native_export_finish(uuid,uuid,text,bigint,bytea,text)','export_session(bytea,text,text)']:op.execute('DROP FUNCTION lv.'+signature)
 op.execute('DROP TABLE lv.native_export_job')
 op.execute('DROP FUNCTION lv.validate_native_export_job()')
 op.execute('DROP FUNCTION lv.guard_native_export_job()')
