"""QuickBooks Online SANDBOX read-only connection: OAuth state, sealed credentials, provider jobs and the raw replica
(PROPOSED PROV-1..PROV-8, docs/QUICKBOOKS_SANDBOX.md). No provider write. Downgrade is refused while any connection exists.
"""
from pathlib import Path
from alembic import op

revision = '0035_provider_connections'
down_revision = '0034_detection_review_filters'
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0035_provider_connections.sql').read_text())


def downgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0035_provider_connections_down.sql').read_text())
