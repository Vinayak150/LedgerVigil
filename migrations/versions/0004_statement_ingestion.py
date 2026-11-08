"""Synthetic processor-statement ingestion: source files, import batches, raw source records and staged
statement documents (Stage 2A §19.2 path A; §11.2 Ingestion group; §19.3 statement evidence).

Path A may write `source_file`, `source_record`, `staged_document` and `import_batch` only, never
documents, versions, journal entries, the ledger sequence or balances. The new `lv_ingest` login therefore
receives INSERT on these ingestion tables and nothing else. It gets no UPDATE, DELETE, TRUNCATE or EXECUTE,
and no grant on any financial table. Rows are immutable. Raw bytes are kept with a database-checked
SHA-256. Amounts are stored exactly as received, as decimal text validated against the ledger's
registered exponent, so no NUMERIC cast can round them.

This milestone accepts only the clearly synthetic fixture format, `ledgervigil.synthetic-processor-statement`
version 1, into NATIVE SANDBOX ledgers. It claims no compatibility with any real processor's files.
Applied migrations 0001–0003 are unchanged. Requires the `lv_ingest` role (`scripts/dev db-prepare`).
"""
from alembic import op

revision = '0004_statement_ingestion'
down_revision = '0003_pin_function_search_path'
branch_labels = None
depends_on = None

MAX_FILE_BYTES = 262144
TABLES = ('source_file', 'import_batch', 'source_record', 'staged_document', 'staged_statement_item')
AMOUNT = r"^[0-9]{1,16}(\.[0-9]{1,4})?$"

