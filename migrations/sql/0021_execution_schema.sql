ALTER TYPE lv.commit_kind ADD VALUE IF NOT EXISTS 'EXECUTION';
ALTER TABLE lv.document_version ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE lv.document_version ADD COLUMN created_by_user uuid REFERENCES lv_identity.app_user(id);
ALTER TABLE lv.document_version ADD CONSTRAINT document_version_actor CHECK((created_by IS NULL)<>(created_by_user IS NULL));
ALTER TABLE lv.document_version ADD CONSTRAINT document_version_origin_actor CHECK((origin::text='EXECUTION')=(created_by_user IS NOT NULL));
ALTER TABLE lv.journal_entry ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE lv.journal_entry ADD COLUMN created_by_user uuid REFERENCES lv_identity.app_user(id);
ALTER TABLE lv.journal_entry ADD CONSTRAINT journal_entry_actor CHECK((created_by IS NULL)<>(created_by_user IS NULL));
CREATE TABLE lv.native_execution_check(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,revision integer NOT NULL,
 simulation_id uuid NOT NULL,observed_version integer NOT NULL,resulting_version integer NOT NULL CHECK(resulting_version=observed_version+1),
 checked_by uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 report jsonb NOT NULL CHECK(report->>'status' IN ('FRESH','STALE')),idempotency_key text NOT NULL,request_hash text NOT NULL,request_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,proposal_id,resulting_version),UNIQUE(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 FOREIGN KEY(tenant_id,ledger_id,simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id)
);
CREATE TABLE lv.native_execution(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,revision integer NOT NULL,
 simulation_id uuid NOT NULL,check_id uuid NOT NULL,approval_id uuid NOT NULL,
 executed_by uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 observed_version integer NOT NULL,resulting_version integer NOT NULL CHECK(resulting_version=observed_version+1),
 state text NOT NULL CHECK(state IN ('BUILDING','RECONCILED')),commit_seq bigint,settlement_document_id uuid,entry_id uuid,
 predicted_hash text,derived_hash text,comparison jsonb,tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,proposal_id,revision),UNIQUE(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 FOREIGN KEY(tenant_id,ledger_id,simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,check_id) REFERENCES lv.native_execution_check(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,approval_id) REFERENCES lv.proposal_decision(tenant_id,id),
 FOREIGN KEY(tenant_id,ledger_id,commit_seq) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq),
 FOREIGN KEY(tenant_id,ledger_id,settlement_document_id) REFERENCES lv.document(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,entry_id) REFERENCES lv.journal_entry(tenant_id,ledger_id,id),
 CHECK(state<>'RECONCILED' OR (commit_seq IS NOT NULL AND settlement_document_id IS NOT NULL AND entry_id IS NOT NULL AND predicted_hash IS NOT NULL AND derived_hash IS NOT NULL AND predicted_hash ~ '^[0-9a-f]{64}$' AND derived_hash=predicted_hash AND comparison IS NOT NULL))
);
ALTER TABLE lv.native_execution ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.native_execution FORCE ROW LEVEL SECURITY;
ALTER TABLE lv.native_execution_check ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.native_execution_check FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.native_execution USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE POLICY tenant_scope ON lv.native_execution_check USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.native_execution,lv.native_execution_check FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;GRANT SELECT ON lv.native_execution,lv.native_execution_check TO lv_app;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.native_execution_check FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE FUNCTION lv.guard_execution_history() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.state<>'BUILDING' OR OLD.tx_id<>pg_current_xact_id() OR NEW.tx_id<>OLD.tx_id
 OR (NEW.tenant_id,NEW.ledger_id,NEW.id,NEW.proposal_id,NEW.revision,NEW.simulation_id,NEW.check_id,NEW.approval_id,NEW.executed_by,NEW.session_id,NEW.observed_version,NEW.resulting_version,NEW.created_at)
 IS DISTINCT FROM (OLD.tenant_id,OLD.ledger_id,OLD.id,OLD.proposal_id,OLD.revision,OLD.simulation_id,OLD.check_id,OLD.approval_id,OLD.executed_by,OLD.session_id,OLD.observed_version,OLD.resulting_version,OLD.created_at)
 THEN RAISE EXCEPTION 'execution history is immutable' USING ERRCODE='23514'; END IF;RETURN NEW;
