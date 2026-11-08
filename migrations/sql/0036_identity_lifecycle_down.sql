-- Refused (by the restored CHECK constraints) once any membership was administered through the API or any session was
-- revoked by a password reset; tokens and re-authentications are dropped only when none exist.
CREATE FUNCTION lv_identity.refuse_lifecycle_downgrade() RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM lv_identity.email_token) OR EXISTS(SELECT 1 FROM lv_identity.reauthentication) OR EXISTS(SELECT 1 FROM lv_identity.token_request) THEN
  RAISE EXCEPTION 'identity lifecycle records exist; downgrade refused' USING ERRCODE='55000';
 END IF;
END $$;
SELECT lv_identity.refuse_lifecycle_downgrade();
DROP FUNCTION lv_identity.refuse_lifecycle_downgrade();
DROP FUNCTION lv_identity.audit_page(bytea,bigint,integer,text,text);
DROP FUNCTION lv_identity.revoke_my_sessions(bytea,text,text);
DROP FUNCTION lv_identity.reauth_finish(bytea,boolean,text,text);
DROP FUNCTION lv_identity.reauth_prepare(bytea,text,text);
DROP FUNCTION lv_identity.member_api(bytea,text,uuid,jsonb,text,text,text,bytea,text,text,text);
DROP FUNCTION lv_identity.member_list(bytea,text,text);
DROP FUNCTION lv_identity.member_json(uuid,uuid);
DROP FUNCTION lv_identity.session_authenticated_at(uuid);
DROP FUNCTION lv_identity.member_session(bytea,text,text);
DROP FUNCTION lv_identity.token_consume(text,bytea,text,text,text);
DROP FUNCTION lv_identity.token_issue(text,text,bytea,text,text);
DROP FUNCTION lv_identity.token_throttle(text,bytea,text,integer,integer);
DROP TABLE lv_identity.reauthentication;
DROP TABLE lv_identity.token_request;
DROP TABLE lv_identity.email_token;
DROP FUNCTION lv_identity.guard_email_token();
ALTER TABLE lv_identity.session DROP CONSTRAINT session_revoke_reason_check;
ALTER TABLE lv_identity.session ADD CONSTRAINT session_revoke_reason_check CHECK (revoke_reason IN ('LOGOUT','ROTATED_LOGIN','TENANT_SWITCH','ADMIN_REVOKED',
 'USER_DISABLED','MEMBERSHIP_REVOKED','ROLES_CHANGED'));
ALTER TABLE lv_identity.membership_role DROP CONSTRAINT membership_role_grant_source_check;
ALTER TABLE lv_identity.membership_role ADD CONSTRAINT membership_role_grant_source_check CHECK (grant_source = 'LOCAL_PROVISIONING');
