from pathlib import Path
from alembic import op
revision='0029_account_patch'
down_revision='0028_workflow_lock_order'
branch_labels=None
depends_on=None
def upgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0029_account_patch.sql').read_text())
def downgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0029_account_patch_down.sql').read_text())
