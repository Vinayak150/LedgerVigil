-- 0036 identity lifecycle (ADR-027, Stage 2B §5.2 and Family 1): email verification, single-use password reset that revokes
-- sessions, invitations, member and role administration with the accepted segregation rules, privileged-action
-- re-authentication, user and administrator session revocation, and the tenant audit-log read. Every table stays in
-- lv_identity with no runtime privilege; lv_app reaches it only through the functions granted at the end.
-- Tokens: the API generates 32 random bytes, sends them only to the mail adapter, and passes only the SHA-256 here.

ALTER TABLE lv_identity.membership_role DROP CONSTRAINT membership_role_grant_source_check;
ALTER TABLE lv_identity.membership_role ADD CONSTRAINT membership_role_grant_source_check CHECK (grant_source IN ('LOCAL_PROVISIONING','MEMBER_ADMINISTRATION'));
ALTER TABLE lv_identity.session DROP CONSTRAINT session_revoke_reason_check;
ALTER TABLE lv_identity.session ADD CONSTRAINT session_revoke_reason_check CHECK (revoke_reason IN ('LOGOUT','ROTATED_LOGIN','TENANT_SWITCH','ADMIN_REVOKED',
 'USER_DISABLED','MEMBERSHIP_REVOKED','ROLES_CHANGED','PASSWORD_RESET','USER_REVOKED_ALL'));

CREATE TABLE lv_identity.email_token(
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 purpose varchar(16) NOT NULL CHECK (purpose IN ('VERIFY_EMAIL','RESET_PASSWORD','INVITATION')),
 user_id uuid NOT NULL REFERENCES lv_identity.app_user(id),
 tenant_id uuid REFERENCES lv.tenant(tenant_id),
 token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash)=32),
 created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
 expires_at timestamptz NOT NULL,
 consumed_at timestamptz,
 outcome varchar(12) CHECK (outcome IN ('USED','SUPERSEDED','EXPIRED','REVOKED')),
 requested_by uuid REFERENCES lv_identity.app_user(id),
 source_key varchar(64) NOT NULL,
 CHECK ((consumed_at IS NULL)=(outcome IS NULL)),
 CHECK (expires_at > created_at AND expires_at <= created_at + interval '7 days'),
 CHECK ((purpose='INVITATION')=(tenant_id IS NOT NULL))
);
CREATE INDEX email_token_open ON lv_identity.email_token(user_id,purpose) WHERE consumed_at IS NULL;
CREATE TABLE lv_identity.token_request(
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 purpose varchar(16) NOT NULL CHECK (purpose IN ('VERIFY_EMAIL','RESET_PASSWORD','CONSUME','REAUTHENTICATE')),
 email_key bytea CHECK (email_key IS NULL OR octet_length(email_key)=32),
 source_key varchar(64) NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT statement_timestamp(),
 outcome varchar(12) NOT NULL CHECK (outcome IN ('ISSUED','NOT_ISSUED','THROTTLED','CONSUMED','REFUSED','ACCEPTED'))
);
CREATE INDEX token_request_email ON lv_identity.token_request(purpose,email_key,requested_at);
CREATE INDEX token_request_source ON lv_identity.token_request(purpose,source_key,requested_at);
-- Re-authentication for privileged actions: the session (or the login it was rotated from) proved the password recently.
CREATE TABLE lv_identity.reauthentication(
 session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 at timestamptz NOT NULL DEFAULT statement_timestamp(),
 PRIMARY KEY (session_id, at)
);
ALTER TABLE lv_identity.email_token ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv_identity.token_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv_identity.reauthentication ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON lv_identity.email_token, lv_identity.token_request, lv_identity.reauthentication FROM PUBLIC;

CREATE FUNCTION lv_identity.guard_email_token() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'tokens are retained' USING ERRCODE='23514'; END IF;
 IF OLD.consumed_at IS NOT NULL OR (NEW.id,NEW.purpose,NEW.user_id,NEW.tenant_id,NEW.token_hash,NEW.created_at,NEW.expires_at,NEW.requested_by,NEW.source_key)
    IS DISTINCT FROM (OLD.id,OLD.purpose,OLD.user_id,OLD.tenant_id,OLD.token_hash,OLD.created_at,OLD.expires_at,OLD.requested_by,OLD.source_key) THEN
  RAISE EXCEPTION 'a token is consumed at most once and is otherwise fixed' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guarded BEFORE UPDATE OR DELETE ON lv_identity.email_token FOR EACH ROW EXECUTE FUNCTION lv_identity.guard_email_token();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv_identity.token_request FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv_identity.reauthentication FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();

