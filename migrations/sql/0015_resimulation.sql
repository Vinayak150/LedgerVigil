-- RC-1 explicit maker freshness check is an additive SANDBOX API contract; accepted documents unchanged.
CREATE TABLE lv.proposal_freshness_check (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,revision integer NOT NULL,
 observed_version integer NOT NULL,resulting_version integer NOT NULL,report jsonb NOT NULL,user_id uuid NOT NULL,session_id uuid NOT NULL,
 idempotency_key text NOT NULL,request_hash text NOT NULL,tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 FOREIGN KEY(user_id) REFERENCES lv_identity.app_user(id),FOREIGN KEY(session_id) REFERENCES lv_identity.session(id),
 CHECK(report->>'status' IN ('FRESH','STALE')),CHECK(resulting_version=observed_version+CASE WHEN report->>'status'='STALE' THEN 1 ELSE 0 END)
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proposal_freshness_check FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check,DROP CONSTRAINT transition_actor;
ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale','resimulate','revalidate'));
ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw','resimulate','revalidate') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL));
ALTER TABLE lv.proposal_revision DROP CONSTRAINT proposal_revision_change_check,DROP CONSTRAINT proposal_revision_check1;
ALTER TABLE lv.proposal_revision ADD CONSTRAINT proposal_revision_change_check CHECK(change IN ('CREATED','EDITED','RESIMULATED'));
ALTER TABLE lv.proposal_revision ADD CONSTRAINT proposal_revision_sequence_kind CHECK((revision=1 AND previous_revision IS NULL AND change='CREATED') OR (revision>1 AND previous_revision=revision-1 AND change IN ('EDITED','RESIMULATED')));

CREATE OR REPLACE FUNCTION lv.guard_proposal_update() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'proposals are retained' USING ERRCODE='23514'; END IF;
 IF (NEW.tenant_id,NEW.ledger_id,NEW.id,NEW.origin,NEW.target_adapter,NEW.created_by,NEW.created_at)
 IS DISTINCT FROM (OLD.tenant_id,OLD.ledger_id,OLD.id,OLD.origin,OLD.target_adapter,OLD.created_by,OLD.created_at) THEN
 RAISE EXCEPTION 'proposal identity is immutable' USING ERRCODE='23514'; END IF;
 IF OLD.state='DRAFT' AND NEW.state='DRAFT' AND NEW.revision=OLD.revision+1 AND NEW.version=OLD.version+1
 AND NEW.submitted_by IS NULL AND NEW.snapshot_id IS NULL AND NEW.latest_simulation_id IS NULL THEN RETURN NEW; END IF;
 IF OLD.state='STALE' AND NEW.state='SUBMITTED' AND NEW.revision=OLD.revision+1 AND NEW.version=OLD.version+1 AND NEW.submitted_by=OLD.created_by AND NEW.snapshot_id IS NULL AND NEW.latest_simulation_id IS NULL THEN RETURN NEW; END IF;
 IF NEW.revision<>OLD.revision THEN RAISE EXCEPTION 'workflow cannot edit revisions' USING ERRCODE='23514'; END IF;
 IF OLD.state='DRAFT' AND NEW.state='SUBMITTED' AND NEW.version=OLD.version+1 AND NEW.submitted_by=OLD.created_by
 AND NEW.snapshot_id IS NULL AND NEW.latest_simulation_id IS NULL THEN RETURN NEW; END IF;
 IF OLD.state='SUBMITTED' AND NEW.state IN ('BLOCKED','READY_FOR_REVIEW') AND NEW.version=OLD.version
 AND NEW.submitted_by=OLD.submitted_by AND NEW.snapshot_id IS NOT NULL AND NEW.latest_simulation_id IS NOT NULL THEN RETURN NEW; END IF;
 IF OLD.state IN ('DRAFT','BLOCKED','READY_FOR_REVIEW','STALE') AND NEW.state='WITHDRAWN' AND NEW.version=OLD.version+1
 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 IF OLD.state IN ('BLOCKED','READY_FOR_REVIEW') AND NEW.state='STALE' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'unsupported proposal transition' USING ERRCODE='23514';
END $$;
CREATE OR REPLACE FUNCTION lv.workflow_request(p_token bytea,p_action text,p_key text,p_hash text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;prior lv.api_idempotency;v_route text:='/api/v1/proposals/{proposal_id}/'||p_action;info jsonb;
BEGIN
 s:=lv.proposal_session(p_token,p_source,p_request);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'propose') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires propose'); END IF;
 IF p_action NOT IN ('submit','withdraw','resimulate','revalidate') OR p_key IS NULL OR p_key!~'^[!-~]{1,64}$' OR p_hash!~'^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'invalid workflow request' USING ERRCODE='22023'; END IF;
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
CREATE OR REPLACE FUNCTION lv.workflow_refused(p_token bytea,p_action text,p_key text,p_hash text,p_problem jsonb,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;
BEGIN
 s:=lv.proposal_session(p_token,p_source,p_request);
 IF p_action NOT IN ('submit','withdraw','resimulate','revalidate') OR (p_problem->>'status')::integer NOT BETWEEN 400 AND 499 THEN RAISE EXCEPTION 'invalid refusal' USING ERRCODE='22023'; END IF;
 PERFORM lv_identity.audit_append('PROPOSAL_'||upper(p_action)||'_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('reason',p_problem->>'code'));
 RETURN lv.workflow_cache(s,p_action,p_key,p_hash,p_problem);
END $$;
CREATE OR REPLACE FUNCTION lv.validate_proposal_revision() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE p lv.proposal; n integer; items integer; ops integer; evidence integer;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id = NEW.tenant_id AND id = NEW.proposal_id;
 SELECT count(*), coalesce(max(revision), 0) INTO n, ops FROM lv.proposal_revision WHERE tenant_id = NEW.tenant_id AND proposal_id = NEW.proposal_id;
 IF p.id IS NULL OR n <> ops OR p.revision <> n OR p.ledger_id <> NEW.ledger_id THEN
  RAISE EXCEPTION 'proposal head does not match its revision history' USING ERRCODE = '23514';
 END IF;
 IF NEW.revision = 1 AND p.created_by <> NEW.authored_by THEN
  RAISE EXCEPTION 'the maker must be the author of revision 1' USING ERRCODE = '23514';
 END IF;
 SELECT coalesce(sum(jsonb_array_length(o -> 'items')), 0), count(*) INTO items, ops FROM jsonb_array_elements(NEW.operations) o;
 SELECT jsonb_array_length(NEW.evidence_refs) INTO evidence;
 IF (SELECT count(*) FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision AND r.kind = 'SETTLEMENT_ITEM_PAYMENT') <> items
    OR (SELECT count(*) FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision AND r.kind = 'SETTLEMENT_STATEMENT') <> ops THEN
  RAISE EXCEPTION 'revision references do not match its content' USING ERRCODE = '23514';
 END IF;
 IF (SELECT count(*) FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision AND r.kind IN ('EVIDENCE_SOURCE_RECORD', 'EVIDENCE_DOCUMENT_VERSION', 'EVIDENCE_STAGED_DOCUMENT')) <> evidence THEN
  RAISE EXCEPTION 'revision evidence references do not match its content' USING ERRCODE = '23514';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM lv_identity.audit_event a WHERE a.chain_key = NEW.tenant_id AND a.tenant_id = NEW.tenant_id
                AND a.event_type = CASE WHEN NEW.revision = 1 THEN 'PROPOSAL_CREATED' WHEN NEW.change='RESIMULATED' THEN 'PROPOSAL_RESIMULATED' ELSE 'PROPOSAL_EDITED' END
                AND a.user_id = NEW.authored_by AND a.session_id = NEW.session_id
                AND a.detail ->> 'proposal_id' = NEW.proposal_id::text AND a.detail ->> 'revision' = NEW.revision::text
                AND a.detail ->> 'content_hash' = NEW.content_hash) THEN
  RAISE EXCEPTION 'a proposal revision must commit with its audit event' USING ERRCODE = '23514';
 END IF;
 -- Verify identity and pointer as well as counts: same-ledger substitutions must not pass.
 IF EXISTS (
  WITH expected AS (
   SELECT 'SETTLEMENT_ITEM_PAYMENT'::text AS kind,
          '/operations/' || (o.ord - 1)::text || '/items/' || (i.ord - 1)::text || '/document_id' AS pointer,
          (i.item ->> 'document_id')::uuid AS document_id, NULL::uuid AS staged_document_id,
          NULL::uuid AS source_record_id, NULL::uuid AS document_version_id
    FROM jsonb_array_elements(NEW.operations) WITH ORDINALITY o(op, ord)
    CROSS JOIN LATERAL jsonb_array_elements(o.op -> 'items') WITH ORDINALITY i(item, ord)
   UNION ALL
   SELECT 'SETTLEMENT_STATEMENT', '/operations/' || (o.ord - 1)::text || '/statement_document_id',
          NULL::uuid, (o.op ->> 'statement_document_id')::uuid, NULL::uuid, NULL::uuid
    FROM jsonb_array_elements(NEW.operations) WITH ORDINALITY o(op, ord)
   UNION ALL
   SELECT 'EVIDENCE_' || (e.ref ->> 'kind'), '/evidence_refs/' || (e.ord - 1)::text || '/id', NULL::uuid,
          CASE WHEN e.ref ->> 'kind' = 'STAGED_DOCUMENT' THEN (e.ref ->> 'id')::uuid END,
          CASE WHEN e.ref ->> 'kind' = 'SOURCE_RECORD' THEN (e.ref ->> 'id')::uuid END,
          CASE WHEN e.ref ->> 'kind' = 'DOCUMENT_VERSION' THEN (e.ref ->> 'id')::uuid END
    FROM jsonb_array_elements(NEW.evidence_refs) WITH ORDINALITY e(ref, ord)
  ), actual AS (
   SELECT r.kind, r.pointer, r.document_id, r.staged_document_id, r.source_record_id, r.document_version_id
    FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision
  )
  SELECT 1 FROM ((SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
                 UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)) mismatch
 ) THEN
  RAISE EXCEPTION 'revision reference identities do not match its content' USING ERRCODE = '23514';
 END IF;
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION lv.workflow_resimulate_begin(p_token bytea,p_id uuid,p_match text,p_key text,p_hash text,p_source text,p_request text) RETURNS jsonb
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
 IF p.state<>'STALE' THEN PERFORM lv.proposal_refusal(409,'INVALID_STATE_TRANSITION','only STALE may be re-simulated',NULL,jsonb_build_object('state',p.state)); END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' THEN PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','native SANDBOX only',NULL,NULL); END IF;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 IF NOT FOUND THEN PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','no committed ledger snapshot',NULL,NULL); END IF;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 SELECT * INTO r FROM lv.proposal_revision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 INSERT INTO lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision,previous_revision,change,authored_by,session_id,target_adapter,operations,evidence_refs,rationale,content_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision+1,p.revision,'RESIMULATED',s.user_id,s.id,r.target_adapter,r.operations,r.evidence_refs,r.rationale,r.content_hash);
 INSERT INTO lv.proposal_reference SELECT tenant_id,ledger_id,proposal_id,revision+1,ordinal,pointer,kind,document_id,staged_document_id,source_record_id,document_version_id FROM lv.proposal_reference WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 SELECT * INTO r FROM lv.proposal_revision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision+1;
 IF jsonb_array_length(r.operations)<>1 OR jsonb_array_length(r.operations->0->'items')>200 THEN
 PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','only one synchronous RecordSettlement is available; no worker exists',NULL,NULL); END IF;
 op:=r.operations->0;
 SELECT * INTO st FROM lv.staged_document WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=(op->>'statement_document_id')::uuid;
 IF (st.processor_ref,st.payout_id,st.payout_date::text) IS DISTINCT FROM (op->>'processor_ref',op->>'payout_id',op->>'payout_date') THEN
 PERFORM lv.proposal_refusal(422,'PRECONDITION_FAILED','payout identity must come from the cited statement',NULL,jsonb_build_object('reason','STATEMENT_IDENTITY_MISMATCH')); END IF;
 FOR d IN SELECT DISTINCT staged_document_id FROM lv.proposal_reference WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=r.revision AND staged_document_id IS NOT NULL ORDER BY staged_document_id LOOP
 INSERT INTO lv.staged_workflow VALUES(p.tenant_id,p.ledger_id,d,'VALID',1,NULL,NULL) ON CONFLICT DO NOTHING;
 SELECT * INTO w FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND staged_document_id=d FOR UPDATE;
 IF w.status<>'VALID' THEN PERFORM lv.proposal_refusal(422,'PRECONDITION_FAILED','staged document is reserved',NULL,jsonb_build_object('reason','STAGED_DOCUMENT_RESERVED')); END IF;
 UPDATE lv.staged_workflow SET status='RESERVED',version=version+1,proposal_id=p.id,revision=r.revision WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND staged_document_id=d AND status='VALID' AND version=w.version;
 IF NOT FOUND THEN RAISE EXCEPTION 'reservation CAS lost' USING ERRCODE='40001'; END IF;
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id)
 VALUES(p.tenant_id,p.ledger_id,d,w.version+1,'RESERVED',p.id,r.revision,s.user_id,s.id);
 END LOOP;
 UPDATE lv.proposal SET state='SUBMITTED',revision=r.revision,version=version+1,snapshot_id=NULL,latest_simulation_id=NULL,submitted_by=s.user_id,updated_at=statement_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,'STALE','SUBMITTED',s.user_id,s.id,'resimulate',p_key,p_hash);
 RETURN jsonb_build_object('revision',p.revision,'version',p.version);
