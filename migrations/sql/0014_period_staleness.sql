-- Native period-close staleness only. No financial posting or human decision authority.
ALTER TABLE lv.proposal DROP CONSTRAINT proposal_workflow_state;
ALTER TABLE lv.proposal ADD CONSTRAINT proposal_workflow_state CHECK(state IN ('DRAFT','SUBMITTED','BLOCKED','READY_FOR_REVIEW','STALE','WITHDRAWN'));
ALTER TABLE lv.work_item ADD COLUMN scan_cursor uuid, ADD COLUMN scanned_count integer NOT NULL DEFAULT 0 CHECK(scanned_count>=0), ADD COLUMN stale_count integer NOT NULL DEFAULT 0 CHECK(stale_count>=0), ADD COLUMN delivery_generation bigint NOT NULL DEFAULT 0 CHECK(delivery_generation>=0);
CREATE POLICY maintenance_queue_read ON lv.work_item FOR SELECT TO lv_owner USING(session_user='lv_worker');
CREATE POLICY maintenance_queue_read ON lv.outbox FOR SELECT TO lv_owner USING(session_user='lv_worker');
CREATE POLICY maintenance_queue_lock ON lv.work_item FOR UPDATE TO lv_owner USING(session_user='lv_worker') WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE POLICY maintenance_queue_lock ON lv.outbox FOR UPDATE TO lv_owner USING(session_user='lv_worker') WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE TABLE lv.maintenance_event (
 tenant_id uuid NOT NULL,job_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),fencing_token bigint NOT NULL,
 event text NOT NULL CHECK(event IN ('CLAIMED','STALE','DONE','RETRY','REDELIVERED','DEAD')),proposal_id uuid,proposal_version integer,
 worker_name text NOT NULL,detail jsonb NOT NULL DEFAULT '{}',tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),FOREIGN KEY(tenant_id,job_id) REFERENCES lv.work_item(tenant_id,id),
 FOREIGN KEY(tenant_id,proposal_id) REFERENCES lv.proposal(tenant_id,id),
 CHECK((proposal_id IS NOT NULL)=(event='STALE')),CHECK((proposal_version IS NOT NULL)=(event='STALE'))
);
CREATE UNIQUE INDEX maintenance_stale_once ON lv.maintenance_event(tenant_id,proposal_id,proposal_version) WHERE event='STALE';
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.maintenance_event FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
ALTER TABLE lv.proposal_transition DROP CONSTRAINT proposal_transition_action_check;
ALTER TABLE lv.proposal_transition ADD CONSTRAINT proposal_transition_action_check CHECK(action IN ('submit','withdraw','stale'));
ALTER TABLE lv.proposal_transition ALTER COLUMN user_id DROP NOT NULL, ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE lv.proposal_transition ADD COLUMN worker_job_id uuid,ADD COLUMN worker_fencing_token bigint;
ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_worker_fkey FOREIGN KEY(tenant_id,worker_job_id) REFERENCES lv.work_item(tenant_id,id);
ALTER TABLE lv.proposal_transition ADD CONSTRAINT transition_actor CHECK((action='stale' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL) OR (action IN ('submit','withdraw') AND user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL));
ALTER TABLE lv.reservation_event ALTER COLUMN user_id DROP NOT NULL, ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE lv.reservation_event ADD COLUMN worker_job_id uuid, ADD COLUMN worker_fencing_token bigint;
ALTER TABLE lv.reservation_event ADD CONSTRAINT reservation_worker_fkey FOREIGN KEY(tenant_id,worker_job_id) REFERENCES lv.work_item(tenant_id,id);
ALTER TABLE lv.reservation_event ADD CONSTRAINT reservation_actor CHECK((user_id IS NOT NULL AND session_id IS NOT NULL AND worker_job_id IS NULL AND worker_fencing_token IS NULL) OR (event='RELEASED' AND user_id IS NULL AND session_id IS NULL AND worker_job_id IS NOT NULL AND worker_fencing_token IS NOT NULL));

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
 IF OLD.state IN ('DRAFT','BLOCKED','READY_FOR_REVIEW','STALE') AND NEW.state='WITHDRAWN' AND NEW.version=OLD.version+1
 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 IF OLD.state IN ('BLOCKED','READY_FOR_REVIEW') AND NEW.state='STALE' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'unsupported proposal transition' USING ERRCODE='23514';
