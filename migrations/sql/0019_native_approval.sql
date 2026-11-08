-- Accepted Stage 2A 9.3/10.3/12.2; Stage 2B family 11. Native SANDBOX approval only.
ALTER TABLE lv.proposal ADD CONSTRAINT proposal_maker_identity UNIQUE(tenant_id,ledger_id,id,created_by);
CREATE TABLE lv.approval_check(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,revision integer NOT NULL,
 simulation_id uuid NOT NULL,observed_version integer NOT NULL,resulting_version integer NOT NULL CHECK(resulting_version=observed_version+1),
 checked_by uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 report jsonb NOT NULL CHECK(report->>'status' IN ('FRESH','STALE')),review_granted_at timestamptz NOT NULL,
 checked_at timestamptz NOT NULL DEFAULT clock_timestamp(),comment text CHECK(comment IS NULL OR length(comment)<=4000),
 idempotency_key text NOT NULL,request_hash text NOT NULL CHECK(request_hash~'^[0-9a-f]{64}$'),request_id text NOT NULL,tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,proposal_id,resulting_version),UNIQUE(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 FOREIGN KEY(tenant_id,ledger_id,simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id)
);
CREATE TABLE lv.proposal_decision(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,revision integer NOT NULL,
 kind text NOT NULL CHECK(kind='APPROVED'),simulation_id uuid NOT NULL,check_id uuid NOT NULL,
 submitted_by uuid NOT NULL,approved_by uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 observed_version integer NOT NULL,resulting_version integer NOT NULL CHECK(resulting_version=observed_version+1),
 comment text CHECK(comment IS NULL OR length(comment)<=4000),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,proposal_id,revision),CHECK(approved_by<>submitted_by),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,submitted_by) REFERENCES lv.proposal(tenant_id,ledger_id,id,created_by),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 FOREIGN KEY(tenant_id,ledger_id,simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,check_id) REFERENCES lv.approval_check(tenant_id,ledger_id,id)
);
ALTER TABLE lv.approval_check ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.approval_check FORCE ROW LEVEL SECURITY;
ALTER TABLE lv.proposal_decision ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.proposal_decision FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.approval_check USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE POLICY tenant_scope ON lv.proposal_decision USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.approval_check FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proposal_decision FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
REVOKE ALL ON lv.approval_check,lv.proposal_decision FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;GRANT SELECT ON lv.approval_check,lv.proposal_decision TO lv_app;
ALTER TABLE lv.proposal DROP CONSTRAINT proposal_workflow_state;
ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','APPROVED','STALE','WITHDRAWN'));
ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor;
ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale','resimulate','revalidate','overrides','approve'));
ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw','resimulate','revalidate','overrides','approve') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL));

CREATE FUNCTION lv.review_blocker(p lv.proposal,actor uuid) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE latest timestamptz;
BEGIN
 IF NOT lv.proposal_may(p.tenant_id,actor,'review') THEN RETURN 'NO_REVIEW_PERMISSION'; END IF;
 IF actor=p.created_by OR actor=p.submitted_by OR EXISTS(SELECT 1 FROM lv.proposal_revision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND authored_by=actor) THEN RETURN 'MAKER_OR_EDITOR'; END IF;
 IF EXISTS(SELECT 1 FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=actor AND rp.permission='review' AND mr.granted_by=actor) THEN RETURN 'SELF_GRANT'; END IF;
 SELECT max(mr.granted_at) INTO latest FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=actor AND rp.permission='review';
 IF latest IS NULL OR latest>statement_timestamp()-interval '24 hours' THEN RETURN 'REVIEW_COOLING_OFF'; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION lv.review_blocker(lv.proposal,uuid) FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