END $$;
CREATE OR REPLACE FUNCTION lv.workflow_finish(p_token bytea,p_id uuid,p_input text,p_input_hash text,p_output text,p_output_hash text,p_source text,p_request text) RETURNS jsonb
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
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,1,'SUBMITTED',target,s.user_id,s.id,tr.action,tr.idempotency_key,tr.request_hash);
 PERFORM lv_identity.audit_append(CASE WHEN tr.action='resimulate' THEN 'PROPOSAL_RESIMULATED' ELSE 'PROPOSAL_SUBMITTED' END,'ALLOWED',s.user_id,p.tenant_id,s.id,p_source,p_request,
 jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'simulation_id',sid::text,'aggregate',agg,'content_hash',r.content_hash));
 RETURN lv.workflow_cache(s,tr.action,tr.idempotency_key,tr.request_hash,jsonb_build_object('status',200,'etag','v'||p.version::text,'body',jsonb_build_object('proposal',lv.proposal_json(p),'simulation',lv.workflow_simulation_json(sid))));
END $$;
CREATE OR REPLACE FUNCTION lv.validate_submission() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;r lv.simulation_run;ls lv.ledger_snapshot;b jsonb;o jsonb;rs jsonb;vs jsonb;act text;
BEGIN
 IF TG_TABLE_NAME='proposal' THEN SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
 ELSE SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id; END IF;
 IF p.state='DRAFT' THEN
 IF p.submitted_by IS NOT NULL OR p.latest_simulation_id IS NOT NULL OR p.snapshot_id IS NOT NULL THEN RAISE EXCEPTION 'draft has submitted artifacts' USING ERRCODE='23514'; END IF;RETURN NULL;
 END IF;
 IF p.state='SUBMITTED' THEN RAISE EXCEPTION 'incomplete submission cannot commit' USING ERRCODE='23514'; END IF;
 IF p.state='STALE' THEN
 IF EXISTS(SELECT 1 FROM lv.proposal_freshness_check c JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.revision,t.version)=(c.tenant_id,c.proposal_id,c.revision,c.resulting_version)
 WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.report->>'status'='STALE' AND c.tx_id=pg_current_xact_id() AND t.tx_id=pg_current_xact_id() AND t.action='revalidate' AND t.user_id=p.created_by AND t.session_id=c.session_id)
 THEN
 IF EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'freshness transition left reservations' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
 IF session_user<>'lv_worker' OR NOT EXISTS(SELECT 1 FROM lv.proposal_transition t JOIN lv.work_item w ON (w.tenant_id,w.id)=(t.tenant_id,t.worker_job_id)
 JOIN lv.maintenance_event e ON (e.tenant_id,e.job_id,e.proposal_id,e.proposal_version,e.fencing_token)=(t.tenant_id,t.worker_job_id,t.proposal_id,t.version,t.worker_fencing_token)
 WHERE t.tenant_id=p.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.to_state='STALE' AND t.action='stale' AND t.tx_id=pg_current_xact_id() AND e.tx_id=pg_current_xact_id()
 AND w.state='CLAIMED' AND w.fencing_token=t.worker_fencing_token AND w.lease_expires_at>clock_timestamp())
 OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED')
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=p.tenant_id AND event_type='PROPOSAL_STALED' AND user_id IS NULL AND detail->>'proposal_id'=p.id::text AND detail->>'version'=p.version::text)
 THEN RAISE EXCEPTION 'stale transition lacks its fenced system proof, release or audit' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
 act:=CASE WHEN p.state='WITHDRAWN' THEN 'withdraw' ELSE coalesce((SELECT action FROM lv.proposal_transition WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND version=p.version AND phase=0 AND tx_id=pg_current_xact_id()),'submit') END;
 IF NOT EXISTS(SELECT 1 FROM lv.proposal_transition t JOIN lv.api_idempotency i ON i.tenant_id=t.tenant_id AND i.principal_id=t.user_id
 AND i.method='POST' AND i.route='/api/v1/proposals/{proposal_id}/'||t.action AND i.idempotency_key=t.idempotency_key AND i.request_hash=t.request_hash AND i.response_status=200
 WHERE t.tenant_id=p.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.to_state=p.state AND t.tx_id=pg_current_xact_id() AND t.user_id=p.created_by)
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event a WHERE a.chain_key=p.tenant_id AND a.tenant_id=p.tenant_id AND a.user_id=p.created_by
 AND a.event_type=CASE WHEN act='withdraw' THEN 'PROPOSAL_WITHDRAWN' WHEN act='resimulate' THEN 'PROPOSAL_RESIMULATED' ELSE 'PROPOSAL_SUBMITTED' END AND a.detail->>'proposal_id'=p.id::text AND a.detail->>'version'=p.version::text)
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
CREATE OR REPLACE FUNCTION lv.validate_reservation() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.staged_workflow;e lv.reservation_event;p lv.proposal;
BEGIN
 SELECT * INTO w FROM lv.staged_workflow WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND staged_document_id=NEW.staged_document_id;
 SELECT * INTO e FROM lv.reservation_event WHERE tenant_id=w.tenant_id AND staged_document_id=w.staged_document_id AND version=w.version;
 IF e.staged_document_id IS NULL OR (w.status='RESERVED') IS DISTINCT FROM (e.event='RESERVED') THEN RAISE EXCEPTION 'reservation history incomplete' USING ERRCODE='23514'; END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=e.tenant_id AND id=e.proposal_id;
 IF e.worker_job_id IS NOT NULL THEN
 IF session_user<>'lv_worker' OR w.status<>'VALID' OR p.state<>'STALE' OR NOT EXISTS(SELECT 1 FROM lv.maintenance_event ev WHERE ev.tenant_id=e.tenant_id AND ev.job_id=e.worker_job_id AND ev.fencing_token=e.worker_fencing_token AND ev.proposal_id=p.id AND ev.proposal_version=p.version AND ev.event='STALE' AND ev.tx_id=pg_current_xact_id()) THEN RAISE EXCEPTION 'system release lacks stale proof' USING ERRCODE='23514'; END IF;
 ELSE
 IF w.status='VALID' AND p.state='STALE' AND NOT EXISTS(SELECT 1 FROM lv.proposal_freshness_check c WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.tx_id=pg_current_xact_id() AND c.report->>'status'='STALE' AND c.user_id=e.user_id) THEN RAISE EXCEPTION 'human stale release lacks freshness proof' USING ERRCODE='23514'; END IF;
 IF e.user_id<>p.created_by THEN RAISE EXCEPTION 'reservation actor is not maker' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND NOT EXISTS(SELECT 1 FROM lv.proposal_reference ref WHERE ref.tenant_id=w.tenant_id AND ref.proposal_id=w.proposal_id AND ref.revision=w.revision AND ref.staged_document_id=w.staged_document_id) THEN RAISE EXCEPTION 'uncited reservation' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND (p.state NOT IN ('BLOCKED','READY_FOR_REVIEW') OR w.proposal_id<>p.id OR w.revision<>p.revision) THEN RAISE EXCEPTION 'orphan reservation' USING ERRCODE='23514'; END IF;
 IF w.status='VALID' AND p.state NOT IN ('WITHDRAWN','STALE') THEN RAISE EXCEPTION 'release requires a completed withdrawal' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION lv.proposal_freshness(p lv.proposal) RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE b jsonb;changes jsonb:='[]';v record;actual integer;current_seq bigint;op jsonb;expected jsonb;observed jsonb;ref jsonb;
