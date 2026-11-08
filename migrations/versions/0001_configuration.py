"""Tenant-scoped configuration prerequisites; no financial posting path.

Authority: Stage 2A §§2, 3.1, 11.1–11.4, 12.1, 19.2; ADR-015/020/024.
"""
from alembic import op

revision = '0001_configuration'
down_revision = None
branch_labels = None
depends_on = None

DDL = r"""
CREATE SCHEMA lv AUTHORIZATION lv_owner;
REVOKE ALL ON SCHEMA lv FROM PUBLIC;
CREATE TYPE lv.ledger_purpose AS ENUM ('BOOKS', 'SANDBOX');
CREATE TYPE lv.source_kind AS ENUM ('NATIVE', 'PROVIDER_CONNECTED');
CREATE TYPE lv.account_type AS ENUM ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE');
CREATE TYPE lv.account_role AS ENUM ('AR_CONTROL', 'REVENUE_DEFAULT', 'SALES_RETURNS', 'PROCESSOR_CLEARING', 'BANK_OPERATING', 'PROCESSOR_FEE_EXPENSE', 'OPENING_BALANCE_EQUITY');
CREATE TYPE lv.period_status AS ENUM ('OPEN', 'CLOSED');

CREATE TABLE lv.currency (
 code char(3) PRIMARY KEY CHECK (code ~ '^[A-Z]{3}$'),
 exponent smallint NOT NULL CHECK (exponent BETWEEN 0 AND 4),
 UNIQUE (code, exponent)
);
INSERT INTO lv.currency VALUES ('INR',2),('USD',2),('GBP',2);
CREATE TABLE lv.tenant (
 tenant_id uuid PRIMARY KEY DEFAULT uuidv7(),
 name varchar(120) NOT NULL CHECK (length(btrim(name)) > 0),
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);
CREATE TABLE lv.ledger (
 tenant_id uuid NOT NULL REFERENCES lv.tenant(tenant_id),
 id uuid NOT NULL DEFAULT uuidv7(),
 name varchar(120) NOT NULL CHECK (length(btrim(name)) > 0),
 purpose lv.ledger_purpose NOT NULL,
 source_kind lv.source_kind NOT NULL,
 currency char(3) NOT NULL,
 currency_exponent smallint NOT NULL,
 profile_version varchar(32) NOT NULL DEFAULT 'sp1/1.0' CHECK (profile_version = 'sp1/1.0'),
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id,id),
 UNIQUE (tenant_id,id,currency,currency_exponent),
 FOREIGN KEY (currency,currency_exponent) REFERENCES lv.currency(code,exponent)
);
CREATE TABLE lv.account (
 tenant_id uuid NOT NULL,
 ledger_id uuid NOT NULL,
 id uuid NOT NULL DEFAULT uuidv7(),
 code varchar(40) NOT NULL CHECK (length(btrim(code)) > 0),
 name varchar(120) NOT NULL CHECK (length(btrim(name)) > 0),
 type lv.account_type NOT NULL,
 subtype varchar(80),
 is_contra boolean NOT NULL DEFAULT false,
 normal_side char(1) GENERATED ALWAYS AS
  (CASE WHEN (type IN ('ASSET','EXPENSE')) <> is_contra THEN 'D' ELSE 'C' END) STORED,
 requires_party boolean NOT NULL DEFAULT false,
 currency char(3) NOT NULL,
 currency_exponent smallint NOT NULL,
 active boolean NOT NULL DEFAULT true,
 parent_id uuid,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id,ledger_id,id),
 UNIQUE (tenant_id,ledger_id,code),
 UNIQUE (tenant_id,ledger_id,id,requires_party),
 UNIQUE (tenant_id,ledger_id,id,type,is_contra,requires_party,active),
 FOREIGN KEY (tenant_id,ledger_id,currency,currency_exponent)
  REFERENCES lv.ledger(tenant_id,id,currency,currency_exponent),
 FOREIGN KEY (tenant_id,ledger_id,parent_id) REFERENCES lv.account(tenant_id,ledger_id,id),
 CHECK (parent_id IS DISTINCT FROM id)
);
CREATE TABLE lv.account_role_binding (
 tenant_id uuid NOT NULL,
 ledger_id uuid NOT NULL,
 role lv.account_role NOT NULL,
 account_id uuid NOT NULL,
 account_type lv.account_type NOT NULL,
 account_is_contra boolean NOT NULL,
 account_requires_party boolean NOT NULL,
 account_active boolean NOT NULL CHECK (account_active),
 PRIMARY KEY (tenant_id,ledger_id,role),
 FOREIGN KEY (tenant_id,ledger_id,account_id,account_type,account_is_contra,account_requires_party,account_active)
  REFERENCES lv.account(tenant_id,ledger_id,id,type,is_contra,requires_party,active),
 CHECK (
  (role = 'AR_CONTROL' AND account_type = 'ASSET' AND NOT account_is_contra AND account_requires_party) OR
  (role = 'REVENUE_DEFAULT' AND account_type = 'REVENUE' AND NOT account_is_contra) OR
  (role = 'SALES_RETURNS' AND account_type = 'REVENUE' AND account_is_contra) OR
  (role IN ('PROCESSOR_CLEARING','BANK_OPERATING') AND account_type = 'ASSET' AND NOT account_is_contra) OR
  (role = 'PROCESSOR_FEE_EXPENSE' AND account_type = 'EXPENSE' AND NOT account_is_contra) OR
  (role = 'OPENING_BALANCE_EQUITY' AND account_type = 'EQUITY' AND NOT account_is_contra)
 )
);
CREATE TABLE lv.party (
 tenant_id uuid NOT NULL,
 ledger_id uuid NOT NULL,
 id uuid NOT NULL DEFAULT uuidv7(),
 name varchar(120) NOT NULL CHECK (length(btrim(name)) > 0),
 external_ref varchar(256),
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id,ledger_id,id),
 UNIQUE (tenant_id,ledger_id,external_ref),
 FOREIGN KEY (tenant_id,ledger_id) REFERENCES lv.ledger(tenant_id,id)
);
CREATE TABLE lv.fiscal_period (
 tenant_id uuid NOT NULL,
 ledger_id uuid NOT NULL,
 id uuid NOT NULL DEFAULT uuidv7(),
 start_date date NOT NULL,
 end_date date NOT NULL,
 status lv.period_status NOT NULL DEFAULT 'OPEN',
 PRIMARY KEY (tenant_id,ledger_id,id),
 FOREIGN KEY (tenant_id,ledger_id) REFERENCES lv.ledger(tenant_id,id),
 CONSTRAINT calendar_month CHECK (
  isfinite(start_date) AND isfinite(end_date) AND extract(day FROM start_date) = 1
  AND end_date = (start_date + interval '1 month')::date
 ),
 CONSTRAINT no_overlapping_periods EXCLUDE USING gist
  (tenant_id WITH =, ledger_id WITH =, daterange(start_date,end_date,'[)') WITH &&)
);

-- No SECURITY DEFINER: these functions never gain privileges or bypass RLS.
CREATE FUNCTION lv.protect_identity() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN
  RAISE EXCEPTION 'configuration deletion is not supported' USING ERRCODE='23514';
 END IF;
 IF (to_jsonb(NEW)->'tenant_id', to_jsonb(NEW)->'ledger_id', to_jsonb(NEW)->'id') IS DISTINCT FROM
    (to_jsonb(OLD)->'tenant_id', to_jsonb(OLD)->'ledger_id', to_jsonb(OLD)->'id') THEN
  RAISE EXCEPTION 'row scope and identity are immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION lv.protect_ledger_purpose() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog AS $$
BEGIN
 IF NEW.purpose IS DISTINCT FROM OLD.purpose THEN
  RAISE EXCEPTION 'ledger purpose is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER ledger_purpose_immutable BEFORE UPDATE ON lv.ledger
 FOR EACH ROW EXECUTE FUNCTION lv.protect_ledger_purpose();
CREATE FUNCTION lv.protect_period() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog AS $$
BEGIN
 IF (NEW.start_date,NEW.end_date) IS DISTINCT FROM (OLD.start_date,OLD.end_date)
    OR (OLD.status = 'CLOSED' AND NEW.status <> 'CLOSED') THEN
  RAISE EXCEPTION 'period dates are immutable and closed periods cannot reopen' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER period_guard BEFORE UPDATE ON lv.fiscal_period
 FOR EACH ROW EXECUTE FUNCTION lv.protect_period();
"""

