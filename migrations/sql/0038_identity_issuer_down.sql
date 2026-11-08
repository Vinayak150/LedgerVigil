-- For disposable historical migration verification only. Restores the vulnerable grants.
REVOKE EXECUTE ON FUNCTION lv_identity.login_prepare(text,text,text),
 lv_identity.login_finish(uuid,boolean,bytea,bytea,text,text),
 lv_identity.switch_tenant(bytea,uuid,bytea,text,text),
 lv_identity.logout(bytea,text,text),
 lv_identity.token_issue(text,text,bytea,text,text),
 lv_identity.token_consume(text,bytea,text,text,text),
 lv_identity.reauth_prepare(bytea,text,text),
 lv_identity.reauth_finish(bytea,boolean,text,text),
 lv_identity.revoke_my_sessions(bytea,text,text),
 lv_identity.member_list(bytea,text,text),
 lv_identity.member_api(bytea,text,uuid,jsonb,text,text,text,bytea,text,text,text),
 lv_identity.session_context(bytea,boolean) FROM lv_authn;
REVOKE USAGE ON SCHEMA lv_identity FROM lv_authn;
GRANT EXECUTE ON FUNCTION lv_identity.login_prepare(text,text,text),
 lv_identity.login_finish(uuid,boolean,bytea,bytea,text,text),
 lv_identity.switch_tenant(bytea,uuid,bytea,text,text),
 lv_identity.logout(bytea,text,text),
 lv_identity.token_issue(text,text,bytea,text,text),
 lv_identity.token_consume(text,bytea,text,text,text),
 lv_identity.reauth_prepare(bytea,text,text),
 lv_identity.reauth_finish(bytea,boolean,text,text),
 lv_identity.revoke_my_sessions(bytea,text,text),
 lv_identity.member_list(bytea,text,text),
 lv_identity.member_api(bytea,text,uuid,jsonb,text,text,text,bytea,text,text,text) TO lv_app;

CREATE OR REPLACE FUNCTION lv_identity.token_consume(p_purpose text, p_token_hash bytea, p_password_hash text, p_source text, p_request text) RETURNS jsonb
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

CREATE OR REPLACE FUNCTION lv_identity.login_prepare(p_email text, p_source text, p_request text) RETURNS jsonb
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

CREATE OR REPLACE FUNCTION lv_identity.reauth_prepare(p_token_hash bytea, p_source text, p_request text) RETURNS jsonb
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

CREATE OR REPLACE FUNCTION lv_identity.session_authenticated_at(p_session uuid) RETURNS timestamptz
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 WITH RECURSIVE chain(id, depth) AS (
  SELECT p_session, 0
  UNION ALL SELECT s.id, c.depth+1 FROM lv_identity.session s JOIN chain c ON s.replaced_by=c.id AND s.revoke_reason='TENANT_SWITCH' WHERE c.depth<20)
 SELECT max(t) FROM (
  SELECT r.at AS t FROM lv_identity.reauthentication r WHERE r.session_id IN (SELECT id FROM chain)
  UNION ALL SELECT s.created_at FROM lv_identity.session s WHERE s.id=(SELECT id FROM chain ORDER BY depth DESC LIMIT 1)) x
$$;

CREATE OR REPLACE FUNCTION lv.guard_restored_writes() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF session_user IN ('lv_app','lv_worker','lv_seed','lv_ingest') AND lv.recovery_state()<>'BOUND' THEN
  RAISE EXCEPTION 'financial writes are refused on a restored database until recovery verification is recorded'
   USING ERRCODE='LV503', HINT='run the recovery verification and record its report with lv.recovery_bind (deploy/RUNBOOK.md)';
 END IF;
 RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION lv.recovery_state() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT CASE WHEN EXISTS(SELECT 1 FROM lv.database_binding b JOIN pg_database d ON d.oid=b.database_oid WHERE d.datname=current_database())
  THEN 'BOUND' ELSE 'RESTORED_UNVERIFIED' END
$$;
CREATE OR REPLACE FUNCTION lv.recovery_bind(p_report_sha256 text) RETURNS text LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE prev oid; here oid;
BEGIN
 IF p_report_sha256 IS NULL OR p_report_sha256 !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'a verification report SHA-256 is required' USING ERRCODE='22023'; END IF;
 SELECT oid INTO here FROM pg_database WHERE datname=current_database();
 SELECT database_oid INTO prev FROM lv.database_binding FOR UPDATE;
 IF prev = here THEN RETURN 'ALREADY_BOUND'; END IF;
 DELETE FROM lv.database_binding;
 INSERT INTO lv.database_binding(database_oid,database_name,reason,report_sha256) VALUES(here,current_database(),'RECOVERY_VERIFIED',p_report_sha256);
 INSERT INTO lv.database_binding_history(database_oid,database_name,reason,report_sha256,previous_oid) VALUES(here,current_database(),'RECOVERY_VERIFIED',p_report_sha256,prev);
 RETURN 'BOUND';
END $$;
ALTER TABLE lv.database_binding_history DROP COLUMN cluster_system_id;
ALTER TABLE lv.database_binding DROP COLUMN cluster_system_id;

