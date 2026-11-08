"""Contracted proposal discussion; no decision, state transition or financial write."""
from pathlib import Path
from alembic import op
revision='0017_proposal_comments'
down_revision='0016_source_registry'
branch_labels=None
depends_on=None

def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0017_proposal_comments.sql').read_text())

def downgrade():
    # Retained discussion must not disappear in a downgrade.
    op.execute('ALTER TABLE lv.proposal_comment ADD CONSTRAINT empty_for_downgrade CHECK(false)')
    op.execute('DROP FUNCTION lv.add_proposal_comment(bytea,uuid,text,text,text,text,jsonb,text,text)')
    op.execute('DROP FUNCTION lv.comment_json(lv.proposal_comment)')
    op.execute('DROP TABLE lv.proposal_comment')
    op.execute('DROP FUNCTION lv.validate_proposal_comment()')
    op.execute("DELETE FROM lv.api_idempotency WHERE route='/api/v1/proposals/{proposal_id}/comments'")
