-- Issuer-only MFA for the SANDBOX candidate. No financial or provider gates change.
ALTER TABLE lv_identity.session ADD COLUMN mfa_verified_at timestamptz;
CREATE TABLE lv_identity.mfa_factor (
 user_id uuid PRIMARY KEY REFERENCES lv_identity.app_user(id),
 enrollment uuid NOT NULL,
 envelope bytea NOT NULL CHECK (octet_length(envelope)=49),
 pending_session uuid REFERENCES lv_identity.session(id),
 expires_at timestamptz,
 last_step bigint CHECK (last_step>=0),
 recovery text[] NOT NULL DEFAULT '{}',
 CHECK ((pending_session IS NULL)=(expires_at IS NULL)),
 CHECK ((pending_session IS NULL)=(last_step IS NOT NULL)),
 CHECK (cardinality(recovery)<=10)
);
CREATE TABLE lv_identity.mfa_attempt (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 user_id uuid NOT NULL REFERENCES lv_identity.app_user(id),
 source_key varchar(64) NOT NULL,
 at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX mfa_attempt_account ON lv_identity.mfa_attempt(user_id,at);
CREATE INDEX mfa_attempt_source ON lv_identity.mfa_attempt(source_key,at);
ALTER TABLE lv_identity.mfa_factor ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv_identity.mfa_factor FORCE ROW LEVEL SECURITY;
ALTER TABLE lv_identity.mfa_attempt ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv_identity.mfa_attempt FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_mfa_factor ON lv_identity.mfa_factor TO lv_owner USING (true) WITH CHECK (true);
CREATE POLICY owner_mfa_attempt ON lv_identity.mfa_attempt TO lv_owner USING (true) WITH CHECK (true);
REVOKE ALL ON lv_identity.mfa_factor,lv_identity.mfa_attempt FROM PUBLIC;

CREATE FUNCTION lv_identity.mfa_prepare(p_hash bytea,p_action text,p_source text,p_request text) RETURNS jsonb
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
CREATE FUNCTION lv_identity.mfa_finish(p_hash bytea,p_action text,p_source text,p_request text,
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
REVOKE ALL ON FUNCTION lv_identity.mfa_prepare(bytea,text,text,text),
 lv_identity.mfa_finish(bytea,text,text,text,uuid,bytea,bigint,text,text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv_identity.mfa_prepare(bytea,text,text,text),
 lv_identity.mfa_finish(bytea,text,text,text,uuid,bytea,bigint,text,text[]) TO lv_authn;
