-- All effects are workflow metadata. Authoritative ledger and immutable source facts are untouched.
ALTER TABLE lv.proposal DROP CONSTRAINT proposal_state_check;
ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','WITHDRAWN'));
ALTER TABLE lv.proposal ADD COLUMN submitted_by uuid REFERENCES lv_identity.app_user(id);
ALTER TABLE lv.proposal ADD COLUMN snapshot_id uuid;
ALTER TABLE lv.proposal ADD COLUMN latest_simulation_id uuid;
CREATE TABLE lv.ledger_snapshot (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,revision integer NOT NULL,
 simulation_id uuid NOT NULL,seq bigint NOT NULL CHECK(seq>=0),snapshot_ref jsonb NOT NULL,
 balances_canonical text NOT NULL,balances_hash char(64) NOT NULL,
 PRIMARY KEY(tenant_id,ledger_id,id),UNIQUE(tenant_id,proposal_id,revision),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 FOREIGN KEY(tenant_id,ledger_id,simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id),
 CHECK(balances_hash=encode(sha256(convert_to(balances_canonical,'UTF8')),'hex'))
);
ALTER TABLE lv.proposal ADD CONSTRAINT submission_snapshot_fkey FOREIGN KEY(tenant_id,ledger_id,snapshot_id) REFERENCES lv.ledger_snapshot(tenant_id,ledger_id,id);
ALTER TABLE lv.proposal ADD CONSTRAINT submission_run_fkey FOREIGN KEY(tenant_id,ledger_id,latest_simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id);
CREATE TABLE lv.proposal_read_set (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,proposal_id uuid NOT NULL,revision integer NOT NULL,
 entity_type text NOT NULL CHECK(entity_type IN ('ACCOUNT','DOCUMENT','PERIOD')),entity_id uuid NOT NULL,version integer NOT NULL CHECK(version>0),
 PRIMARY KEY(tenant_id,proposal_id,revision,entity_type,entity_id),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision)
);
CREATE TABLE lv.verdict (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,simulation_id uuid NOT NULL,policy_id text NOT NULL,
 body jsonb NOT NULL CHECK(body->>'outcome' IN ('PASS','FAIL','INSUFFICIENT_EVIDENCE','NOT_APPLICABLE')),
 PRIMARY KEY(tenant_id,simulation_id,policy_id),
 FOREIGN KEY(tenant_id,ledger_id,simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id),CHECK(body->>'policy_id'=policy_id)
);
CREATE TABLE lv.proposal_transition (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,proposal_id uuid NOT NULL,revision integer NOT NULL,version integer NOT NULL,
 phase smallint NOT NULL CHECK(phase IN (0,1)),from_state lv.proposal_state NOT NULL,to_state lv.proposal_state NOT NULL,
 user_id uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 action text NOT NULL CHECK(action IN ('submit','withdraw')),idempotency_key text NOT NULL,request_hash char(64) NOT NULL,
 tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
 PRIMARY KEY(tenant_id,proposal_id,version,phase),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision)
);
CREATE TABLE lv.staged_workflow (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,staged_document_id uuid NOT NULL,status text NOT NULL CHECK(status IN ('VALID','RESERVED')),
 version integer NOT NULL CHECK(version>0),proposal_id uuid,revision integer,
 PRIMARY KEY(tenant_id,ledger_id,staged_document_id),
 FOREIGN KEY(tenant_id,ledger_id,staged_document_id) REFERENCES lv.staged_document(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 CHECK((status='RESERVED' AND proposal_id IS NOT NULL AND revision IS NOT NULL) OR (status='VALID' AND proposal_id IS NULL AND revision IS NULL))
);
CREATE TABLE lv.reservation_event (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,staged_document_id uuid NOT NULL,version integer NOT NULL,
 event text NOT NULL CHECK(event IN ('RESERVED','RELEASED')),proposal_id uuid NOT NULL,revision integer NOT NULL,
 user_id uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
 PRIMARY KEY(tenant_id,staged_document_id,version),
 FOREIGN KEY(tenant_id,ledger_id,staged_document_id) REFERENCES lv.staged_document(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision)
);
CREATE INDEX proposal_read_set_entity ON lv.proposal_read_set(tenant_id,ledger_id,entity_type,entity_id);
CREATE INDEX staged_workflow_owner ON lv.staged_workflow(tenant_id,proposal_id,revision) WHERE status='RESERVED';
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.ledger_snapshot FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proposal_read_set FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.verdict FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proposal_transition FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.reservation_event FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE FUNCTION lv.guard_staged_workflow() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' OR (NEW.tenant_id,NEW.ledger_id,NEW.staged_document_id) IS DISTINCT FROM (OLD.tenant_id,OLD.ledger_id,OLD.staged_document_id)
 OR NEW.version<>OLD.version+1 OR NEW.status=OLD.status THEN RAISE EXCEPTION 'invalid reservation CAS' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER workflow_guard BEFORE UPDATE OR DELETE ON lv.staged_workflow FOR EACH ROW EXECUTE FUNCTION lv.guard_staged_workflow();
CREATE OR REPLACE FUNCTION lv.guard_proposal_update() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'proposals are retained' USING ERRCODE='23514'; END IF;
 IF (NEW.tenant_id,NEW.ledger_id,NEW.id,NEW.origin,NEW.target_adapter,NEW.created_by,NEW.created_at)
 IS DISTINCT FROM (OLD.tenant_id,OLD.ledger_id,OLD.id,OLD.origin,OLD.target_adapter,OLD.created_by,OLD.created_at) THEN
 RAISE EXCEPTION 'proposal identity is immutable' USING ERRCODE='23514'; END IF;
 IF OLD.state='DRAFT' AND NEW.state='DRAFT' AND NEW.revision=OLD.revision+1 AND NEW.version=OLD.version+1
 AND NEW.submitted_by IS NULL AND NEW.snapshot_id IS NULL AND NEW.latest_simulation_id IS NULL THEN RETURN NEW; END IF;
 IF NEW.revision<>OLD.revision THEN RAISE EXCEPTION 'workflow cannot edit revisions' USING ERRCODE='23514'; END IF;
 IF OLD.state='DRAFT' AND NEW.state='SUBMITTED' AND NEW.version=OLD.version+1 AND NEW.submitted_by=OLD.created_by
 AND NEW.snapshot_id IS NULL AND NEW.latest_simulation_id IS NULL THEN RETURN NEW; END IF;
 IF OLD.state='SUBMITTED' AND NEW.state IN ('BLOCKED','READY_FOR_REVIEW') AND NEW.version=OLD.version
 AND NEW.submitted_by=OLD.submitted_by AND NEW.snapshot_id IS NOT NULL AND NEW.latest_simulation_id IS NOT NULL THEN RETURN NEW; END IF;
 IF OLD.state IN ('DRAFT','BLOCKED','READY_FOR_REVIEW') AND NEW.state='WITHDRAWN' AND NEW.version=OLD.version+1
 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'unsupported proposal transition' USING ERRCODE='23514';
END $$;
CREATE OR REPLACE FUNCTION lv.proposal_json(p lv.proposal) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',p.id::text,'ledger_id',p.ledger_id::text,'ledger_purpose',l.purpose::text,'state',p.state::text,
 'revision',p.revision,'version',p.version,'origin',p.origin,'ai_draft_id',NULL,'target_adapter',p.target_adapter,
 'operations',r.operations,'evidence_refs',r.evidence_refs,'rationale',r.rationale,
 'snapshot',(SELECT snapshot_ref FROM lv.ledger_snapshot WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=p.snapshot_id),
 'read_set',coalesce((SELECT jsonb_agg(jsonb_build_object('entity_type',entity_type,'entity_id',entity_id::text,'version',version) ORDER BY entity_type,entity_id)
 FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision),'[]'::jsonb),
 'latest_simulation_id',p.latest_simulation_id::text,'created_by',p.created_by::text,'submitted_by',p.submitted_by::text,
 'editors',(SELECT jsonb_agg(x.authored_by::text ORDER BY x.first) FROM (SELECT authored_by,min(revision) AS first FROM lv.proposal_revision
 WHERE tenant_id=p.tenant_id AND proposal_id=p.id GROUP BY authored_by) x),
 'created_at',to_char(p.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
 'updated_at',to_char(p.updated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
 'accounting_status','UNREVIEWED','data_label',CASE WHEN l.purpose='SANDBOX' THEN 'SANDBOX — not accounting records' END)
 FROM lv.ledger l JOIN lv.proposal_revision r ON r.tenant_id=l.tenant_id WHERE l.tenant_id=p.tenant_id AND l.id=p.ledger_id AND r.proposal_id=p.id AND r.revision=p.revision;
$$;
CREATE FUNCTION lv.workflow_simulation_json(p_id uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',s.id::text,'proposal_id',s.proposal_id::text,'revision',s.revision,'ledger_id',s.ledger_id::text,
 'data_label','SANDBOX — not accounting records','status',s.status,'simulator_version',s.simulator_version,
 'snapshot',((b->'snapshot')-ARRAY['batch_head','event_head'])||jsonb_build_object('seq',s.snapshot_seq),
 'read_set',b->'read_set','versions',b->'versions','pinned_sources',b->'pinned_sources','input_hash',s.input_hash,'result_hash',s.result_hash,
 'outcome',s.outcome_canonical::jsonb,'accounting_status','UNREVIEWED','created_by',s.created_by::text,
 'created_at',to_char(s.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
 FROM lv.simulation_run s CROSS JOIN LATERAL (SELECT s.input_canonical::jsonb AS b) x WHERE s.id=p_id;
$$;
CREATE FUNCTION lv.workflow_cache(s lv_identity.session,p_action text,p_key text,p_hash text,p_result jsonb) RETURNS jsonb
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at)
 VALUES(s.tenant_id,s.user_id,'POST','/api/v1/proposals/{proposal_id}/'||p_action,p_key,p_hash,(p_result->>'status')::smallint,p_result,transaction_timestamp()+interval '24 hours');
 RETURN p_result||jsonb_build_object('replayed',false);
END $$;
CREATE FUNCTION lv.workflow_request(p_token bytea,p_action text,p_key text,p_hash text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;prior lv.api_idempotency;v_route text:='/api/v1/proposals/{proposal_id}/'||p_action;info jsonb;
BEGIN
 s:=lv.proposal_session(p_token,p_source,p_request);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'propose') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires propose'); END IF;
 IF p_action NOT IN ('submit','withdraw') OR p_key IS NULL OR p_key!~'^[!-~]{1,64}$' OR p_hash!~'^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'invalid workflow request' USING ERRCODE='22023'; END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-idem:'||s.tenant_id::text||':'||s.user_id::text||':POST:'||v_route||':'||p_key,0)) THEN
 RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1,'detail','the first request with this Idempotency-Key is still running'); END IF;
 -- Session activity is touched after the idempotency try-lock, before any proposal/ledger locks.
 info:=lv_identity.session_context(p_token,true);
 IF NOT (info->>'valid')::boolean THEN RAISE EXCEPTION 'session unavailable' USING ERRCODE='42501'; END IF;
 s:=lv.proposal_session(p_token,p_source,p_request);
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=v_route AND i.idempotency_key=p_key;
 IF FOUND THEN
 IF prior.expires_at<=statement_timestamp() THEN DELETE FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=v_route AND i.idempotency_key=p_key;
 ELSIF prior.request_hash<>p_hash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED','detail','key used for a different request');
 ELSE RETURN prior.response||jsonb_build_object('replayed',true); END IF;
 END IF;
 RETURN jsonb_build_object('ready',true);