BEGIN
 SELECT input_canonical::jsonb INTO b FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF b IS NULL OR b->'versions'->>'simulator' IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' OR b->'versions'->>'account_read_set' IS DISTINCT FROM 'lv-account-dependencies/1' THEN
 PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','freshness requires the complete native dependency profile',NULL,NULL); END IF;
 op:=b->'proposal'->'operations'->0;
 SELECT last_seq INTO current_seq FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id;
 FOR v IN SELECT * FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision ORDER BY entity_type,entity_id LOOP
 actual:=NULL;
 IF v.entity_type='ACCOUNT' THEN SELECT version INTO actual FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=v.entity_id;
 ELSIF v.entity_type='DOCUMENT' THEN SELECT version INTO actual FROM lv.document_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND document_id=v.entity_id;
 ELSIF v.entity_type='PERIOD' THEN
 SELECT version INTO actual FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=v.entity_id;
 IF EXISTS(SELECT 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=v.entity_id AND status='CLOSED') THEN changes:=changes||jsonb_build_array(jsonb_build_object('reason','PERIOD_CLOSED','entity_id',v.entity_id::text)); END IF;
 END IF;
 IF actual IS DISTINCT FROM v.version THEN changes:=changes||jsonb_build_array(jsonb_build_object('reason','DEPENDENCY_VERSION_CHANGED','entity_type',v.entity_type,'entity_id',v.entity_id::text,'pinned_version',v.version,'current_version',actual)); END IF;
 END LOOP;
 SELECT coalesce(jsonb_agg(jsonb_build_object('entity_type',entity_type,'entity_id',entity_id::text,'version',version) ORDER BY entity_type,entity_id),'[]') INTO observed FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 IF observed IS DISTINCT FROM b->'read_set' THEN changes:=changes||'[{"reason":"PINNED_DEPENDENCIES_INCOMPLETE"}]'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_array(role::text,account_id::text) ORDER BY role::text),'[]') INTO observed FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id;
 SELECT coalesce(jsonb_agg(jsonb_build_array(x->>'role',x->>'account_id') ORDER BY x->>'role'),'[]') INTO expected FROM jsonb_array_elements(b->'facts'->'bindings') x;
 IF observed IS DISTINCT FROM expected THEN changes:=changes||'[{"reason":"ROLE_BINDINGS_CHANGED"}]'; END IF;
 IF EXISTS(SELECT 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id AND (profile_version IS DISTINCT FROM b->'ledger'->>'profile_version' OR currency::text IS DISTINCT FROM b->'ledger'->>'currency' OR currency_exponent IS DISTINCT FROM (b->'ledger'->>'currency_exponent')::integer)) THEN changes:=changes||'[{"reason":"LEDGER_PROFILE_CHANGED"}]'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('import_batch_id',ib.id::text,'source_file_sha256',f.sha256,'failure_code',ib.failure_code) ORDER BY ib.id),'[]') INTO observed
 FROM lv.import_batch ib JOIN lv.source_file f ON (f.tenant_id,f.ledger_id,f.id)=(ib.tenant_id,ib.ledger_id,ib.source_file_id)
 WHERE ib.tenant_id=p.tenant_id AND ib.ledger_id=p.ledger_id AND ib.status='REJECTED' AND ib.related_staged_document_id=(op->>'statement_document_id')::uuid;
 IF observed IS DISTINCT FROM b->'facts'->'statement_conflicts' THEN changes:=changes||'[{"reason":"STATEMENT_CONFLICT_RANGE_CHANGED"}]'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('processor_ref',i.processor_ref,'processor_item_id',i.processor_item_id,'document_id',i.document_id::text) ORDER BY i.processor_ref,i.processor_item_id),'[]') INTO observed FROM lv.processor_payment_identity i
 WHERE i.tenant_id=p.tenant_id AND i.ledger_id=p.ledger_id AND i.processor_item_id IN (SELECT value->>1 FROM jsonb_array_elements(b->'facts'->'statement'->'items'));
 IF observed IS DISTINCT FROM b->'facts'->'statement_identities' THEN changes:=changes||'[{"reason":"STATEMENT_PAYMENT_RANGE_CHANGED"}]'; END IF;
 IF EXISTS(SELECT 1 FROM lv.processor_settlement_identity WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND processor_ref=op->>'processor_ref' AND payout_id=op->>'payout_id') AND b->'facts'->'payout_settlement'='null'::jsonb THEN changes:=changes||'[{"reason":"PAYOUT_NOW_USED"}]'; END IF;
 IF EXISTS(SELECT 1 FROM lv.settlement_inclusion_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND item_document_id IN (SELECT (value->>'document_id')::uuid FROM jsonb_array_elements(op->'items'))) AND b->'facts'->'active_inclusions'='[]'::jsonb THEN changes:=changes||'[{"reason":"PAYMENT_NOW_INCLUDED"}]'; END IF;
 FOR ref IN SELECT value FROM jsonb_array_elements(b->'pinned_sources'->'evidence') LOOP
 IF ref->>'kind'='DOCUMENT_VERSION' AND (SELECT version FROM lv.document_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND document_id=(ref->>'document_id')::uuid) IS DISTINCT FROM (ref->>'current_version')::integer THEN changes:=changes||jsonb_build_array(jsonb_build_object('reason','EVIDENCE_CURRENT_VERSION_CHANGED','entity_id',ref->>'document_id')); END IF;
 END LOOP;
 RETURN jsonb_build_object('status',CASE WHEN changes='[]'::jsonb THEN 'FRESH' ELSE 'STALE' END,'changes',changes,'pinned_seq',(b->'snapshot'->>'seq')::bigint,'observed_seq',current_seq,'scope','native RecordSettlement dependencies; point-in-time, not approval');
