"""Identity lifecycle (ADR-027, Stage 2B §5.2 and Family 1): email verification, password reset, invitations, member and role
administration with segregation rules, re-authentication, session revocation and the tenant audit-log read."""
from pathlib import Path
from alembic import op

revision = '0036_identity_lifecycle'
down_revision = '0035_provider_connections'
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0036_identity_lifecycle.sql').read_text())


def downgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0036_identity_lifecycle_down.sql').read_text())