-- Rate limit shared by the public token endpoints: per account key and per source, per hour.
CREATE FUNCTION lv_identity.token_throttle(p_purpose text, ek bytea, p_source text, p_per_account integer, p_per_source integer) RETURNS integer
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE a bigint := 0; b bigint; since timestamptz := statement_timestamp()-interval '1 hour';
BEGIN
 IF ek IS NOT NULL THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('lv-token-account:'||p_purpose||':'||encode(ek,'hex'),0));
  SELECT count(*) INTO a FROM lv_identity.token_request WHERE purpose=p_purpose AND email_key=ek AND requested_at>since AND outcome<>'THROTTLED';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('lv-token-source:'||p_purpose||':'||p_source,0));
 SELECT count(*) INTO b FROM lv_identity.token_request WHERE purpose=p_purpose AND source_key=p_source AND requested_at>since AND outcome<>'THROTTLED';
 IF a>=p_per_account OR b>=p_per_source THEN
  INSERT INTO lv_identity.token_request(purpose,email_key,source_key,outcome) VALUES(p_purpose,ek,p_source,'THROTTLED');
  RETURN 3600;
 END IF;
 RETURN 0;
END $$;

-- Public: request a verification or reset message. The answer never says whether the account exists (the API always
-- replies 202 and delivers in the background); only throttling, counted per account key and source alike, is visible.
CREATE FUNCTION lv_identity.token_issue(p_purpose text, p_email text, p_token_hash bytea, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE normalized text; ek bytea; u lv_identity.app_user; retry integer; eligible boolean; ttl interval;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_purpose NOT IN ('VERIFY_EMAIL','RESET_PASSWORD') OR p_email IS NULL OR length(p_email)>320 OR p_token_hash IS NULL OR octet_length(p_token_hash)<>32 THEN
  RAISE EXCEPTION 'invalid token request' USING ERRCODE='22023'; END IF;
 normalized := lower(btrim(p_email)); ek := sha256(convert_to(normalized,'UTF8'));
 retry := lv_identity.token_throttle(p_purpose, ek, p_source, 3, 20);
 IF retry>0 THEN
  PERFORM lv_identity.audit_append('TOKEN_REQUEST_THROTTLED','DENIED',NULL,NULL,NULL,p_source,p_request,jsonb_build_object('purpose',p_purpose,'email_key',encode(ek,'hex')));
  RETURN jsonb_build_object('throttled',true,'retry_after_seconds',retry);
 END IF;
 SELECT * INTO u FROM lv_identity.app_user WHERE email=normalized FOR UPDATE;
 eligible := FOUND AND u.disabled_at IS NULL AND (p_purpose='RESET_PASSWORD' OR u.email_verified_at IS NULL);
 IF NOT eligible THEN
  INSERT INTO lv_identity.token_request(purpose,email_key,source_key,outcome) VALUES(p_purpose,ek,p_source,'NOT_ISSUED');
  PERFORM lv_identity.audit_append('TOKEN_NOT_ISSUED','DENIED',u.id,NULL,NULL,p_source,p_request,jsonb_build_object('purpose',p_purpose,'email_key',encode(ek,'hex')));
  RETURN jsonb_build_object('throttled',false,'deliver',false);
 END IF;
 ttl := CASE p_purpose WHEN 'VERIFY_EMAIL' THEN interval '24 hours' ELSE interval '30 minutes' END;
 UPDATE lv_identity.email_token SET consumed_at=statement_timestamp(),outcome='SUPERSEDED' WHERE user_id=u.id AND purpose=p_purpose AND consumed_at IS NULL;
 INSERT INTO lv_identity.email_token(purpose,user_id,token_hash,expires_at,source_key) VALUES(p_purpose,u.id,p_token_hash,statement_timestamp()+ttl,p_source);
 INSERT INTO lv_identity.token_request(purpose,email_key,source_key,outcome) VALUES(p_purpose,ek,p_source,'ISSUED');
 PERFORM lv_identity.audit_append('TOKEN_ISSUED','ALLOWED',u.id,NULL,NULL,p_source,p_request,jsonb_build_object('purpose',p_purpose));
 RETURN jsonb_build_object('throttled',false,'deliver',true,'email',u.email,'display_name',u.display_name,'purpose',p_purpose,
  'expires_in_seconds',extract(epoch FROM ttl)::integer::text);
END $$;

-- Public: use a token. Every refusal (unknown, wrong purpose, used, superseded, expired, disabled user) looks the same.
-- Single use is enforced by the row lock and the consumed_at guard, so concurrent uses produce exactly one success.
CREATE FUNCTION lv_identity.token_consume(p_purpose text, p_token_hash bytea, p_password_hash text, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE k lv_identity.email_token; u lv_identity.app_user; retry integer; n integer := 0;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_purpose NOT IN ('VERIFY_EMAIL','RESET_PASSWORD','INVITATION') OR p_token_hash IS NULL OR octet_length(p_token_hash)<>32 THEN
  RAISE EXCEPTION 'invalid token use' USING ERRCODE='22023'; END IF;
 IF p_purpose<>'VERIFY_EMAIL' AND (p_password_hash IS NULL OR left(p_password_hash,10)<>'$argon2id$' OR length(p_password_hash)>512) THEN
  RAISE EXCEPTION 'a new password hash is required' USING ERRCODE='22023'; END IF;
 -- Guessing is bounded per source: refused uses count against the source.
 PERFORM pg_advisory_xact_lock(hashtextextended('lv-token-source:CONSUME:'||p_source,0));
 IF (SELECT count(*) FROM lv_identity.token_request WHERE purpose='CONSUME' AND source_key=p_source AND requested_at>statement_timestamp()-interval '1 hour' AND outcome='REFUSED')>=20 THEN
  INSERT INTO lv_identity.token_request(purpose,source_key,outcome) VALUES('CONSUME',p_source,'THROTTLED');
  RETURN jsonb_build_object('ok',false,'throttled',true,'retry_after_seconds',3600);
 END IF;
 SELECT * INTO k FROM lv_identity.email_token WHERE token_hash=p_token_hash FOR UPDATE;
 IF FOUND THEN SELECT * INTO u FROM lv_identity.app_user WHERE id=k.user_id FOR UPDATE; END IF;
 IF NOT FOUND OR k.purpose<>p_purpose OR k.consumed_at IS NOT NULL OR k.expires_at<=statement_timestamp() OR u.disabled_at IS NOT NULL
    OR (k.purpose='INVITATION' AND NOT EXISTS(SELECT 1 FROM lv_identity.membership m WHERE m.tenant_id=k.tenant_id AND m.user_id=k.user_id AND m.status='ACTIVE')) THEN
  IF k.id IS NOT NULL AND k.consumed_at IS NULL AND k.expires_at<=statement_timestamp() THEN
   UPDATE lv_identity.email_token SET consumed_at=statement_timestamp(),outcome='EXPIRED' WHERE id=k.id;
  END IF;
  INSERT INTO lv_identity.token_request(purpose,source_key,outcome) VALUES('CONSUME',p_source,'REFUSED');
  PERFORM lv_identity.audit_append('TOKEN_REFUSED','DENIED',k.user_id,NULL,NULL,p_source,p_request,jsonb_build_object('purpose',p_purpose));
  RETURN jsonb_build_object('ok',false,'throttled',false);
 END IF;
 UPDATE lv_identity.email_token SET consumed_at=statement_timestamp(),outcome='USED' WHERE id=k.id;
 IF k.purpose='VERIFY_EMAIL' THEN
  UPDATE lv_identity.app_user SET email_verified_at=coalesce(email_verified_at,statement_timestamp()) WHERE id=u.id;
 ELSE
  -- Reset and invitation set a new password; proving control of the address also verifies it. Every session ends.
  UPDATE lv_identity.app_user SET password_hash=p_password_hash,email_verified_at=coalesce(email_verified_at,statement_timestamp()) WHERE id=u.id;
  UPDATE lv_identity.session SET revoked_at=statement_timestamp(),revoke_reason='PASSWORD_RESET' WHERE user_id=u.id AND revoked_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  UPDATE lv_identity.email_token SET consumed_at=statement_timestamp(),outcome='SUPERSEDED' WHERE user_id=u.id AND purpose IN ('RESET_PASSWORD','INVITATION') AND consumed_at IS NULL;
 END IF;
 INSERT INTO lv_identity.token_request(purpose,source_key,outcome) VALUES('CONSUME',p_source,'CONSUMED');
 PERFORM lv_identity.audit_append(CASE k.purpose WHEN 'VERIFY_EMAIL' THEN 'EMAIL_VERIFIED' WHEN 'RESET_PASSWORD' THEN 'PASSWORD_RESET' ELSE 'INVITATION_ACCEPTED' END,
  'ALLOWED',u.id,k.tenant_id,NULL,p_source,p_request,jsonb_build_object('sessions_revoked',n::text));
 RETURN jsonb_build_object('ok',true,'purpose',k.purpose,'sessions_revoked',n);
END $$;

-- Internal: the presented session, valid and tenant-bound (42501 otherwise).
CREATE FUNCTION lv_identity.member_session(p_token_hash bytea, p_source text, p_request text) RETURNS lv_identity.session
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN RETURN lv.proposal_session(p_token_hash, p_source, p_request); END $$;

-- Internal: when the session (or the session it was rotated from by a tenant switch) last proved the password.
CREATE FUNCTION lv_identity.session_authenticated_at(p_session uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 WITH RECURSIVE chain(id, depth) AS (
  SELECT p_session, 0
  UNION ALL SELECT s.id, c.depth+1 FROM lv_identity.session s JOIN chain c ON s.replaced_by=c.id AND s.revoke_reason='TENANT_SWITCH' WHERE c.depth<20)
 SELECT max(t) FROM (
  SELECT r.at AS t FROM lv_identity.reauthentication r WHERE r.session_id IN (SELECT id FROM chain)
  UNION ALL SELECT s.created_at FROM lv_identity.session s WHERE s.id=(SELECT id FROM chain ORDER BY depth DESC LIMIT 1)) x
$$;

CREATE FUNCTION lv_identity.member_json(p_tenant uuid, p_user uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('user_id',u.id::text,'email',u.email,'display_name',u.display_name,'status',m.status,
  'email_verified',u.email_verified_at IS NOT NULL,'disabled',u.disabled_at IS NOT NULL,
  'roles',coalesce((SELECT jsonb_agg(jsonb_build_object('role',r.role::text,'granted_at',to_char(r.granted_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
     'granted_by',r.granted_by::text,'cooling_off_until',CASE WHEN r.role IN ('CONTROLLER','ADMINISTRATOR') THEN to_char((r.granted_at+interval '24 hours') AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"') END)
     ORDER BY r.role) FROM lv_identity.membership_role r WHERE r.tenant_id=m.tenant_id AND r.user_id=m.user_id),'[]'::jsonb),
  'member_since',to_char(m.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'invitation_pending',EXISTS(SELECT 1 FROM lv_identity.email_token k WHERE k.user_id=u.id AND k.tenant_id=m.tenant_id AND k.purpose='INVITATION' AND k.consumed_at IS NULL AND k.expires_at>statement_timestamp()),
  'etag',encode(sha256(convert_to(m.status||':'||coalesce((SELECT string_agg(r.role::text||'@'||r.granted_at::text,',' ORDER BY r.role) FROM lv_identity.membership_role r
     WHERE r.tenant_id=m.tenant_id AND r.user_id=m.user_id),''),'UTF8')),'hex'))
 FROM lv_identity.membership m JOIN lv_identity.app_user u ON u.id=m.user_id WHERE m.tenant_id=p_tenant AND m.user_id=p_user
$$;

-- GET /members (2B Family 1: permission read). Active members first, then removed ones; bounded.
CREATE FUNCTION lv_identity.member_list(p_token_hash bytea, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;
BEGIN
 s := lv_identity.member_session(p_token_hash, p_source, p_request);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'read') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires read'); END IF;
 RETURN jsonb_build_object('status',200,'body',jsonb_build_object('items',coalesce((SELECT jsonb_agg(lv_identity.member_json(m.tenant_id,m.user_id) ORDER BY m.status, m.created_at, m.user_id)
  FROM (SELECT * FROM lv_identity.membership WHERE tenant_id=s.tenant_id ORDER BY status, created_at, user_id LIMIT 500) m),'[]'::jsonb)));
END $$;

-- POST/PATCH/DELETE /members and member session revocation (permission administer; fresh authentication within 15 minutes).
-- Segregation (2B §5.1, 2A §12.2): no self-grant of review, execute or ledger.bootstrap; new grants start the 24-hour
-- cooling-off that approval and execution already enforce; the last administrator cannot be removed or demoted.
CREATE FUNCTION lv_identity.member_api(p_token_hash bytea, p_action text, p_target uuid, p_args jsonb, p_if_match text, p_ikey text, p_rhash text,
  p_invite_token_hash bytea, p_unusable_password_hash text, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; prior lv.api_idempotency; route_name text; result jsonb; target uuid := p_target; u lv_identity.app_user; m lv_identity.membership;
 wanted lv_identity.member_role[]; current_roles lv_identity.member_role[]; added lv_identity.member_role[]; removed lv_identity.member_role[]; email text; n integer := 0;
 deliver text := NULL; invited_new boolean := false;
BEGIN
 route_name := CASE p_action WHEN 'INVITE' THEN '/api/v1/members' WHEN 'SET_ROLES' THEN '/api/v1/members/{user_id}' WHEN 'REMOVE' THEN '/api/v1/members/{user_id}#delete'
  WHEN 'REVOKE_SESSIONS' THEN '/api/v1/members/{user_id}/sessions/revoke' END;
 IF route_name IS NULL THEN RAISE EXCEPTION 'invalid member action' USING ERRCODE='22023'; END IF;
 s := lv_identity.member_session(p_token_hash, p_source, p_request);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'administer') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires administer'); END IF;
 IF coalesce(lv_identity.session_authenticated_at(s.id),'-infinity')<statement_timestamp()-interval '15 minutes' THEN
  RETURN jsonb_build_object('status',403,'code','REAUTHENTICATION_REQUIRED','detail','confirm your password to change memberships (POST /api/v1/auth/reauthenticate)');
 END IF;
 IF p_ikey IS NULL OR p_ikey !~ '^[!-~]{1,64}$' OR p_rhash IS NULL OR p_rhash !~ '^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('lv-members:'||s.tenant_id::text,0));   -- one membership change at a time per tenant
 PERFORM lv_identity.session_context(p_token_hash, true);
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=p_ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
  IF prior.request_hash<>p_rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
  RETURN prior.response || jsonb_build_object('replayed',true);
 END IF;
 IF p_action IN ('INVITE','SET_ROLES') THEN
  BEGIN
   SELECT array_agg(DISTINCT x::lv_identity.member_role ORDER BY x::lv_identity.member_role) INTO wanted FROM jsonb_array_elements_text(p_args->'roles') x;
  EXCEPTION WHEN invalid_text_representation OR invalid_parameter_value OR wrong_object_type THEN wanted := NULL; END;
  IF wanted IS NULL OR cardinality(wanted)=0 THEN
   result := jsonb_build_object('status',422,'code','SCHEMA_INVALID','errors',jsonb_build_array(jsonb_build_object('pointer','/roles','code','roles')));
  END IF;
 END IF;
 IF result IS NULL AND p_action='INVITE' THEN
  email := lower(btrim(p_args->>'email'));
  IF email IS NULL OR length(email)>320 OR email !~ '^[^@[:space:]]{1,64}@[^@[:space:]]{1,255}$' THEN
   result := jsonb_build_object('status',422,'code','SCHEMA_INVALID','errors',jsonb_build_array(jsonb_build_object('pointer','/email','code','email')));
  ELSE
   SELECT * INTO u FROM lv_identity.app_user WHERE app_user.email=lower(btrim(p_args->>'email')) FOR UPDATE;
   IF NOT FOUND THEN
    IF p_unusable_password_hash IS NULL OR left(p_unusable_password_hash,10)<>'$argon2id$' OR p_invite_token_hash IS NULL OR octet_length(p_invite_token_hash)<>32 THEN
     RAISE EXCEPTION 'invitation material required' USING ERRCODE='22023'; END IF;
    INSERT INTO lv_identity.app_user(email,display_name,password_hash) VALUES(email,coalesce(nullif(btrim(p_args->>'display_name'),''),split_part(email,'@',1)),p_unusable_password_hash) RETURNING * INTO u;
    invited_new := true;
   END IF;
   target := u.id;
   SELECT * INTO m FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=u.id FOR UPDATE;
   IF FOUND AND m.status='ACTIVE' THEN
    result := jsonb_build_object('status',409,'code','MEMBER_EXISTS','detail','this person is already a member; change their roles instead');
   END IF;
  END IF;
 END IF;
 IF result IS NULL AND p_action<>'INVITE' THEN
  SELECT * INTO m FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=target FOR UPDATE;
  IF NOT FOUND OR m.status<>'ACTIVE' THEN result := jsonb_build_object('status',404,'code','NOT_FOUND');
  ELSIF p_action IN ('SET_ROLES','REMOVE') AND (p_if_match IS NULL OR p_if_match<>(lv_identity.member_json(s.tenant_id,target)->>'etag')) THEN
   result := CASE WHEN p_if_match IS NULL THEN jsonb_build_object('status',428,'code','PRECONDITION_REQUIRED')
     ELSE jsonb_build_object('status',412,'code','PRECONDITION_FAILED','detail','the membership changed; reload it') END;
  END IF;
 END IF;
 IF result IS NULL THEN
  SELECT coalesce(array_agg(role ORDER BY role),'{}') INTO current_roles FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=target;
  IF p_action='REMOVE' THEN wanted := '{}'; END IF;
  IF p_action IN ('INVITE','SET_ROLES','REMOVE') THEN
   SELECT coalesce(array_agg(r ORDER BY r),'{}') INTO added FROM unnest(wanted) r WHERE NOT r = ANY(current_roles);
   SELECT coalesce(array_agg(r ORDER BY r),'{}') INTO removed FROM unnest(current_roles) r WHERE NOT r = ANY(wanted);
   IF target=s.user_id AND EXISTS(SELECT 1 FROM unnest(added) r JOIN lv_identity.role_permission p ON p.role=r WHERE p.permission IN ('review','execute','ledger.bootstrap')) THEN
    result := jsonb_build_object('status',403,'code','SEGREGATION_OF_DUTIES','detail','you cannot grant yourself review, execute or ledger.bootstrap');
   ELSIF 'ADMINISTRATOR'=ANY(removed) AND NOT EXISTS(SELECT 1 FROM lv_identity.membership_role r JOIN lv_identity.membership x ON (x.tenant_id,x.user_id)=(r.tenant_id,r.user_id)
       JOIN lv_identity.app_user y ON y.id=r.user_id WHERE r.tenant_id=s.tenant_id AND r.role='ADMINISTRATOR' AND r.user_id<>target AND x.status='ACTIVE' AND y.disabled_at IS NULL) THEN
    result := jsonb_build_object('status',409,'code','LAST_ADMINISTRATOR','detail','an organization must keep at least one administrator');
   END IF;
  END IF;
 END IF;
 IF result IS NULL THEN
  IF p_action='INVITE' THEN
   IF m.user_id IS NULL THEN INSERT INTO lv_identity.membership(tenant_id,user_id,status) VALUES(s.tenant_id,target,'ACTIVE');
   ELSE UPDATE lv_identity.membership SET status='ACTIVE',revoked_at=NULL WHERE tenant_id=s.tenant_id AND user_id=target; END IF;
   INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,granted_by,grant_source) SELECT s.tenant_id,target,r,s.user_id,'MEMBER_ADMINISTRATION' FROM unnest(wanted) r;
   IF u.email_verified_at IS NULL OR invited_new THEN
    IF p_invite_token_hash IS NULL OR octet_length(p_invite_token_hash)<>32 THEN RAISE EXCEPTION 'invitation token required' USING ERRCODE='22023'; END IF;
    UPDATE lv_identity.email_token SET consumed_at=statement_timestamp(),outcome='SUPERSEDED' WHERE user_id=target AND purpose='INVITATION' AND consumed_at IS NULL;
    INSERT INTO lv_identity.email_token(purpose,user_id,tenant_id,token_hash,expires_at,requested_by,source_key) VALUES('INVITATION',target,s.tenant_id,p_invite_token_hash,statement_timestamp()+interval '7 days',s.user_id,p_source);
    deliver := 'INVITATION';
   ELSE deliver := 'ADDED'; END IF;
   PERFORM lv_identity.audit_append('MEMBER_INVITED','ALLOWED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('member',target::text,'roles',to_jsonb(wanted::text[]),'new_user',invited_new::text));
   result := jsonb_build_object('status',201,'body',lv_identity.member_json(s.tenant_id,target));
  ELSIF p_action='SET_ROLES' THEN
   DELETE FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=target AND role=ANY(removed);
   INSERT INTO lv_identity.membership_role(tenant_id,user_id,role,granted_by,grant_source) SELECT s.tenant_id,target,r,s.user_id,'MEMBER_ADMINISTRATION' FROM unnest(added) r;
   IF cardinality(added)+cardinality(removed)>0 THEN   -- privilege change: the member's sessions in this organization end (2B §5.2 rotation)
    UPDATE lv_identity.session SET revoked_at=statement_timestamp(),revoke_reason='ROLES_CHANGED' WHERE user_id=target AND tenant_id=s.tenant_id AND revoked_at IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
   END IF;
   PERFORM lv_identity.audit_append('ROLES_CHANGED','ALLOWED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('member',target::text,
     'added',to_jsonb(added::text[]),'removed',to_jsonb(removed::text[]),'sessions_revoked',n::text));
   result := jsonb_build_object('status',200,'body',lv_identity.member_json(s.tenant_id,target));
  ELSIF p_action='REMOVE' THEN
   DELETE FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=target;
   UPDATE lv_identity.membership SET status='REVOKED',revoked_at=statement_timestamp() WHERE tenant_id=s.tenant_id AND user_id=target;
   UPDATE lv_identity.session SET revoked_at=statement_timestamp(),revoke_reason='MEMBERSHIP_REVOKED' WHERE user_id=target AND tenant_id=s.tenant_id AND revoked_at IS NULL;
   GET DIAGNOSTICS n = ROW_COUNT;
   UPDATE lv_identity.email_token SET consumed_at=statement_timestamp(),outcome='REVOKED' WHERE user_id=target AND tenant_id=s.tenant_id AND consumed_at IS NULL;
   PERFORM lv_identity.audit_append('MEMBERSHIP_REVOKED','ALLOWED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('member',target::text,'roles',to_jsonb(current_roles::text[]),'sessions_revoked',n::text));
   result := jsonb_build_object('status',200,'body',lv_identity.member_json(s.tenant_id,target));
  ELSE   -- REVOKE_SESSIONS
   UPDATE lv_identity.session SET revoked_at=statement_timestamp(),revoke_reason='ADMIN_REVOKED' WHERE user_id=target AND tenant_id=s.tenant_id AND revoked_at IS NULL;
   GET DIAGNOSTICS n = ROW_COUNT;
   PERFORM lv_identity.audit_append('SESSIONS_REVOKED','ALLOWED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('member',target::text,'count',n::text));
   result := jsonb_build_object('status',200,'body',jsonb_build_object('user_id',target::text,'sessions_revoked',n));
  END IF;
 ELSE
  PERFORM lv_identity.audit_append('MEMBER_CHANGE_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('action',p_action,'code',result->>'code'));
 END IF;
 IF (result->>'status')::integer BETWEEN 200 AND 499 AND (result->>'status')::integer NOT IN (412,428) THEN
  DELETE FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=p_ikey;
  INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at)
  VALUES(s.tenant_id,s.user_id,'POST',route_name,p_ikey,p_rhash,(result->>'status')::smallint,result,transaction_timestamp()+interval '24 hours');
 END IF;
 RETURN result || jsonb_build_object('replayed',false,'deliver',deliver,'deliver_email',CASE WHEN deliver IS NOT NULL THEN (SELECT x.email FROM lv_identity.app_user x WHERE x.id=target) END);
END $$;

-- POST /auth/reauthenticate, step 1: throttled like sign-in (per account and source); returns the stored hash for the API
-- to verify (Q-16 applies: see docs/IDENTITY_LIFECYCLE.md).
CREATE FUNCTION lv_identity.reauth_prepare(p_token_hash bytea, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; u lv_identity.app_user; ek bytea; retry integer;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_token_hash;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('valid',false); END IF;
 SELECT * INTO u FROM lv_identity.app_user WHERE id=s.user_id;
 ek := sha256(convert_to(u.email,'UTF8'));
 retry := lv_identity.token_throttle('REAUTHENTICATE', ek, p_source, 5, 30);
 IF retry>0 THEN RETURN jsonb_build_object('valid',true,'throttled',true,'retry_after_seconds',900); END IF;
 RETURN jsonb_build_object('valid',true,'throttled',false,'password_hash',u.password_hash,'email_key',encode(ek,'hex'));
END $$;
CREATE FUNCTION lv_identity.reauth_finish(p_token_hash bytea, p_password_ok boolean, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; u lv_identity.app_user; ek bytea;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_token_hash FOR UPDATE;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('valid',false); END IF;
 SELECT * INTO u FROM lv_identity.app_user WHERE id=s.user_id;
 ek := sha256(convert_to(u.email,'UTF8'));
 INSERT INTO lv_identity.token_request(purpose,email_key,source_key,outcome) VALUES('REAUTHENTICATE',ek,p_source,CASE WHEN p_password_ok THEN 'ACCEPTED' ELSE 'REFUSED' END);
 PERFORM lv_identity.audit_append('REAUTHENTICATION',CASE WHEN p_password_ok THEN 'ALLOWED' ELSE 'DENIED' END,s.user_id,s.tenant_id,s.id,p_source,p_request,'{}'::jsonb);
 IF NOT coalesce(p_password_ok,false) THEN RETURN jsonb_build_object('valid',true,'accepted',false); END IF;
 INSERT INTO lv_identity.reauthentication(session_id) VALUES(s.id);
 RETURN jsonb_build_object('valid',true,'accepted',true,'fresh_until',to_char((statement_timestamp()+interval '15 minutes') AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
END $$;

-- POST /auth/sessions/revoke-all: the user ends every one of their sessions, including this one.
CREATE FUNCTION lv_identity.revoke_my_sessions(p_token_hash bytea, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; n integer;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_token_hash;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('valid',false); END IF;
 UPDATE lv_identity.session SET revoked_at=statement_timestamp(),revoke_reason='USER_REVOKED_ALL' WHERE user_id=s.user_id AND revoked_at IS NULL;
 GET DIAGNOSTICS n = ROW_COUNT;
 PERFORM lv_identity.audit_append('SESSIONS_REVOKED','ALLOWED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('scope','ALL_OWN','count',n::text));
 RETURN jsonb_build_object('valid',true,'revoked',n);
END $$;

-- GET /audit-events (permission audit.read): this organization's hash chain, newest first, keyset-paged by sequence.
-- Each item carries prev_hash and hash so a reader can verify the chain without trusting the server's rendering.
CREATE FUNCTION lv_identity.audit_page(p_token_hash bytea, p_before bigint, p_limit integer, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; items jsonb; last_seq bigint; h lv_identity.audit_chain_head;
BEGIN
 s := lv_identity.member_session(p_token_hash, p_source, p_request);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'audit.read') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires audit.read'); END IF;
 IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 OR (p_before IS NOT NULL AND p_before<1) THEN RAISE EXCEPTION 'invalid page' USING ERRCODE='22023'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('seq',e.seq::text,'occurred_at',to_char(e.occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
   'event_type',e.event_type,'outcome',e.outcome,'user_id',e.user_id::text,'session_id',e.session_id::text,'source_key',e.source_key,'request_id',e.request_id,
   'detail',e.detail,'canonical_content',e.canonical_content,'prev_hash',e.prev_hash,'hash',e.hash) ORDER BY e.seq DESC),'[]'::jsonb), min(e.seq)
  INTO items, last_seq
  FROM (SELECT * FROM lv_identity.audit_event WHERE chain_key=s.tenant_id AND (p_before IS NULL OR seq<p_before) ORDER BY seq DESC LIMIT p_limit) e;
 SELECT * INTO h FROM lv_identity.audit_chain_head WHERE chain_key=s.tenant_id;
 PERFORM lv_identity.audit_append('AUDIT_READ','ALLOWED',s.user_id,s.tenant_id,s.id,p_source,p_request,jsonb_build_object('before',coalesce(p_before::text,'HEAD'),'count',jsonb_array_length(items)::text));
 RETURN jsonb_build_object('status',200,'body',jsonb_build_object('items',items,'next_before',CASE WHEN last_seq>1 AND jsonb_array_length(items)=p_limit THEN last_seq::text END,
  'chain',jsonb_build_object('last_seq',coalesce(h.last_seq,0)::text,'head',h.head)));
END $$;

REVOKE ALL ON FUNCTION lv_identity.guard_email_token(),lv_identity.token_throttle(text,bytea,text,integer,integer),lv_identity.token_issue(text,text,bytea,text,text),
 lv_identity.token_consume(text,bytea,text,text,text),lv_identity.member_session(bytea,text,text),lv_identity.session_authenticated_at(uuid),lv_identity.member_json(uuid,uuid),
 lv_identity.member_list(bytea,text,text),lv_identity.member_api(bytea,text,uuid,jsonb,text,text,text,bytea,text,text,text),lv_identity.reauth_prepare(bytea,text,text),
 lv_identity.reauth_finish(bytea,boolean,text,text),lv_identity.revoke_my_sessions(bytea,text,text),lv_identity.audit_page(bytea,bigint,integer,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv_identity.token_issue(text,text,bytea,text,text),lv_identity.token_consume(text,bytea,text,text,text),lv_identity.member_list(bytea,text,text),
 lv_identity.member_api(bytea,text,uuid,jsonb,text,text,text,bytea,text,text,text),lv_identity.reauth_prepare(bytea,text,text),lv_identity.reauth_finish(bytea,boolean,text,text),
 lv_identity.revoke_my_sessions(bytea,text,text),lv_identity.audit_page(bytea,bigint,integer,text,text) TO lv_app;
