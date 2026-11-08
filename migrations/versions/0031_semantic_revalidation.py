from pathlib import Path
from alembic import op
revision='0031_semantic_revalidation'
down_revision='0030_override_independence'
branch_labels=None
depends_on=None
def upgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0031_semantic_revalidation.sql').read_text())
def downgrade():op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0031_semantic_revalidation_down.sql').read_text())
