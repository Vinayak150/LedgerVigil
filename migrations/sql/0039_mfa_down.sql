DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM lv_identity.mfa_factor) OR EXISTS (SELECT 1 FROM lv_identity.mfa_attempt) THEN
  RAISE EXCEPTION 'MFA enrollment or attempt history exists; refusing downgrade' USING ERRCODE='55000';
 END IF;
END $$;
DROP FUNCTION lv_identity.mfa_finish(bytea,text,text,text,uuid,bytea,bigint,text,text[]);
DROP FUNCTION lv_identity.mfa_prepare(bytea,text,text,text);
DROP TABLE lv_identity.mfa_attempt,lv_identity.mfa_factor;
ALTER TABLE lv_identity.session DROP COLUMN mfa_verified_at;