END $$;
CREATE FUNCTION lv.workflow_final_session(p_token bytea,p_source text,p_request text) RETURNS lv_identity.session
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;
BEGIN
 s:=lv.proposal_session(p_token,p_source,p_request);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(p_token,p_source,p_request);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'propose') THEN PERFORM lv.proposal_refusal(403,'PERMISSION_DENIED','requires propose',NULL,NULL); END IF;
 RETURN s;
END $$;
CREATE FUNCTION lv.workflow_begin(p_token bytea,p_id uuid,p_match text,p_key text,p_hash text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;l lv.ledger;r lv.proposal_revision;d uuid;w lv.staged_workflow;st lv.staged_document;op jsonb;
BEGIN
 s:=lv.proposal_session(p_token,p_source,p_request);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'propose') THEN PERFORM lv.proposal_refusal(403,'PERMISSION_DENIED','requires propose',NULL,NULL); END IF;
 IF p_match IS NULL THEN PERFORM lv.proposal_refusal(428,'VERSION_REQUIRED','If-Match is required',NULL,NULL); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=p_id FOR UPDATE;
 IF NOT FOUND THEN PERFORM lv.proposal_refusal(404,'NOT_FOUND','proposal not found',NULL,NULL); END IF;
 IF p.created_by<>s.user_id THEN PERFORM lv.proposal_refusal(403,'PERMISSION_DENIED','only the maker may submit',NULL,NULL); END IF;
 IF p_match<>'"v'||p.version::text||'"' THEN PERFORM lv.proposal_refusal(412,'VERSION_MISMATCH','proposal has changed',NULL,jsonb_build_object('current_version',p.version)); END IF;
 IF p.state<>'DRAFT' THEN PERFORM lv.proposal_refusal(409,'INVALID_STATE_TRANSITION','only DRAFT may be submitted',NULL,jsonb_build_object('state',p.state)); END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' THEN PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','native SANDBOX only',NULL,NULL); END IF;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 IF NOT FOUND THEN PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','no committed ledger snapshot',NULL,NULL); END IF;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 SELECT * INTO r FROM lv.proposal_revision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 IF jsonb_array_length(r.operations)<>1 OR jsonb_array_length(r.operations->0->'items')>200 THEN
 PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','only one synchronous RecordSettlement is available; no worker exists',NULL,NULL); END IF;
 op:=r.operations->0;
 SELECT * INTO st FROM lv.staged_document WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=(op->>'statement_document_id')::uuid;
 IF (st.processor_ref,st.payout_id,st.payout_date::text) IS DISTINCT FROM (op->>'processor_ref',op->>'payout_id',op->>'payout_date') THEN
 PERFORM lv.proposal_refusal(422,'PRECONDITION_FAILED','payout identity must come from the cited statement',NULL,jsonb_build_object('reason','STATEMENT_IDENTITY_MISMATCH')); END IF;
 FOR d IN SELECT DISTINCT staged_document_id FROM lv.proposal_reference WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND staged_document_id IS NOT NULL ORDER BY staged_document_id LOOP
 INSERT INTO lv.staged_workflow VALUES(p.tenant_id,p.ledger_id,d,'VALID',1,NULL,NULL) ON CONFLICT DO NOTHING;
 SELECT * INTO w FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND staged_document_id=d FOR UPDATE;
 IF w.status<>'VALID' THEN PERFORM lv.proposal_refusal(422,'PRECONDITION_FAILED','staged document is reserved',NULL,jsonb_build_object('reason','STAGED_DOCUMENT_RESERVED')); END IF;
 UPDATE lv.staged_workflow SET status='RESERVED',version=version+1,proposal_id=p.id,revision=p.revision WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND staged_document_id=d AND status='VALID' AND version=w.version;
 IF NOT FOUND THEN RAISE EXCEPTION 'reservation CAS lost' USING ERRCODE='40001'; END IF;
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id)
 VALUES(p.tenant_id,p.ledger_id,d,w.version+1,'RESERVED',p.id,p.revision,s.user_id,s.id);
 END LOOP;
 UPDATE lv.proposal SET state='SUBMITTED',version=version+1,submitted_by=s.user_id,updated_at=statement_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,'DRAFT','SUBMITTED',s.user_id,s.id,'submit',p_key,p_hash);
 RETURN jsonb_build_object('revision',p.revision,'version',p.version);
