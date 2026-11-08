"""Account read-set versions: configuration, bindings and balance projections invalidate dependencies.

2A §9.1 and §10.3; 2B ReadSetEntry ACCOUNT. Metadata only; no new posting or public mutation path.
Every account UPDATE, including a no-op, advances its version. Role-binding mutations touch old/new accounts;
projection mutations touch their account. All changes share the caller's transaction and existing FORCE RLS.
"""
from alembic import op
revision = '0012_account_versions'
down_revision = '0011_simulation_runs'
branch_labels = None
depends_on = None

DDL = r"""
ALTER TABLE lv.account ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version > 0);
CREATE FUNCTION lv.guard_account_version() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF TG_OP = 'INSERT' THEN
  IF NEW.version IS DISTINCT FROM 1 THEN
   RAISE EXCEPTION 'account version starts at one' USING ERRCODE = '23514';
  END IF;
 ELSE
  IF NEW.version IS DISTINCT FROM OLD.version THEN
   RAISE EXCEPTION 'account version is managed by the database' USING ERRCODE = '23514';
  END IF;
  NEW.version := OLD.version + 1;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER account_version_guard BEFORE INSERT OR UPDATE ON lv.account
 FOR EACH ROW EXECUTE FUNCTION lv.guard_account_version();

-- Invoker rights; no grants or tenant scope are widened. The account row's own trigger does the increment.
-- Updating name to itself preserves interpretation, normal-side derivation and every financial field.
CREATE FUNCTION lv.touch_account_dependencies() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
DECLARE a uuid; ids uuid[]; t uuid; l uuid;
BEGIN
 IF TG_OP = 'INSERT' THEN
  ids := ARRAY[NEW.account_id]; t := NEW.tenant_id; l := NEW.ledger_id;
 ELSIF TG_OP = 'DELETE' THEN
  ids := ARRAY[OLD.account_id]; t := OLD.tenant_id; l := OLD.ledger_id;
 ELSE
  ids := ARRAY[OLD.account_id, NEW.account_id]; t := NEW.tenant_id; l := NEW.ledger_id;
 END IF;
 FOR a IN SELECT id FROM lv.account WHERE tenant_id = t AND ledger_id = l AND id = ANY(ids) ORDER BY id FOR UPDATE LOOP
  UPDATE lv.account SET name = name WHERE tenant_id = t AND ledger_id = l AND id = a;
 END LOOP;
 RETURN NULL;
END $$;
CREATE TRIGGER binding_account_version AFTER INSERT OR UPDATE OR DELETE ON lv.account_role_binding
 FOR EACH ROW EXECUTE FUNCTION lv.touch_account_dependencies();
CREATE TRIGGER balance_account_version AFTER INSERT OR UPDATE OR DELETE ON lv.account_balance_current
 FOR EACH ROW EXECUTE FUNCTION lv.touch_account_dependencies();
REVOKE ALL ON FUNCTION lv.guard_account_version(), lv.touch_account_dependencies() FROM PUBLIC;
"""

def upgrade():
    op.get_bind().exec_driver_sql(DDL)


def downgrade():
    op.get_bind().exec_driver_sql('''
DROP TRIGGER balance_account_version ON lv.account_balance_current;
DROP TRIGGER binding_account_version ON lv.account_role_binding;
DROP TRIGGER account_version_guard ON lv.account;
DROP FUNCTION lv.touch_account_dependencies();
DROP FUNCTION lv.guard_account_version();
ALTER TABLE lv.account DROP COLUMN version;
''')
