"""Add issuer-owned SANDBOX MFA enrollment and challenge state."""
from pathlib import Path

from alembic import op

revision = '0039_mfa'
down_revision = '0038_identity_issuer'
branch_labels = None
depends_on = None


def _run(suffix):
    script = (Path(__file__).parents[1] / 'sql' / f'0039_mfa{suffix}.sql').read_text()
    op.get_bind().exec_driver_sql(script)


def upgrade():
    _run('')


def downgrade():
    _run('_down')
