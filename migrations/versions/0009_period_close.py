"""Authenticated, authorized, atomic period close; remediation of R-02.

Authority:
- Stage 2B Family 4 `POST /periods/{id}/close` (perm `administer`; `If-Match` -> Period CLOSED; 200; 409 already closed or an
  earlier period still open; "Period row locked FOR UPDATE, status CAS, audit, outbox for the staleness job"; Idem Y;
  job STALENESS_SCAN) and Part 12 (tables fiscal_period, outbox; I-12, P-PER-01).
- Stage 2B Part 2 (If-Match/ETag "v<version>", 428/412), Part 4 codes, Part 8.1 Idempotency-Key semantics, Part 8.2
  ("Close takes the period lock; the staleness job marks affected proposals STALE").
- Stage 2A §8.1 commit model (commit kind PERIOD_CLOSE: counter row lock, commit_batch, ledger_event, same transaction),
  §8.4 hash chains, §11.2 `api_idempotency`, `outbox`, `work_item`, §12.1/12.2 (`administer` includes period close),
  §13.1/13.2 outbox and work-item columns, §13.4 inbound idempotency. ADR-009, ADR-010, ADR-015, ADR-023.

What this migration does:
- PERIOD_CLOSE becomes a commit kind written ONLY by `lv.close_period`, a pinned SECURITY DEFINER function that resolves the
  caller's session from its token hash, requires `administer` in the session's tenant, and in one transaction writes the
  commit batch, the ledger event, the period CAS (OPEN -> CLOSED, version + 1), the STALENESS_SCAN work item and its
  outbox row, the counter heads, the security audit event and the stored idempotent response.
- A deferred constraint trigger re-derives and checks all of those facts at COMMIT, so a partial close cannot commit.
- R-02: `lv_app` loses UPDATE on `lv.fiscal_period`. Periods can still be created (OPEN only, never earlier than a closed
  period); a guard trigger refuses any status/close-field change that is not backed by a PERIOD_CLOSE commit made in the
  same transaction. Periods closed before this migration through the unaudited path are labelled LEGACY_UNAUDITED.
- Locking: ledger FOR SHARE -> ledger_counter FOR UPDATE -> period FOR UPDATE, the same order every posting path uses
  (ledger FOR SHARE -> counter FOR UPDATE -> period FOR SHARE), so close and post serialize on the counter row.
"""
from alembic import op

revision = '0009_period_close'
down_revision = '0008_identity_force_rls'
branch_labels = None
depends_on = None

NEW_TABLES = ('work_item', 'outbox', 'api_idempotency')
POLICY = "tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid"
ROUTE = '/api/v1/periods/{period_id}/close'

