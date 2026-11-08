-- 0035_provider_connections: QuickBooks Online SANDBOX read-only connection (Stage 2C §2, §9.1, §11; ADR-007, ADR-012, ADR-016,
-- ADR-021, ADR-024). PROPOSED PROV-1..PROV-8 (docs/QUICKBOOKS_SANDBOX.md). No provider write exists anywhere in this revision.
-- Tokens and the sealed authorization code are readable only through lv_worker SECURITY DEFINER functions, and only as
-- ciphertext; the key that opens them lives in the integration worker process. Provider objects are stored raw, labelled
-- PROVIDER_SANDBOX, and never routed into native ledger tables.

CREATE TABLE lv.provider_connection(
 tenant_id uuid NOT NULL REFERENCES lv.tenant(tenant_id),
 id uuid NOT NULL DEFAULT uuidv7(),
 provider text NOT NULL CHECK(provider='QUICKBOOKS_ONLINE'),
 environment text NOT NULL CHECK(environment='SANDBOX'),
 scope text NOT NULL CHECK(scope='com.intuit.quickbooks.accounting'),
 state text NOT NULL DEFAULT 'AUTHORIZING' CHECK(state IN ('AUTHORIZING','EXCHANGING','ACTIVE','DEGRADED','REVOKING','REVOKED','FAILED')),
 version integer NOT NULL DEFAULT 1 CHECK(version>=1),
 realm_id text CHECK(realm_id ~ '^[0-9]{1,32}$'),
 created_by uuid NOT NULL REFERENCES lv_identity.app_user(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 connected_at timestamptz,
 changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 failure_code text CHECK(failure_code ~ '^[A-Z_]{1,64}$'),
 revoked_by uuid REFERENCES lv_identity.app_user(id),
 revoked_at timestamptz,
 revoke_outcome text CHECK(revoke_outcome IN ('PROVIDER_CONFIRMED','PROVIDER_UNCONFIRMED','NO_TOKENS','ABANDONED')),
 PRIMARY KEY(tenant_id,id),
 CHECK(state NOT IN ('EXCHANGING','ACTIVE','DEGRADED','REVOKING') OR realm_id IS NOT NULL),
 CHECK(state<>'FAILED' OR failure_code IS NOT NULL),
 CHECK((state='REVOKED')=(revoked_at IS NOT NULL AND revoke_outcome IS NOT NULL))
);
-- One open connection per tenant; a realm can be bound to only one tenant at a time (Stage 2C §11).
CREATE UNIQUE INDEX provider_connection_open_per_tenant ON lv.provider_connection(tenant_id,provider,environment) WHERE state NOT IN ('REVOKED','FAILED');
-- Only a realm the worker has verified (token exchanged and CompanyInfo read) is exclusive; an unverified callback cannot squat it.
CREATE UNIQUE INDEX provider_connection_realm_once ON lv.provider_connection(provider,environment,realm_id) WHERE state IN ('ACTIVE','DEGRADED','REVOKING');

CREATE TABLE lv.provider_oauth_state(
 tenant_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(), connection_id uuid NOT NULL,
 state_hash char(64) NOT NULL UNIQUE CHECK(state_hash ~ '^[0-9a-f]{64}$'),
 session_id uuid NOT NULL, user_id uuid NOT NULL REFERENCES lv_identity.app_user(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz NOT NULL,
 consumed_at timestamptz, outcome text CHECK(outcome IN ('CODE_RECEIVED','PROVIDER_ERROR','SUPERSEDED','CANCELLED','EXPIRED')),
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,connection_id) REFERENCES lv.provider_connection(tenant_id,id),
 CHECK(expires_at=created_at+interval '10 minutes'), CHECK((consumed_at IS NULL)=(outcome IS NULL))
);

CREATE TABLE lv.provider_credential(
 tenant_id uuid NOT NULL, connection_id uuid NOT NULL,
 kek_id char(64) NOT NULL CHECK(kek_id ~ '^[0-9a-f]{64}$'),
 envelope bytea NOT NULL CHECK(octet_length(envelope) BETWEEN 64 AND 16384),
 CHECK(substring(envelope from 5 for 32)=decode(kek_id,'hex')),
 token_version integer NOT NULL CHECK(token_version>=1),
 access_expires_at timestamptz NOT NULL, refresh_expires_at timestamptz,
 lease_owner text, lease_expires_at timestamptz, lease_fence bigint NOT NULL DEFAULT 0,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,connection_id), FOREIGN KEY(tenant_id,connection_id) REFERENCES lv.provider_connection(tenant_id,id),
 CHECK((lease_owner IS NULL)=(lease_expires_at IS NULL))
);

CREATE TABLE lv.provider_job(
 tenant_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(), connection_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('EXCHANGE','SYNC','REVOKE')),
 state text NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','CLAIMED','DONE','DEAD','DENIED')),
 fence bigint NOT NULL DEFAULT 0, attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
 throttles integer NOT NULL DEFAULT 0 CHECK(throttles BETWEEN 0 AND 20),
 lease_owner text, lease_expires_at timestamptz, available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 dispatched_at timestamptz, dispatch_count integer NOT NULL DEFAULT 0,
 requested_by uuid NOT NULL REFERENCES lv_identity.app_user(id),
 payload bytea CHECK(payload IS NULL OR octet_length(payload) BETWEEN 64 AND 16384),
 progress jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(progress)='object' AND octet_length(progress::text)<=65536),
 result jsonb CHECK(result IS NULL OR (jsonb_typeof(result)='object' AND octet_length(result::text)<=65536)),
 last_error text CHECK(last_error ~ '^[A-Z_]{1,64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), started_at timestamptz, finished_at timestamptz,
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,connection_id) REFERENCES lv.provider_connection(tenant_id,id),
 CHECK(kind='EXCHANGE' OR payload IS NULL), CHECK((lease_owner IS NULL)=(lease_expires_at IS NULL)),
 CHECK(state IN ('QUEUED','CLAIMED') OR finished_at IS NOT NULL)
);
CREATE UNIQUE INDEX provider_job_one_open_kind ON lv.provider_job(tenant_id,connection_id,kind) WHERE state IN ('QUEUED','CLAIMED');
CREATE INDEX provider_job_due ON lv.provider_job(available_at,id) WHERE state IN ('QUEUED','CLAIMED');

CREATE TABLE lv.provider_page(
 tenant_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(), connection_id uuid NOT NULL, job_id uuid NOT NULL,
 realm_id text NOT NULL CHECK(realm_id ~ '^[0-9]{1,32}$'),
 entity text NOT NULL CHECK(entity IN ('CompanyInfo','Preferences','Account','Customer','Invoice','Payment','Deposit','JournalEntry','CreditMemo','RefundReceipt')),
 start_position integer NOT NULL CHECK(start_position>=1), max_results integer NOT NULL CHECK(max_results BETWEEN 1 AND 1000),
 minor_version text NOT NULL CHECK(minor_version ~ '^[0-9]{1,4}$'), intuit_tid text CHECK(intuit_tid ~ '^[!-~]{1,128}$'),
 http_status smallint NOT NULL CHECK(http_status=200),
 body bytea NOT NULL CHECK(octet_length(body) BETWEEN 2 AND 8388608), body_sha256 char(64) NOT NULL,
 object_count integer NOT NULL CHECK(object_count BETWEEN 0 AND 1000),
 provenance text NOT NULL CHECK(provenance IN ('PROVIDER_SANDBOX','RECORDED_MOCK')),
 received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,connection_id) REFERENCES lv.provider_connection(tenant_id,id),
 FOREIGN KEY(tenant_id,job_id) REFERENCES lv.provider_job(tenant_id,id), CHECK(body_sha256=encode(sha256(body),'hex'))
);