END $$;
CREATE FUNCTION lv.workflow_finish(p_token bytea,p_id uuid,p_input text,p_input_hash text,p_output text,p_output_hash text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;tr lv.proposal_transition;b jsonb:=p_input::jsonb;o jsonb:=p_output::jsonb;
 r lv.proposal_revision;sid uuid:=uuidv7();snap uuid:=uuidv7();e jsonb;agg text;target lv.proposal_state;ref jsonb;
BEGIN
 s:=lv.workflow_final_session(p_token,p_source,p_request);
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=p_id FOR UPDATE;
 SELECT * INTO tr FROM lv.proposal_transition WHERE tenant_id=s.tenant_id AND proposal_id=p_id AND version=p.version AND phase=0
 AND tx_id=pg_current_xact_id() AND session_id=s.id AND to_state='SUBMITTED';
 IF tr.proposal_id IS NULL OR p.state<>'SUBMITTED' OR p.created_by<>s.user_id THEN RAISE EXCEPTION 'submission not begun in this transaction' USING ERRCODE='23514'; END IF;
 SELECT * INTO r FROM lv.proposal_revision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 IF b->>'tenant_id' IS DISTINCT FROM p.tenant_id::text OR b->'ledger'->>'id' IS DISTINCT FROM p.ledger_id::text
 OR b->'proposal'->>'id' IS DISTINCT FROM p.id::text OR b->'proposal'->>'revision' IS DISTINCT FROM p.revision::text
 OR b->'proposal'->>'content_hash' IS DISTINCT FROM r.content_hash::text OR b->'proposal'->'operations' IS DISTINCT FROM r.operations
 OR b->'proposal'->'evidence_refs' IS DISTINCT FROM r.evidence_refs OR b->'versions'->>'account_read_set' IS DISTINCT FROM 'lv-account-dependencies/1'
 OR b->'versions'->>'simulator' IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0'
 OR (b->'snapshot'->>'seq')::bigint IS DISTINCT FROM (SELECT last_seq FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id)
 OR o->>'status' IS DISTINCT FROM 'COMPLETED' OR o->>'input_hash' IS DISTINCT FROM p_input_hash
 OR o->'verification'->>'status' IS DISTINCT FROM 'EVALUATED' OR o->>'accounting_status' IS DISTINCT FROM 'UNREVIEWED'
 OR o->'eligible_for_execution' IS DISTINCT FROM 'false'::jsonb THEN RAISE EXCEPTION 'inconsistent submission artifact' USING ERRCODE='23514'; END IF;
 SELECT CASE WHEN bool_or(value->>'outcome'='FAIL') THEN 'BLOCKED_FAIL'
 WHEN bool_or(value->>'outcome'='INSUFFICIENT_EVIDENCE') THEN 'BLOCKED_INSUFFICIENT'
 WHEN bool_or(value->>'outcome'='PASS') THEN 'CLEAR' END INTO agg FROM jsonb_array_elements(o->'verdicts');
 IF agg IS NULL OR agg IS DISTINCT FROM o->>'aggregate' THEN RAISE EXCEPTION 'invalid verdict aggregate' USING ERRCODE='23514'; END IF;
 -- The currently admitted staged statements are SYNTHETIC; never accept a manufactured CLEAR/evidence pass.
 IF EXISTS(SELECT 1 FROM lv.staged_document WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=(r.operations->0->>'statement_document_id')::uuid AND provenance::text<>'SOURCE')
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(o->'verdicts') v WHERE v->>'policy_id'='P-EVD-01' AND v->>'outcome'='INSUFFICIENT_EVIDENCE') THEN
 RAISE EXCEPTION 'synthetic evidence cannot pass' USING ERRCODE='23514'; END IF;
 target:=CASE WHEN agg='CLEAR' THEN 'READY_FOR_REVIEW'::lv.proposal_state ELSE 'BLOCKED'::lv.proposal_state END;
 INSERT INTO lv.simulation_run(tenant_id,ledger_id,id,proposal_id,revision,status,simulator_version,snapshot_seq,input_canonical,input_hash,outcome_canonical,result_hash,created_by,session_id)
 VALUES(p.tenant_id,p.ledger_id,sid,p.id,p.revision,'COMPLETED',b->'versions'->>'simulator',(b->'snapshot'->>'seq')::bigint,p_input,p_input_hash,p_output,p_output_hash,s.user_id,s.id);
 ref:=((b->'snapshot')-ARRAY['batch_head','event_head'])||jsonb_build_object('seq',(b->'snapshot'->>'seq')::bigint);
 INSERT INTO lv.ledger_snapshot VALUES(p.tenant_id,p.ledger_id,snap,p.id,p.revision,sid,(b->'snapshot'->>'seq')::bigint,ref,
 lv.proposal_canonical_json(b->'facts'->'projection'),encode(sha256(convert_to(lv.proposal_canonical_json(b->'facts'->'projection'),'UTF8')),'hex'));
 FOR e IN SELECT value FROM jsonb_array_elements(b->'read_set') LOOP
 INSERT INTO lv.proposal_read_set VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,e->>'entity_type',(e->>'entity_id')::uuid,(e->>'version')::integer);
 END LOOP;
 FOR e IN SELECT value FROM jsonb_array_elements(o->'verdicts') LOOP INSERT INTO lv.verdict VALUES(p.tenant_id,p.ledger_id,sid,e->>'policy_id',e); END LOOP;
 UPDATE lv.proposal SET state=target,snapshot_id=snap,latest_simulation_id=sid,updated_at=statement_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,1,'SUBMITTED',target,s.user_id,s.id,'submit',tr.idempotency_key,tr.request_hash);
 PERFORM lv_identity.audit_append('PROPOSAL_SUBMITTED','ALLOWED',s.user_id,p.tenant_id,s.id,p_source,p_request,
 jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'simulation_id',sid::text,'aggregate',agg));
 RETURN lv.workflow_cache(s,'submit',tr.idempotency_key,tr.request_hash,jsonb_build_object('status',200,'etag','v'||p.version::text,'body',jsonb_build_object('proposal',lv.proposal_json(p),'simulation',lv.workflow_simulation_json(sid))));