DDL = rf"""
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lv_ingest') THEN
  RAISE EXCEPTION 'role lv_ingest is missing: run scripts/dev db-prepare before this migration';
 END IF;
END $$;

CREATE TYPE lv.import_status AS ENUM ('STAGED', 'REJECTED', 'DUPLICATE');
CREATE TYPE lv.staged_document_type AS ENUM ('SETTLEMENT_STATEMENT');
CREATE TYPE lv.statement_item_kind AS ENUM ('PAYMENT', 'REFUND');

-- Raw file. sha256 unique per ledger (2A §11.2). Content kept byte-for-byte when within the size bound;
-- an oversized file is recorded by hash and size only, then rejected.
CREATE TABLE lv.source_file (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
 byte_size integer NOT NULL CHECK (byte_size >= 0),
 content bytea,
 media_type varchar(40) NOT NULL CHECK (media_type = 'application/json'),
 declared_format varchar(80) NOT NULL, declared_version varchar(16) NOT NULL,
 data_classification varchar(16) NOT NULL CHECK (data_classification = 'SYNTHETIC'),
 received_by name NOT NULL DEFAULT session_user,
 received_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id, ledger_id, id),
 UNIQUE (tenant_id, ledger_id, sha256),
 FOREIGN KEY (tenant_id, ledger_id) REFERENCES lv.ledger (tenant_id, id),
 CHECK ((content IS NULL) = (byte_size > {MAX_FILE_BYTES})),
 CHECK (content IS NULL OR (octet_length(content) = byte_size AND encode(sha256(content), 'hex') = sha256))
);

-- One import attempt per file; its outcome is fixed at insert (rows are immutable).
CREATE TABLE lv.import_batch (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 source_file_id uuid NOT NULL,
 parser varchar(80) NOT NULL, parser_version varchar(16) NOT NULL,
 status lv.import_status NOT NULL,
 failure_code varchar(64), failure_detail varchar(500),
 related_staged_document_id uuid,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id, ledger_id, id),
 UNIQUE (tenant_id, ledger_id, source_file_id),
 FOREIGN KEY (tenant_id, ledger_id, source_file_id) REFERENCES lv.source_file (tenant_id, ledger_id, id),
 CHECK ((status = 'STAGED') = (failure_code IS NULL AND related_staged_document_id IS NULL)),
 CHECK (status <> 'DUPLICATE' OR (related_staged_document_id IS NOT NULL AND failure_code = 'DUPLICATE_STATEMENT')),
 CHECK (status <> 'REJECTED' OR failure_code ~ '^[A-Z0-9_]{{1,64}}$')
);

-- Raw source row (2A §11.2): unique (ledger, source_system, source_key, content_hash). Never parsed in place.
CREATE TABLE lv.source_record (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 import_batch_id uuid NOT NULL, record_index integer NOT NULL CHECK (record_index >= 0),
 source_system varchar(80) NOT NULL, source_key varchar(210) NOT NULL,
 content text NOT NULL CHECK (octet_length(content) <= {MAX_FILE_BYTES}),
 content_hash char(64) NOT NULL,
 PRIMARY KEY (tenant_id, ledger_id, id),
 UNIQUE (tenant_id, ledger_id, source_system, source_key, content_hash),
 UNIQUE (tenant_id, ledger_id, id, import_batch_id),
 FOREIGN KEY (tenant_id, ledger_id, import_batch_id) REFERENCES lv.import_batch (tenant_id, ledger_id, id),
 CHECK (content_hash = encode(sha256(convert_to(content, 'UTF8')), 'hex'))
);

-- Parsed, VALID candidate (2A §19.2 "staged_document ... with validation results"). An observation from a
-- source, not an authoritative ledger fact: it is never a document, version or posting.
CREATE TABLE lv.staged_document (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 import_batch_id uuid NOT NULL, source_record_id uuid NOT NULL,
 doc_type lv.staged_document_type NOT NULL,
 validation_status varchar(8) NOT NULL CHECK (validation_status = 'VALID'),
 provenance lv.provenance NOT NULL CHECK (provenance = 'SYNTHETIC'),
 processor_ref varchar(100) NOT NULL, payout_id varchar(100) NOT NULL, payout_date date NOT NULL,
 currency char(3) NOT NULL, currency_exponent smallint NOT NULL,
 gross text NOT NULL, fee text NOT NULL, net text NOT NULL,
 item_count integer NOT NULL CHECK (item_count BETWEEN 1 AND 1000),
 financial_fingerprint char(64) NOT NULL CHECK (financial_fingerprint ~ '^[0-9a-f]{{64}}$'),
 description_untrusted varchar(4096) NOT NULL,
 PRIMARY KEY (tenant_id, ledger_id, id),
 UNIQUE (tenant_id, ledger_id, processor_ref, payout_id),
 UNIQUE (tenant_id, ledger_id, id, currency_exponent),
 FOREIGN KEY (tenant_id, ledger_id, source_record_id, import_batch_id) REFERENCES lv.source_record (tenant_id, ledger_id, id, import_batch_id),
 FOREIGN KEY (tenant_id, ledger_id, currency, currency_exponent) REFERENCES lv.ledger (tenant_id, id, currency, currency_exponent),
 CHECK (gross ~ '{AMOUNT}' AND fee ~ '{AMOUNT}' AND net ~ '{AMOUNT}'),
 CHECK (length(split_part(gross, '.', 2)) <= currency_exponent AND length(split_part(fee, '.', 2)) <= currency_exponent
        AND length(split_part(net, '.', 2)) <= currency_exponent),
 CHECK (gross::numeric > 0 AND gross::numeric = fee::numeric + net::numeric)
);

CREATE TABLE lv.staged_statement_item (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, staged_document_id uuid NOT NULL,
 line_no integer NOT NULL CHECK (line_no >= 1),
 item_ref varchar(100) NOT NULL, kind lv.statement_item_kind NOT NULL,
 amount text NOT NULL, currency_exponent smallint NOT NULL,
 PRIMARY KEY (tenant_id, ledger_id, staged_document_id, line_no),
 UNIQUE (tenant_id, ledger_id, staged_document_id, item_ref),
 FOREIGN KEY (tenant_id, ledger_id, staged_document_id, currency_exponent) REFERENCES lv.staged_document (tenant_id, ledger_id, id, currency_exponent),
 CHECK (amount ~ '{AMOUNT}' AND length(split_part(amount, '.', 2)) <= currency_exponent AND amount::numeric > 0)
);

ALTER TABLE lv.import_batch ADD CONSTRAINT import_batch_related_staged_document_fkey
 FOREIGN KEY (tenant_id, ledger_id, related_staged_document_id) REFERENCES lv.staged_document (tenant_id, ledger_id, id);

-- Synthetic fixtures enter only NATIVE SANDBOX ledgers; provider-connected ledgers accept no uploads (2A §19.2).
CREATE FUNCTION lv.guard_source_file() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$ DECLARE l lv.ledger; BEGIN
 SELECT * INTO l FROM lv.ledger WHERE tenant_id = NEW.tenant_id AND id = NEW.ledger_id;
 IF NOT FOUND OR l.purpose <> 'SANDBOX' OR l.source_kind <> 'NATIVE' THEN
  RAISE EXCEPTION 'synthetic statement ingestion requires a native SANDBOX ledger' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER source_file_scope BEFORE INSERT ON lv.source_file FOR EACH ROW EXECUTE FUNCTION lv.guard_source_file();

-- A staged statement is complete at commit: its items exist, are counted, and sum (payments - refunds) to gross.
CREATE FUNCTION lv.validate_staged_statement() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$ DECLARE n integer; total numeric; BEGIN
 SELECT count(*), coalesce(sum(CASE kind WHEN 'PAYMENT' THEN amount::numeric ELSE -amount::numeric END), 0) INTO n, total
   FROM lv.staged_statement_item WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND staged_document_id = NEW.id;
 IF n <> NEW.item_count OR total <> NEW.gross::numeric THEN
  RAISE EXCEPTION 'staged statement items do not reconcile to its gross' USING ERRCODE = '23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER staged_statement_complete AFTER INSERT ON lv.staged_document
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_staged_statement();

CREATE FUNCTION lv.reject_ingestion_change() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$ BEGIN
 RAISE EXCEPTION 'ingested source facts are immutable' USING ERRCODE = '23514';
END $$;
"""


