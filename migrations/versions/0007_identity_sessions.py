"""Identity boundary: users, tenant memberships and roles, server-side sessions, login throttling, security audit chain.

Owning requirements (accepted copies dated 20261102-132232):
- ADR-015 (+ amendment): FastAPI owns authentication with httpOnly SameSite session cookies and CSRF tokens; permission
  verbs read, propose, review, execute, source.import, ledger.bootstrap, regression, evidence.export, audit.read,
  administer; administrator has no financial approval by default; RLS with FORCE, non-owner app role, SET LOCAL tenant.
- ADR-027: email + password; Argon2id; HttpOnly/Secure/SameSite cookies rotated on login and privilege change; CSRF;
  tenant selection per session; email verification before first sign-in; rate limits per account and per source;
  user and administrator session revocation; audit of every security event.
- Stage 2A §11.2 (platform tables app_user, membership, session, audit_event — append-only, hash chain per tenant),
  §12.1 (lv_app not owner, no BYPASSRLS; tenant context per transaction from the authenticated session), §12.2 roles.
- Stage 2B §5.1 (permissions by role), §5.2 (argon2id; HttpOnly, Secure, SameSite=Lax; rotation; 30-minute idle and
  12-hour absolute timeouts; X-CSRF-Token; one active tenant per session; switching creates a new session),
  §5.3, §5.5 (login, logout, failed login, membership/role changes audited), Family 1 endpoints.

Placement: identity tables live in a separate schema `lv_identity` that lv_app cannot read or write. lv_app may only
EXECUTE five SECURITY DEFINER functions (search_path pinned to pg_catalog, pg_temp; EXECUTE revoked from PUBLIC).
Administrative functions (provisioning, disabling, revocation) are executable only by the schema owner, i.e. through the
local provisioning command — never by the runtime service. RLS is ENABLED without FORCE and with no policies, so any
role other than the owner sees no rows even if a grant were added by mistake; the owner-run functions can read them.

Passwords are verified by the API process with argon2-cffi (PostgreSQL has no Argon2). The database stores only Argon2id
PHC strings, SHA-256 hashes of session tokens, and SHA-256 keys of login emails for throttling. No usable bearer token,
password or CSRF secret is stored. Session lifetimes are fixed in the database (30 min idle, 12 h absolute) so the API
cannot extend them. Throttling limits (5 failures per account and 30 attempts per source per 15 minutes) are local
deployment defaults, NOT an accepted product rule (ADR-027 requires limits but does not set them).
"""
from alembic import op

revision = '0007_identity_sessions'
down_revision = '0006_processor_settlements'
branch_labels = None
depends_on = None

NIL = '00000000-0000-0000-0000-000000000000'

