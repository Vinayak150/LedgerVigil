"""RG-05: runtime configuration DML removal and audited administrator account editing."""
from pathlib import Path
from alembic import op
revision='0023_configuration_boundary'
down_revision='0022_approved_withdrawal'
branch_labels=None
depends_on=None

def upgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0023_configuration_boundary.sql').read_text())
def downgrade():
 op.execute('ALTER TABLE lv.configuration_change ADD CONSTRAINT downgrade_empty CHECK(false)')
 op.execute('DROP TRIGGER account_configuration_complete ON lv.account')
 op.execute('DROP FUNCTION lv.edit_account(bytea,uuid,text,jsonb,jsonb,text,text)')
 op.execute('DROP TABLE lv.configuration_change')
 op.execute('DROP FUNCTION lv.validate_configuration_change(),lv.validate_account_configuration()')
 op.execute('DROP FUNCTION lv.configuration_account_json(lv.account)')
 op.execute('GRANT INSERT,UPDATE ON lv.ledger,lv.account,lv.account_role_binding,lv.party TO lv_app')
 op.execute('GRANT INSERT ON lv.fiscal_period TO lv_app')