CREATE FUNCTION lv.approval_available(pid uuid,actor uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;r lv.simulation_run;l lv.ledger;f jsonb;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid AND id=pid;
 IF p.id IS NULL OR p.state<>'READY_FOR_REVIEW' OR lv.review_blocker(p,actor) IS NOT NULL THEN RETURN false; END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' THEN RETURN false; END IF;
 IF EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN RETURN false; END IF;
 f:=lv.proposal_freshness(p);RETURN f->>'status'='FRESH' AND f->'pinned_seq'=f->'observed_seq';
END $$;
REVOKE ALL ON FUNCTION lv.approval_available(uuid,uuid) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.approval_available(uuid,uuid) TO lv_app;
CREATE FUNCTION lv.decision_json(d lv.proposal_decision) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('kind','APPROVAL','id',d.id::text,'proposal_id',d.proposal_id::text,'ledger_id',d.ledger_id::text,'revision',d.revision,'simulation_id',d.simulation_id::text,'approved_by',d.approved_by::text,'submitted_by',d.submitted_by::text,'comment',d.comment,'observed_version',d.observed_version,'resulting_version',d.resulting_version,'check_id',d.check_id::text,'created_at',to_char(d.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
$$;
REVOKE ALL ON FUNCTION lv.decision_json(lv.proposal_decision) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.decision_json(lv.proposal_decision) TO lv_app;

CREATE FUNCTION lv.approve_native(tok bytea,pid uuid,p_match text,ikey text,rhash text,requested jsonb,p_problem jsonb,src text,req text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;l lv.ledger;r lv.simulation_run;prior lv.api_idempotency;problem jsonb:=p_problem;response jsonb;f jsonb;blocker text;grant_time timestamptz;check_id uuid:=uuidv7();before_version integer;previous lv.proposal_state;
 route_name text:='/api/v1/proposals/{proposal_id}/approve';
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 IF ikey IS NULL OR ikey!~'^[!-~]{1,64}$' OR rhash IS NULL OR rhash!~'^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-approve:'||s.tenant_id::text||':'||s.user_id::text||':'||ikey,0)) THEN RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1); END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid FOR UPDATE;
 IF p.id IS NOT NULL THEN
 blocker:=lv.review_blocker(p,s.user_id);
 IF blocker IS NOT NULL THEN RETURN jsonb_build_object('status',403,'code','SEGREGATION_OF_DUTIES','reason',blocker); END IF;
 END IF;
 SELECT * INTO prior FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
 IF prior.request_hash<>rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
 RETURN prior.response||jsonb_build_object('replayed',true); END IF;
 IF p.id IS NULL THEN problem:=jsonb_build_object('status',404,'code','NOT_FOUND');
 ELSIF problem IS NOT NULL THEN NULL;
 ELSIF p_match IS NULL THEN problem:=jsonb_build_object('status',428,'code','VERSION_REQUIRED');
 ELSIF requested IS NULL OR jsonb_typeof(requested)<>'object' OR NOT requested?'revision' OR (requested-ARRAY['revision','comment'])<>'{}'::jsonb OR jsonb_typeof(requested->'revision') IS DISTINCT FROM 'number' OR (requested->>'revision')!~'^[1-9][0-9]*$' OR (requested?'comment' AND jsonb_typeof(requested->'comment') NOT IN ('string','null')) OR length(requested->>'comment')>4000 THEN problem:=jsonb_build_object('status',422,'code','SCHEMA_INVALID');
 ELSIF requested->>'revision'<>p.revision::text THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','reason','REVISION_MISMATCH','current_version',p.version);
 ELSIF EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN problem:=jsonb_build_object('status',409,'code','ALREADY_DECIDED');
 ELSIF p_match<>'"v'||p.version::text||'"' THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','current_version',p.version);
 ELSIF p.state<>'READY_FOR_REVIEW' THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','state',p.state);
 END IF;
 IF problem IS NULL THEN
 PERFORM 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' OR r.revision<>p.revision OR r.proposal_id<>p.id THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE');
 ELSE
 f:=lv.proposal_freshness(p);
 IF f->>'status'='FRESH' AND f->'pinned_seq' IS DISTINCT FROM f->'observed_seq' THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE','reason','FULL_CAPTURE_CHANGED'); END IF;
 END IF;
 END IF;
 IF problem IS NULL THEN
 before_version:=p.version;previous:=p.state;
 SELECT max(mr.granted_at) INTO grant_time FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=s.user_id AND rp.permission='review';
 INSERT INTO lv.approval_check(tenant_id,ledger_id,id,proposal_id,revision,simulation_id,observed_version,resulting_version,checked_by,session_id,report,review_granted_at,comment,idempotency_key,request_hash,request_id)
 VALUES(p.tenant_id,p.ledger_id,check_id,p.id,p.revision,r.id,p.version,p.version+1,s.user_id,s.id,f,grant_time,requested->>'comment',ikey,rhash,req);
 IF f->>'status'='STALE' THEN
 UPDATE lv.proposal SET state='STALE',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 WITH released AS(UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id) SELECT tenant_id,ledger_id,staged_document_id,version,'RELEASED',p.id,p.revision,s.user_id,s.id FROM released;
 response:=jsonb_build_object('status',409,'code','PROPOSAL_STALE','reason','DEPENDENCIES_CHANGED','current_version',p.version);
 ELSE
 INSERT INTO lv.proposal_decision(tenant_id,ledger_id,proposal_id,revision,kind,simulation_id,check_id,submitted_by,approved_by,session_id,observed_version,resulting_version,comment)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,'APPROVED',r.id,check_id,p.created_by,s.user_id,s.id,p.version,p.version+1,requested->>'comment');
 UPDATE lv.proposal SET state='APPROVED',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 response:=jsonb_build_object('status',200,'etag','v'||p.version::text,'body',lv.proposal_json(p));
 END IF;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,previous,p.state,s.user_id,s.id,'approve',ikey,rhash);
 PERFORM lv_identity.audit_append(CASE WHEN p.state='APPROVED' THEN 'PROPOSAL_APPROVED' ELSE 'PROPOSAL_APPROVAL_STALE' END,CASE WHEN p.state='APPROVED' THEN 'ALLOWED' ELSE 'DENIED' END,s.user_id,p.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'check_id',check_id::text));
 ELSE
 PERFORM lv_identity.audit_append('PROPOSAL_APPROVAL_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',pid::text,'reason',problem->>'code'));response:=problem;
 END IF;
 DELETE FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at) VALUES(s.tenant_id,s.user_id,'POST',route_name,ikey,rhash,(response->>'status')::smallint,response,transaction_timestamp()+interval '24 hours');
 RETURN response||jsonb_build_object('replayed',false);
