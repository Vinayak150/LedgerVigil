-- 0037 restore interlock (Stage 2C §4.3 condition 5; release blocker OP-15). A logical restore always lands in a NEW database
-- (a different pg_database oid). The binding below records which database financial writes belong to; on any other database
-- (a restored copy), runtime logins (lv_app, lv_worker, lv_seed, lv_ingest) cannot write ledger state, approvals, executions,
-- period closes, accounts or ledgers until recovery verification passes and an operator records a new binding with the
-- verification report's SHA-256 (lv.recovery_bind, owner only). Reads and diagnostics keep working. Migrations (lv_migrator)
-- are not blocked, so a restored copy can be upgraded and verified. Physical (file-level) restores keep the oid and are NOT
-- detected; deploy/RUNBOOK.md requires logical restores into a new database.
CREATE TABLE lv.database_binding(
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 database_oid oid NOT NULL,
 database_name text NOT NULL,
 bound_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 reason text NOT NULL CHECK (reason IN ('INSTALLED','RECOVERY_VERIFIED')),
 report_sha256 char(64) CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
 CHECK ((reason='RECOVERY_VERIFIED')=(report_sha256 IS NOT NULL))
);
CREATE TABLE lv.database_binding_history(
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 database_oid oid NOT NULL, database_name text NOT NULL, bound_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 reason text NOT NULL, report_sha256 char(64), previous_oid oid, session_role text NOT NULL DEFAULT session_user
);
ALTER TABLE lv.database_binding ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv.database_binding FORCE ROW LEVEL SECURITY;
ALTER TABLE lv.database_binding_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv.database_binding_history FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON lv.database_binding TO lv_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_only ON lv.database_binding_history TO lv_owner USING (true) WITH CHECK (true);
REVOKE ALL ON lv.database_binding, lv.database_binding_history FROM PUBLIC, lv_app, lv_worker, lv_seed, lv_ingest;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.database_binding_history FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
INSERT INTO lv.database_binding(database_oid, database_name, reason) SELECT oid, datname, 'INSTALLED' FROM pg_database WHERE datname = current_database();
INSERT INTO lv.database_binding_history(database_oid, database_name, reason) SELECT oid, datname, 'INSTALLED' FROM pg_database WHERE datname = current_database();

-- Diagnostics (granted to the runtime roles): BOUND, or RESTORED_UNVERIFIED with no other detail.
CREATE FUNCTION lv.recovery_state() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT CASE WHEN EXISTS(SELECT 1 FROM lv.database_binding b JOIN pg_database d ON d.oid=b.database_oid WHERE d.datname=current_database())
  THEN 'BOUND' ELSE 'RESTORED_UNVERIFIED' END
$$;

CREATE FUNCTION lv.guard_restored_writes() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF session_user IN ('lv_app','lv_worker','lv_seed','lv_ingest') AND lv.recovery_state()<>'BOUND' THEN
  RAISE EXCEPTION 'financial writes are refused on a restored database until recovery verification is recorded'
   USING ERRCODE='LV503', HINT='run the recovery verification and record its report with lv.recovery_bind (deploy/RUNBOOK.md)';
 END IF;
 RETURN NULL;
END $$;

-- Owner only (no grant): record that this database passed recovery verification. Requires a verification report digest.
CREATE FUNCTION lv.recovery_bind(p_report_sha256 text) RETURNS text LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE prev oid; here oid;
BEGIN
 IF p_report_sha256 IS NULL OR p_report_sha256 !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'a verification report SHA-256 is required' USING ERRCODE='22023'; END IF;
 SELECT oid INTO here FROM pg_database WHERE datname=current_database();
 SELECT database_oid INTO prev FROM lv.database_binding FOR UPDATE;
 IF prev = here THEN RETURN 'ALREADY_BOUND'; END IF;
 DELETE FROM lv.database_binding;
 INSERT INTO lv.database_binding(database_oid,database_name,reason,report_sha256) VALUES(here,current_database(),'RECOVERY_VERIFIED',p_report_sha256);
 INSERT INTO lv.database_binding_history(database_oid,database_name,reason,report_sha256,previous_oid) VALUES(here,current_database(),'RECOVERY_VERIFIED',p_report_sha256,prev);
 RETURN 'BOUND';
END $$;
REVOKE ALL ON FUNCTION lv.recovery_state(), lv.guard_restored_writes(), lv.recovery_bind(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.recovery_state() TO lv_app, lv_worker;

CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.commit_batch FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.document FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.document_version FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.document_current FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.journal_entry FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.journal_line FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.ledger_event FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.ledger_counter FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.account_balance_current FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.party_balance_current FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.payment_current FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.settlement_current FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.settlement_inclusion FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.settlement_inclusion_current FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.fiscal_period FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.native_execution FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.proposal_decision FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.proposal_override FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.account FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
CREATE TRIGGER restore_interlock BEFORE INSERT OR UPDATE OR DELETE ON lv.ledger FOR EACH STATEMENT EXECUTE FUNCTION lv.guard_restored_writes();