TABLES = ('tenant','ledger','account','account_role_binding','party','fiscal_period')

def upgrade():
    op.get_bind().exec_driver_sql(DDL)
    for name in TABLES:
        # Names are migration constants, not user input.
        op.execute(f'ALTER TABLE lv.{name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE lv.{name} FORCE ROW LEVEL SECURITY')
        predicate = "tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid"
        op.execute(f'CREATE POLICY tenant_scope ON lv.{name} USING ({predicate}) WITH CHECK ({predicate})')
        op.execute(f'CREATE TRIGGER identity_guard BEFORE UPDATE OR DELETE ON lv.{name} FOR EACH ROW EXECUTE FUNCTION lv.protect_identity()')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC')
    op.execute('GRANT USAGE ON SCHEMA lv TO lv_app')
    op.execute('GRANT SELECT ON ALL TABLES IN SCHEMA lv TO lv_app')
    # No tenant creation, global currency writes, DELETE, TRUNCATE, DDL or posting grants.
    op.execute('GRANT INSERT, UPDATE ON lv.ledger,lv.account,lv.account_role_binding,lv.party,lv.fiscal_period TO lv_app')

def downgrade():
    # Disposable development DBs only; the runner requires an explicit flag.
    # RESTRICT catches dependencies introduced outside the migration chain.
    for name in ('account_role_binding','account','party','fiscal_period','ledger','tenant','currency'):
        op.execute(f'DROP TABLE lv.{name}')
    for name in ('protect_identity','protect_ledger_purpose','protect_period'):
        op.execute(f'DROP FUNCTION lv.{name}()')
    for name in ('ledger_purpose','source_kind','account_type','account_role','period_status'):
        op.execute(f'DROP TYPE lv.{name}')
    op.execute('DROP SCHEMA lv')
