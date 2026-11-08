from pathlib import Path
from alembic import op
revision='0030_override_independence'
down_revision='0029_account_patch'
branch_labels=None
depends_on=None
def upgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0030_override_independence.sql').read_text())
def downgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0030_override_independence_down.sql').read_text())
