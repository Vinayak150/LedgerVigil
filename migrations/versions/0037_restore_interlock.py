"""Restore interlock: financial writes are refused on a restored (unverified) database until recovery verification is recorded."""
from pathlib import Path
from alembic import op

revision = '0037_restore_interlock'
down_revision = '0036_identity_lifecycle'
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0037_restore_interlock.sql').read_text())


def downgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0037_restore_interlock_down.sql').read_text())