CREATE TABLE lv.provider_object(
 tenant_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(), connection_id uuid NOT NULL, page_id uuid NOT NULL,
 realm_id text NOT NULL, entity text NOT NULL, provider_id text NOT NULL CHECK(provider_id ~ '^[!-~]{1,64}$'),
 sync_token text NOT NULL CHECK(sync_token ~ '^[0-9]{1,18}$'), provider_last_updated text CHECK(length(provider_last_updated)<=64),
 object jsonb NOT NULL CHECK(jsonb_typeof(object)='object'), object_sha256 char(64) NOT NULL,
 provenance text NOT NULL CHECK(provenance IN ('PROVIDER_SANDBOX','RECORDED_MOCK')),
 first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,connection_id,entity,provider_id,sync_token),
 FOREIGN KEY(tenant_id,page_id) REFERENCES lv.provider_page(tenant_id,id), CHECK(object_sha256=encode(sha256(convert_to(object::text,'UTF8')),'hex'))
);
CREATE INDEX provider_object_browse ON lv.provider_object(tenant_id,connection_id,entity,id);

ALTER TABLE lv.provider_connection ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.provider_connection FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.provider_connection USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.provider_connection FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
ALTER TABLE lv.provider_oauth_state ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.provider_oauth_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.provider_oauth_state USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.provider_oauth_state FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
ALTER TABLE lv.provider_credential ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.provider_credential FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.provider_credential USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.provider_credential FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
ALTER TABLE lv.provider_job ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.provider_job FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.provider_job USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.provider_job FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
ALTER TABLE lv.provider_page ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.provider_page FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.provider_page USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.provider_page FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
ALTER TABLE lv.provider_object ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.provider_object FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.provider_object USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.provider_object FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
CREATE POLICY provider_worker_inventory ON lv.provider_job FOR SELECT TO lv_owner USING(session_user='lv_worker');
-- Worker-only cross-tenant scans through SECURITY DEFINER functions (pending revocations, key-rotation inventory). lv_worker
-- has no table grants, so these policies widen nothing outside those functions, which always filter by tenant otherwise.
CREATE POLICY provider_worker_inventory ON lv.provider_connection FOR SELECT TO lv_owner USING(session_user='lv_worker');
CREATE POLICY provider_worker_inventory ON lv.provider_credential FOR SELECT TO lv_owner USING(session_user='lv_worker');
-- Runtime reads: everything except the sealed exchange payload, page bodies, OAuth states and credentials.
GRANT SELECT ON lv.provider_connection TO lv_app;
GRANT SELECT(tenant_id,id,connection_id,kind,state,attempts,throttles,available_at,requested_by,progress,result,last_error,created_at,started_at,finished_at) ON lv.provider_job TO lv_app;
GRANT SELECT(tenant_id,id,connection_id,job_id,realm_id,entity,start_position,max_results,minor_version,intuit_tid,http_status,body_sha256,object_count,provenance,received_at) ON lv.provider_page TO lv_app;
GRANT SELECT ON lv.provider_object TO lv_app;

-- Retention: replica pages and objects are append-only; finished jobs and consumed states are kept.
CREATE FUNCTION lv.guard_provider_append_only() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN RAISE EXCEPTION 'provider replica rows are append-only' USING ERRCODE='23514'; END $$;
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON lv.provider_page FOR EACH ROW EXECUTE FUNCTION lv.guard_provider_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON lv.provider_object FOR EACH ROW EXECUTE FUNCTION lv.guard_provider_append_only();
CREATE FUNCTION lv.guard_provider_retained() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'provider history is retained' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='provider_connection' THEN
  IF (NEW.tenant_id,NEW.id,NEW.provider,NEW.environment,NEW.scope,NEW.created_by,NEW.created_at) IS DISTINCT FROM (OLD.tenant_id,OLD.id,OLD.provider,OLD.environment,OLD.scope,OLD.created_by,OLD.created_at)
     OR (OLD.realm_id IS NOT NULL AND NEW.realm_id IS DISTINCT FROM OLD.realm_id) OR OLD.state IN ('REVOKED','FAILED') OR NEW.version<>OLD.version+1 THEN
   RAISE EXCEPTION 'provider connection identity is fixed and terminal states are final' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='provider_job' THEN
  IF (NEW.tenant_id,NEW.id,NEW.connection_id,NEW.kind,NEW.requested_by,NEW.created_at) IS DISTINCT FROM (OLD.tenant_id,OLD.id,OLD.connection_id,OLD.kind,OLD.requested_by,OLD.created_at)
     OR OLD.state IN ('DONE','DEAD','DENIED') OR (NEW.payload IS NOT NULL AND NEW.payload IS DISTINCT FROM OLD.payload) THEN
   RAISE EXCEPTION 'provider job identity is fixed and finished jobs are final' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='provider_oauth_state' THEN
  IF OLD.consumed_at IS NOT NULL OR (NEW.tenant_id,NEW.id,NEW.connection_id,NEW.state_hash,NEW.session_id,NEW.user_id,NEW.created_at,NEW.expires_at) IS DISTINCT FROM (OLD.tenant_id,OLD.id,OLD.connection_id,OLD.state_hash,OLD.session_id,OLD.user_id,OLD.created_at,OLD.expires_at) THEN
   RAISE EXCEPTION 'OAuth state is single-use' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER retained BEFORE UPDATE OR DELETE ON lv.provider_connection FOR EACH ROW EXECUTE FUNCTION lv.guard_provider_retained();
CREATE TRIGGER retained BEFORE UPDATE OR DELETE ON lv.provider_job FOR EACH ROW EXECUTE FUNCTION lv.guard_provider_retained();
CREATE TRIGGER retained BEFORE UPDATE OR DELETE ON lv.provider_oauth_state FOR EACH ROW EXECUTE FUNCTION lv.guard_provider_retained();