END $$;
CREATE FUNCTION lv.workflow_withdraw(p_token bytea,p_id uuid,p_match text,p_key text,p_hash text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;w lv.staged_workflow;previous lv.proposal_state;
BEGIN
 s:=lv.workflow_final_session(p_token,p_source,p_request);
 IF p_match IS NULL THEN PERFORM lv.proposal_refusal(428,'VERSION_REQUIRED','If-Match is required',NULL,NULL); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=p_id FOR UPDATE;
 IF NOT FOUND THEN PERFORM lv.proposal_refusal(404,'NOT_FOUND','proposal not found',NULL,NULL); END IF;
 IF p.created_by<>s.user_id THEN PERFORM lv.proposal_refusal(403,'PERMISSION_DENIED','only the maker may withdraw',NULL,NULL); END IF;
 IF p_match<>'"v'||p.version::text||'"' THEN PERFORM lv.proposal_refusal(412,'VERSION_MISMATCH','proposal has changed',NULL,jsonb_build_object('current_version',p.version)); END IF;
 IF p.state NOT IN ('DRAFT','BLOCKED','READY_FOR_REVIEW') THEN PERFORM lv.proposal_refusal(409,'INVALID_STATE_TRANSITION','withdrawal is unavailable from this state',NULL,jsonb_build_object('state',p.state)); END IF;
 PERFORM 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 previous:=p.state;
 FOR w IN SELECT * FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' ORDER BY staged_document_id FOR UPDATE LOOP
 UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND staged_document_id=w.staged_document_id AND version=w.version AND proposal_id=p.id;
 IF NOT FOUND THEN RAISE EXCEPTION 'release CAS lost' USING ERRCODE='40001'; END IF;
 INSERT INTO lv.reservation_event VALUES(p.tenant_id,p.ledger_id,w.staged_document_id,w.version+1,'RELEASED',p.id,p.revision,s.user_id,s.id,statement_timestamp());
 END LOOP;
 UPDATE lv.proposal SET state='WITHDRAWN',version=version+1,updated_at=statement_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,previous,'WITHDRAWN',s.user_id,s.id,'withdraw',p_key,p_hash);
 PERFORM lv_identity.audit_append('PROPOSAL_WITHDRAWN','ALLOWED',s.user_id,p.tenant_id,s.id,p_source,p_request,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text));
 RETURN lv.workflow_cache(s,'withdraw',p_key,p_hash,jsonb_build_object('status',200,'etag','v'||p.version::text,'body',lv.proposal_json(p)));