END $$;
CREATE OR REPLACE FUNCTION lv.workflow_withdraw(p_token bytea,p_id uuid,p_match text,p_key text,p_hash text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;w lv.staged_workflow;previous lv.proposal_state;
BEGIN
 s:=lv.workflow_final_session(p_token,p_source,p_request);
 IF p_match IS NULL THEN PERFORM lv.proposal_refusal(428,'VERSION_REQUIRED','If-Match is required',NULL,NULL); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=p_id FOR UPDATE;
 IF NOT FOUND THEN PERFORM lv.proposal_refusal(404,'NOT_FOUND','proposal not found',NULL,NULL); END IF;
 IF p.created_by<>s.user_id THEN PERFORM lv.proposal_refusal(403,'PERMISSION_DENIED','only the maker may withdraw',NULL,NULL); END IF;
 IF p_match<>'"v'||p.version::text||'"' THEN PERFORM lv.proposal_refusal(412,'VERSION_MISMATCH','proposal has changed',NULL,jsonb_build_object('current_version',p.version)); END IF;
 IF p.state NOT IN ('DRAFT','BLOCKED','READY_FOR_REVIEW','STALE') THEN PERFORM lv.proposal_refusal(409,'INVALID_STATE_TRANSITION','withdrawal is unavailable from this state',NULL,jsonb_build_object('state',p.state)); END IF;
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
 IF session_user<>'lv_worker' OR NOT EXISTS(SELECT 1 FROM lv.proposal_transition t JOIN lv.work_item w ON (w.tenant_id,w.id)=(t.tenant_id,t.worker_job_id)
 JOIN lv.maintenance_event e ON (e.tenant_id,e.job_id,e.proposal_id,e.proposal_version,e.fencing_token)=(t.tenant_id,t.worker_job_id,t.proposal_id,t.version,t.worker_fencing_token)
 WHERE t.tenant_id=p.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.to_state='STALE' AND t.action='stale' AND t.tx_id=pg_current_xact_id() AND e.tx_id=pg_current_xact_id()
 AND w.state='CLAIMED' AND w.fencing_token=t.worker_fencing_token AND w.lease_expires_at>clock_timestamp())
 OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED')
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=p.tenant_id AND event_type='PROPOSAL_STALED' AND user_id IS NULL AND detail->>'proposal_id'=p.id::text AND detail->>'version'=p.version::text)
 THEN RAISE EXCEPTION 'stale transition lacks its fenced system proof, release or audit' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
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
 IF e.user_id<>p.created_by THEN RAISE EXCEPTION 'reservation actor is not maker' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND NOT EXISTS(SELECT 1 FROM lv.proposal_reference ref WHERE ref.tenant_id=w.tenant_id AND ref.proposal_id=w.proposal_id AND ref.revision=w.revision AND ref.staged_document_id=w.staged_document_id) THEN RAISE EXCEPTION 'uncited reservation' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND (p.state NOT IN ('BLOCKED','READY_FOR_REVIEW') OR w.proposal_id<>p.id OR w.revision<>p.revision) THEN RAISE EXCEPTION 'orphan reservation' USING ERRCODE='23514'; END IF;
 IF w.status='VALID' AND p.state<>'WITHDRAWN' THEN RAISE EXCEPTION 'release requires a completed withdrawal' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION lv.maintenance_context(t uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF session_user<>'lv_worker' THEN RAISE EXCEPTION 'maintenance credential required' USING ERRCODE='42501'; END IF;
 PERFORM set_config('app.tenant_id',coalesce(t::text,''),true);
END $$;
CREATE FUNCTION lv.maintenance_due(n integer) RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE r record;
BEGIN
 PERFORM lv.maintenance_context(NULL);
 IF n IS NULL OR n NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'invalid relay bound' USING ERRCODE='22023'; END IF;
 FOR r IN SELECT o.tenant_id,o.id,o.work_item_id FROM lv.outbox o JOIN lv.work_item w ON (w.tenant_id,w.id)=(o.tenant_id,o.work_item_id)
 WHERE o.topic='maintenance' AND o.status='PENDING' AND o.available_at<=clock_timestamp() AND w.kind='STALENESS_SCAN'
 ORDER BY o.available_at,o.id LIMIT n FOR UPDATE OF o SKIP LOCKED LOOP
 RETURN NEXT jsonb_build_object('tenant_id',r.tenant_id::text,'outbox_id',r.id::text,'job_id',r.work_item_id::text);
 END LOOP;
END $$;
CREATE FUNCTION lv.maintenance_dispatched(t uuid,o_id uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM lv.maintenance_context(t);
 UPDATE lv.outbox SET status='DISPATCHED',dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE tenant_id=t AND id=o_id AND topic='maintenance' AND status='PENDING';
 RETURN FOUND;
END $$;
CREATE FUNCTION lv.maintenance_enqueue(w lv.work_item,delay_seconds integer) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO lv.outbox(tenant_id,topic,work_item_id,dedupe_key,available_at)
 VALUES(w.tenant_id,'maintenance',w.id,'STALENESS_REDELIVERY:'||w.id::text||':'||w.delivery_generation::text,clock_timestamp()+make_interval(secs=>delay_seconds));
END $$;
CREATE FUNCTION lv.maintenance_claim(t uuid,j uuid,worker text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.work_item;
BEGIN
 PERFORM lv.maintenance_context(t);
 IF worker IS NULL OR worker!~'^[A-Za-z0-9_.:-]{1,100}$' THEN RAISE EXCEPTION 'invalid worker identity' USING ERRCODE='22023'; END IF;
 SELECT * INTO w FROM lv.work_item WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT FOUND OR w.kind<>'STALENESS_SCAN' OR w.subject_type<>'PERIOD' OR w.state<>'QUEUED' OR w.attempts>=w.max_attempts
 OR EXISTS(SELECT 1 FROM lv.outbox WHERE tenant_id=t AND work_item_id=j AND status='PENDING' AND available_at>clock_timestamp()) OR NOT EXISTS(SELECT 1 FROM lv.outbox WHERE tenant_id=t AND work_item_id=j AND available_at<=clock_timestamp()) THEN RETURN NULL; END IF;
 UPDATE lv.work_item SET state='CLAIMED',fencing_token=fencing_token+1,lease_owner=worker,lease_expires_at=clock_timestamp()+interval '60 seconds',attempts=attempts+1
 WHERE tenant_id=t AND id=j RETURNING * INTO w;
 INSERT INTO lv.maintenance_event(tenant_id,job_id,fencing_token,event,worker_name) VALUES(t,j,w.fencing_token,'CLAIMED',worker);
 RETURN jsonb_build_object('tenant_id',t::text,'job_id',j::text,'fencing_token',w.fencing_token,'attempt',w.attempts);
END $$;
CREATE FUNCTION lv.maintenance_step(t uuid,j uuid,worker text,fence bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.work_item;p lv.proposal;fp lv.fiscal_period;previous lv.proposal_state;why jsonb;
BEGIN
 PERFORM lv.maintenance_context(t);
 SELECT * INTO w FROM lv.work_item WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT FOUND OR w.kind<>'STALENESS_SCAN' OR w.state<>'CLAIMED' OR w.lease_owner IS DISTINCT FROM worker OR w.fencing_token IS DISTINCT FROM fence OR w.lease_expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('status','LOST'); END IF;
 SELECT p0.* INTO p FROM lv.proposal p0 WHERE p0.tenant_id=t AND p0.ledger_id=w.ledger_id AND p0.state IN ('BLOCKED','READY_FOR_REVIEW')
 AND (w.scan_cursor IS NULL OR p0.id>w.scan_cursor) AND EXISTS(SELECT 1 FROM lv.proposal_read_set rs WHERE rs.tenant_id=t AND rs.proposal_id=p0.id AND rs.revision=p0.revision AND rs.entity_type='PERIOD' AND rs.entity_id=w.subject_id)
 ORDER BY p0.id LIMIT 1 FOR UPDATE;
 IF NOT FOUND THEN
 UPDATE lv.work_item SET state='DONE',lease_expires_at=NULL WHERE tenant_id=t AND id=j AND fencing_token=fence AND lease_expires_at>clock_timestamp();
 IF NOT FOUND THEN RETURN jsonb_build_object('status','LOST'); END IF;
 INSERT INTO lv.maintenance_event(tenant_id,job_id,fencing_token,event,worker_name,detail) VALUES(t,j,fence,'DONE',worker,jsonb_build_object('scanned',w.scanned_count,'staled',w.stale_count));
 RETURN jsonb_build_object('status','DONE','scanned',w.scanned_count,'staled',w.stale_count);
 END IF;
 -- Match the proposal -> ledger ordering used by submission and withdrawal. One proposal per transaction.
 PERFORM 1 FROM lv.ledger WHERE tenant_id=t AND id=w.ledger_id FOR UPDATE;
 SELECT * INTO fp FROM lv.fiscal_period WHERE tenant_id=t AND ledger_id=w.ledger_id AND id=w.subject_id FOR SHARE;
 IF fp.id IS NULL OR fp.status<>'CLOSED' OR fp.closed_seq IS NULL OR fp.closed_seq<>w.trigger_seq OR fp.close_provenance<>'AUDITED_COMMIT' THEN RAISE EXCEPTION 'period scan is not backed by an audited close' USING ERRCODE='23514'; END IF;
 IF w.lease_expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('status','LOST'); END IF;
 IF EXISTS(SELECT 1 FROM lv.proposal_read_set WHERE tenant_id=t AND proposal_id=p.id AND revision=p.revision AND entity_type='PERIOD' AND entity_id=fp.id AND version<>fp.version) THEN
 previous:=p.state;why:=jsonb_build_object('entity_type','PERIOD','entity_id',fp.id::text,'current_version',fp.version::text,'trigger_seq',w.trigger_seq::text);
 UPDATE lv.proposal SET state='STALE',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=t AND id=p.id RETURNING * INTO p;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,action,idempotency_key,request_hash,worker_job_id,worker_fencing_token)
 VALUES(t,p.ledger_id,p.id,p.revision,p.version,0,previous,'STALE','stale',j::text,encode(sha256(convert_to(j::text||':'||fence::text,'UTF8')),'hex'),j,fence);
 INSERT INTO lv.maintenance_event(tenant_id,job_id,fencing_token,event,proposal_id,proposal_version,worker_name,detail) VALUES(t,j,fence,'STALE',p.id,p.version,worker,why);
 WITH released AS (UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=t AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,worker_job_id,worker_fencing_token)
 SELECT tenant_id,ledger_id,staged_document_id,version,'RELEASED',p.id,p.revision,j,fence FROM released;
 PERFORM lv_identity.audit_append('PROPOSAL_STALED','ALLOWED',NULL,t,NULL,'maintenance',j::text||'-'||fence::text,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'reason',why));
 UPDATE lv.work_item SET stale_count=stale_count+1 WHERE tenant_id=t AND id=j AND fencing_token=fence;
 END IF;
 UPDATE lv.work_item SET scan_cursor=p.id,scanned_count=scanned_count+1,lease_expires_at=clock_timestamp()+interval '60 seconds' WHERE tenant_id=t AND id=j AND fencing_token=fence AND lease_expires_at>clock_timestamp();
 IF NOT FOUND THEN RAISE EXCEPTION 'lease expired before progress commit' USING ERRCODE='40001'; END IF;
 RETURN jsonb_build_object('status','PROGRESS','proposal_id',p.id::text,'state',p.state::text);
END $$;
CREATE FUNCTION lv.maintenance_retry(w lv.work_item,worker text,error_class text) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE terminal boolean:=w.attempts>=w.max_attempts OR error_class='INFRA_TERMINAL';
BEGIN
 UPDATE lv.work_item SET state=CASE WHEN terminal THEN 'DEAD' ELSE 'QUEUED' END,lease_owner=NULL,lease_expires_at=NULL,fencing_token=fencing_token+1,delivery_generation=delivery_generation+1,last_error_class=error_class
 WHERE tenant_id=w.tenant_id AND id=w.id RETURNING * INTO w;
 INSERT INTO lv.maintenance_event(tenant_id,job_id,fencing_token,event,worker_name,detail) VALUES(w.tenant_id,w.id,w.fencing_token,CASE WHEN terminal THEN 'DEAD' ELSE 'RETRY' END,worker,jsonb_build_object('error_class',error_class,'attempts',w.attempts));
 IF terminal THEN PERFORM lv_identity.audit_append('MAINTENANCE_JOB_DEAD','DENIED',NULL,w.tenant_id,NULL,'maintenance',w.id::text,jsonb_build_object('job_id',w.id::text,'error_class',error_class));
 ELSE PERFORM lv.maintenance_enqueue(w,least(60,power(2,w.attempts)::integer)); END IF;
END $$;
CREATE FUNCTION lv.maintenance_fail(t uuid,j uuid,worker text,fence bigint,error_class text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.work_item;
BEGIN
 PERFORM lv.maintenance_context(t);
 IF error_class IS NULL OR error_class NOT IN ('INFRA_RETRYABLE','INFRA_TERMINAL') THEN RAISE EXCEPTION 'invalid failure class' USING ERRCODE='22023'; END IF;
 SELECT * INTO w FROM lv.work_item WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT FOUND OR w.state<>'CLAIMED' OR w.lease_owner IS DISTINCT FROM worker OR w.fencing_token IS DISTINCT FROM fence OR w.lease_expires_at<=clock_timestamp() THEN RETURN false; END IF;
 PERFORM lv.maintenance_retry(w,worker,error_class);RETURN true;
END $$;
CREATE FUNCTION lv.maintenance_sweep(n integer) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.work_item;total integer:=0;
BEGIN
 PERFORM lv.maintenance_context(NULL);
 IF n IS NULL OR n NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'invalid sweep bound' USING ERRCODE='22023'; END IF;
 FOR w IN SELECT wi.* FROM lv.work_item wi WHERE wi.kind='STALENESS_SCAN' AND ((wi.state='CLAIMED' AND wi.lease_expires_at<=clock_timestamp()) OR (wi.state='QUEUED'
 AND NOT EXISTS(SELECT 1 FROM lv.outbox o WHERE o.tenant_id=wi.tenant_id AND o.work_item_id=wi.id AND o.status='PENDING')
 AND (SELECT max(o.dispatched_at) FROM lv.outbox o WHERE o.tenant_id=wi.tenant_id AND o.work_item_id=wi.id)<clock_timestamp()-interval '30 seconds'))
 ORDER BY wi.created_at,wi.id LIMIT n FOR UPDATE SKIP LOCKED LOOP
 PERFORM lv.maintenance_context(w.tenant_id);
 IF w.state='CLAIMED' THEN PERFORM lv.maintenance_retry(w,'sweeper','INFRA_RETRYABLE');
 ELSE
 UPDATE lv.work_item SET delivery_generation=delivery_generation+1 WHERE tenant_id=w.tenant_id AND id=w.id RETURNING * INTO w;
 PERFORM lv.maintenance_enqueue(w,0);
 INSERT INTO lv.maintenance_event(tenant_id,job_id,fencing_token,event,worker_name) VALUES(w.tenant_id,w.id,w.fencing_token,'REDELIVERED','sweeper');
 END IF;total:=total+1;
 END LOOP;
 RETURN total;
END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC;

CREATE FUNCTION lv.maintenance_health() RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE answer jsonb;
BEGIN
 PERFORM lv.maintenance_context(NULL);
 SELECT coalesce(jsonb_object_agg(state,n),'{}'::jsonb) INTO answer FROM (SELECT state,count(*) AS n FROM lv.work_item WHERE kind='STALENESS_SCAN' GROUP BY state) x;
 RETURN answer;
END $$;
REVOKE ALL ON FUNCTION lv.maintenance_health() FROM PUBLIC;