-- ------------------------------------------------------------------------------------------------------------ JSON views
CREATE FUNCTION lv.provider_job_json(j lv.provider_job) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',j.id::text,'connection_id',j.connection_id::text,'kind',j.kind,'state',j.state,'attempts',j.attempts,'throttles',j.throttles,
  'available_at',to_char(j.available_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'requested_by',j.requested_by::text,'progress',j.progress,'result',j.result,
  'last_error',j.last_error,'created_at',to_char(j.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'started_at',to_char(j.started_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'finished_at',to_char(j.finished_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
$$;
-- SECURITY DEFINER only so the job summary can read the job rows without exposing the sealed payload column to lv_app;
-- FORCE RLS still limits it to the caller's tenant (app.tenant_id), whatever row is passed in.
CREATE FUNCTION lv.provider_connection_json(c lv.provider_connection) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',c.id::text,'provider',c.provider,'environment',c.environment,'scope',c.scope,'state',c.state,'version',c.version,
  'etag','v'||c.version::text,'realm_id',c.realm_id,'created_by',c.created_by::text,
  'created_at',to_char(c.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'connected_at',to_char(c.connected_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'changed_at',to_char(c.changed_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'failure_code',c.failure_code,
  'revoked_at',to_char(c.revoked_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'revoke_outcome',c.revoke_outcome,
  'revocation_jobs',(SELECT count(*) FROM lv.provider_job q WHERE q.tenant_id=c.tenant_id AND q.connection_id=c.id AND q.kind='REVOKE'),
  'revocation_auto_retry',coalesce(c.state='REVOKING' AND c.failure_code='REVOCATION_PENDING',false) AND (SELECT count(*) FROM lv.provider_job q WHERE q.tenant_id=c.tenant_id AND q.connection_id=c.id AND q.kind='REVOKE')<25
   AND coalesce((SELECT q.last_error FROM lv.provider_job q WHERE q.tenant_id=c.tenant_id AND q.connection_id=c.id AND q.kind='REVOKE' ORDER BY q.id DESC LIMIT 1),'') NOT IN ('ENVELOPE_INVALID','TRANSPORT_REFUSED'),
  'jobs',(SELECT coalesce(jsonb_agg(lv.provider_job_json(j) ORDER BY j.id DESC),'[]'::jsonb) FROM (SELECT * FROM lv.provider_job j WHERE j.tenant_id=c.tenant_id AND j.connection_id=c.id ORDER BY j.id DESC LIMIT 10) j),
  'replica',(SELECT coalesce(jsonb_object_agg(entity,n),'{}'::jsonb) FROM (SELECT o.entity,count(*) n FROM lv.provider_object o WHERE o.tenant_id=c.tenant_id AND o.connection_id=c.id GROUP BY o.entity) x),
  'data_label','PROVIDER SANDBOX DATA - not business evidence, not accounting records','writes','DISABLED','accounting_review_status','UNREVIEWED')
$$;
CREATE FUNCTION lv.provider_object_json(o lv.provider_object, with_object boolean) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',o.id::text,'connection_id',o.connection_id::text,'realm_id',o.realm_id,'entity',o.entity,'provider_id',o.provider_id,
  'sync_token',o.sync_token,'provider_last_updated',o.provider_last_updated,'object_sha256',o.object_sha256,'page_id',o.page_id::text,'provenance',o.provenance,
  'first_seen_at',to_char(o.first_seen_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'page',(SELECT jsonb_build_object('start_position',p.start_position,'max_results',p.max_results,'minor_version',p.minor_version,'intuit_tid',p.intuit_tid,
          'body_sha256',p.body_sha256,'received_at',to_char(p.received_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'job_id',p.job_id::text)
          FROM lv.provider_page p WHERE p.tenant_id=o.tenant_id AND p.id=o.page_id))
  -- the raw object travels as text so provider amounts keep their exact decimal digits through every JSON parser
  || CASE WHEN with_object THEN jsonb_build_object('object_text',o.object::text) ELSE '{}'::jsonb END
$$;

-- ------------------------------------------------------------------------------------------------------------ API writer
-- One dispatcher for the four administrator actions. Lock order follows 0027/0028: idempotency advisory lock -> user ->
-- membership -> ordered roles -> session; then the tenant's provider advisory lock; then provider rows. No ledger lock is
-- taken: provider work touches no ledger, counter or financial table.
CREATE FUNCTION lv.provider_api(tok bytea, action text, conn uuid, ikey text, rhash text, args jsonb, p_problem jsonb, src text, req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; prior lv.api_idempotency; c lv.provider_connection; st lv.provider_oauth_state; j lv.provider_job;
 route_name text; result jsonb; problem jsonb := p_problem; new_conn uuid := uuidv7(); realm text; now_ts timestamptz := clock_timestamp();
BEGIN
 route_name := CASE action WHEN 'CONNECT' THEN '/api/v1/provider-connections' WHEN 'CALLBACK' THEN '/api/v1/provider-connections/quickbooks/callback'
  WHEN 'SYNC' THEN '/api/v1/provider-connections/{connection_id}/syncs' WHEN 'DISCONNECT' THEN '/api/v1/provider-connections/{connection_id}/disconnect' END;
 IF route_name IS NULL THEN RAISE EXCEPTION 'invalid provider action' USING ERRCODE='22023'; END IF;
 s := lv.proposal_session(tok, src, req);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'administer') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires administer'); END IF;
 IF ikey IS NULL OR ikey !~ '^[!-~]{1,64}$' OR rhash IS NULL OR rhash !~ '^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-provider:'||s.tenant_id::text||':'||s.user_id::text||':'||ikey,0)) THEN
  RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1); END IF;
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv_identity.session WHERE id=s.id FOR UPDATE;
 s := lv.proposal_session(tok, src, req);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'administer') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires administer'); END IF;
 PERFORM lv_identity.session_context(tok, true);
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
  IF prior.request_hash<>rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
  RETURN prior.response || jsonb_build_object('replayed',true);
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('lv-provider-tenant:'||s.tenant_id::text,0));
 IF problem IS NULL THEN
  IF action='CONNECT' THEN
   IF args->>'state_hash' IS NULL OR args->>'state_hash' !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'state hash required' USING ERRCODE='22023'; END IF;
   SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=s.tenant_id AND provider='QUICKBOOKS_ONLINE' AND environment='SANDBOX' AND state NOT IN ('REVOKED','FAILED') FOR UPDATE;
   IF FOUND AND c.state<>'AUTHORIZING' THEN
    problem := jsonb_build_object('status',409,'code','CONNECTION_EXISTS','detail','disconnect the current QuickBooks sandbox connection first','connection_id',c.id::text);
   ELSE
    IF FOUND THEN   -- an unfinished authorization is superseded, never resumed
     UPDATE lv.provider_oauth_state SET consumed_at=clock_timestamp(),outcome='SUPERSEDED' WHERE tenant_id=s.tenant_id AND connection_id=c.id AND consumed_at IS NULL;
     UPDATE lv.provider_connection SET state='FAILED',failure_code='AUTHORIZATION_SUPERSEDED',version=version+1,changed_at=clock_timestamp() WHERE tenant_id=s.tenant_id AND id=c.id;
    END IF;
    INSERT INTO lv.provider_connection(tenant_id,id,provider,environment,scope,created_by) VALUES(s.tenant_id,new_conn,'QUICKBOOKS_ONLINE','SANDBOX','com.intuit.quickbooks.accounting',s.user_id) RETURNING * INTO c;
    INSERT INTO lv.provider_oauth_state(tenant_id,connection_id,state_hash,session_id,user_id,created_at,expires_at) VALUES(s.tenant_id,c.id,args->>'state_hash',s.id,s.user_id,now_ts,now_ts+interval '10 minutes');
    PERFORM lv_identity.audit_append('PROVIDER_CONNECT_STARTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text,'provider','QUICKBOOKS_ONLINE','environment','SANDBOX'));
    result := jsonb_build_object('status',201,'body',lv.provider_connection_json(c));
   END IF;
  ELSIF action='CALLBACK' THEN
   SELECT * INTO st FROM lv.provider_oauth_state WHERE state_hash=args->>'state_hash' FOR UPDATE;   -- RLS: only this tenant's states are visible
   IF NOT FOUND OR st.session_id<>s.id OR st.user_id<>s.user_id THEN
    problem := jsonb_build_object('status',400,'code','OAUTH_STATE_INVALID','detail','the authorization response does not belong to this session; start the connection again');
    PERFORM lv_identity.audit_append('PROVIDER_AUTHORIZATION_RETURNED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('reason',CASE WHEN st.id IS NULL THEN 'UNKNOWN_STATE' ELSE 'SESSION_MISMATCH' END));
   ELSIF st.consumed_at IS NOT NULL THEN
    problem := jsonb_build_object('status',409,'code','OAUTH_STATE_USED','detail','this authorization response was already processed');
   ELSE
    SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=s.tenant_id AND id=st.connection_id FOR UPDATE;
    IF st.expires_at<=clock_timestamp() THEN
     UPDATE lv.provider_oauth_state SET consumed_at=clock_timestamp(),outcome='EXPIRED' WHERE tenant_id=s.tenant_id AND id=st.id;
     IF c.state='AUTHORIZING' THEN UPDATE lv.provider_connection SET state='FAILED',failure_code='AUTHORIZATION_EXPIRED',version=version+1,changed_at=clock_timestamp() WHERE tenant_id=s.tenant_id AND id=c.id; END IF;
     problem := jsonb_build_object('status',400,'code','OAUTH_STATE_EXPIRED','detail','the authorization took longer than 10 minutes; start the connection again');
    ELSIF c.state<>'AUTHORIZING' THEN
     UPDATE lv.provider_oauth_state SET consumed_at=clock_timestamp(),outcome='CANCELLED' WHERE tenant_id=s.tenant_id AND id=st.id;
     problem := jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','detail','this connection attempt is no longer waiting for authorization');
    ELSIF args ? 'provider_error' THEN
     UPDATE lv.provider_oauth_state SET consumed_at=clock_timestamp(),outcome='PROVIDER_ERROR' WHERE tenant_id=s.tenant_id AND id=st.id;
     UPDATE lv.provider_connection SET state='FAILED',failure_code=CASE WHEN args->>'provider_error'='access_denied' THEN 'AUTHORIZATION_DENIED' ELSE 'AUTHORIZATION_ERROR' END,
      version=version+1,changed_at=clock_timestamp() WHERE tenant_id=s.tenant_id AND id=c.id RETURNING * INTO c;
     PERFORM lv_identity.audit_append('PROVIDER_AUTHORIZATION_RETURNED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text,'reason',c.failure_code));
     result := jsonb_build_object('status',200,'body',lv.provider_connection_json(c));
    ELSE
     realm := args->>'realm_id';
     IF realm IS NULL OR realm !~ '^[0-9]{1,32}$' OR args->>'code_envelope' IS NULL THEN RAISE EXCEPTION 'realm and sealed code required' USING ERRCODE='22023'; END IF;
     UPDATE lv.provider_oauth_state SET consumed_at=clock_timestamp(),outcome='CODE_RECEIVED' WHERE tenant_id=s.tenant_id AND id=st.id;
     UPDATE lv.provider_connection SET state='EXCHANGING',realm_id=realm,version=version+1,changed_at=clock_timestamp() WHERE tenant_id=s.tenant_id AND id=c.id RETURNING * INTO c;
     IF problem IS NULL THEN
      INSERT INTO lv.provider_job(tenant_id,connection_id,kind,requested_by,payload) VALUES(s.tenant_id,c.id,'EXCHANGE',s.user_id,decode(args->>'code_envelope','hex')) RETURNING * INTO j;
      PERFORM lv_identity.audit_append('PROVIDER_AUTHORIZATION_RETURNED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text,'realm_id',realm,'job_id',j.id::text));
      result := jsonb_build_object('status',202,'body',lv.provider_connection_json(c));
     END IF;
    END IF;
   END IF;
  ELSE
   SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=s.tenant_id AND id=conn FOR UPDATE;
   IF NOT FOUND THEN problem := jsonb_build_object('status',404,'code','NOT_FOUND','detail','connection not found');
   ELSIF args->>'if_match' IS NULL THEN problem := jsonb_build_object('status',428,'code','PRECONDITION_REQUIRED','detail','If-Match "v<version>" is required');
   ELSIF args->>'if_match' IS DISTINCT FROM '"v'||c.version::text||'"' THEN problem := jsonb_build_object('status',412,'code','VERSION_MISMATCH','current_version',c.version);
   ELSIF action='SYNC' THEN
    IF c.state='DEGRADED' THEN problem := jsonb_build_object('status',409,'code','CONNECTION_DEGRADED','detail','QuickBooks rejected the stored authorization; disconnect and connect again');
    ELSIF c.state<>'ACTIVE' THEN problem := jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','detail','only an active connection can be synchronized');
    ELSIF EXISTS(SELECT 1 FROM lv.provider_job x WHERE x.tenant_id=s.tenant_id AND x.connection_id=c.id AND x.kind='SYNC' AND x.state IN ('QUEUED','CLAIMED')) THEN
     problem := jsonb_build_object('status',409,'code','SYNC_IN_PROGRESS','detail','a read is already queued or running for this connection');
    ELSE
     INSERT INTO lv.provider_job(tenant_id,connection_id,kind,requested_by) VALUES(s.tenant_id,c.id,'SYNC',s.user_id) RETURNING * INTO j;
     PERFORM lv_identity.audit_append('PROVIDER_SYNC_REQUESTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text,'job_id',j.id::text));
     result := jsonb_build_object('status',202,'body',lv.provider_job_json(j));
    END IF;
   ELSIF action='DISCONNECT' THEN
    IF c.state='AUTHORIZING' THEN
     UPDATE lv.provider_oauth_state SET consumed_at=clock_timestamp(),outcome='CANCELLED' WHERE tenant_id=s.tenant_id AND connection_id=c.id AND consumed_at IS NULL;
     UPDATE lv.provider_connection SET state='FAILED',failure_code='AUTHORIZATION_CANCELLED',version=version+1,changed_at=clock_timestamp() WHERE tenant_id=s.tenant_id AND id=c.id RETURNING * INTO c;
     PERFORM lv_identity.audit_append('PROVIDER_DISCONNECT_REQUESTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text,'from_state','AUTHORIZING'));
     result := jsonb_build_object('status',200,'body',lv.provider_connection_json(c));
    ELSIF c.state='EXCHANGING' THEN problem := jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','detail','the authorization is being completed; try again shortly','retry_after',5);
    ELSIF c.state IN ('ACTIVE','DEGRADED') THEN
     UPDATE lv.provider_connection SET state='REVOKING',revoked_by=s.user_id,version=version+1,changed_at=clock_timestamp() WHERE tenant_id=s.tenant_id AND id=c.id RETURNING * INTO c;
     INSERT INTO lv.provider_job(tenant_id,connection_id,kind,requested_by) VALUES(s.tenant_id,c.id,'REVOKE',s.user_id) RETURNING * INTO j;
     PERFORM lv_identity.audit_append('PROVIDER_DISCONNECT_REQUESTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text,'job_id',j.id::text));
     result := jsonb_build_object('status',202,'body',lv.provider_connection_json(c));
    ELSIF c.state='REVOKING' AND coalesce(args->>'abandon_revocation','false')='true' THEN
     IF c.failure_code IS DISTINCT FROM 'REVOCATION_PENDING' OR EXISTS(SELECT 1 FROM lv.provider_job q WHERE q.tenant_id=s.tenant_id AND q.connection_id=c.id AND q.kind='REVOKE' AND q.state IN ('QUEUED','CLAIMED')) THEN
      problem := jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','detail','a revocation attempt is queued or running; abandon only a pending revocation','retry_after',5);
     ELSE
      -- Explicit, audited end of a revocation that could not reach Intuit: the local tokens are erased and the outcome says
      -- the grant may still be live; the administrator must disconnect the app in the Intuit account.
      DELETE FROM lv.provider_credential WHERE tenant_id=s.tenant_id AND connection_id=c.id;
      UPDATE lv.provider_connection SET state='REVOKED',revoked_at=clock_timestamp(),revoke_outcome='ABANDONED',failure_code=NULL,version=version+1,changed_at=clock_timestamp()
       WHERE tenant_id=s.tenant_id AND id=c.id RETURNING * INTO c;
      PERFORM lv_identity.audit_append('PROVIDER_REVOCATION_ABANDONED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text));
      result := jsonb_build_object('status',200,'body',lv.provider_connection_json(c));
     END IF;
    ELSIF c.state='REVOKING' AND NOT EXISTS(SELECT 1 FROM lv.provider_job q WHERE q.tenant_id=s.tenant_id AND q.connection_id=c.id AND q.kind='REVOKE' AND q.state IN ('QUEUED','CLAIMED')) THEN
     INSERT INTO lv.provider_job(tenant_id,connection_id,kind,requested_by) VALUES(s.tenant_id,c.id,'REVOKE',s.user_id) RETURNING * INTO j;
     PERFORM lv_identity.audit_append('PROVIDER_DISCONNECT_REQUESTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('connection_id',c.id::text,'job_id',j.id::text,'retry','true'));
     result := jsonb_build_object('status',202,'body',lv.provider_connection_json(c));
    ELSIF c.state='REVOKING' THEN problem := jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','detail','a revocation is already queued or running','retry_after',5);
    ELSE problem := jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','detail','this connection is already disconnected');
    END IF;
   END IF;
  END IF;
 END IF;
 IF problem IS NOT NULL THEN
  IF (problem->>'status')::integer NOT BETWEEN 400 AND 499 THEN RAISE EXCEPTION 'invalid provider refusal' USING ERRCODE='22023'; END IF;
  result := problem;
 ELSIF (result->>'status')::integer NOT IN (200,201,202) THEN RAISE EXCEPTION 'invalid provider response' USING ERRCODE='22023';
 END IF;
 IF (result->>'status')::integer<>409 OR result->>'code' NOT IN ('REQUEST_IN_PROGRESS') THEN
  DELETE FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=ikey;
  INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at)
  VALUES(s.tenant_id,s.user_id,'POST',route_name,ikey,rhash,(result->>'status')::smallint,result,transaction_timestamp()+interval '24 hours');
 END IF;
 RETURN result || jsonb_build_object('replayed',false);
END $$;

-- ---------------------------------------------------------------------------------------------------------- worker side
CREATE FUNCTION lv.provider_due(n integer) RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;c lv.provider_connection;BEGIN
 PERFORM lv.maintenance_context(NULL);IF n IS NULL OR n NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'invalid bound' USING ERRCODE='22023';END IF;
 FOR c IN SELECT x.* FROM lv.provider_connection x WHERE x.state='REVOKING' AND x.failure_code='REVOCATION_PENDING'
   AND EXISTS(SELECT 1 FROM lv.provider_credential k WHERE k.tenant_id=x.tenant_id AND k.connection_id=x.id)
   AND NOT EXISTS(SELECT 1 FROM lv.provider_job q WHERE q.tenant_id=x.tenant_id AND q.connection_id=x.id AND q.kind='REVOKE' AND (q.state IN ('QUEUED','CLAIMED') OR q.finished_at>clock_timestamp()-interval '1 hour'))
   AND (SELECT count(*) FROM lv.provider_job q WHERE q.tenant_id=x.tenant_id AND q.connection_id=x.id AND q.kind='REVOKE')<25
   AND coalesce((SELECT q.last_error FROM lv.provider_job q WHERE q.tenant_id=x.tenant_id AND q.connection_id=x.id AND q.kind='REVOKE' ORDER BY q.id DESC LIMIT 1),'') NOT IN ('ENVELOPE_INVALID','TRANSPORT_REFUSED')
   ORDER BY x.changed_at LIMIT n LOOP
  PERFORM lv.maintenance_context(c.tenant_id);
  INSERT INTO lv.provider_job(tenant_id,connection_id,kind,requested_by) VALUES(c.tenant_id,c.id,'REVOKE',coalesce(c.revoked_by,c.created_by));
 END LOOP;
 PERFORM lv.maintenance_context(NULL);
 FOR w IN SELECT * FROM lv.provider_job WHERE (state='QUEUED' AND available_at<=clock_timestamp() AND (dispatched_at IS NULL OR dispatched_at<clock_timestamp()-interval '30 seconds')) OR (state='CLAIMED' AND lease_expires_at<=clock_timestamp()) ORDER BY available_at,id LIMIT n LOOP
  PERFORM lv.maintenance_context(w.tenant_id);
  IF w.state='CLAIMED' THEN
   UPDATE lv.provider_job SET state=CASE WHEN attempts>=5 THEN 'DEAD' ELSE 'QUEUED' END,fence=fence+1,lease_owner=NULL,lease_expires_at=NULL,dispatched_at=NULL,last_error='LEASE_EXPIRED',
    finished_at=CASE WHEN attempts>=5 THEN clock_timestamp() END,payload=CASE WHEN attempts>=5 THEN NULL ELSE payload END WHERE tenant_id=w.tenant_id AND id=w.id AND state='CLAIMED' AND lease_expires_at<=clock_timestamp() RETURNING * INTO w;
   IF w.state='DEAD' THEN PERFORM lv.provider_settle_connection(w,'LEASE_EXPIRED'); END IF;
  END IF;
  IF w.state='QUEUED' THEN RETURN NEXT jsonb_build_object('tenant_id',w.tenant_id::text,'job_id',w.id::text);END IF;
 END LOOP;END $$;
CREATE FUNCTION lv.provider_dispatch(t uuid,j uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM lv.maintenance_context(t);UPDATE lv.provider_job SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE tenant_id=t AND id=j AND state='QUEUED';END $$;

-- A finished job's effect on its connection (worker context already set).
CREATE FUNCTION lv.provider_settle_connection(w lv.provider_job, error text) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE c lv.provider_connection;BEGIN
 SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=w.tenant_id AND id=w.connection_id FOR UPDATE;
 IF w.kind='EXCHANGE' AND c.state='EXCHANGING' AND w.state IN ('DEAD','DENIED') THEN
  UPDATE lv.provider_connection SET state='FAILED',failure_code=coalesce(error,'EXCHANGE_FAILED'),version=version+1,changed_at=clock_timestamp() WHERE tenant_id=c.tenant_id AND id=c.id;
  PERFORM lv_identity.audit_append('PROVIDER_CONNECT_FAILED','DENIED',w.requested_by,w.tenant_id,NULL,'provider-worker',w.id::text,jsonb_build_object('connection_id',c.id::text,'reason',coalesce(error,'EXCHANGE_FAILED')));
 ELSIF w.kind='SYNC' AND w.state IN ('DEAD','DENIED') THEN
  PERFORM lv_identity.audit_append('PROVIDER_SYNC_FINISHED','DENIED',w.requested_by,w.tenant_id,NULL,'provider-worker',w.id::text,jsonb_build_object('connection_id',c.id::text,'job_id',w.id::text,'error_class',error));
 ELSIF w.kind='REVOKE' AND w.state IN ('DEAD','DENIED') AND c.state='REVOKING' THEN
  -- Intuit could not be reached (or the worker could not finish): the encrypted tokens are the only material that can revoke
  -- the grant, so they are KEPT. The connection stays REVOKING (unusable: reads and refreshes need ACTIVE) and provider_due
  -- queues another revocation attempt hourly, bounded; an administrator can also retry by disconnecting again.
  UPDATE lv.provider_connection SET failure_code='REVOCATION_PENDING',version=version+1,changed_at=clock_timestamp() WHERE tenant_id=c.tenant_id AND id=c.id;
  PERFORM lv_identity.audit_append('PROVIDER_REVOKE_DEFERRED','DENIED',w.requested_by,w.tenant_id,NULL,'provider-worker',w.id::text,jsonb_build_object('connection_id',c.id::text,'error_class',coalesce(error,'UNKNOWN')));
 END IF;
END $$;

-- Claim. Returns ciphertext only. The requester's administer permission and membership are re-checked here for EXCHANGE and
-- SYNC (revocation between request and work denies the job); REVOKE proceeds, because revoking is always safe.
CREATE FUNCTION lv.provider_claim(t uuid,j uuid,worker text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;c lv.provider_connection;k lv.provider_credential;BEGIN
 PERFORM lv.maintenance_context(t);
 IF worker IS NULL OR worker!~'^[A-Za-z0-9_.:-]{1,100}$' THEN RAISE EXCEPTION 'invalid worker' USING ERRCODE='22023';END IF;
 SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT FOUND OR w.state<>'QUEUED' OR w.available_at>clock_timestamp() OR w.attempts>=5 THEN RETURN NULL;END IF;
 SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=t AND id=w.connection_id;
 IF w.kind='REVOKE' AND EXISTS(SELECT 1 FROM lv.provider_job x WHERE x.tenant_id=t AND x.connection_id=w.connection_id AND x.kind='SYNC' AND x.state='CLAIMED' AND x.lease_expires_at>clock_timestamp()) THEN
  UPDATE lv.provider_job SET available_at=clock_timestamp()+interval '5 seconds',dispatched_at=NULL WHERE tenant_id=t AND id=j;RETURN NULL;
 END IF;
 IF w.kind<>'REVOKE' AND (NOT lv.proposal_may(t,w.requested_by,'administer') OR NOT EXISTS(SELECT 1 FROM lv_identity.app_user u JOIN lv_identity.membership m ON m.user_id=u.id WHERE u.id=w.requested_by AND u.disabled_at IS NULL AND m.tenant_id=t AND m.status='ACTIVE')
     OR (w.kind='EXCHANGE' AND c.state<>'EXCHANGING') OR (w.kind='SYNC' AND c.state<>'ACTIVE')) THEN
  UPDATE lv.provider_job SET state='DENIED',fence=fence+1,payload=NULL,last_error=CASE WHEN w.kind='SYNC' AND c.state<>'ACTIVE' THEN 'CONNECTION_NOT_ACTIVE' ELSE 'REQUESTER_REVOKED' END,finished_at=clock_timestamp() WHERE tenant_id=t AND id=j RETURNING * INTO w;
  PERFORM lv.provider_settle_connection(w,w.last_error);RETURN NULL;
 END IF;
 UPDATE lv.provider_job SET state='CLAIMED',fence=fence+1,attempts=attempts+1,lease_owner=worker,lease_expires_at=clock_timestamp()+interval '120 seconds',started_at=coalesce(started_at,clock_timestamp())
  WHERE tenant_id=t AND id=j RETURNING * INTO w;
 SELECT * INTO k FROM lv.provider_credential WHERE tenant_id=t AND connection_id=w.connection_id;
 RETURN jsonb_build_object('fence',w.fence,'kind',w.kind,'connection_id',w.connection_id::text,'realm_id',c.realm_id,'progress',w.progress,'attempts',w.attempts,
  'payload_hex',CASE WHEN w.kind='EXCHANGE' THEN encode(w.payload,'hex') END,
  'state_hash',CASE WHEN w.kind='EXCHANGE' THEN (SELECT o.state_hash FROM lv.provider_oauth_state o WHERE o.tenant_id=t AND o.connection_id=w.connection_id AND o.outcome='CODE_RECEIVED') END,
  'credential',CASE WHEN k.connection_id IS NULL THEN NULL ELSE jsonb_build_object('kek_id',k.kek_id,'envelope_hex',encode(k.envelope,'hex'),'token_version',k.token_version,
   'access_expires_at',extract(epoch FROM k.access_expires_at)::bigint,'refresh_expires_at',extract(epoch FROM k.refresh_expires_at)::bigint) END);
END $$;

CREATE FUNCTION lv.provider_lease_ok(w lv.provider_job, worker text, f bigint) RETURNS boolean LANGUAGE sql SET search_path=pg_catalog,pg_temp AS $$
 SELECT w.id IS NOT NULL AND w.state='CLAIMED' AND w.fence=f AND w.lease_owner=worker AND w.lease_expires_at>clock_timestamp() $$;

-- Exchange completion: stores the first token envelope and activates the connection, or reports the realm conflict.
CREATE FUNCTION lv.provider_exchange_finish(t uuid,j uuid,worker text,f bigint,p_kek text,p_envelope bytea,p_access_exp bigint,p_refresh_exp bigint) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;c lv.provider_connection;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT lv.provider_lease_ok(w,worker,f) OR w.kind<>'EXCHANGE' THEN RETURN 'LOST';END IF;
 SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=t AND id=w.connection_id FOR UPDATE;
 IF c.state<>'EXCHANGING' THEN RETURN 'LOST';END IF;
 BEGIN
  UPDATE lv.provider_connection SET state='ACTIVE',connected_at=clock_timestamp(),version=version+1,changed_at=clock_timestamp() WHERE tenant_id=t AND id=c.id;
 EXCEPTION WHEN unique_violation THEN   -- the verified realm is bound to another organization: fail; the worker revokes the new tokens
  UPDATE lv.provider_connection SET state='FAILED',failure_code='REALM_ALREADY_CONNECTED',version=version+1,changed_at=clock_timestamp() WHERE tenant_id=t AND id=c.id;
  UPDATE lv.provider_job SET state='DONE',payload=NULL,lease_owner=NULL,lease_expires_at=NULL,finished_at=clock_timestamp(),result=jsonb_build_object('outcome','REALM_ALREADY_CONNECTED') WHERE tenant_id=t AND id=j;
  PERFORM lv_identity.audit_append('PROVIDER_CONNECT_FAILED','DENIED',w.requested_by,t,NULL,'provider-worker',j::text,jsonb_build_object('connection_id',c.id::text,'reason','REALM_ALREADY_CONNECTED'));
  RETURN 'REALM_ALREADY_CONNECTED';
 END;
 INSERT INTO lv.provider_credential(tenant_id,connection_id,kek_id,envelope,token_version,access_expires_at,refresh_expires_at)
 VALUES(t,c.id,p_kek,p_envelope,1,to_timestamp(p_access_exp),to_timestamp(p_refresh_exp));
 UPDATE lv.provider_job SET state='DONE',payload=NULL,lease_owner=NULL,lease_expires_at=NULL,finished_at=clock_timestamp(),result=jsonb_build_object('outcome','CONNECTED','realm_id',c.realm_id) WHERE tenant_id=t AND id=j;
 PERFORM lv_identity.audit_append('PROVIDER_CONNECTED','ALLOWED',w.requested_by,t,NULL,'provider-worker',j::text,jsonb_build_object('connection_id',c.id::text,'realm_id',c.realm_id));
 RETURN 'ACTIVE';
END $$;

-- Refresh coordination: one refresher per connection. The lease is committed before the network call; the new envelope is
-- accepted only if the fence and the token version are still the ones the refresher started from (compare-and-set).
CREATE FUNCTION lv.provider_refresh_claim(t uuid,j uuid,worker text,f bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;k lv.provider_credential;c lv.provider_connection;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j;
 IF NOT lv.provider_lease_ok(w,worker,f) THEN RAISE EXCEPTION 'lost provider job lease' USING ERRCODE='40001';END IF;
 SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=t AND id=w.connection_id FOR UPDATE;
 IF c.state<>'ACTIVE' THEN RETURN jsonb_build_object('granted',false,'reason','CONNECTION_NOT_ACTIVE');END IF;
 SELECT * INTO k FROM lv.provider_credential WHERE tenant_id=t AND connection_id=w.connection_id FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('granted',false,'reason','NO_CREDENTIAL');END IF;
 IF k.lease_owner IS NOT NULL AND k.lease_expires_at>clock_timestamp() AND k.lease_owner<>worker THEN
  RETURN jsonb_build_object('granted',false,'reason','REFRESH_IN_PROGRESS','token_version',k.token_version);END IF;
 UPDATE lv.provider_credential SET lease_owner=worker,lease_expires_at=clock_timestamp()+interval '60 seconds',lease_fence=lease_fence+1 WHERE tenant_id=t AND connection_id=w.connection_id RETURNING * INTO k;
 RETURN jsonb_build_object('granted',true,'lease_fence',k.lease_fence,'kek_id',k.kek_id,'envelope_hex',encode(k.envelope,'hex'),'token_version',k.token_version,
  'access_expires_at',extract(epoch FROM k.access_expires_at)::bigint,'refresh_expires_at',extract(epoch FROM k.refresh_expires_at)::bigint);
END $$;
CREATE FUNCTION lv.provider_refresh_finish(t uuid,j uuid,worker text,lf bigint,from_version integer,p_kek text,p_envelope bytea,p_access_exp bigint,p_refresh_exp bigint,error text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;k lv.provider_credential;c lv.provider_connection;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j;
 SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=t AND id=w.connection_id FOR UPDATE;
 SELECT * INTO k FROM lv.provider_credential WHERE tenant_id=t AND connection_id=w.connection_id FOR UPDATE;
 -- A new pair is accepted only while the connection is ACTIVE; a refresh landing after disconnect began is refused, and the
 -- worker revokes the pair it just received (runtime._access_token).
 IF error IS NULL AND c.state IS DISTINCT FROM 'ACTIVE' THEN
  UPDATE lv.provider_credential SET lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=t AND connection_id=w.connection_id AND lease_owner=worker;
  RETURN jsonb_build_object('accepted',false,'reason','CONNECTION_NOT_ACTIVE');
 END IF;
 IF NOT FOUND OR k.lease_owner IS DISTINCT FROM worker OR k.lease_fence<>lf OR k.token_version<>from_version OR (error IS NULL AND k.lease_expires_at<=clock_timestamp()) THEN
  RETURN jsonb_build_object('accepted',false);END IF;
 IF error IS NOT NULL THEN
  IF error NOT IN ('AUTH_INVALID','RELEASE') THEN RAISE EXCEPTION 'invalid refresh error' USING ERRCODE='22023';END IF;
  UPDATE lv.provider_credential SET lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=t AND connection_id=w.connection_id;
  IF error='AUTH_INVALID' THEN
   UPDATE lv.provider_connection SET state='DEGRADED',failure_code='AUTH_INVALID',version=version+1,changed_at=clock_timestamp() WHERE tenant_id=t AND id=w.connection_id AND state='ACTIVE';
   PERFORM lv_identity.audit_append('PROVIDER_AUTH_INVALID','DENIED',w.requested_by,t,NULL,'provider-worker',j::text,jsonb_build_object('connection_id',w.connection_id::text));
  END IF;
  RETURN jsonb_build_object('accepted',true,'token_version',k.token_version);
 END IF;
 UPDATE lv.provider_credential SET kek_id=p_kek,envelope=p_envelope,token_version=token_version+1,access_expires_at=to_timestamp(p_access_exp),refresh_expires_at=to_timestamp(p_refresh_exp),
  lease_owner=NULL,lease_expires_at=NULL,updated_at=clock_timestamp() WHERE tenant_id=t AND connection_id=w.connection_id RETURNING * INTO k;
 PERFORM lv_identity.audit_append('PROVIDER_CREDENTIAL_REFRESHED','ALLOWED',w.requested_by,t,NULL,'provider-worker',j::text,jsonb_build_object('connection_id',w.connection_id::text,'token_version',k.token_version::text));
 RETURN jsonb_build_object('accepted',true,'token_version',k.token_version);
END $$;
CREATE FUNCTION lv.provider_credential_current(t uuid,j uuid,worker text,f bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;k lv.provider_credential;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j;
 IF NOT lv.provider_lease_ok(w,worker,f) THEN RAISE EXCEPTION 'lost provider job lease' USING ERRCODE='40001';END IF;
 SELECT * INTO k FROM lv.provider_credential WHERE tenant_id=t AND connection_id=w.connection_id;
 IF NOT FOUND THEN RETURN NULL;END IF;
 RETURN jsonb_build_object('kek_id',k.kek_id,'envelope_hex',encode(k.envelope,'hex'),'token_version',k.token_version,'access_expires_at',extract(epoch FROM k.access_expires_at)::bigint,
  'refresh_expires_at',extract(epoch FROM k.refresh_expires_at)::bigint,'refresh_in_progress',k.lease_owner IS NOT NULL AND k.lease_expires_at>clock_timestamp());
END $$;

-- One committed page. Objects are extracted by PostgreSQL from the exact bytes received, so provider amounts keep their exact
-- decimal text (jsonb numeric); (entity, Id, SyncToken) already stored is skipped (Stage 2C §9.2 deduplication).
CREATE FUNCTION lv.provider_sync_page(t uuid,j uuid,worker text,f bigint,p_entity text,p_start integer,p_max integer,p_body bytea,p_tid text,p_minor text,p_provenance text,p_next jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;c lv.provider_connection;doc jsonb;arr jsonb;pid uuid:=uuidv7();n integer;ins integer;o jsonb;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT lv.provider_lease_ok(w,worker,f) OR w.kind<>'SYNC' THEN RAISE EXCEPTION 'lost provider job lease' USING ERRCODE='40001';END IF;
 SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=t AND id=w.connection_id;
 IF c.state<>'ACTIVE' THEN RAISE EXCEPTION 'provider connection is no longer active' USING ERRCODE='55000';END IF;
 IF octet_length(p_body)>8388608 THEN RAISE EXCEPTION 'provider page too large' USING ERRCODE='54000';END IF;
 doc := convert_from(p_body,'UTF8')::jsonb;
 IF jsonb_typeof(doc->'QueryResponse') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'provider page has no QueryResponse' USING ERRCODE='22023';END IF;
 arr := coalesce(doc->'QueryResponse'->p_entity,'[]'::jsonb);
 IF jsonb_typeof(arr) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'provider page entity is not a list' USING ERRCODE='22023';END IF;
 n := jsonb_array_length(arr);
 IF n>p_max THEN RAISE EXCEPTION 'provider page larger than requested' USING ERRCODE='22023';END IF;
 FOR o IN SELECT x FROM jsonb_array_elements(arr) x LOOP
  IF jsonb_typeof(o) IS DISTINCT FROM 'object' OR jsonb_typeof(o->'Id') IS DISTINCT FROM 'string' OR coalesce(o->>'Id','')!~'^[!-~]{1,64}$'
     OR jsonb_typeof(o->'SyncToken') IS DISTINCT FROM 'string' OR coalesce(o->>'SyncToken','')!~'^[0-9]{1,18}$' THEN
   RAISE EXCEPTION 'provider object without a valid Id and SyncToken' USING ERRCODE='22023';END IF;
 END LOOP;
 INSERT INTO lv.provider_page(tenant_id,id,connection_id,job_id,realm_id,entity,start_position,max_results,minor_version,intuit_tid,http_status,body,body_sha256,object_count,provenance)
 VALUES(t,pid,c.id,j,c.realm_id,p_entity,p_start,p_max,p_minor,p_tid,200,p_body,encode(sha256(p_body),'hex'),n,p_provenance);
 INSERT INTO lv.provider_object(tenant_id,connection_id,page_id,realm_id,entity,provider_id,sync_token,provider_last_updated,object,object_sha256,provenance)
 SELECT t,c.id,pid,c.realm_id,p_entity,x->>'Id',x->>'SyncToken',left(x->'MetaData'->>'LastUpdatedTime',64),x,encode(sha256(convert_to(x::text,'UTF8')),'hex'),p_provenance
 FROM jsonb_array_elements(arr) x ON CONFLICT(tenant_id,connection_id,entity,provider_id,sync_token) DO NOTHING;
 GET DIAGNOSTICS ins = ROW_COUNT;
 UPDATE lv.provider_job SET progress=p_next || jsonb_build_object('counts',coalesce(progress->'counts','{}'::jsonb) || jsonb_build_object(p_entity,jsonb_build_object(
   'pages',coalesce((progress->'counts'->p_entity->>'pages')::integer,0)+1,'objects',coalesce((progress->'counts'->p_entity->>'objects')::integer,0)+n,
   'new_versions',coalesce((progress->'counts'->p_entity->>'new_versions')::integer,0)+ins))),
  lease_expires_at=clock_timestamp()+interval '120 seconds' WHERE tenant_id=t AND id=j;
 RETURN jsonb_build_object('page_id',pid::text,'objects',n,'new_versions',ins);
END $$;

-- Finish any job (SYNC/EXCHANGE/REVOKE failure paths, SYNC success). RETRYABLE backs off; THROTTLED waits 60 s without using an
-- attempt (Intuit documents waiting 60 seconds after a 429); TERMINAL and AUTH_INVALID end the job.
CREATE FUNCTION lv.provider_job_finish(t uuid,j uuid,worker text,f bigint,outcome text,error_code text,p_result jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT lv.provider_lease_ok(w,worker,f) THEN RETURN false;END IF;
 IF outcome NOT IN ('DONE','RETRYABLE','THROTTLED','TERMINAL','AUTH_INVALID','REVOKED') OR (error_code IS NOT NULL AND error_code!~'^[A-Z_]{1,64}$') THEN RAISE EXCEPTION 'invalid provider outcome' USING ERRCODE='22023';END IF;
 IF outcome='DONE' THEN
  IF w.kind<>'SYNC' THEN RAISE EXCEPTION 'only a read finishes as DONE here' USING ERRCODE='22023';END IF;
  UPDATE lv.provider_job SET state='DONE',lease_owner=NULL,lease_expires_at=NULL,finished_at=clock_timestamp(),result=coalesce(p_result,'{}'::jsonb)||jsonb_build_object('counts',progress->'counts') WHERE tenant_id=t AND id=j RETURNING * INTO w;
  PERFORM lv_identity.audit_append('PROVIDER_SYNC_FINISHED','ALLOWED',w.requested_by,t,NULL,'provider-worker',j::text,jsonb_build_object('connection_id',w.connection_id::text,'job_id',j::text,'status',w.result->>'status'));
  RETURN true;
 END IF;
 IF outcome='THROTTLED' AND w.throttles<20 THEN
  UPDATE lv.provider_job SET state='QUEUED',fence=fence+1,attempts=attempts-1,throttles=throttles+1,lease_owner=NULL,lease_expires_at=NULL,dispatched_at=NULL,available_at=clock_timestamp()+interval '60 seconds',last_error='PROVIDER_THROTTLED' WHERE tenant_id=t AND id=j;
  RETURN true;
 END IF;
 IF outcome='AUTH_INVALID' THEN
  UPDATE lv.provider_connection SET state='DEGRADED',failure_code='AUTH_INVALID',version=version+1,changed_at=clock_timestamp() WHERE tenant_id=t AND id=w.connection_id AND state='ACTIVE';
  IF FOUND THEN PERFORM lv_identity.audit_append('PROVIDER_AUTH_INVALID','DENIED',w.requested_by,t,NULL,'provider-worker',j::text,jsonb_build_object('connection_id',w.connection_id::text));END IF;
 END IF;
 UPDATE lv.provider_job SET state=CASE WHEN outcome='REVOKED' THEN 'DENIED' WHEN outcome IN ('TERMINAL','AUTH_INVALID') OR attempts>=5 OR outcome='THROTTLED' THEN 'DEAD' ELSE 'QUEUED' END,
  fence=fence+1,lease_owner=NULL,lease_expires_at=NULL,dispatched_at=NULL,available_at=clock_timestamp()+make_interval(secs=>least(60,power(2,attempts)::integer)),
  last_error=coalesce(error_code,outcome),result=CASE WHEN p_result IS NULL THEN result ELSE p_result END,
  finished_at=CASE WHEN outcome IN ('TERMINAL','AUTH_INVALID','REVOKED','THROTTLED') OR attempts>=5 THEN clock_timestamp() END,
  payload=CASE WHEN outcome IN ('TERMINAL','AUTH_INVALID','REVOKED','THROTTLED') OR attempts>=5 THEN NULL ELSE payload END
  WHERE tenant_id=t AND id=j RETURNING * INTO w;
 IF w.state IN ('DEAD','DENIED') THEN PERFORM lv.provider_settle_connection(w,coalesce(error_code,outcome));END IF;
 RETURN true;
END $$;

-- Revocation completion: local tokens are erased in the same transaction that records the outcome.
CREATE FUNCTION lv.provider_revoke_finish(t uuid,j uuid,worker text,f bigint,p_outcome text,p_version integer) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.provider_job;c lv.provider_connection;k lv.provider_credential;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.provider_job WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT lv.provider_lease_ok(w,worker,f) OR w.kind<>'REVOKE' THEN RETURN false;END IF;
 IF p_outcome NOT IN ('PROVIDER_CONFIRMED','PROVIDER_UNCONFIRMED','NO_TOKENS') THEN RAISE EXCEPTION 'invalid revoke outcome' USING ERRCODE='22023';END IF;
 SELECT * INTO c FROM lv.provider_connection WHERE tenant_id=t AND id=w.connection_id FOR UPDATE;
 SELECT * INTO k FROM lv.provider_credential WHERE tenant_id=t AND connection_id=c.id FOR UPDATE;
 IF k.connection_id IS NOT NULL AND k.token_version IS DISTINCT FROM p_version THEN
  RETURN false;   -- a newer pair was committed after this worker read the credential: the job is retried and revokes that one too
 END IF;
 DELETE FROM lv.provider_credential WHERE tenant_id=t AND connection_id=c.id;
 IF c.state='REVOKING' THEN
  UPDATE lv.provider_connection SET state='REVOKED',revoked_at=clock_timestamp(),revoke_outcome=p_outcome,failure_code=NULL,version=version+1,changed_at=clock_timestamp() WHERE tenant_id=t AND id=c.id;
 END IF;
 UPDATE lv.provider_job SET state='DONE',lease_owner=NULL,lease_expires_at=NULL,finished_at=clock_timestamp(),result=jsonb_build_object('outcome',p_outcome) WHERE tenant_id=t AND id=j;
 PERFORM lv_identity.audit_append('PROVIDER_REVOKED','ALLOWED',w.requested_by,t,NULL,'provider-worker',j::text,jsonb_build_object('connection_id',c.id::text,'outcome',p_outcome));
 RETURN true;
END $$;

-- Key-encryption-key rotation (worker only). Lists credentials sealed under any other KEK, and rewraps one under the new KEK
-- only if neither its token version nor its KEK changed and no refresh lease is live (compare-and-set; never a refresh).
CREATE FUNCTION lv.provider_kek_inventory(p_current text,n integer) RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE r record;BEGIN
 PERFORM lv.maintenance_context(NULL);
 IF p_current IS NULL OR p_current!~'^[0-9a-f]{64}$' OR n IS NULL OR n NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'invalid inventory request' USING ERRCODE='22023';END IF;
 FOR r IN SELECT k.tenant_id,k.connection_id,c.realm_id,k.kek_id,k.token_version,k.envelope FROM lv.provider_credential k JOIN lv.provider_connection c ON (c.tenant_id,c.id)=(k.tenant_id,k.connection_id)
   WHERE k.kek_id<>p_current ORDER BY k.tenant_id,k.connection_id LIMIT n LOOP
  RETURN NEXT jsonb_build_object('tenant_id',r.tenant_id::text,'connection_id',r.connection_id::text,'realm_id',r.realm_id,'kek_id',r.kek_id,'token_version',r.token_version,'envelope_hex',encode(r.envelope,'hex'));
 END LOOP;
 RETURN NEXT jsonb_build_object('pending_code_envelopes',(SELECT count(*) FROM lv.provider_job j WHERE j.kind='EXCHANGE' AND j.payload IS NOT NULL));
END $$;
CREATE FUNCTION lv.provider_rewrap(t uuid,conn uuid,p_version integer,p_old_kek text,p_new_kek text,p_envelope bytea) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE k lv.provider_credential;BEGIN
 PERFORM lv.maintenance_context(t);
 IF p_new_kek IS NULL OR p_new_kek!~'^[0-9a-f]{64}$' OR p_envelope IS NULL OR substring(p_envelope from 5 for 32)<>decode(p_new_kek,'hex') THEN RAISE EXCEPTION 'invalid rewrap' USING ERRCODE='22023';END IF;
 SELECT * INTO k FROM lv.provider_credential WHERE tenant_id=t AND connection_id=conn FOR UPDATE;
 IF NOT FOUND OR k.token_version<>p_version OR k.kek_id<>p_old_kek OR (k.lease_owner IS NOT NULL AND k.lease_expires_at>clock_timestamp()) THEN RETURN false;END IF;
 UPDATE lv.provider_credential SET kek_id=p_new_kek,envelope=p_envelope,lease_fence=lease_fence+1,updated_at=clock_timestamp() WHERE tenant_id=t AND connection_id=conn;
 PERFORM lv_identity.audit_append('PROVIDER_KEK_ROTATED','ALLOWED',NULL,t,NULL,'provider-worker',conn::text,jsonb_build_object('connection_id',conn::text,'from_kek',p_old_kek,'to_kek',p_new_kek));
 RETURN true;
END $$;

REVOKE ALL ON FUNCTION lv.provider_kek_inventory(text,integer),lv.provider_rewrap(uuid,uuid,integer,text,text,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.provider_kek_inventory(text,integer),lv.provider_rewrap(uuid,uuid,integer,text,text,bytea) TO lv_worker;
REVOKE ALL ON FUNCTION lv.guard_provider_append_only(),lv.guard_provider_retained(),lv.provider_job_json(lv.provider_job),lv.provider_connection_json(lv.provider_connection),
 lv.provider_object_json(lv.provider_object,boolean),lv.provider_api(bytea,text,uuid,text,text,jsonb,jsonb,text,text),lv.provider_due(integer),lv.provider_dispatch(uuid,uuid),
 lv.provider_settle_connection(lv.provider_job,text),lv.provider_claim(uuid,uuid,text),lv.provider_lease_ok(lv.provider_job,text,bigint),
 lv.provider_exchange_finish(uuid,uuid,text,bigint,text,bytea,bigint,bigint),lv.provider_refresh_claim(uuid,uuid,text,bigint),
 lv.provider_refresh_finish(uuid,uuid,text,bigint,integer,text,bytea,bigint,bigint,text),lv.provider_credential_current(uuid,uuid,text,bigint),
 lv.provider_sync_page(uuid,uuid,text,bigint,text,integer,integer,bytea,text,text,text,jsonb),lv.provider_job_finish(uuid,uuid,text,bigint,text,text,jsonb),
 lv.provider_revoke_finish(uuid,uuid,text,bigint,text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.provider_api(bytea,text,uuid,text,text,jsonb,jsonb,text,text),lv.provider_job_json(lv.provider_job),lv.provider_connection_json(lv.provider_connection),
 lv.provider_object_json(lv.provider_object,boolean) TO lv_app;
GRANT EXECUTE ON FUNCTION lv.provider_due(integer),lv.provider_dispatch(uuid,uuid),lv.provider_claim(uuid,uuid,text),lv.provider_exchange_finish(uuid,uuid,text,bigint,text,bytea,bigint,bigint),
 lv.provider_refresh_claim(uuid,uuid,text,bigint),lv.provider_refresh_finish(uuid,uuid,text,bigint,integer,text,bytea,bigint,bigint,text),lv.provider_credential_current(uuid,uuid,text,bigint),
 lv.provider_sync_page(uuid,uuid,text,bigint,text,integer,integer,bytea,text,text,text,jsonb),lv.provider_job_finish(uuid,uuid,text,bigint,text,text,jsonb),
 lv.provider_revoke_finish(uuid,uuid,text,bigint,text,integer) TO lv_worker;