END $$;
CREATE FUNCTION lv.workflow_revalidate(p_token bytea,p_id uuid,p_match text,p_key text,p_hash text,p_source text,p_request text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;report jsonb;check_id uuid:=uuidv7();previous lv.proposal_state;before_version integer;
BEGIN
 s:=lv.workflow_final_session(p_token,p_source,p_request);
 IF p_match IS NULL THEN PERFORM lv.proposal_refusal(428,'VERSION_REQUIRED','If-Match required',NULL,NULL); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=p_id FOR UPDATE;
 IF NOT FOUND THEN PERFORM lv.proposal_refusal(404,'NOT_FOUND','proposal not found',NULL,NULL); END IF;
 IF p.created_by<>s.user_id THEN PERFORM lv.proposal_refusal(403,'PERMISSION_DENIED','maker only',NULL,NULL); END IF;
 IF p_match<>'"v'||p.version::text||'"' THEN PERFORM lv.proposal_refusal(412,'VERSION_MISMATCH','proposal has changed',NULL,NULL); END IF;
 IF p.state NOT IN ('BLOCKED','READY_FOR_REVIEW') THEN PERFORM lv.proposal_refusal(409,'INVALID_STATE_TRANSITION','only pending submitted proposals may be checked',NULL,NULL); END IF;
 PERFORM 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 report:=lv.proposal_freshness(p);previous:=p.state;before_version:=p.version;
 IF report->>'status'='STALE' THEN
 UPDATE lv.proposal SET state='STALE',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,previous,'STALE',s.user_id,s.id,'revalidate',p_key,p_hash);
 WITH released AS(UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id) SELECT tenant_id,ledger_id,staged_document_id,version,'RELEASED',p.id,p.revision,s.user_id,s.id FROM released;
 END IF;
 INSERT INTO lv.proposal_freshness_check(tenant_id,ledger_id,id,proposal_id,revision,observed_version,resulting_version,report,user_id,session_id,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,check_id,p.id,p.revision,before_version,p.version,report,s.user_id,s.id,p_key,p_hash);
 PERFORM lv_identity.audit_append('PROPOSAL_REVALIDATED','ALLOWED',s.user_id,p.tenant_id,s.id,p_source,p_request,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'check_id',check_id::text,'status',report->>'status'));
 RETURN lv.workflow_cache(s,'revalidate',p_key,p_hash,jsonb_build_object('status',200,'etag','v'||p.version::text,'body',jsonb_build_object('proposal',lv.proposal_json(p),'freshness',report,'check_id',check_id::text)));
END $$;
CREATE FUNCTION lv.validate_freshness_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id;
 IF p.id IS NULL OR NEW.tx_id<>pg_current_xact_id() OR p.version<>NEW.resulting_version OR p.revision<>NEW.revision OR p.created_by<>NEW.user_id OR NEW.report IS DISTINCT FROM lv.proposal_freshness(p)
 OR (NEW.report->>'status'='STALE') IS DISTINCT FROM (p.state='STALE')
 OR NOT EXISTS(SELECT 1 FROM lv.api_idempotency WHERE tenant_id=NEW.tenant_id AND principal_id=NEW.user_id AND route='/api/v1/proposals/{proposal_id}/revalidate' AND idempotency_key=NEW.idempotency_key AND request_hash=NEW.request_hash AND response_status=200 AND response->'body'->>'check_id'=NEW.id::text)
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=NEW.tenant_id AND event_type='PROPOSAL_REVALIDATED' AND user_id=NEW.user_id AND session_id=NEW.session_id AND detail->>'check_id'=NEW.id::text)
 THEN RAISE EXCEPTION 'freshness check lacks current report, cache or audit' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER freshness_complete AFTER INSERT ON lv.proposal_freshness_check DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_freshness_check();
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC;
