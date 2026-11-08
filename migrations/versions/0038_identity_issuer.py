"""Separate credential handling from the ordinary application database role."""
from pathlib import Path

from alembic import op

revision = '0038_identity_issuer'
down_revision = '0037_restore_interlock'
branch_labels = None
depends_on = None


def _run(suffix):
    script = (Path(__file__).parents[1] / 'sql' / f'0038_identity_issuer{suffix}.sql').read_text()
    op.get_bind().exec_driver_sql(script)


def upgrade():
    _run('')


def downgrade():
    _run('_down')
