-- Frozen owning 0019 functions for lossless empty-history downgrade.
CREATE OR REPLACE FUNCTION lv.guard_proposal_update()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
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
 IF OLD.state IN ('BLOCKED','READY_FOR_REVIEW','APPROVED') AND NEW.state='STALE' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 IF OLD.state='BLOCKED' AND NEW.state='READY_FOR_REVIEW' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 IF OLD.state='READY_FOR_REVIEW' AND NEW.state='APPROVED' AND NEW.version=OLD.version+1 AND (NEW.submitted_by,NEW.snapshot_id,NEW.latest_simulation_id) IS NOT DISTINCT FROM (OLD.submitted_by,OLD.snapshot_id,OLD.latest_simulation_id) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'unsupported proposal transition' USING ERRCODE='23514';
END $function$
;
CREATE OR REPLACE FUNCTION lv.validate_submission()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p lv.proposal;r lv.simulation_run;ls lv.ledger_snapshot;b jsonb;o jsonb;rs jsonb;vs jsonb;act text;
BEGIN
 IF TG_TABLE_NAME='proposal' THEN SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
 ELSE SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id; END IF;
 IF p.state='DRAFT' THEN
 IF p.submitted_by IS NOT NULL OR p.latest_simulation_id IS NOT NULL OR p.snapshot_id IS NOT NULL THEN RAISE EXCEPTION 'draft has submitted artifacts' USING ERRCODE='23514'; END IF;RETURN NULL;
 END IF;
 IF p.state='SUBMITTED' THEN RAISE EXCEPTION 'incomplete submission cannot commit' USING ERRCODE='23514'; END IF;
 IF p.state='STALE' THEN
 IF EXISTS(SELECT 1 FROM lv.approval_check c JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.revision,t.version)=(c.tenant_id,c.proposal_id,c.revision,c.resulting_version)
 WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.report->>'status'='STALE' AND c.tx_id=pg_current_xact_id() AND t.tx_id=pg_current_xact_id() AND t.action='approve' AND t.user_id=c.checked_by AND t.session_id=c.session_id)
 THEN
 IF EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'approval stale left reservations' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
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
 WHERE t.tenant_id=p.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.to_state=p.state AND t.tx_id=pg_current_xact_id() AND (t.user_id=p.created_by OR (t.action='approve' AND EXISTS(SELECT 1 FROM lv.proposal_decision d WHERE d.tenant_id=p.tenant_id AND d.proposal_id=p.id AND d.resulting_version=p.version AND d.approved_by=t.user_id AND d.session_id=t.session_id AND d.tx_id=pg_current_xact_id())) OR (t.action='overrides' AND EXISTS(SELECT 1 FROM lv.proposal_override ov WHERE ov.tenant_id=p.tenant_id AND ov.proposal_id=p.id AND ov.resulting_version=p.version AND ov.overridden_by=t.user_id AND ov.session_id=t.session_id AND ov.tx_id=pg_current_xact_id()))))
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event a WHERE a.chain_key=p.tenant_id AND a.tenant_id=p.tenant_id AND a.user_id=CASE WHEN act='approve' THEN (SELECT approved_by FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version) WHEN act='overrides' THEN (SELECT overridden_by FROM lv.proposal_override WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version) ELSE p.created_by END
 AND a.event_type=CASE WHEN act='withdraw' THEN 'PROPOSAL_WITHDRAWN' WHEN act='resimulate' THEN 'PROPOSAL_RESIMULATED' WHEN act='overrides' THEN 'PROPOSAL_OVERRIDDEN' WHEN act='approve' THEN 'PROPOSAL_APPROVED' ELSE 'PROPOSAL_SUBMITTED' END AND a.detail->>'proposal_id'=p.id::text AND a.detail->>'version'=p.version::text)
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
 OR ls.snapshot_ref IS DISTINCT FROM (((b->'snapshot')-ARRAY['batch_head','event_head'])||jsonb_build_object('seq',r.snapshot_seq)) OR ls.seq<>r.snapshot_seq OR (p.state IN ('READY_FOR_REVIEW','APPROVED')) IS DISTINCT FROM (o->>'aggregate'='CLEAR' OR lv.override_covers(p)) THEN RAISE EXCEPTION 'incomplete pinned submission' USING ERRCODE='23514'; END IF;
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
END $function$
;
CREATE OR REPLACE FUNCTION lv.validate_reservation()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE w lv.staged_workflow;e lv.reservation_event;p lv.proposal;
BEGIN
 SELECT * INTO w FROM lv.staged_workflow WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND staged_document_id=NEW.staged_document_id;
 SELECT * INTO e FROM lv.reservation_event WHERE tenant_id=w.tenant_id AND staged_document_id=w.staged_document_id AND version=w.version;
 IF e.staged_document_id IS NULL OR (w.status='RESERVED') IS DISTINCT FROM (e.event='RESERVED') THEN RAISE EXCEPTION 'reservation history incomplete' USING ERRCODE='23514'; END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=e.tenant_id AND id=e.proposal_id;
 IF e.worker_job_id IS NOT NULL THEN
 IF session_user<>'lv_worker' OR w.status<>'VALID' OR p.state<>'STALE' OR NOT EXISTS(SELECT 1 FROM lv.maintenance_event ev WHERE ev.tenant_id=e.tenant_id AND ev.job_id=e.worker_job_id AND ev.fencing_token=e.worker_fencing_token AND ev.proposal_id=p.id AND ev.proposal_version=p.version AND ev.event='STALE' AND ev.tx_id=pg_current_xact_id()) THEN RAISE EXCEPTION 'system release lacks stale proof' USING ERRCODE='23514'; END IF;
 ELSE
 IF w.status='VALID' AND p.state='STALE' AND EXISTS(SELECT 1 FROM lv.approval_check c JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.version)=(c.tenant_id,c.proposal_id,c.resulting_version)
 WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.report->>'status'='STALE' AND c.checked_by=e.user_id AND c.session_id=e.session_id AND c.tx_id=pg_current_xact_id() AND t.action='approve' AND t.tx_id=pg_current_xact_id()) THEN RETURN NULL; END IF;
 IF w.status='VALID' AND p.state='STALE' AND NOT EXISTS(SELECT 1 FROM lv.proposal_freshness_check c WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.tx_id=pg_current_xact_id() AND c.report->>'status'='STALE' AND c.user_id=e.user_id) THEN RAISE EXCEPTION 'human stale release lacks freshness proof' USING ERRCODE='23514'; END IF;
 IF e.user_id<>p.created_by THEN RAISE EXCEPTION 'reservation actor is not maker' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND NOT EXISTS(SELECT 1 FROM lv.proposal_reference ref WHERE ref.tenant_id=w.tenant_id AND ref.proposal_id=w.proposal_id AND ref.revision=w.revision AND ref.staged_document_id=w.staged_document_id) THEN RAISE EXCEPTION 'uncited reservation' USING ERRCODE='23514'; END IF;
 IF w.status='RESERVED' AND (p.state NOT IN ('BLOCKED','READY_FOR_REVIEW','APPROVED') OR w.proposal_id<>p.id OR w.revision<>p.revision) THEN RAISE EXCEPTION 'orphan reservation' USING ERRCODE='23514'; END IF;
 IF w.status='VALID' AND p.state NOT IN ('WITHDRAWN','STALE') THEN RAISE EXCEPTION 'release requires a completed withdrawal' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END $function$
