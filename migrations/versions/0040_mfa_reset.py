"""Add mandatory reset cooling-off and privileged BOOKS step-up enforcement."""
from pathlib import Path

from alembic import op

revision = '0040_mfa_reset'
down_revision = '0039_mfa'
branch_labels = None
depends_on = None


def _run(suffix):
    script = (Path(__file__).parents[1] / 'sql' / f'0040_mfa_reset{suffix}.sql').read_text()
    op.get_bind().exec_driver_sql(script)


def upgrade():
    _run('')


def downgrade():
    _run('_down')