END $$;
REVOKE ALL ON FUNCTION lv.approve_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.approve_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text) TO lv_app;

CREATE FUNCTION lv.validate_approval_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;wanted text;expected_status integer;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id;
 wanted:=CASE WHEN NEW.report->>'status'='STALE' THEN 'STALE' ELSE 'APPROVED' END;expected_status:=CASE WHEN wanted='STALE' THEN 409 ELSE 200 END;
 IF p.state::text<>wanted OR p.version<>NEW.resulting_version OR p.revision<>NEW.revision OR p.latest_simulation_id<>NEW.simulation_id OR NEW.tx_id<>pg_current_xact_id() OR lv.review_blocker(p,NEW.checked_by) IS NOT NULL
 OR NEW.review_granted_at IS DISTINCT FROM (SELECT max(mr.granted_at) FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=NEW.checked_by AND rp.permission='review')
 OR NEW.review_granted_at>NEW.checked_at-interval '24 hours' OR NEW.report IS DISTINCT FROM lv.proposal_freshness(p)
 OR NOT EXISTS(SELECT 1 FROM lv.proposal_transition WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND version=p.version AND revision=p.revision AND action='approve' AND user_id=NEW.checked_by AND session_id=NEW.session_id AND tx_id=pg_current_xact_id() AND idempotency_key=NEW.idempotency_key AND request_hash=NEW.request_hash AND to_state=p.state)
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=p.tenant_id AND user_id=NEW.checked_by AND session_id=NEW.session_id AND event_type=CASE WHEN wanted='STALE' THEN 'PROPOSAL_APPROVAL_STALE' ELSE 'PROPOSAL_APPROVED' END AND detail->>'check_id'=NEW.id::text AND detail->>'proposal_id'=p.id::text AND detail->>'version'=p.version::text)
 OR NOT EXISTS(SELECT 1 FROM lv.api_idempotency WHERE tenant_id=p.tenant_id AND principal_id=NEW.checked_by AND method='POST' AND route='/api/v1/proposals/{proposal_id}/approve' AND idempotency_key=NEW.idempotency_key AND request_hash=NEW.request_hash AND response_status=expected_status AND ((wanted='STALE' AND response->>'code'='PROPOSAL_STALE') OR (wanted='APPROVED' AND response->'body'=lv.proposal_json(p))))
 OR (wanted='STALE' AND (EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED')))
 THEN RAISE EXCEPTION 'approval check lacks independent actor, current freshness or atomic artifacts' USING ERRCODE='23514'; END IF;
 IF wanted='APPROVED' AND NOT EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND check_id=NEW.id AND approved_by=NEW.checked_by AND submitted_by=p.created_by AND simulation_id=p.latest_simulation_id AND tx_id=pg_current_xact_id()) THEN RAISE EXCEPTION 'approval lacks its revision decision' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION lv.validate_proposal_decision() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;c lv.approval_check;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id;
 SELECT * INTO c FROM lv.approval_check WHERE tenant_id=NEW.tenant_id AND id=NEW.check_id;
 IF p.state<>'APPROVED' OR NEW.kind<>'APPROVED' OR NEW.tx_id<>pg_current_xact_id() OR NEW.resulting_version<>p.version OR NEW.revision<>p.revision OR NEW.simulation_id<>p.latest_simulation_id OR NEW.submitted_by<>p.created_by OR lv.review_blocker(p,NEW.approved_by) IS NOT NULL
 OR c.proposal_id<>p.id OR c.revision<>p.revision OR c.report->>'status'<>'FRESH' OR c.resulting_version<>p.version OR c.checked_by<>NEW.approved_by OR c.session_id<>NEW.session_id OR c.comment IS DISTINCT FROM NEW.comment OR c.tx_id<>pg_current_xact_id()
 THEN RAISE EXCEPTION 'decision lacks its independent revision-bound check' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER approval_check_complete AFTER INSERT ON lv.approval_check DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_approval_check();
CREATE CONSTRAINT TRIGGER decision_complete AFTER INSERT ON lv.proposal_decision DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_proposal_decision();
REVOKE ALL ON FUNCTION lv.validate_approval_check(),lv.validate_proposal_decision() FROM PUBLIC;
