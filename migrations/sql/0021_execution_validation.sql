CREATE FUNCTION lv.validate_native_execution() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE e lv.native_execution;p lv.proposal;c lv.native_execution_check;b lv.commit_batch;d lv.proposal_decision;comparison jsonb;
BEGIN
 IF TG_TABLE_NAME='commit_batch' THEN SELECT * INTO e FROM lv.native_execution WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND commit_seq=NEW.seq;
 ELSE SELECT * INTO e FROM lv.native_execution WHERE tenant_id=NEW.tenant_id AND id=NEW.id; END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=e.tenant_id AND id=e.proposal_id;
 SELECT * INTO c FROM lv.native_execution_check WHERE tenant_id=e.tenant_id AND id=e.check_id;
 SELECT * INTO b FROM lv.commit_batch WHERE tenant_id=e.tenant_id AND ledger_id=e.ledger_id AND seq=e.commit_seq;
 SELECT * INTO d FROM lv.proposal_decision WHERE tenant_id=e.tenant_id AND id=e.approval_id;
 IF e.id IS NULL OR e.state<>'RECONCILED' OR e.tx_id<>pg_current_xact_id() OR p.state<>'EXECUTED' OR p.version<>e.resulting_version OR p.revision<>e.revision OR p.latest_simulation_id<>e.simulation_id
 OR lv.executor_blocker(p,e.executed_by) IS NOT NULL OR b.kind::text<>'EXECUTION' OR b.created_by IS NOT NULL OR b.created_by_user<>e.executed_by OR b.created_xid<>pg_current_xact_id()::text
 OR c.id IS NULL OR c.proposal_id<>e.proposal_id OR c.revision<>e.revision OR c.simulation_id<>e.simulation_id OR c.observed_version<>e.observed_version OR c.resulting_version<>e.resulting_version OR c.checked_by<>e.executed_by OR c.session_id<>e.session_id OR c.tx_id<>pg_current_xact_id() OR c.report->>'status'<>'FRESH' OR c.report->'changes'<>'[]'::jsonb OR c.report->'pinned_seq' IS DISTINCT FROM c.report->'observed_seq' OR (c.report->>'observed_seq')::bigint+1<>e.commit_seq
 OR d.id IS NULL OR d.kind<>'APPROVED' OR d.proposal_id<>p.id OR d.revision<>p.revision OR d.simulation_id<>e.simulation_id
 OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND status<>'POSTED')
 OR NOT EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND status='POSTED')
 OR NOT EXISTS(SELECT 1 FROM lv.proposal_transition WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND version=p.version AND from_state='APPROVED' AND to_state='EXECUTED' AND action='execute' AND user_id=e.executed_by AND session_id=e.session_id AND tx_id=pg_current_xact_id() AND idempotency_key=c.idempotency_key AND request_hash=c.request_hash)
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=p.tenant_id AND user_id=e.executed_by AND session_id=e.session_id AND event_type='PROPOSAL_EXECUTED' AND detail->>'proposal_id'=p.id::text AND detail->>'version'=p.version::text AND detail->>'check_id'=c.id::text)
 OR NOT EXISTS(SELECT 1 FROM lv.outbox WHERE tenant_id=e.tenant_id AND native_execution_id=e.id AND work_item_id IS NULL AND topic='execution' AND status='PENDING' AND native_tx_id=pg_current_xact_id() AND native_payload=lv.native_execution_json(e))
 OR NOT EXISTS(SELECT 1 FROM lv.api_idempotency WHERE tenant_id=p.tenant_id AND principal_id=e.executed_by AND route='/api/v1/proposals/{proposal_id}/execute' AND idempotency_key=c.idempotency_key AND request_hash=c.request_hash AND response_status=200 AND response->'body'=lv.native_execution_json(e))
 THEN RAISE EXCEPTION 'native execution lacks independent atomic completion proof' USING ERRCODE='23514'; END IF;
 PERFORM lv.validate_settlement_execution(e.tenant_id,e.ledger_id,e.commit_seq,e.executed_by,b.canonical_content);
 comparison:=lv.native_posting_match(e);
 IF e.comparison IS DISTINCT FROM comparison OR e.predicted_hash IS DISTINCT FROM comparison->>'predicted_hash' OR e.derived_hash IS DISTINCT FROM comparison->>'derived_hash' THEN RAISE EXCEPTION 'native execution posting proof changed' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION lv.validate_execution_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id;
 IF NEW.tx_id<>pg_current_xact_id() OR p.version<>NEW.resulting_version OR p.revision<>NEW.revision OR p.latest_simulation_id<>NEW.simulation_id OR lv.executor_blocker(p,NEW.checked_by) IS NOT NULL THEN RAISE EXCEPTION 'execution check actor or revision mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.report->>'status'='FRESH' THEN
 IF NOT EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=NEW.tenant_id AND check_id=NEW.id AND state='RECONCILED' AND tx_id=pg_current_xact_id()) THEN RAISE EXCEPTION 'fresh execution check lacks completion' USING ERRCODE='23514'; END IF;
 ELSE
 IF p.state<>'STALE' OR NEW.report IS DISTINCT FROM lv.proposal_freshness(p) OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED')
 OR EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision)
 OR NOT EXISTS(SELECT 1 FROM lv.proposal_transition WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND version=p.version AND to_state='STALE' AND action='execute' AND user_id=NEW.checked_by AND session_id=NEW.session_id AND tx_id=pg_current_xact_id())
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=p.tenant_id AND user_id=NEW.checked_by AND session_id=NEW.session_id AND event_type='PROPOSAL_EXECUTION_STALE' AND detail->>'check_id'=NEW.id::text AND detail->>'proposal_id'=p.id::text AND detail->>'version'=p.version::text)
 OR NOT EXISTS(SELECT 1 FROM lv.api_idempotency WHERE tenant_id=p.tenant_id AND principal_id=NEW.checked_by AND route='/api/v1/proposals/{proposal_id}/execute' AND idempotency_key=NEW.idempotency_key AND request_hash=NEW.request_hash AND response_status=409 AND response->>'code'='PROPOSAL_STALE')
 THEN RAISE EXCEPTION 'stale execution check lacks atomic refusal' USING ERRCODE='23514'; END IF;
 END IF;RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER execution_complete AFTER INSERT OR UPDATE ON lv.native_execution DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_native_execution();