END $$;
CREATE FUNCTION lv.workflow_refused(p_token bytea,p_action text,p_key text,p_hash text,p_problem jsonb,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;
BEGIN
 s:=lv.proposal_session(p_token,p_source,p_request);
 IF p_action NOT IN ('submit','withdraw') OR (p_problem->>'status')::integer NOT BETWEEN 400 AND 499 THEN RAISE EXCEPTION 'invalid refusal' USING ERRCODE='22023'; END IF;
 PERFORM lv_identity.audit_append('PROPOSAL_'||upper(p_action)||'_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('reason',p_problem->>'code'));
 RETURN lv.workflow_cache(s,p_action,p_key,p_hash,p_problem);
END $$;
CREATE FUNCTION lv.validate_submission() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;r lv.simulation_run;ls lv.ledger_snapshot;b jsonb;o jsonb;rs jsonb;vs jsonb;act text;
BEGIN
 IF TG_TABLE_NAME='proposal' THEN SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
 ELSE SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id; END IF;
 IF p.state='DRAFT' THEN
 IF p.submitted_by IS NOT NULL OR p.latest_simulation_id IS NOT NULL OR p.snapshot_id IS NOT NULL THEN RAISE EXCEPTION 'draft has submitted artifacts' USING ERRCODE='23514'; END IF;RETURN NULL;
 END IF;
 IF p.state='SUBMITTED' THEN RAISE EXCEPTION 'incomplete submission cannot commit' USING ERRCODE='23514'; END IF;
 act:=CASE WHEN p.state='WITHDRAWN' THEN 'withdraw' ELSE 'submit' END;
 IF NOT EXISTS(SELECT 1 FROM lv.proposal_transition t JOIN lv.api_idempotency i ON i.tenant_id=t.tenant_id AND i.principal_id=t.user_id
 AND i.method='POST' AND i.route='/api/v1/proposals/{proposal_id}/'||t.action AND i.idempotency_key=t.idempotency_key AND i.request_hash=t.request_hash AND i.response_status=200
 WHERE t.tenant_id=p.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.to_state=p.state AND t.tx_id=pg_current_xact_id() AND t.user_id=p.created_by)
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event a WHERE a.chain_key=p.tenant_id AND a.tenant_id=p.tenant_id AND a.user_id=p.created_by
 AND a.event_type=CASE WHEN act='withdraw' THEN 'PROPOSAL_WITHDRAWN' ELSE 'PROPOSAL_SUBMITTED' END AND a.detail->>'proposal_id'=p.id::text AND a.detail->>'version'=p.version::text)
 THEN RAISE EXCEPTION 'workflow transition lacks cache or audit' USING ERRCODE='23514'; END IF;
 IF p.state='WITHDRAWN' THEN
 IF EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'withdrawal left a reservation' USING ERRCODE='23514'; END IF; RETURN NULL; END IF;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 SELECT * INTO ls FROM lv.ledger_snapshot WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=p.snapshot_id;
 IF r.id IS NULL OR ls.id IS NULL OR r.proposal_id<>p.id OR r.revision<>p.revision OR ls.proposal_id<>p.id OR ls.revision<>p.revision OR ls.simulation_id<>r.id OR p.submitted_by<>p.created_by THEN RAISE EXCEPTION 'submission artifacts do not match head' USING ERRCODE='23514'; END IF;
 b:=r.input_canonical::jsonb;o:=r.outcome_canonical::jsonb;
 SELECT coalesce(jsonb_agg(jsonb_build_object('entity_type',entity_type,'entity_id',entity_id::text,'version',version) ORDER BY entity_type,entity_id),'[]'::jsonb) INTO rs FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 SELECT coalesce(jsonb_agg(body ORDER BY policy_id),'[]'::jsonb) INTO vs FROM lv.verdict WHERE tenant_id=p.tenant_id AND simulation_id=r.id;
 IF rs IS DISTINCT FROM b->'read_set' OR vs IS DISTINCT FROM o->'verdicts' OR ls.balances_canonical::jsonb IS DISTINCT FROM b->'facts'->'projection'
 OR ls.snapshot_ref IS DISTINCT FROM (((b->'snapshot')-ARRAY['batch_head','event_head'])||jsonb_build_object('seq',r.snapshot_seq)) OR ls.seq<>r.snapshot_seq OR (p.state='READY_FOR_REVIEW') IS DISTINCT FROM (o->>'aggregate'='CLEAR') THEN RAISE EXCEPTION 'incomplete pinned submission' USING ERRCODE='23514'; END IF;
 IF ls.seq IS DISTINCT FROM (SELECT last_seq FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id)
 OR EXISTS(
 WITH expected AS (
 SELECT 'ACCOUNT'::text AS typ,a.id,a.version FROM lv.account a JOIN lv.account_role_binding rb ON (rb.tenant_id,rb.ledger_id,rb.account_id)=(a.tenant_id,a.ledger_id,a.id) WHERE a.tenant_id=p.tenant_id AND a.ledger_id=p.ledger_id
 UNION ALL SELECT DISTINCT 'DOCUMENT',dc.document_id,dc.version FROM lv.document_current dc JOIN lv.proposal_reference ref ON (ref.tenant_id,ref.ledger_id,ref.document_id)=(dc.tenant_id,dc.ledger_id,dc.document_id) WHERE ref.tenant_id=p.tenant_id AND ref.proposal_id=p.id AND ref.revision=p.revision AND ref.kind='SETTLEMENT_ITEM_PAYMENT'
 UNION ALL SELECT 'PERIOD',fp.id,fp.version FROM lv.fiscal_period fp WHERE fp.tenant_id=p.tenant_id AND fp.ledger_id=p.ledger_id AND fp.start_date<=(b->'proposal'->'operations'->0->>'payout_date')::date AND (b->'proposal'->'operations'->0->>'payout_date')::date<fp.end_date
 ), actual AS (SELECT entity_type AS typ,entity_id AS id,version FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision)
 SELECT 1 FROM ((SELECT * FROM expected EXCEPT ALL SELECT * FROM actual) UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)) delta
 ) THEN RAISE EXCEPTION 'submission dependency set changed or is incomplete' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM lv.proposal_reference ref WHERE ref.tenant_id=p.tenant_id AND ref.proposal_id=p.id AND ref.revision=p.revision AND ref.staged_document_id IS NOT NULL
 AND NOT EXISTS(SELECT 1 FROM lv.staged_workflow w WHERE w.tenant_id=p.tenant_id AND w.ledger_id=p.ledger_id AND w.staged_document_id=ref.staged_document_id AND w.proposal_id=p.id AND w.revision=p.revision AND w.status='RESERVED')) THEN RAISE EXCEPTION 'submission lacks its reservations' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER workflow_complete AFTER UPDATE ON lv.proposal DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_submission();
