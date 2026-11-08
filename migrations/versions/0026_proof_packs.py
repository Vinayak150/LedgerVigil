from pathlib import Path
from alembic import op
revision='0026_proof_packs'
down_revision='0025_export_jobs'
branch_labels=None
depends_on=None

def upgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0026_proof_packs.sql').read_text())
def downgrade():
 op.execute('ALTER TABLE lv.proof_pack ADD CONSTRAINT no_pack_downgrade CHECK(false)')
 op.execute('DROP TRIGGER proof_pack_requested ON lv.proposal')
 op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0026_predecessors.sql').read_text())
 op.execute('ALTER TABLE lv.native_export_job DROP CONSTRAINT native_export_format,DROP CONSTRAINT export_subject,DROP CONSTRAINT pack_export_reference,DROP COLUMN proof_pack_id,ALTER COLUMN execution_id SET NOT NULL')
 op.execute("ALTER TABLE lv.native_export_job ADD CONSTRAINT native_export_job_format_check CHECK(format='JSON')")
 for signature in ['pack_status(bytea,uuid,text,text)','pack_anchor_preflight(uuid,bigint,text)','check_pack_prefix(uuid,bigint,text)','auto_proof_pack()','pack_request(bytea,uuid,integer,text,text,text)','pack_export_request(bytea,uuid,text,text,text)','pack_chain_check(bytea,bigint,text)','pack_fail(uuid,uuid,text,bigint,text)','pack_api(bytea,text,uuid,integer,text,text)','enqueue_proof_pack(lv.proposal)','pack_due(integer)','pack_dispatch(uuid,uuid)','pack_claim(uuid,uuid,text)','pack_snapshot(uuid,uuid,text,bigint)','pack_seal(uuid,uuid,text,bigint,text)','pack_anchor(uuid,uuid,text,bigint,text,bytea)']:op.execute('DROP FUNCTION lv.'+signature)
 op.execute('DROP TABLE lv.proof_pack_request,lv.proof_pack,lv.proof_pack_chain')
 op.execute('DROP FUNCTION lv.guard_proof_pack(),lv.validate_proof_pack()')
 op.execute('GRANT EXECUTE ON FUNCTION lv.authorize_proof_export(bytea,uuid,text,text,text,text,text) TO lv_app')
