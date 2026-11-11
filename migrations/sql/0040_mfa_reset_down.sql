DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM lv_identity.audit_event WHERE left(event_type,9)='MFA_RESET')
 OR EXISTS (SELECT 1 FROM lv_identity.session WHERE revoke_reason='MFA_RESET') THEN
  RAISE EXCEPTION 'MFA reset history exists; refusing downgrade' USING ERRCODE='55000';
 END IF;
END $$;
CREATE OR REPLACE FUNCTION lv_identity.mfa_prepare(p_hash bytea,p_action text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; f lv_identity.mfa_factor; since timestamptz := statement_timestamp()-interval '15 minutes';
BEGIN
 PERFORM lv_identity.check_request(p_source,p_request);
 IF p_action IS NULL OR p_action NOT IN ('STATUS','START','CONFIRM','CHALLENGE','RECOVERY','REGENERATE') THEN
  RAISE EXCEPTION 'invalid MFA action' USING ERRCODE='22023';
 END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('status','INVALID'); END IF;
 IF p_action<>'STATUS' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('mfa-source:'||p_source,0));
  PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR UPDATE;
  SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash FOR UPDATE;
  IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('status','INVALID'); END IF;
  IF (SELECT count(*) FROM lv_identity.mfa_attempt WHERE user_id=s.user_id AND at>since)>=10
     OR (SELECT count(*) FROM lv_identity.mfa_attempt WHERE source_key=p_source AND at>since)>=40 THEN
   PERFORM lv_identity.audit_append('MFA_THROTTLED','DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
   RETURN jsonb_build_object('status','THROTTLED');
  END IF;
  INSERT INTO lv_identity.mfa_attempt(user_id,source_key) VALUES(s.user_id,p_source);
 END IF;
 SELECT * INTO f FROM lv_identity.mfa_factor WHERE user_id=s.user_id;
 IF p_action='STATUS' THEN
  RETURN jsonb_build_object('status','OK','enrolled',f.last_step IS NOT NULL,'verified_at',s.mfa_verified_at);
 END IF;
 IF p_action IN ('START','REGENERATE') AND NOT EXISTS (
   SELECT 1 FROM lv_identity.reauthentication r WHERE r.session_id=s.id AND r.at>statement_timestamp()-interval '15 minutes'
 ) THEN
  PERFORM lv_identity.audit_append('MFA_'||p_action,'DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
  RETURN jsonb_build_object('status','REAUTH_REQUIRED');
 END IF;
 IF (p_action='START' AND f.last_step IS NOT NULL)
 OR (p_action='CONFIRM' AND (f.pending_session IS DISTINCT FROM s.id OR f.expires_at<=statement_timestamp()))
 OR (p_action IN ('CHALLENGE','RECOVERY','REGENERATE') AND f.last_step IS NULL) THEN
  PERFORM lv_identity.audit_append('MFA_'||p_action,'DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
  RETURN jsonb_build_object('status','WRONG_STATE');
 END IF;
 RETURN jsonb_build_object('status','OK','user_id',s.user_id,'enrollment',f.enrollment,
                           'envelope',encode(f.envelope,'hex'));
END $$;

-- The issuer attests the cryptographic match. No other application role may call this.
CREATE OR REPLACE FUNCTION lv_identity.mfa_finish(p_hash bytea,p_action text,p_source text,p_request text,
 p_enrollment uuid,p_envelope bytea,p_step bigint,p_recovery text,p_new_codes text[]) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; f lv_identity.mfa_factor; ok boolean := false;
BEGIN
 PERFORM lv_identity.check_request(p_source,p_request);
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN false; END IF;
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR UPDATE;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash FOR UPDATE;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN false; END IF;
 SELECT * INTO f FROM lv_identity.mfa_factor WHERE user_id=s.user_id;
 IF p_action='START' AND f.last_step IS NULL AND p_envelope IS NOT NULL AND p_enrollment IS NOT NULL THEN
  INSERT INTO lv_identity.mfa_factor(user_id,enrollment,envelope,pending_session,expires_at)
   VALUES(s.user_id,p_enrollment,p_envelope,s.id,statement_timestamp()+interval '10 minutes')
   ON CONFLICT(user_id) DO UPDATE SET enrollment=excluded.enrollment,envelope=excluded.envelope,
    pending_session=excluded.pending_session,expires_at=excluded.expires_at;
  ok := true;
 ELSIF p_enrollment=f.enrollment THEN
  IF p_action='CONFIRM' AND f.pending_session=s.id AND f.expires_at>statement_timestamp() AND p_step>=0 THEN
   UPDATE lv_identity.mfa_factor SET pending_session=NULL,expires_at=NULL,last_step=p_step,recovery=p_new_codes WHERE user_id=s.user_id;
   ok := true;
  ELSIF p_action IN ('CHALLENGE','REGENERATE') AND p_step>f.last_step THEN
   UPDATE lv_identity.mfa_factor SET last_step=p_step,recovery=CASE WHEN p_action='REGENERATE' THEN p_new_codes ELSE recovery END WHERE user_id=s.user_id;
   ok := true;
  ELSIF p_action='RECOVERY' AND p_recovery=ANY(f.recovery) THEN
   UPDATE lv_identity.mfa_factor SET recovery=array_remove(recovery,p_recovery) WHERE user_id=s.user_id;
   ok := true;
  END IF;
 END IF;
 IF ok AND p_action<>'START' THEN
  UPDATE lv_identity.session SET mfa_verified_at=statement_timestamp() WHERE id=s.id;
 END IF;
 PERFORM lv_identity.audit_append('MFA_'||p_action,CASE WHEN ok THEN 'ALLOWED' ELSE 'DENIED' END,
  s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
 RETURN ok;
END $$;

ALTER TABLE lv_identity.mfa_factor DROP COLUMN reset_after;
ALTER TABLE lv_identity.session DROP CONSTRAINT session_revoke_reason_check;
ALTER TABLE lv_identity.session ADD CONSTRAINT session_revoke_reason_check CHECK (revoke_reason IN
 ('LOGOUT','ROTATED_LOGIN','TENANT_SWITCH','ADMIN_REVOKED','USER_DISABLED','MEMBERSHIP_REVOKED',
 'ROLES_CHANGED','PASSWORD_RESET','USER_REVOKED_ALL'));

CREATE OR REPLACE FUNCTION lv_identity.session_info(p_session uuid) RETURNS jsonb
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
CREATE OR REPLACE FUNCTION lv.proposal_may(p_tenant uuid, p_user uuid, p_permission text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM lv_identity.membership_role r JOIN lv_identity.role_permission rp ON rp.role = r.role
                WHERE r.tenant_id = p_tenant AND r.user_id = p_user AND rp.permission::text = p_permission);
$$;

CREATE OR REPLACE FUNCTION lv_identity.switch_tenant(p_token_hash bytea, p_tenant uuid, p_new_token_hash bytea, p_source text, p_request text) RETURNS jsonb
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