CREATE CONSTRAINT TRIGGER workflow_complete AFTER INSERT ON lv.proposal_transition DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_submission();
CREATE FUNCTION lv.validate_reservation() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.staged_workflow;e lv.reservation_event;p lv.proposal;
BEGIN
 SELECT * INTO w FROM lv.staged_workflow WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND staged_document_id=NEW.staged_document_id;
 SELECT * INTO e FROM lv.reservation_event WHERE tenant_id=w.tenant_id AND staged_document_id=w.staged_document_id AND version=w.version;
 IF e.staged_document_id IS NULL OR (w.status='RESERVED') IS DISTINCT FROM (e.event='RESERVED') THEN RAISE EXCEPTION 'reservation history incomplete' USING ERRCODE='23514'; END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=e.tenant_id AND id=e.proposal_id;
 IF e.user_id<>p.created_by THEN RAISE EXCEPTION 'reservation actor is not maker' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND NOT EXISTS(SELECT 1 FROM lv.proposal_reference ref WHERE ref.tenant_id=w.tenant_id AND ref.proposal_id=w.proposal_id AND ref.revision=w.revision AND ref.staged_document_id=w.staged_document_id) THEN RAISE EXCEPTION 'uncited reservation' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND (p.state NOT IN ('BLOCKED','READY_FOR_REVIEW') OR w.proposal_id<>p.id OR w.revision<>p.revision) THEN RAISE EXCEPTION 'orphan reservation' USING ERRCODE='23514'; END IF;
 IF w.status='VALID' AND p.state<>'WITHDRAWN' THEN RAISE EXCEPTION 'release requires a completed withdrawal' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER reservation_complete AFTER INSERT OR UPDATE ON lv.staged_workflow DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_reservation();