;
CREATE OR REPLACE FUNCTION lv.validate_approval_check()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
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
END $function$
;
CREATE OR REPLACE FUNCTION lv.validate_proposal_decision()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p lv.proposal;c lv.approval_check;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id;
 SELECT * INTO c FROM lv.approval_check WHERE tenant_id=NEW.tenant_id AND id=NEW.check_id;
 IF p.state<>'APPROVED' OR NEW.kind<>'APPROVED' OR NEW.tx_id<>pg_current_xact_id() OR NEW.resulting_version<>p.version OR NEW.revision<>p.revision OR NEW.simulation_id<>p.latest_simulation_id OR NEW.submitted_by<>p.created_by OR lv.review_blocker(p,NEW.approved_by) IS NOT NULL
 OR c.proposal_id<>p.id OR c.revision<>p.revision OR c.report->>'status'<>'FRESH' OR c.resulting_version<>p.version OR c.checked_by<>NEW.approved_by OR c.session_id<>NEW.session_id OR c.comment IS DISTINCT FROM NEW.comment OR c.tx_id<>pg_current_xact_id()
 THEN RAISE EXCEPTION 'decision lacks its independent revision-bound check' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $function$
;
CREATE OR REPLACE FUNCTION lv.decision_json(d lv.proposal_decision)
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT jsonb_build_object('kind','APPROVAL','id',d.id::text,'proposal_id',d.proposal_id::text,'ledger_id',d.ledger_id::text,'revision',d.revision,'simulation_id',d.simulation_id::text,'approved_by',d.approved_by::text,'submitted_by',d.submitted_by::text,'comment',d.comment,'observed_version',d.observed_version,'resulting_version',d.resulting_version,'check_id',d.check_id::text,'created_at',to_char(d.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
$function$
;