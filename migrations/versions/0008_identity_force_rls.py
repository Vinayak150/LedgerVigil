"""Make identity owner access explicit under FORCE RLS (Q-10).

Identity resolution precedes tenant selection. Only the migration owner may
access identity rows, through explicit owner-only policies. Runtime roles retain
zero identity table grants and reach scoped checks through the five reviewed
SECURITY DEFINER functions. This grants no runtime privilege or new capability.
"""
from alembic import op
revision='0008_identity_force_rls'
down_revision='0007_identity_sessions'
branch_labels=None
depends_on=None
TABLES=('role_permission','app_user','membership','membership_role','session','login_attempt','audit_event','audit_chain_head')

def upgrade():
    for table in TABLES:
        op.execute(f'CREATE POLICY identity_owner_access ON lv_identity.{table} TO lv_owner USING (true) WITH CHECK (true)')
        op.execute(f'ALTER TABLE lv_identity.{table} FORCE ROW LEVEL SECURITY')

def downgrade():
    for table in TABLES:
        op.execute(f'ALTER TABLE lv_identity.{table} NO FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY identity_owner_access ON lv_identity.{table}')
