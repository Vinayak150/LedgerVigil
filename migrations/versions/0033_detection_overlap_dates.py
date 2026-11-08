"""Detection 1.1.0: PAYMENT_CLAIMED_BY_OTHER_PAYOUT and PAYOUT_DATE_DIFFERS (PROPOSED DET-9, DET-10; deterministic, advisory).

No new table; migrations 0001-0032 are unchanged. Existing 1.0.0 findings keep their v1 evidence fingerprints. Downgrade is
refused while any 1.1.0 run exists in any tenant; otherwise it restores the exact 0032 function definitions and constraints.
"""
from pathlib import Path
from alembic import op

revision = '0033_detection_overlap_dates'
down_revision = '0032_detection_findings'
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0033_detection_overlap_dates.sql').read_text())


def downgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0033_detection_overlap_dates_down.sql').read_text())