UPGRADE = rf"""
CREATE SCHEMA lv_identity AUTHORIZATION lv_owner;
REVOKE ALL ON SCHEMA lv_identity FROM PUBLIC;

CREATE TYPE lv_identity.member_role AS ENUM ('ANALYST', 'CONTROLLER', 'AUTOMATION_DEVELOPER', 'ADMINISTRATOR');
CREATE TYPE lv_identity.permission AS ENUM ('read', 'propose', 'review', 'execute', 'source.import', 'ledger.bootstrap',
                                            'regression', 'evidence.export', 'audit.read', 'administer');

-- Stage 2B §5.1 (= 2A §12.2 + 2B additions). Administrator: no review/execute (ADR-015 "no financial approval by default").
CREATE TABLE lv_identity.role_permission (
 role lv_identity.member_role NOT NULL, permission lv_identity.permission NOT NULL, PRIMARY KEY (role, permission));
INSERT INTO lv_identity.role_permission VALUES
 ('ANALYST', 'read'), ('ANALYST', 'propose'), ('ANALYST', 'source.import'),
 ('CONTROLLER', 'read'), ('CONTROLLER', 'review'), ('CONTROLLER', 'execute'), ('CONTROLLER', 'regression'),
 ('CONTROLLER', 'evidence.export'), ('CONTROLLER', 'audit.read'),
 ('AUTOMATION_DEVELOPER', 'read'), ('AUTOMATION_DEVELOPER', 'propose'), ('AUTOMATION_DEVELOPER', 'source.import'),
 ('AUTOMATION_DEVELOPER', 'regression'), ('AUTOMATION_DEVELOPER', 'evidence.export'),
 ('ADMINISTRATOR', 'read'), ('ADMINISTRATOR', 'ledger.bootstrap'), ('ADMINISTRATOR', 'audit.read'), ('ADMINISTRATOR', 'administer');

CREATE TABLE lv_identity.app_user (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 email text NOT NULL UNIQUE CHECK (email = lower(btrim(email)) AND length(email) <= 320 AND email ~ '^[^@[:space:]]{{1,64}}@[^@[:space:]]{{1,255}}$'),
 display_name varchar(120) NOT NULL CHECK (length(btrim(display_name)) > 0),
 password_hash text NOT NULL CHECK (password_hash LIKE '$argon2id$%%' AND length(password_hash) <= 512),
 email_verified_at timestamptz,
 disabled_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TABLE lv_identity.membership (
 tenant_id uuid NOT NULL REFERENCES lv.tenant (tenant_id),
 user_id uuid NOT NULL REFERENCES lv_identity.app_user (id),
 status varchar(8) NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED')),
 created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
 revoked_at timestamptz,
 PRIMARY KEY (tenant_id, user_id),
 CHECK ((status = 'REVOKED') = (revoked_at IS NOT NULL))
);

-- Roles per membership, with grant time and grantor kept for the future segregation rule (no approval within the
-- cooling-off window after a review/execute grant; 2A §12.2). That rule is NOT enforced here: approvals do not exist yet.
CREATE TABLE lv_identity.membership_role (
 tenant_id uuid NOT NULL, user_id uuid NOT NULL, role lv_identity.member_role NOT NULL,
 granted_at timestamptz NOT NULL DEFAULT statement_timestamp(),
 granted_by uuid REFERENCES lv_identity.app_user (id),
 grant_source varchar(32) NOT NULL CHECK (grant_source = 'LOCAL_PROVISIONING'),
 PRIMARY KEY (tenant_id, user_id, role),
 FOREIGN KEY (tenant_id, user_id) REFERENCES lv_identity.membership (tenant_id, user_id)
);

CREATE TABLE lv_identity.session (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
 user_id uuid NOT NULL REFERENCES lv_identity.app_user (id),
 tenant_id uuid,
 created_at timestamptz NOT NULL,
 last_seen_at timestamptz NOT NULL,
 idle_timeout interval NOT NULL CHECK (idle_timeout = interval '30 minutes'),
 absolute_expires_at timestamptz NOT NULL CHECK (absolute_expires_at > created_at - interval '12 hours'
                                                 AND absolute_expires_at <= created_at + interval '12 hours'),
 source_key varchar(64) NOT NULL,
 revoked_at timestamptz,
 revoke_reason varchar(24) CHECK (revoke_reason IN ('LOGOUT', 'ROTATED_LOGIN', 'TENANT_SWITCH', 'ADMIN_REVOKED',
                                                    'USER_DISABLED', 'MEMBERSHIP_REVOKED', 'ROLES_CHANGED')),
 replaced_by uuid REFERENCES lv_identity.session (id),
 CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL)),
 CHECK (last_seen_at >= created_at),
 FOREIGN KEY (tenant_id, user_id) REFERENCES lv_identity.membership (tenant_id, user_id)
);
CREATE INDEX session_user_active ON lv_identity.session (user_id) WHERE revoked_at IS NULL;

CREATE TABLE lv_identity.login_attempt (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 email_key bytea NOT NULL CHECK (octet_length(email_key) = 32),
 source_key varchar(64) NOT NULL,
 user_id uuid REFERENCES lv_identity.app_user (id),
 started_at timestamptz NOT NULL,
 finished_at timestamptz,
 outcome varchar(12) CHECK (outcome IN ('SUCCEEDED', 'FAILED', 'THROTTLED')),
 CHECK ((outcome IS NULL) = (finished_at IS NULL))
);
CREATE INDEX login_attempt_email ON lv_identity.login_attempt (email_key, started_at);
CREATE INDEX login_attempt_source ON lv_identity.login_attempt (source_key, started_at);

-- Security audit: append-only, hash-chained per tenant (2A §11.2); events without a tenant use the platform chain {NIL}.
CREATE TABLE lv_identity.audit_event (
 chain_key uuid NOT NULL, seq bigint NOT NULL CHECK (seq > 0),
 occurred_at timestamptz NOT NULL, event_type varchar(40) NOT NULL, outcome varchar(12) NOT NULL,
 user_id uuid, tenant_id uuid, session_id uuid, source_key varchar(64), request_id varchar(64),
 detail jsonb NOT NULL, canonical_content text NOT NULL, prev_hash char(64) NOT NULL, hash char(64) NOT NULL,
 PRIMARY KEY (chain_key, seq),
 CHECK (chain_key = coalesce(tenant_id, '{NIL}'::uuid)),
 CHECK (hash = encode(sha256(convert_to(canonical_content || prev_hash, 'UTF8')), 'hex'))
);
CREATE TABLE lv_identity.audit_chain_head (chain_key uuid PRIMARY KEY, last_seq bigint NOT NULL, head char(64) NOT NULL);

-- Immutable history; constrained session updates.
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv_identity.audit_event FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv_identity.role_permission FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE FUNCTION lv_identity.guard_session_update() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$ BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'sessions are retained, not deleted' USING ERRCODE = '23514'; END IF;
 IF (NEW.id, NEW.token_hash, NEW.user_id, NEW.tenant_id, NEW.created_at, NEW.idle_timeout, NEW.absolute_expires_at, NEW.source_key)
    IS DISTINCT FROM (OLD.id, OLD.token_hash, OLD.user_id, OLD.tenant_id, OLD.created_at, OLD.idle_timeout, OLD.absolute_expires_at, OLD.source_key)
    OR NEW.last_seen_at < OLD.last_seen_at
    OR (OLD.revoked_at IS NOT NULL AND (NEW.revoked_at, NEW.revoke_reason, NEW.last_seen_at) IS DISTINCT FROM (OLD.revoked_at, OLD.revoke_reason, OLD.last_seen_at))
    OR (OLD.replaced_by IS NOT NULL AND NEW.replaced_by IS DISTINCT FROM OLD.replaced_by) THEN
  RAISE EXCEPTION 'session identity is fixed; only activity and a single revocation may be recorded' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guarded BEFORE UPDATE OR DELETE ON lv_identity.session FOR EACH ROW EXECUTE FUNCTION lv_identity.guard_session_update();

-- Internal: append one security event to its chain (no grant to any login role).
CREATE FUNCTION lv_identity.audit_append(p_event text, p_outcome text, p_user uuid, p_tenant uuid, p_session uuid,
                                         p_source text, p_request text, p_detail jsonb) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE chain uuid := coalesce(p_tenant, '{NIL}'::uuid); h lv_identity.audit_chain_head; content text; at timestamptz := clock_timestamp();
BEGIN
 INSERT INTO lv_identity.audit_chain_head VALUES (chain, 0, repeat('0', 64)) ON CONFLICT DO NOTHING;
 SELECT * INTO h FROM lv_identity.audit_chain_head WHERE chain_key = chain FOR UPDATE;
 content := lv.canonical_seed_json(jsonb_build_object('chain_key', chain::text, 'seq', (h.last_seq + 1)::text,
   'occurred_at', to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'event_type', p_event, 'outcome', p_outcome,
   'user_id', p_user::text, 'tenant_id', p_tenant::text, 'session_id', p_session::text, 'source_key', p_source,
   'request_id', p_request, 'detail', p_detail));
 INSERT INTO lv_identity.audit_event VALUES (chain, h.last_seq + 1, at, p_event, p_outcome, p_user, p_tenant, p_session, p_source,
   p_request, p_detail, content, h.head, encode(sha256(convert_to(content || h.head, 'UTF8')), 'hex'));
 UPDATE lv_identity.audit_chain_head SET last_seq = h.last_seq + 1, head = encode(sha256(convert_to(content || h.head, 'UTF8')), 'hex')
  WHERE chain_key = chain;
END $$;

CREATE FUNCTION lv_identity.check_request(p_source text, p_request text) RETURNS void
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$ BEGIN
 IF p_source IS NULL OR length(p_source) NOT BETWEEN 1 AND 64 OR p_request IS NULL OR p_request !~ '^[A-Za-z0-9._-]{{1,64}}$' THEN
  RAISE EXCEPTION 'invalid request attribution' USING ERRCODE = '22023';
 END IF;
END $$;

-- Internal: the effective view of one session (roles and permissions are read live, never cached in the session).
CREATE FUNCTION lv_identity.session_info(p_session uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object(
  'session_id', s.id::text,
  'user', jsonb_build_object('id', u.id::text, 'email', u.email, 'display_name', u.display_name),
  'tenant_id', s.tenant_id::text,
  'roles', coalesce((SELECT jsonb_agg(r.role::text ORDER BY r.role) FROM lv_identity.membership_role r
                     WHERE r.tenant_id = s.tenant_id AND r.user_id = s.user_id), '[]'::jsonb),
  'permissions', coalesce((SELECT jsonb_agg(DISTINCT p.permission::text) FROM lv_identity.membership_role r
                           JOIN lv_identity.role_permission p ON p.role = r.role
                           WHERE r.tenant_id = s.tenant_id AND r.user_id = s.user_id), '[]'::jsonb),
  'available_tenant_ids', coalesce((SELECT jsonb_agg(m.tenant_id::text ORDER BY m.tenant_id) FROM lv_identity.membership m
                                    WHERE m.user_id = s.user_id AND m.status = 'ACTIVE'), '[]'::jsonb),
  'tenant_selection_required', s.tenant_id IS NULL,
  'idle_expires_at', to_char((s.last_seen_at + s.idle_timeout) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'absolute_expires_at', to_char(s.absolute_expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
 FROM lv_identity.session s JOIN lv_identity.app_user u ON u.id = s.user_id WHERE s.id = p_session
$$;

-- Internal: why a session is not usable now (NULL = usable). Read-only.
CREATE FUNCTION lv_identity.session_problem(s lv_identity.session) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT CASE
  WHEN s.id IS NULL THEN 'UNKNOWN'
  WHEN s.revoked_at IS NOT NULL THEN 'REVOKED'
  WHEN statement_timestamp() >= s.absolute_expires_at THEN 'ABSOLUTE_EXPIRED'
  WHEN statement_timestamp() >= s.last_seen_at + s.idle_timeout THEN 'IDLE_EXPIRED'
  WHEN NOT EXISTS (SELECT 1 FROM lv_identity.app_user u WHERE u.id = s.user_id AND u.disabled_at IS NULL AND u.email_verified_at IS NOT NULL) THEN 'USER_UNAVAILABLE'
  WHEN s.tenant_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM lv_identity.membership m WHERE m.tenant_id = s.tenant_id
       AND m.user_id = s.user_id AND m.status = 'ACTIVE') THEN 'MEMBERSHIP_INACTIVE'
  WHEN NOT EXISTS (SELECT 1 FROM lv_identity.membership m WHERE m.user_id = s.user_id AND m.status = 'ACTIVE') THEN 'NO_ACTIVE_MEMBERSHIP'
  END
$$;

-- 1/5 Runtime: start a login attempt. Applies throttling BEFORE any password check and returns the stored Argon2id hash
-- (or NULL for an unknown account; the API then verifies against a dummy hash so timing does not reveal existence).
CREATE FUNCTION lv_identity.login_prepare(p_email text, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE normalized text; ek bytea; u lv_identity.app_user; account_failures bigint; source_attempts bigint;
 last_success timestamptz; window_start timestamptz := statement_timestamp() - interval '15 minutes'; attempt uuid; retry integer;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_email IS NULL OR length(p_email) > 320 THEN RAISE EXCEPTION 'invalid login request' USING ERRCODE = '22023'; END IF;
 normalized := lower(btrim(p_email));
 ek := sha256(convert_to(normalized, 'UTF8'));
 -- Serialize attempts per account key and per source so parallel guesses cannot all pass the limit check.
 PERFORM pg_advisory_xact_lock(hashtextextended('lv-login-account:' || encode(ek, 'hex'), 0));
 PERFORM pg_advisory_xact_lock(hashtextextended('lv-login-source:' || p_source, 0));
 SELECT * INTO u FROM lv_identity.app_user WHERE email = normalized;
 SELECT max(started_at) INTO last_success FROM lv_identity.login_attempt WHERE email_key = ek AND outcome = 'SUCCEEDED';
 SELECT count(*) INTO account_failures FROM lv_identity.login_attempt
  WHERE email_key = ek AND started_at > greatest(window_start, coalesce(last_success, '-infinity')) AND (outcome = 'FAILED' OR outcome IS NULL);
 SELECT count(*) INTO source_attempts FROM lv_identity.login_attempt
  WHERE source_key = p_source AND started_at > window_start AND outcome IS DISTINCT FROM 'THROTTLED';
 IF account_failures >= 5 OR source_attempts >= 30 THEN
  INSERT INTO lv_identity.login_attempt(email_key, source_key, user_id, started_at, finished_at, outcome)
  VALUES (ek, p_source, u.id, statement_timestamp(), statement_timestamp(), 'THROTTLED');
  SELECT greatest(1, ceil(extract(epoch FROM (min(started_at) + interval '15 minutes' - statement_timestamp()))))::integer INTO retry
    FROM lv_identity.login_attempt
   WHERE started_at > window_start AND outcome IS DISTINCT FROM 'THROTTLED'
     AND ((account_failures >= 5 AND email_key = ek AND (outcome = 'FAILED' OR outcome IS NULL)) OR (account_failures < 5 AND source_key = p_source));
  PERFORM lv_identity.audit_append('LOGIN_THROTTLED', 'DENIED', u.id, NULL, NULL, p_source, p_request,
    jsonb_build_object('email_key', encode(ek, 'hex'), 'limit', CASE WHEN account_failures >= 5 THEN 'ACCOUNT' ELSE 'SOURCE' END));
  RETURN jsonb_build_object('throttled', true, 'retry_after_seconds', coalesce(retry, 900));
 END IF;
 INSERT INTO lv_identity.login_attempt(email_key, source_key, user_id, started_at)
 VALUES (ek, p_source, u.id, statement_timestamp()) RETURNING id INTO attempt;
 RETURN jsonb_build_object('throttled', false, 'attempt_id', attempt::text, 'password_hash', u.password_hash);
END $$;

-- 2/5 Runtime: finish a login attempt. Only an ACTIVE, verified user with at least one ACTIVE membership gets a session;
-- every refusal looks identical to the client and is audited with its internal reason. The session lifetimes are fixed here.
CREATE FUNCTION lv_identity.login_finish(p_attempt uuid, p_password_ok boolean, p_token_hash bytea, p_previous_token_hash bytea,
                                         p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE a lv_identity.login_attempt; u lv_identity.app_user; reason text; tenants uuid[]; sid uuid; prev lv_identity.session;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 OR p_password_ok IS NULL THEN
  RAISE EXCEPTION 'invalid login completion' USING ERRCODE = '22023';
 END IF;
 SELECT * INTO a FROM lv_identity.login_attempt WHERE id = p_attempt FOR UPDATE;
 IF NOT FOUND OR a.outcome IS NOT NULL OR a.source_key <> p_source OR a.started_at < statement_timestamp() - interval '2 minutes' THEN
  RAISE EXCEPTION 'login attempt is not pending' USING ERRCODE = '55000';
 END IF;
 SELECT * INTO u FROM lv_identity.app_user WHERE id = a.user_id;
 SELECT array_agg(tenant_id ORDER BY tenant_id) INTO tenants FROM lv_identity.membership WHERE user_id = u.id AND status = 'ACTIVE';
 reason := CASE WHEN u.id IS NULL THEN 'UNKNOWN_ACCOUNT' WHEN NOT p_password_ok THEN 'BAD_PASSWORD'
                WHEN u.disabled_at IS NOT NULL THEN 'USER_DISABLED' WHEN u.email_verified_at IS NULL THEN 'EMAIL_UNVERIFIED'
                WHEN tenants IS NULL THEN 'NO_ACTIVE_MEMBERSHIP' END;
 IF reason IS NOT NULL THEN
  UPDATE lv_identity.login_attempt SET outcome = 'FAILED', finished_at = statement_timestamp() WHERE id = a.id;
  PERFORM lv_identity.audit_append('LOGIN_FAILED', 'DENIED', u.id, NULL, NULL, p_source, p_request,
    jsonb_build_object('reason', reason, 'email_key', encode(a.email_key, 'hex')));
  RETURN jsonb_build_object('authenticated', false);
 END IF;
 -- Rotation: whatever session the browser presented is ended; the new session gets a fresh token.
 IF p_previous_token_hash IS NOT NULL THEN
  SELECT * INTO prev FROM lv_identity.session WHERE token_hash = p_previous_token_hash AND revoked_at IS NULL FOR UPDATE;
  IF FOUND THEN
   UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'ROTATED_LOGIN' WHERE id = prev.id;
  END IF;
 END IF;
 INSERT INTO lv_identity.session(token_hash, user_id, tenant_id, created_at, last_seen_at, idle_timeout, absolute_expires_at, source_key)
 VALUES (p_token_hash, u.id, CASE WHEN cardinality(tenants) = 1 THEN tenants[1] END, statement_timestamp(), statement_timestamp(),
         interval '30 minutes', statement_timestamp() + interval '12 hours', p_source) RETURNING id INTO sid;
 IF prev.id IS NOT NULL THEN UPDATE lv_identity.session SET replaced_by = sid WHERE id = prev.id; END IF;
 UPDATE lv_identity.login_attempt SET outcome = 'SUCCEEDED', finished_at = statement_timestamp() WHERE id = a.id;
 PERFORM lv_identity.audit_append('LOGIN_SUCCEEDED', 'ALLOWED', u.id, CASE WHEN cardinality(tenants) = 1 THEN tenants[1] END, sid,
   p_source, p_request, jsonb_build_object('rotated_previous_session', prev.id::text));
 RETURN jsonb_build_object('authenticated', true, 'session', lv_identity.session_info(sid));
END $$;

-- 3/5 Runtime: resolve the presented session token INSIDE the caller's transaction and bind that transaction's tenant and
-- user from the session row. The caller supplies only the token hash; it never supplies a tenant or user id.
CREATE FUNCTION lv_identity.session_context(p_token_hash bytea, p_touch boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; problem text;
BEGIN
 PERFORM set_config('app.tenant_id', '', true);
 PERFORM set_config('app.user_id', '', true);
 PERFORM set_config('app.session_id', '', true);
 IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN RETURN jsonb_build_object('valid', false, 'reason', 'MALFORMED'); END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash = p_token_hash;
 problem := lv_identity.session_problem(s);
 IF problem IS NOT NULL THEN RETURN jsonb_build_object('valid', false, 'reason', problem); END IF;
 IF p_touch THEN
  UPDATE lv_identity.session SET last_seen_at = greatest(last_seen_at, statement_timestamp()) WHERE id = s.id;
 END IF;
 PERFORM set_config('app.tenant_id', coalesce(s.tenant_id::text, ''), true);
 PERFORM set_config('app.user_id', s.user_id::text, true);
 PERFORM set_config('app.session_id', s.id::text, true);
 RETURN jsonb_build_object('valid', true, 'session', lv_identity.session_info(s.id));
END $$;

-- 4/5 Runtime: logout revokes the presented session.
CREATE FUNCTION lv_identity.logout(p_token_hash bytea, p_source text, p_request text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 SELECT * INTO s FROM lv_identity.session WHERE token_hash = p_token_hash AND revoked_at IS NULL FOR UPDATE;
 IF NOT FOUND THEN RETURN false; END IF;
 UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'LOGOUT' WHERE id = s.id;
 PERFORM lv_identity.audit_append('LOGOUT', 'ALLOWED', s.user_id, s.tenant_id, s.id, p_source, p_request, '{{}}'::jsonb);
 RETURN true;
END $$;

-- 5/5 Runtime: tenant selection. The requested tenant id is only a request: an ACTIVE membership is required. Success
-- creates a NEW session (new token, same absolute expiry so switching cannot extend a session) and revokes the old one.
CREATE FUNCTION lv_identity.switch_tenant(p_token_hash bytea, p_tenant uuid, p_new_token_hash bytea, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; problem text; sid uuid;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_new_token_hash IS NULL OR octet_length(p_new_token_hash) <> 32 OR p_tenant IS NULL THEN
  RAISE EXCEPTION 'invalid tenant selection' USING ERRCODE = '22023';
 END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash = p_token_hash FOR UPDATE;
 problem := lv_identity.session_problem(s);
 -- A session whose current tenant membership was revoked may still select another tenant it is a member of.
 IF problem IS NOT NULL AND problem <> 'MEMBERSHIP_INACTIVE' THEN RETURN jsonb_build_object('valid', false, 'reason', problem); END IF;
 IF NOT EXISTS (SELECT 1 FROM lv_identity.membership WHERE tenant_id = p_tenant AND user_id = s.user_id AND status = 'ACTIVE') THEN
  PERFORM lv_identity.audit_append('TENANT_SELECTION_DENIED', 'DENIED', s.user_id, NULL, s.id, p_source, p_request,
    jsonb_build_object('requested_tenant_id', p_tenant::text));
  RETURN jsonb_build_object('valid', true, 'selected', false);
 END IF;
 UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'TENANT_SWITCH' WHERE id = s.id;
 INSERT INTO lv_identity.session(token_hash, user_id, tenant_id, created_at, last_seen_at, idle_timeout, absolute_expires_at, source_key)
 VALUES (p_new_token_hash, s.user_id, p_tenant, statement_timestamp(), statement_timestamp(), interval '30 minutes', s.absolute_expires_at, p_source)
 RETURNING id INTO sid;
 UPDATE lv_identity.session SET replaced_by = sid WHERE id = s.id;
 PERFORM lv_identity.audit_append('TENANT_SELECTED', 'ALLOWED', s.user_id, p_tenant, sid, p_source, p_request,
   jsonb_build_object('previous_session_id', s.id::text, 'previous_tenant_id', s.tenant_id::text));
 RETURN jsonb_build_object('valid', true, 'selected', true, 'session', lv_identity.session_info(sid));
END $$;

-- Owner-only administration, used by the local provisioning command (no runtime grant, no HTTP route). Each change is audited
-- and ends the affected sessions (rotation on privilege change, ADR-027).
CREATE FUNCTION lv_identity.admin_create_user(p_email text, p_display_name text, p_password_hash text, p_mark_verified boolean) RETURNS uuid
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE uid uuid;
BEGIN
 INSERT INTO lv_identity.app_user(email, display_name, password_hash, email_verified_at)
 VALUES (lower(btrim(p_email)), p_display_name, p_password_hash, CASE WHEN p_mark_verified THEN statement_timestamp() END) RETURNING id INTO uid;
 PERFORM lv_identity.audit_append('USER_PROVISIONED', 'ALLOWED', uid, NULL, NULL, 'local-provisioning', 'local-provisioning',
   jsonb_build_object('email_verification', CASE WHEN p_mark_verified THEN 'MARKED_BY_LOCAL_PROVISIONING' ELSE 'PENDING' END));
 RETURN uid;
END $$;

CREATE FUNCTION lv_identity.admin_grant_membership(p_tenant uuid, p_user uuid, p_roles lv_identity.member_role[]) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 INSERT INTO lv_identity.membership(tenant_id, user_id, status) VALUES (p_tenant, p_user, 'ACTIVE');
 INSERT INTO lv_identity.membership_role(tenant_id, user_id, role, grant_source) SELECT p_tenant, p_user, r, 'LOCAL_PROVISIONING' FROM unnest(p_roles) r;
 PERFORM lv_identity.audit_append('MEMBERSHIP_GRANTED', 'ALLOWED', p_user, p_tenant, NULL, 'local-provisioning', 'local-provisioning',
   jsonb_build_object('roles', to_jsonb(p_roles::text[])));
END $$;

CREATE FUNCTION lv_identity.admin_set_roles(p_tenant uuid, p_user uuid, p_roles lv_identity.member_role[]) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 DELETE FROM lv_identity.membership_role WHERE tenant_id = p_tenant AND user_id = p_user AND NOT role = ANY(p_roles);
 INSERT INTO lv_identity.membership_role(tenant_id, user_id, role, grant_source)
 SELECT p_tenant, p_user, r, 'LOCAL_PROVISIONING' FROM unnest(p_roles) r ON CONFLICT DO NOTHING;
 UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'ROLES_CHANGED'
  WHERE user_id = p_user AND revoked_at IS NULL;
 PERFORM lv_identity.audit_append('ROLES_CHANGED', 'ALLOWED', p_user, p_tenant, NULL, 'local-provisioning', 'local-provisioning',
   jsonb_build_object('roles', to_jsonb(p_roles::text[])));
END $$;

CREATE FUNCTION lv_identity.admin_revoke_membership(p_tenant uuid, p_user uuid) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 UPDATE lv_identity.membership SET status = 'REVOKED', revoked_at = statement_timestamp()
  WHERE tenant_id = p_tenant AND user_id = p_user AND status = 'ACTIVE';
 UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'MEMBERSHIP_REVOKED'
  WHERE user_id = p_user AND tenant_id = p_tenant AND revoked_at IS NULL;
 PERFORM lv_identity.audit_append('MEMBERSHIP_REVOKED', 'ALLOWED', p_user, p_tenant, NULL, 'local-provisioning', 'local-provisioning', '{{}}'::jsonb);
END $$;

CREATE FUNCTION lv_identity.admin_disable_user(p_user uuid) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 UPDATE lv_identity.app_user SET disabled_at = statement_timestamp() WHERE id = p_user AND disabled_at IS NULL;
 UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'USER_DISABLED' WHERE user_id = p_user AND revoked_at IS NULL;
 PERFORM lv_identity.audit_append('USER_DISABLED', 'ALLOWED', p_user, NULL, NULL, 'local-provisioning', 'local-provisioning', '{{}}'::jsonb);
END $$;

CREATE FUNCTION lv_identity.admin_revoke_sessions(p_user uuid) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE n integer;
BEGIN
 UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'ADMIN_REVOKED' WHERE user_id = p_user AND revoked_at IS NULL;
 GET DIAGNOSTICS n = ROW_COUNT;
 PERFORM lv_identity.audit_append('SESSIONS_REVOKED', 'ALLOWED', p_user, NULL, NULL, 'local-provisioning', 'local-provisioning',
   jsonb_build_object('count', n::text));
 RETURN n;
END $$;
"""