# NOTE: executed with exec_driver_sql, so the text must not contain a percent sign.
UPGRADE = r"""
ALTER TYPE lv.commit_kind ADD VALUE IF NOT EXISTS 'PERIOD_CLOSE';

-- A human (session) actor for non-seed commits. Exactly one actor per batch; seeds keep their service principal.
ALTER TABLE lv.commit_batch ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE lv.commit_batch ADD COLUMN created_by_user uuid REFERENCES lv_identity.app_user(id);
ALTER TABLE lv.commit_batch ADD CONSTRAINT commit_batch_one_actor CHECK ((created_by IS NULL) <> (created_by_user IS NULL));

-- Period version (ETag) and close facts.
ALTER TABLE lv.fiscal_period ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version > 0);
ALTER TABLE lv.fiscal_period ADD COLUMN closed_seq bigint;
ALTER TABLE lv.fiscal_period ADD COLUMN closed_at timestamptz;
ALTER TABLE lv.fiscal_period ADD COLUMN closed_by uuid REFERENCES lv_identity.app_user(id);
ALTER TABLE lv.fiscal_period ADD COLUMN close_provenance text CHECK (close_provenance IN ('AUDITED_COMMIT', 'LEGACY_UNAUDITED'));
ALTER TABLE lv.fiscal_period ADD CONSTRAINT fiscal_period_close_commit FOREIGN KEY (tenant_id, ledger_id, closed_seq)
 REFERENCES lv.commit_batch(tenant_id, ledger_id, seq);

-- Stage 2A §13.2 work_item (claim state). Runtime roles have no grants; no relay or worker exists yet.
CREATE TABLE lv.work_item (
 tenant_id uuid NOT NULL REFERENCES lv.tenant(tenant_id),
 id uuid NOT NULL DEFAULT uuidv7(),
 kind text NOT NULL CHECK (kind IN ('STALENESS_SCAN')),
 subject_type text NOT NULL CHECK (subject_type IN ('PERIOD')),
 subject_id uuid NOT NULL,
 ledger_id uuid NOT NULL,
 trigger_seq bigint NOT NULL,
 state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'CLAIMED', 'DONE', 'FAILED', 'DEAD')),
 lease_owner text, lease_expires_at timestamptz,
 fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
 attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
 max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
 last_error_class text,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id, id),
 UNIQUE (tenant_id, kind, subject_id, trigger_seq),
 FOREIGN KEY (tenant_id, ledger_id, trigger_seq) REFERENCES lv.commit_batch(tenant_id, ledger_id, seq),
 FOREIGN KEY (tenant_id, ledger_id, subject_id) REFERENCES lv.fiscal_period(tenant_id, ledger_id, id),
 CHECK (state <> 'CLAIMED' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);

-- Stage 2A §13.1 outbox (transport). dedupe_key is unique per tenant (2A §12.1: unique keys include the tenant).
CREATE TABLE lv.outbox (
 tenant_id uuid NOT NULL REFERENCES lv.tenant(tenant_id),
 id uuid NOT NULL DEFAULT uuidv7(),
 topic text NOT NULL CHECK (topic IN ('execution', 'integration', 'ai', 'regression', 'maintenance')),
 work_item_id uuid NOT NULL,
 dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 200),
 status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'DISPATCHED')),
 available_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 dispatched_at timestamptz,
 dispatch_count integer NOT NULL DEFAULT 0 CHECK (dispatch_count >= 0),
 PRIMARY KEY (tenant_id, id),
 UNIQUE (tenant_id, dedupe_key),
 FOREIGN KEY (tenant_id, work_item_id) REFERENCES lv.work_item(tenant_id, id),
 CHECK ((status = 'DISPATCHED') = (dispatched_at IS NOT NULL))
);
CREATE INDEX outbox_pending ON lv.outbox (available_at) WHERE status = 'PENDING';

-- Stage 2A §11.2 / §13.4, Stage 2B Part 8.1: stored responses for client retries, scope (tenant, principal, method, route, key).
CREATE TABLE lv.api_idempotency (
 tenant_id uuid NOT NULL REFERENCES lv.tenant(tenant_id),
 principal_id uuid NOT NULL REFERENCES lv_identity.app_user(id),
 method text NOT NULL CHECK (method = 'POST'),
 route text NOT NULL CHECK (length(route) BETWEEN 1 AND 200),
 idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[!-~]{1,64}$'),
 request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
 response_status smallint NOT NULL CHECK (response_status BETWEEN 200 AND 499),
 response jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 expires_at timestamptz NOT NULL,
 PRIMARY KEY (tenant_id, principal_id, method, route, idempotency_key),
 CHECK (expires_at = created_at + interval '24 hours')
);

-- Rows written by the unaudited pre-0009 path keep their status and are labelled; nothing is invented about them.
ALTER TABLE lv.fiscal_period NO FORCE ROW LEVEL SECURITY;
UPDATE lv.fiscal_period SET close_provenance = 'LEGACY_UNAUDITED' WHERE status = 'CLOSED';
ALTER TABLE lv.fiscal_period FORCE ROW LEVEL SECURITY;
ALTER TABLE lv.fiscal_period ADD CONSTRAINT fiscal_period_close_state CHECK (
 (status = 'OPEN' AND closed_seq IS NULL AND closed_at IS NULL AND closed_by IS NULL AND close_provenance IS NULL)
 OR (status = 'CLOSED' AND close_provenance = 'LEGACY_UNAUDITED' AND closed_seq IS NULL AND closed_by IS NULL)
 OR (status = 'CLOSED' AND close_provenance = 'AUDITED_COMMIT' AND closed_seq IS NOT NULL AND closed_at IS NOT NULL AND closed_by IS NOT NULL)
);

-- Period creation stays available to the runtime (ledger setup), but only OPEN, and never before a closed period.
CREATE FUNCTION lv.guard_period_insert() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF NEW.status <> 'OPEN' OR NEW.version <> 1 OR NEW.closed_seq IS NOT NULL OR NEW.closed_at IS NOT NULL
    OR NEW.closed_by IS NOT NULL OR NEW.close_provenance IS NOT NULL THEN
  RAISE EXCEPTION 'periods are created OPEN; closing requires the audited period-close commit' USING ERRCODE = '23514';
 END IF;
 -- Same per-ledger lock as lv.close_period: "no earlier open period" cannot be broken by a concurrent insert.
 PERFORM pg_advisory_xact_lock(hashtextextended('lv-period-config:' || NEW.tenant_id::text || ':' || NEW.ledger_id::text, 0));
 IF EXISTS (SELECT 1 FROM lv.fiscal_period WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id
            AND status = 'CLOSED' AND start_date > NEW.start_date) THEN
  RAISE EXCEPTION 'cannot create a period earlier than a closed period' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER period_insert_guard BEFORE INSERT ON lv.fiscal_period FOR EACH ROW EXECUTE FUNCTION lv.guard_period_insert();

-- Status CAS backstop for every role, including the owner: OPEN -> CLOSED only with this transaction's PERIOD_CLOSE commit.
CREATE FUNCTION lv.guard_period_transition() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF OLD.status = 'OPEN' AND NEW.status = 'CLOSED' THEN
  IF NEW.close_provenance IS DISTINCT FROM 'AUDITED_COMMIT' OR NEW.version <> OLD.version + 1
     OR NEW.closed_seq IS NULL OR NEW.closed_by IS NULL OR NEW.closed_at IS NULL
     OR NOT EXISTS (SELECT 1 FROM lv.commit_batch b WHERE b.tenant_id = NEW.tenant_id AND b.ledger_id = NEW.ledger_id
                    AND b.seq = NEW.closed_seq AND b.kind::text = 'PERIOD_CLOSE' AND b.created_xid = pg_current_xact_id()::text
                    AND b.created_by_user = NEW.closed_by AND (b.canonical_content::jsonb ->> 'period_id') = NEW.id::text) THEN
   RAISE EXCEPTION 'a period closes only through its audited PERIOD_CLOSE commit' USING ERRCODE = '23514';
  END IF;
 ELSIF (NEW.status, NEW.version, NEW.closed_seq, NEW.closed_at, NEW.closed_by, NEW.close_provenance)
       IS DISTINCT FROM (OLD.status, OLD.version, OLD.closed_seq, OLD.closed_at, OLD.closed_by, OLD.close_provenance) THEN
  RAISE EXCEPTION 'period close state is immutable' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER period_transition_guard BEFORE UPDATE ON lv.fiscal_period FOR EACH ROW EXECUTE FUNCTION lv.guard_period_transition();

-- Seed guards keep applying to every non-PERIOD_CLOSE batch exactly as before; PERIOD_CLOSE gets its own guard/validator.
DROP TRIGGER seed_guard ON lv.commit_batch;
CREATE TRIGGER seed_guard BEFORE INSERT ON lv.commit_batch FOR EACH ROW
 WHEN (NEW.kind::text <> 'PERIOD_CLOSE') EXECUTE FUNCTION lv.guard_seed_batch();
DROP TRIGGER seed_commit_complete ON lv.commit_batch;
CREATE CONSTRAINT TRIGGER seed_commit_complete AFTER INSERT ON lv.commit_batch DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN (NEW.kind::text <> 'PERIOD_CLOSE') EXECUTE FUNCTION lv.validate_seed_batch();

CREATE FUNCTION lv.guard_period_close_batch() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
DECLARE l lv.ledger;
BEGIN
 SELECT * INTO l FROM lv.ledger WHERE tenant_id = NEW.tenant_id AND id = NEW.ledger_id FOR SHARE;
 IF NOT FOUND OR l.purpose <> 'SANDBOX' OR l.source_kind <> 'NATIVE' THEN
  RAISE EXCEPTION 'period close is enabled for native SANDBOX ledgers only' USING ERRCODE = '23514';
 END IF;
 IF NEW.created_by IS NOT NULL OR NEW.created_by_user IS NULL THEN
  RAISE EXCEPTION 'a period close is attributed to an authenticated user' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER period_close_guard BEFORE INSERT ON lv.commit_batch FOR EACH ROW
 WHEN (NEW.kind::text = 'PERIOD_CLOSE') EXECUTE FUNCTION lv.guard_period_close_batch();

-- Canonical ledger-event content of one close, derived only from committed facts (shared by writer and validator).
CREATE FUNCTION lv.period_close_event(p_tenant uuid, p_ledger uuid, p_seq bigint, p_session uuid, p_request text) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT lv.canonical_seed_json(jsonb_build_object(
  'tenant_id', b.tenant_id::text, 'ledger_id', b.ledger_id::text, 'seq', b.seq::text, 'kind', 'PERIOD_CLOSE',
  'created_by_user', b.created_by_user::text, 'session_id', p_session::text, 'request_id', p_request,
  'period_id', p.id::text, 'start_date', p.start_date::text, 'end_date', p.end_date::text,
  'period_version_before', (p.version - 1)::text, 'period_version_after', p.version::text,
  'staleness_work_item_id', w.id::text))
 FROM lv.commit_batch b
 JOIN lv.fiscal_period p ON p.tenant_id = b.tenant_id AND p.ledger_id = b.ledger_id AND p.closed_seq = b.seq
 JOIN lv.work_item w ON w.tenant_id = b.tenant_id AND w.ledger_id = b.ledger_id AND w.trigger_seq = b.seq
  AND w.kind = 'STALENESS_SCAN' AND w.subject_id = p.id
 WHERE b.tenant_id = p_tenant AND b.ledger_id = p_ledger AND b.seq = p_seq
$$;

-- Commit-time validator: re-derives every effect of a PERIOD_CLOSE batch; any missing or divergent fact aborts COMMIT.
CREATE FUNCTION lv.validate_period_close_batch() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE p lv.fiscal_period; ev lv.ledger_event; payload jsonb; w lv.work_item;
BEGIN
 IF (SELECT count(*) FROM lv.fiscal_period WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND closed_seq = NEW.seq) <> 1
    OR (SELECT count(*) FROM lv.ledger_event WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND seq = NEW.seq) <> 1
    OR EXISTS (SELECT 1 FROM lv.journal_entry WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND seq = NEW.seq)
    OR EXISTS (SELECT 1 FROM lv.document_version WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND seq = NEW.seq)
    OR EXISTS (SELECT 1 FROM lv.document WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND seq = NEW.seq) THEN
  RAISE EXCEPTION 'incomplete period-close commit' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND closed_seq = NEW.seq;
 SELECT * INTO ev FROM lv.ledger_event WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND seq = NEW.seq;
 payload := ev.canonical_content::jsonb;
 IF p.status <> 'CLOSED' OR p.close_provenance <> 'AUDITED_COMMIT' OR p.closed_by IS DISTINCT FROM NEW.created_by_user
    OR (NEW.canonical_content::jsonb ->> 'period_id') <> p.id::text
    OR NEW.canonical_content <> lv.canonical_seed_json(jsonb_build_object('tenant_id', NEW.tenant_id::text, 'ledger_id', NEW.ledger_id::text,
        'seq', NEW.seq::text, 'kind', 'PERIOD_CLOSE', 'created_by_user', NEW.created_by_user::text, 'period_id', p.id::text,
        'request_hash', NEW.canonical_content::jsonb ->> 'request_hash'))
    OR ev.canonical_content IS DISTINCT FROM lv.period_close_event(NEW.tenant_id, NEW.ledger_id, NEW.seq,
        (payload ->> 'session_id')::uuid, payload ->> 'request_id') THEN
  RAISE EXCEPTION 'period-close history differs from committed facts' USING ERRCODE = '23514';
 END IF;
 IF EXISTS (SELECT 1 FROM lv.fiscal_period WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id
            AND status = 'OPEN' AND start_date < p.start_date) THEN
  RAISE EXCEPTION 'an earlier period is still open' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO w FROM lv.work_item WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id AND trigger_seq = NEW.seq
  AND kind = 'STALENESS_SCAN' AND subject_type = 'PERIOD' AND subject_id = p.id AND state = 'QUEUED';
 IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM lv.outbox o WHERE o.tenant_id = NEW.tenant_id AND o.work_item_id = w.id
                             AND o.topic = 'maintenance' AND o.status = 'PENDING') THEN
  RAISE EXCEPTION 'period close must queue its staleness scan through the outbox' USING ERRCODE = '23514';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM lv_identity.audit_event a WHERE a.chain_key = NEW.tenant_id AND a.tenant_id = NEW.tenant_id
                AND a.event_type = 'PERIOD_CLOSED' AND a.outcome = 'ALLOWED' AND a.user_id = NEW.created_by_user
                AND a.session_id = (payload ->> 'session_id')::uuid AND a.request_id = payload ->> 'request_id'
                AND a.detail ->> 'ledger_id' = NEW.ledger_id::text AND a.detail ->> 'seq' = NEW.seq::text
                AND a.detail ->> 'period_id' = p.id::text) THEN
  RAISE EXCEPTION 'period close requires its attributed audit event' USING ERRCODE = '23514';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM lv.api_idempotency i WHERE i.tenant_id = NEW.tenant_id AND i.principal_id = NEW.created_by_user
                AND i.response_status = 200 AND i.response -> 'body' ->> 'close_seq' = NEW.seq::text
                AND i.response -> 'body' ->> 'id' = p.id::text) THEN
  RAISE EXCEPTION 'period close requires its stored idempotent response' USING ERRCODE = '23514';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM lv.ledger_counter c
  JOIN lv.commit_batch b ON (b.tenant_id, b.ledger_id, b.seq, b.hash) = (c.tenant_id, c.ledger_id, c.last_seq, c.batch_head)
  JOIN lv.ledger_event h ON (h.tenant_id, h.ledger_id, h.seq, h.hash) = (c.tenant_id, c.ledger_id, c.last_seq, c.event_head)
  WHERE c.tenant_id = NEW.tenant_id AND c.ledger_id = NEW.ledger_id
  AND c.last_seq = (SELECT max(seq) FROM lv.commit_batch WHERE tenant_id = NEW.tenant_id AND ledger_id = NEW.ledger_id)) THEN
  RAISE EXCEPTION 'counter and history heads diverge' USING ERRCODE = '23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER period_close_complete AFTER INSERT ON lv.commit_batch DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN (NEW.kind::text = 'PERIOD_CLOSE') EXECUTE FUNCTION lv.validate_period_close_batch();

-- Problem helper for lv.close_period's refusal branch: raise a structured refusal that the outer block turns into a response.
CREATE FUNCTION lv.period_close_refusal(p_status integer, p_code text, p_detail text, p_extra jsonb) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 RAISE EXCEPTION 'period close refused' USING ERRCODE = 'LVR01',
  DETAIL = (jsonb_build_object('status', p_status, 'code', p_code, 'detail', p_detail) || coalesce(p_extra, '{}'::jsonb))::text;
END $$;

CREATE FUNCTION lv.period_json(p lv.fiscal_period) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object('id', p.id::text, 'ledger_id', p.ledger_id::text, 'start_date', p.start_date::text,
  'end_date', p.end_date::text, 'status', p.status::text, 'version', p.version,
  'closed_at', to_char(p.closed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'closed_by', p.closed_by::text, 'close_seq', p.closed_seq::text, 'close_provenance', p.close_provenance)
$$;

-- The ONLY way to close a period. Runtime entry point (EXECUTE granted to lv_app only).
CREATE FUNCTION lv.close_period(p_token_hash bytea, p_period uuid, p_if_match text, p_idempotency_key text,
                                p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
 v_route constant text := '/api/v1/periods/{period_id}/close';
 s lv_identity.session; v_problem text; t uuid; u uuid;
 req_hash text; prior lv.api_idempotency; result jsonb; refusal text;
 p lv.fiscal_period; l lv.ledger; counter lv.ledger_counter; wanted integer;
 next_seq bigint; batch_content text; event_content text; bh text; eh text;
 job uuid := uuidv7(); v_now timestamptz := statement_timestamp();
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN
  RAISE EXCEPTION 'an authenticated session is required' USING ERRCODE = '42501';
 END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash = p_token_hash;
 v_problem := lv_identity.session_problem(s);
 IF v_problem IS NOT NULL OR s.tenant_id IS NULL THEN
  RAISE EXCEPTION 'an authenticated session with a selected tenant is required' USING ERRCODE = '42501';
 END IF;
 t := s.tenant_id; u := s.user_id;
 -- Never adopt a context the caller set for someone else; bind this transaction to the verified session.
 IF coalesce(current_setting('app.tenant_id', true), '') NOT IN ('', t::text)
    OR coalesce(current_setting('app.user_id', true), '') NOT IN ('', u::text)
    OR coalesce(current_setting('app.session_id', true), '') NOT IN ('', s.id::text) THEN
  RAISE EXCEPTION 'transaction context belongs to a different session' USING ERRCODE = '42501';
 END IF;
 PERFORM set_config('app.tenant_id', t::text, true);
 PERFORM set_config('app.user_id', u::text, true);
 PERFORM set_config('app.session_id', s.id::text, true);
 IF NOT EXISTS (SELECT 1 FROM lv_identity.membership_role r JOIN lv_identity.role_permission rp ON rp.role = r.role
                WHERE r.tenant_id = t AND r.user_id = u AND rp.permission = 'administer') THEN
  PERFORM lv_identity.audit_append('PERIOD_CLOSE_DENIED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('period_id', p_period::text, 'reason', 'PERMISSION_DENIED'));
  RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires administer', 'stored', false);
 END IF;
 IF p_idempotency_key IS NULL OR p_idempotency_key !~ '^[!-~]{1,64}$' THEN
  RAISE EXCEPTION 'invalid idempotency key' USING ERRCODE = '22023';
 END IF;
 IF p_if_match IS NOT NULL AND length(p_if_match) > 64 THEN
  RAISE EXCEPTION 'invalid If-Match' USING ERRCODE = '22023';
 END IF;
 req_hash := encode(sha256(convert_to(lv.canonical_seed_json(jsonb_build_object('method', 'POST', 'route', v_route,
              'period_id', p_period::text, 'if_match', p_if_match)), 'UTF8')), 'hex');
 -- Part 8.1: a repeat while the first is still running is 409 REQUEST_IN_PROGRESS (never a second execution).
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-idem:' || t::text || ':' || u::text || ':POST:' || v_route || ':' || p_idempotency_key, 0)) THEN
  RETURN jsonb_build_object('status', 409, 'code', 'REQUEST_IN_PROGRESS', 'detail', 'the first request with this Idempotency-Key is still running',
                            'retry_after', 1, 'stored', false);
 END IF;
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id = t AND i.principal_id = u AND i.method = 'POST'
  AND i.route = v_route AND i.idempotency_key = p_idempotency_key;
 IF FOUND THEN
  IF prior.expires_at <= statement_timestamp() THEN
   DELETE FROM lv.api_idempotency i WHERE i.tenant_id = t AND i.principal_id = u AND i.method = 'POST'
    AND i.route = v_route AND i.idempotency_key = p_idempotency_key;
  ELSIF prior.request_hash <> req_hash THEN
   RETURN jsonb_build_object('status', 422, 'code', 'IDEMPOTENCY_KEY_REUSED', 'detail', 'this Idempotency-Key was used with a different request',
                             'stored', false);
  ELSE
   RETURN prior.response || jsonb_build_object('replayed', true, 'stored', true);
  END IF;
 END IF;

 -- Every effect happens inside this block; any refusal rolls the block back (including locks and the counter row it may
 -- have created) before the refusal itself is stored and audited.
 BEGIN
  IF p_if_match IS NULL THEN
   PERFORM lv.period_close_refusal(428, 'VERSION_REQUIRED', 'If-Match is required', NULL);
  END IF;
  SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id = t AND id = p_period;
  IF NOT FOUND THEN PERFORM lv.period_close_refusal(404, 'NOT_FOUND', 'period not found', NULL); END IF;
  -- Lock order shared with every posting path: ledger (share) -> counter (update) -> period.
  SELECT * INTO l FROM lv.ledger WHERE tenant_id = t AND id = p.ledger_id FOR SHARE;
  IF l.purpose <> 'SANDBOX' OR l.source_kind <> 'NATIVE' THEN
   PERFORM lv.period_close_refusal(422, 'CAPABILITY_UNAVAILABLE', 'period close is enabled for native SANDBOX ledgers only', NULL);
  END IF;
  INSERT INTO lv.ledger_counter(tenant_id, ledger_id) VALUES (t, l.id) ON CONFLICT DO NOTHING;
  SELECT * INTO counter FROM lv.ledger_counter WHERE tenant_id = t AND ledger_id = l.id FOR UPDATE;
  PERFORM pg_advisory_xact_lock(hashtextextended('lv-period-config:' || t::text || ':' || l.id::text, 0));
  SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id = t AND ledger_id = l.id AND id = p_period FOR UPDATE;
  wanted := CASE WHEN p_if_match ~ '^"?v[0-9]{1,9}"?$' THEN substring(p_if_match FROM 'v([0-9]+)')::integer ELSE -1 END;
  IF wanted <> p.version THEN
   PERFORM lv.period_close_refusal(412, 'VERSION_MISMATCH', 'If-Match does not match the current period version',
    jsonb_build_object('current_version', p.version));
  END IF;
  IF p.status <> 'OPEN' THEN
   PERFORM lv.period_close_refusal(409, 'INVALID_STATE_TRANSITION', 'the period is already closed',
    jsonb_build_object('state', p.status::text, 'current_version', p.version));
  END IF;
  IF EXISTS (SELECT 1 FROM lv.fiscal_period WHERE tenant_id = t AND ledger_id = l.id AND status = 'OPEN' AND start_date < p.start_date) THEN
   PERFORM lv.period_close_refusal(409, 'INVALID_STATE_TRANSITION', 'an earlier period is still open',
    jsonb_build_object('state', p.status::text, 'current_version', p.version));
  END IF;

  next_seq := counter.last_seq + 1;
  batch_content := lv.canonical_seed_json(jsonb_build_object('tenant_id', t::text, 'ledger_id', l.id::text, 'seq', next_seq::text,
   'kind', 'PERIOD_CLOSE', 'created_by_user', u::text, 'period_id', p.id::text, 'request_hash', req_hash));
  bh := encode(sha256(convert_to(batch_content || counter.batch_head, 'UTF8')), 'hex');
  INSERT INTO lv.commit_batch(tenant_id, ledger_id, seq, kind, created_by, created_by_user, previous_seq, previous_hash, canonical_content, hash)
  VALUES (t, l.id, next_seq, 'PERIOD_CLOSE', NULL, u, nullif(next_seq - 1, 0), counter.batch_head, batch_content, bh);
  UPDATE lv.fiscal_period SET status = 'CLOSED', version = p.version + 1, closed_seq = next_seq, closed_at = v_now,
   closed_by = u, close_provenance = 'AUDITED_COMMIT'
  WHERE tenant_id = t AND ledger_id = l.id AND id = p.id AND status = 'OPEN' AND version = p.version;
  INSERT INTO lv.work_item(tenant_id, id, kind, subject_type, subject_id, ledger_id, trigger_seq)
  VALUES (t, job, 'STALENESS_SCAN', 'PERIOD', p.id, l.id, next_seq);
  INSERT INTO lv.outbox(tenant_id, topic, work_item_id, dedupe_key)
  VALUES (t, 'maintenance', job, 'STALENESS_SCAN:' || l.id::text || ':' || p.id::text || ':' || next_seq::text);
  event_content := lv.period_close_event(t, l.id, next_seq, s.id, p_request);
  eh := encode(sha256(convert_to(event_content || counter.event_head, 'UTF8')), 'hex');
  INSERT INTO lv.ledger_event VALUES (t, l.id, next_seq, nullif(next_seq - 1, 0), counter.event_head, eh, event_content);
  UPDATE lv.ledger_counter SET last_seq = next_seq, batch_head = bh, event_head = eh WHERE tenant_id = t AND ledger_id = l.id;
  PERFORM lv_identity.audit_append('PERIOD_CLOSED', 'ALLOWED', u, t, s.id, p_source, p_request,
   jsonb_build_object('ledger_id', l.id::text, 'period_id', p.id::text, 'seq', next_seq::text, 'start_date', p.start_date::text,
                      'end_date', p.end_date::text, 'period_version', (p.version + 1)::text, 'staleness_work_item_id', job::text));
  SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id = t AND ledger_id = l.id AND id = p_period;
  result := jsonb_build_object('status', 200, 'etag', 'v' || p.version::text,
   'body', lv.period_json(p) || jsonb_build_object('staleness_job', jsonb_build_object('id', job::text, 'kind', 'STALENESS_SCAN', 'state', 'QUEUED')));
 EXCEPTION WHEN SQLSTATE 'LVR01' THEN
  GET STACKED DIAGNOSTICS refusal = PG_EXCEPTION_DETAIL;
  result := refusal::jsonb;
  PERFORM lv_identity.audit_append('PERIOD_CLOSE_REFUSED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('period_id', p_period::text, 'reason', result ->> 'code'));
 END;
 -- Part 8.1: success and 4xx refusals are stored in the same transaction and replayed for the same key and request.
 INSERT INTO lv.api_idempotency(tenant_id, principal_id, method, route, idempotency_key, request_hash, response_status, response, expires_at)
 VALUES (t, u, 'POST', v_route, p_idempotency_key, req_hash, (result ->> 'status')::smallint, result, transaction_timestamp() + interval '24 hours');
 RETURN result || jsonb_build_object('replayed', false, 'stored', true);
END $$;

CREATE FUNCTION lv.reject_idempotency_update() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 RAISE EXCEPTION 'stored idempotent responses are immutable; an expired key is deleted and reused' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER immutable BEFORE UPDATE ON lv.api_idempotency FOR EACH ROW EXECUTE FUNCTION lv.reject_idempotency_update();
"""