END $$;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.native_execution FOR EACH ROW EXECUTE FUNCTION lv.guard_execution_history();
REVOKE ALL ON FUNCTION lv.guard_execution_history() FROM PUBLIC;
ALTER TABLE lv.proposal DROP CONSTRAINT proposal_workflow_state;
ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','APPROVED','REJECTED','STALE','WITHDRAWN','EXECUTED'));
ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor;
ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale','resimulate','revalidate','overrides','approve','reject','execute'));
ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw','resimulate','revalidate','overrides','approve','reject','execute') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL));
ALTER TABLE lv.staged_workflow DROP CONSTRAINT staged_workflow_status_check,DROP CONSTRAINT staged_workflow_check;
ALTER TABLE lv.staged_workflow ADD CONSTRAINT staged_workflow_status_check CHECK(status IN ('VALID','RESERVED','POSTED'));
ALTER TABLE lv.staged_workflow ADD CONSTRAINT staged_workflow_check CHECK((status IN ('RESERVED','POSTED') AND proposal_id IS NOT NULL AND revision IS NOT NULL) OR (status='VALID' AND proposal_id IS NULL AND revision IS NULL));
ALTER TABLE lv.reservation_event DROP CONSTRAINT reservation_event_event_check;
ALTER TABLE lv.reservation_event ADD CONSTRAINT reservation_event_event_check CHECK(event IN ('RESERVED','RELEASED','POSTED'));
DROP TRIGGER seed_guard ON lv.commit_batch;
CREATE TRIGGER seed_guard BEFORE INSERT ON lv.commit_batch FOR EACH ROW WHEN(NEW.kind::text NOT IN ('PERIOD_CLOSE','EXECUTION')) EXECUTE FUNCTION lv.guard_seed_batch();
DROP TRIGGER seed_commit_complete ON lv.commit_batch;
CREATE CONSTRAINT TRIGGER seed_commit_complete AFTER INSERT ON lv.commit_batch DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(NEW.kind::text NOT IN ('PERIOD_CLOSE','EXECUTION')) EXECUTE FUNCTION lv.validate_seed_batch();
-- Native completion notification: durable in the same outbox, never dispatched by
-- the existing maintenance-only relay. No unsupported worker job is invented.
ALTER TABLE lv.outbox ALTER COLUMN work_item_id DROP NOT NULL;
ALTER TABLE lv.outbox ADD COLUMN native_execution_id uuid;
ALTER TABLE lv.outbox ADD COLUMN native_payload jsonb;
ALTER TABLE lv.outbox ADD COLUMN native_tx_id xid8;
ALTER TABLE lv.outbox ADD CONSTRAINT outbox_native_execution FOREIGN KEY(tenant_id,native_execution_id) REFERENCES lv.native_execution(tenant_id,id);
ALTER TABLE lv.outbox ADD CONSTRAINT outbox_subject CHECK(
 (work_item_id IS NOT NULL AND native_execution_id IS NULL AND native_payload IS NULL AND native_tx_id IS NULL)
 OR (work_item_id IS NULL AND native_execution_id IS NOT NULL AND native_payload IS NOT NULL AND native_tx_id IS NOT NULL AND topic='execution' AND status='PENDING'));
CREATE TRIGGER native_notification_immutable BEFORE UPDATE OR DELETE ON lv.outbox FOR EACH ROW WHEN(OLD.native_execution_id IS NOT NULL) EXECUTE FUNCTION lv.reject_history_change();