RUNTIME_FUNCTIONS = ('login_prepare(text, text, text)', 'login_finish(uuid, boolean, bytea, bytea, text, text)',
                     'session_context(bytea, boolean)', 'logout(bytea, text, text)', 'switch_tenant(bytea, uuid, bytea, text, text)')
TABLES = ('role_permission', 'app_user', 'membership', 'membership_role', 'session', 'login_attempt', 'audit_event', 'audit_chain_head')


def upgrade():
    op.get_bind().exec_driver_sql(UPGRADE)
    for name in TABLES:
        # ENABLE without FORCE and without policies: the owner-run functions can read; every other role sees no rows.
        op.execute(f'ALTER TABLE lv_identity.{name} ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv_identity FROM PUBLIC')
    op.execute('REVOKE ALL ON ALL TABLES IN SCHEMA lv_identity FROM PUBLIC')
    op.execute('GRANT USAGE ON SCHEMA lv_identity TO lv_app')
    for signature in RUNTIME_FUNCTIONS:
        op.execute(f'GRANT EXECUTE ON FUNCTION lv_identity.{signature} TO lv_app')


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM lv_identity.app_user) THEN
        RAISE EXCEPTION 'identity records exist; downgrade is for disposable databases without users' USING ERRCODE = '55000';
      END IF; END $$""")
    op.execute('DROP SCHEMA lv_identity CASCADE')