def upgrade():
    bind = op.get_bind()
    bind.exec_driver_sql(UPGRADE)
    for name in NEW_TABLES:
        op.execute(f'ALTER TABLE lv.{name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE lv.{name} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_scope ON lv.{name} USING ({POLICY}) WITH CHECK ({POLICY})')
    # R-02: the runtime can no longer change a period after creating it.
    op.execute('REVOKE UPDATE ON lv.fiscal_period FROM lv_app')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC')
    op.execute('GRANT EXECUTE ON FUNCTION lv.close_period(bytea, uuid, text, text, text, text) TO lv_app')


DOWNGRADE = r"""
DROP FUNCTION lv.close_period(bytea, uuid, text, text, text, text);
DROP TRIGGER period_close_complete ON lv.commit_batch;
DROP TRIGGER period_close_guard ON lv.commit_batch;
DROP TRIGGER seed_commit_complete ON lv.commit_batch;
CREATE CONSTRAINT TRIGGER seed_commit_complete AFTER INSERT ON lv.commit_batch DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_seed_batch();
DROP TRIGGER seed_guard ON lv.commit_batch;
CREATE TRIGGER seed_guard BEFORE INSERT ON lv.commit_batch FOR EACH ROW EXECUTE FUNCTION lv.guard_seed_batch();
DROP TRIGGER period_transition_guard ON lv.fiscal_period;
DROP TRIGGER period_insert_guard ON lv.fiscal_period;
DROP TABLE lv.api_idempotency;
DROP TABLE lv.outbox;
DROP TABLE lv.work_item;
ALTER TABLE lv.fiscal_period DROP CONSTRAINT fiscal_period_close_state;
ALTER TABLE lv.fiscal_period DROP CONSTRAINT fiscal_period_close_commit;
ALTER TABLE lv.fiscal_period DROP COLUMN close_provenance;
ALTER TABLE lv.fiscal_period DROP COLUMN closed_by;
ALTER TABLE lv.fiscal_period DROP COLUMN closed_at;
ALTER TABLE lv.fiscal_period DROP COLUMN closed_seq;
ALTER TABLE lv.fiscal_period DROP COLUMN version;
ALTER TABLE lv.commit_batch DROP CONSTRAINT commit_batch_one_actor;
ALTER TABLE lv.commit_batch DROP COLUMN created_by_user;
ALTER TABLE lv.commit_batch ALTER COLUMN created_by SET NOT NULL;
DROP FUNCTION lv.validate_period_close_batch();
DROP FUNCTION lv.period_close_event(uuid, uuid, bigint, uuid, text);
DROP FUNCTION lv.guard_period_close_batch();
DROP FUNCTION lv.guard_period_transition();
DROP FUNCTION lv.guard_period_insert();
DROP FUNCTION lv.period_close_refusal(integer, text, text, jsonb);
DROP FUNCTION lv.period_json(lv.fiscal_period);
DROP FUNCTION lv.reject_idempotency_update();
"""


def downgrade():
    # Disposable databases only. Under FORCE RLS the owner sees nothing without tenant context, so lift FORCE for the check.
    op.execute('ALTER TABLE lv.commit_batch NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE lv.api_idempotency NO FORCE ROW LEVEL SECURITY')
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM lv.commit_batch WHERE kind::text = 'PERIOD_CLOSE') OR EXISTS (SELECT 1 FROM lv.api_idempotency) THEN
        RAISE EXCEPTION 'audited period closes or stored responses exist; downgrade is for disposable databases without them' USING ERRCODE = '55000';
      END IF; END $$""")
    op.execute('ALTER TABLE lv.commit_batch FORCE ROW LEVEL SECURITY')
    op.get_bind().exec_driver_sql(DOWNGRADE)
    # Restores the 0008 grants exactly, including the unaudited UPDATE path (R-02) that 0009 removes.
    op.execute('GRANT UPDATE ON lv.fiscal_period TO lv_app')
    # The enum label PERIOD_CLOSE cannot be removed by PostgreSQL; it is unused after downgrade.
