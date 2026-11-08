"""Atomic, developer-only SANDBOX manual-journal seeds.

Stage 2A §§3–4, 8, 11–12, 19.1–19.2. No BOOKS/execution authorization.
"""
from pathlib import Path
from alembic import op
revision='0002_posting'
down_revision='0001_configuration'
branch_labels=None
depends_on=None
TABLES=('service_principal','ledger_counter','commit_batch','document','document_version','document_current','journal_entry','journal_line','ledger_event','account_balance_current','party_balance_current')
HISTORY=('commit_batch','document','document_version','journal_entry','journal_line','ledger_event')

def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).parents[1]/'sql/0002_posting.sql').read_text())
    for name in TABLES:
        op.execute(f'ALTER TABLE lv.{name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE lv.{name} FORCE ROW LEVEL SECURITY')
        p="tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid"
        op.execute(f'CREATE POLICY tenant_scope ON lv.{name} USING ({p}) WITH CHECK ({p})')
    for name in HISTORY:
        op.execute(f'CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.{name} FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change()')
    for name in ('document','document_version','journal_entry','journal_line','ledger_event'):
        op.execute(f'CREATE TRIGGER fresh_batch BEFORE INSERT ON lv.{name} FOR EACH ROW EXECUTE FUNCTION lv.require_fresh_batch()')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC')
    op.execute('GRANT USAGE ON SCHEMA lv TO lv_seed')
    op.execute('GRANT SELECT ON ALL TABLES IN SCHEMA lv TO lv_app,lv_seed')
    op.execute('GRANT EXECUTE ON FUNCTION lv.seed_manual_journal(uuid,jsonb) TO lv_seed')

def downgrade():
    for table,trigger in [('account','posted_account_guard'),('ledger','posted_ledger_guard'),('account_role_binding','posted_binding_guard')]:
        op.execute(f'DROP TRIGGER {trigger} ON lv.{table}')
    for name in ('party_balance_current','account_balance_current','ledger_event','journal_line','journal_entry','document_current','document_version','document','commit_batch','ledger_counter','service_principal'):
        op.execute(f'DROP TABLE lv.{name}')
    for signature in ('seed_manual_journal(uuid,jsonb)','validate_seed_batch()','validate_entry_balance()','guard_posting_entry()','guard_posting_line()','guard_seed_batch()','require_fresh_batch()','reject_history_change()','guard_posted_configuration()','guard_posted_binding()','canonical_seed_json(jsonb)','parse_line_amount(text,smallint)'):
        op.execute(f'DROP FUNCTION lv.{signature}')
    for name in ('commit_kind','entry_kind','document_type','provenance','principal_purpose'):
        op.execute(f'DROP TYPE lv.{name}')