def upgrade():
    op.get_bind().exec_driver_sql(DDL)
    predicate = "tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid"
    for name in TABLES:
        op.execute(f'ALTER TABLE lv.{name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE lv.{name} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_scope ON lv.{name} USING ({predicate}) WITH CHECK ({predicate})')
        op.execute(f'CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.{name} FOR EACH ROW EXECUTE FUNCTION lv.reject_ingestion_change()')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC')
    op.execute('GRANT USAGE ON SCHEMA lv TO lv_ingest')
    op.execute('GRANT SELECT ON lv.ledger, lv.currency TO lv_ingest')
    op.execute('GRANT SELECT, INSERT ON ' + ', '.join(f'lv.{t}' for t in TABLES) + ' TO lv_ingest')
    op.execute('GRANT SELECT ON ' + ', '.join(f'lv.{t}' for t in TABLES) + ' TO lv_app')


def downgrade():
    for name in TABLES:
        op.execute(f'REVOKE ALL ON lv.{name} FROM lv_ingest, lv_app')
    op.execute('REVOKE ALL ON lv.ledger, lv.currency FROM lv_ingest')
    op.execute('REVOKE ALL ON SCHEMA lv FROM lv_ingest')
    op.execute('ALTER TABLE lv.import_batch DROP CONSTRAINT import_batch_related_staged_document_fkey')
    for name in ('staged_statement_item', 'staged_document', 'source_record', 'import_batch', 'source_file'):
        op.execute(f'DROP TABLE lv.{name}')
    for signature in ('guard_source_file()', 'validate_staged_statement()', 'reject_ingestion_change()'):
        op.execute(f'DROP FUNCTION lv.{signature}')
    for name in ('statement_item_kind', 'staged_document_type', 'import_status'):
        op.execute(f'DROP TYPE lv.{name}')
