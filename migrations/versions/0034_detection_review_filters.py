"""Indexes for the bounded detection-finding filters used by the review UI (PROPOSED DET-11). Indexes only."""
from pathlib import Path
from alembic import op

revision = '0034_detection_review_filters'
down_revision = '0033_detection_overlap_dates'
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0034_detection_review_filters.sql').read_text())


def downgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0034_detection_review_filters_down.sql').read_text())
