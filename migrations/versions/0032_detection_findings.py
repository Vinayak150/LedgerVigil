"""First detection slice: statement-to-ledger settlement reconciliation findings and reviewer dispositions (PROPOSED DET-1..DET-8).

Deterministic baseline only; no model, no financial write. Migrations 0001-0031 are unchanged. Downgrade is refused while any
detection row exists in ANY tenant (validating CHECK constraints ignore RLS), so retained findings and reviewer history cannot
silently disappear; an empty downgrade drops exactly what this revision created.
"""
from pathlib import Path
from alembic import op

revision = '0032_detection_findings'
down_revision = '0031_semantic_revalidation'
branch_labels = None
depends_on = None

FUNCTIONS = ('lv.detection_dispose(bytea, uuid, text, text, text, text, text, text, jsonb, text, text)',
             'lv.detection_record(bytea, uuid, text, text, text, text, jsonb, jsonb, text, text)',
             'lv.detection_begin(bytea, uuid, text, text, jsonb, jsonb, text, text)',
             'lv.detection_finding_json(lv.detection_finding)', 'lv.detection_run_json(lv.detection_run)',
             'lv.detection_facts(jsonb, bigint)', 'lv.detection_expected(jsonb)', 'lv.detection_config()',
             'lv.detection_fingerprint(jsonb)', 'lv.detection_inputs(uuid, uuid, uuid)')
TABLES = ('detection_disposition', 'detection_review', 'detection_run_finding', 'detection_finding', 'detection_run')


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1] / 'sql/0032_detection_findings.sql').read_text())


def downgrade():
    # Refuse while any tenant has a run (every finding, review and disposition belongs to a run).
    op.execute('ALTER TABLE lv.detection_run ADD CONSTRAINT no_detection_downgrade CHECK (false)')
    for signature in FUNCTIONS:
        op.execute(f'DROP FUNCTION {signature}')
    for table in TABLES:
        op.execute(f'DROP TABLE lv.{table}')
    for signature in ('lv.validate_detection_run()', 'lv.validate_detection_finding()', 'lv.validate_detection_disposition()',
                      'lv.guard_detection_review()'):
        op.execute(f'DROP FUNCTION {signature}')
    op.execute("DELETE FROM lv.api_idempotency WHERE route IN ('/api/v1/ledgers/{ledger_id}/detection-runs', "
               "'/api/v1/detection-findings/{finding_id}/dispositions')")
