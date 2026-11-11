-- Self-service factor removal requires fresh password and TOTP proof twice, separated by 24 hours.
-- No administrator bypass; losing both factor and recovery capability does not authorize reset.
ALTER TABLE lv_identity.mfa_factor ADD COLUMN reset_after timestamptz;
ALTER TABLE lv_identity.session DROP CONSTRAINT session_revoke_reason_check;
ALTER TABLE lv_identity.session ADD CONSTRAINT session_revoke_reason_check CHECK (revoke_reason IN
 ('LOGOUT','ROTATED_LOGIN','TENANT_SWITCH','ADMIN_REVOKED','USER_DISABLED','MEMBERSHIP_REVOKED',
 'ROLES_CHANGED','PASSWORD_RESET','USER_REVOKED_ALL','MFA_RESET'));
CREATE OR REPLACE FUNCTION lv_identity.mfa_prepare(p_hash bytea,p_action text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; f lv_identity.mfa_factor; since timestamptz := statement_timestamp()-interval '15 minutes';
BEGIN
 PERFORM lv_identity.check_request(p_source,p_request);
 IF p_action IS NULL OR p_action NOT IN ('STATUS','START','CONFIRM','CHALLENGE','RECOVERY','REGENERATE','RESET_REQUEST','RESET_CANCEL','RESET_COMPLETE') THEN
  RAISE EXCEPTION 'invalid MFA action' USING ERRCODE='22023';
 END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('status','INVALID'); END IF;
 IF p_action<>'STATUS' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('mfa-source:'||p_source,0));
  PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR NO KEY UPDATE;
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
  RETURN jsonb_build_object('status','OK','enrolled',f.last_step IS NOT NULL,'verified_at',s.mfa_verified_at,'reset_after',f.reset_after);
 END IF;
 IF p_action IN ('START','REGENERATE','RESET_REQUEST','RESET_CANCEL','RESET_COMPLETE') AND NOT EXISTS (
   SELECT 1 FROM lv_identity.reauthentication r WHERE r.session_id=s.id AND r.at>statement_timestamp()-interval '15 minutes'
 ) THEN
  PERFORM lv_identity.audit_append('MFA_'||p_action,'DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
  RETURN jsonb_build_object('status','REAUTH_REQUIRED');
 END IF;
 IF (p_action='START' AND f.last_step IS NOT NULL)
 OR (p_action='CONFIRM' AND (f.pending_session IS DISTINCT FROM s.id OR f.expires_at<=statement_timestamp()))
 OR (p_action IN ('CHALLENGE','RECOVERY','REGENERATE','RESET_REQUEST','RESET_CANCEL','RESET_COMPLETE') AND f.last_step IS NULL) THEN
  PERFORM lv_identity.audit_append('MFA_'||p_action,'DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
  RETURN jsonb_build_object('status','WRONG_STATE');
 END IF;
 IF (p_action='RESET_REQUEST' AND f.reset_after IS NOT NULL)
 OR (p_action IN ('RESET_CANCEL','RESET_COMPLETE') AND f.reset_after IS NULL) THEN
  RETURN jsonb_build_object('status','WRONG_STATE');
 END IF;
 IF p_action='RESET_COMPLETE' AND f.reset_after>statement_timestamp() THEN
  PERFORM lv_identity.audit_append('MFA_RESET_COOLING','DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
  RETURN jsonb_build_object('status','COOLING_OFF');
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
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR NO KEY UPDATE;
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
  ELSIF p_action IN ('CHALLENGE','REGENERATE','RESET_REQUEST','RESET_CANCEL','RESET_COMPLETE') AND (p_step>f.last_step
   OR (p_action IN ('RESET_REQUEST','RESET_CANCEL','RESET_COMPLETE') AND p_recovery=ANY(f.recovery))) THEN
   UPDATE lv_identity.mfa_factor SET last_step=coalesce(p_step,last_step),recovery=CASE WHEN p_action='REGENERATE' THEN p_new_codes
    WHEN p_recovery IS NOT NULL THEN array_remove(recovery,p_recovery) ELSE recovery END WHERE user_id=s.user_id;
   ok := true;
  ELSIF p_action='RECOVERY' AND p_recovery=ANY(f.recovery) THEN
   UPDATE lv_identity.mfa_factor SET recovery=array_remove(recovery,p_recovery) WHERE user_id=s.user_id;
   ok := true;
  END IF;
 END IF;
 IF ok AND p_action='RESET_REQUEST' THEN
  UPDATE lv_identity.mfa_factor SET reset_after=statement_timestamp()+interval '24 hours' WHERE user_id=s.user_id;
 ELSIF ok AND p_action='RESET_CANCEL' THEN
  UPDATE lv_identity.mfa_factor SET reset_after=NULL WHERE user_id=s.user_id;
 ELSIF ok AND p_action='RESET_COMPLETE' THEN
  IF f.reset_after IS NULL OR f.reset_after>statement_timestamp() THEN
   RAISE EXCEPTION 'MFA reset cooling-off not complete' USING ERRCODE='23514';
  END IF;
  DELETE FROM lv_identity.mfa_factor WHERE user_id=s.user_id;
  UPDATE lv_identity.session SET revoked_at=statement_timestamp(),revoke_reason='MFA_RESET'
   WHERE user_id=s.user_id AND revoked_at IS NULL;
 END IF;
 IF ok AND p_action NOT IN ('START','RESET_COMPLETE') THEN
  UPDATE lv_identity.session SET mfa_verified_at=statement_timestamp() WHERE id=s.id;
 END IF;
 PERFORM lv_identity.audit_append('MFA_'||p_action,CASE WHEN ok THEN 'ALLOWED' ELSE 'DENIED' END,
  s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
 RETURN ok;
END $$;

CREATE OR REPLACE FUNCTION lv_identity.session_info(p_session uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object(
  'session_id', s.id::text,
  'mfa_required', EXISTS(SELECT 1 FROM lv.ledger l WHERE l.tenant_id=s.tenant_id AND l.purpose='BOOKS')
 AND NOT (s.mfa_verified_at IS NOT NULL AND s.mfa_verified_at>statement_timestamp()-interval '15 minutes'
 AND EXISTS(SELECT 1 FROM lv_identity.mfa_factor f WHERE f.user_id=s.user_id AND f.last_step IS NOT NULL AND f.reset_after IS NULL)),
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
                WHERE r.tenant_id = p_tenant AND r.user_id = p_user AND rp.permission::text = p_permission)
 AND (p_permission NOT IN ('review','execute','ledger.bootstrap','administer')
 OR NOT EXISTS (SELECT 1 FROM lv.ledger l WHERE l.tenant_id=p_tenant AND l.purpose='BOOKS')
 OR EXISTS (
  SELECT 1 FROM lv_identity.session s JOIN lv_identity.mfa_factor f ON f.user_id=s.user_id
  WHERE s.id=nullif(current_setting('app.session_id',true),'')::uuid
   AND s.user_id=p_user AND s.tenant_id=p_tenant
   AND lv_identity.session_problem(s) IS NULL AND f.last_step IS NOT NULL AND f.reset_after IS NULL
   AND s.mfa_verified_at>statement_timestamp()-interval '15 minutes'));

$$;