CREATE CONSTRAINT TRIGGER execution_batch_complete AFTER INSERT ON lv.commit_batch DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(NEW.kind::text='EXECUTION') EXECUTE FUNCTION lv.validate_native_execution();
CREATE CONSTRAINT TRIGGER execution_check_complete AFTER INSERT ON lv.native_execution_check DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_execution_check();
REVOKE ALL ON FUNCTION lv.validate_native_execution(),lv.validate_execution_check() FROM PUBLIC;
CREATE TABLE lv.native_execution_incident(
 tenant_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,user_id uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 request_id text NOT NULL,code text NOT NULL CHECK(code='NATIVE_EXECUTION_INVARIANT_FAILURE'),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,id),FOREIGN KEY(tenant_id,proposal_id) REFERENCES lv.proposal(tenant_id,id)
);
ALTER TABLE lv.native_execution_incident ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.native_execution_incident FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.native_execution_incident USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.native_execution_incident FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.native_execution_incident FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE FUNCTION lv.record_execution_incident(tok bytea,pid uuid,src text,req text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;i uuid;
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT EXISTS(SELECT 1 FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid) THEN RETURN NULL; END IF;
 INSERT INTO lv.native_execution_incident(tenant_id,proposal_id,user_id,session_id,request_id,code) VALUES(s.tenant_id,pid,s.user_id,s.id,req,'NATIVE_EXECUTION_INVARIANT_FAILURE') RETURNING id INTO i;
 PERFORM lv_identity.audit_append('NATIVE_EXECUTION_INCIDENT','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',pid::text,'incident_id',i::text));RETURN i;
END $$;
REVOKE ALL ON FUNCTION lv.record_execution_incident(bytea,uuid,text,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.record_execution_incident(bytea,uuid,text,text) TO lv_app;
