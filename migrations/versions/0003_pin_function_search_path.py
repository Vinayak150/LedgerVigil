"""Pin every lv function's search_path to pg_catalog, pg_temp (review finding R-01).

Authority: ADR-015 and Stage 2A §12.1 (restricted logins never gain owner privileges; the seed
login has EXECUTE on one function only). PostgreSQL searches the session's temporary schema FIRST
for relation and type names unless pg_temp is listed explicitly in search_path. With
`SET search_path = pg_catalog`, a caller holding TEMPORARY could shadow an unqualified type (for
example `uuid`) with a temporary domain whose CHECK runs the caller's own temporary function, which
then executes as lv_owner inside the SECURITY DEFINER posting function. Listing pg_temp last is the
form the PostgreSQL documentation prescribes for SECURITY DEFINER functions. It is applied to every
lv function, because triggers fired by the definer's own inserts also run as lv_owner.

Data and function bodies are unchanged; 0001_configuration and 0002_posting are untouched.
"""
from alembic import op

revision = '0003_pin_function_search_path'
down_revision = '0002_posting'
branch_labels = None
depends_on = None

FUNCTIONS = (
    'protect_identity()', 'protect_ledger_purpose()', 'protect_period()',
    'canonical_seed_json(jsonb)', 'parse_line_amount(text,smallint)', 'reject_history_change()',
    'require_fresh_batch()', 'guard_seed_batch()', 'guard_posting_entry()', 'guard_posting_line()',
    'validate_entry_balance()', 'guard_posted_configuration()', 'seed_manual_journal(uuid,jsonb)',
    'validate_seed_batch()', 'guard_posted_binding()',
)

VERIFY = r"""
DO $$
DECLARE missing text;
BEGIN
 SELECT string_agg(p.oid::regprocedure::text, ', ') INTO missing
   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'lv' AND NOT coalesce(p.proconfig @> ARRAY['search_path=pg_catalog, pg_temp'], false);
 IF missing IS NOT NULL THEN
  RAISE EXCEPTION 'functions without a pinned search_path: %', missing;
 END IF;
END $$;
"""


def upgrade():
    for signature in FUNCTIONS:
        op.execute(f'ALTER FUNCTION lv.{signature} SET search_path = pg_catalog, pg_temp')
    op.execute(VERIFY)


def downgrade():
    for signature in FUNCTIONS:
        op.execute(f'ALTER FUNCTION lv.{signature} SET search_path = pg_catalog')
