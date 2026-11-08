-- No runtime direct configuration writes; existing SECURITY DEFINER writers retain their own authority.
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON lv.ledger,lv.account,lv.account_role_binding,lv.party,lv.fiscal_period FROM lv_app;
CREATE TABLE lv.configuration_change(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),account_id uuid NOT NULL,
 previous_version integer NOT NULL,resulting_version integer NOT NULL CHECK(resulting_version=previous_version+1),
 actor_id uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 before_fields jsonb NOT NULL,after_fields jsonb NOT NULL,request_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,ledger_id,account_id,resulting_version),
 FOREIGN KEY(tenant_id,ledger_id,account_id) REFERENCES lv.account(tenant_id,ledger_id,id)
);
ALTER TABLE lv.configuration_change ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.configuration_change FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.configuration_change USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.configuration_change FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.configuration_change FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE FUNCTION lv.configuration_account_json(a lv.account) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',a.id::text,'ledger_id',a.ledger_id::text,'code',a.code,'name',a.name,'type',a.type::text,'subtype',a.subtype,'is_contra',a.is_contra,'normal_side',CASE WHEN a.normal_side='D' THEN 'DEBIT' ELSE 'CREDIT' END,'requires_party',a.requires_party,'currency',btrim(a.currency),'active',a.active,'parent_id',a.parent_id::text,'version',a.version)
$$;
REVOKE ALL ON FUNCTION lv.configuration_account_json(lv.account) FROM PUBLIC;
CREATE FUNCTION lv.edit_account(tok bytea,aid uuid,p_match text,requested jsonb,p_problem jsonb,src text,req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;a lv.account;l lv.ledger;problem jsonb:=p_problem;before jsonb;change_id uuid:=uuidv7();prior integer;
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'administer') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'administer') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 SELECT * INTO a FROM lv.account WHERE tenant_id=s.tenant_id AND id=aid;
 IF NOT FOUND THEN problem:=jsonb_build_object('status',404,'code','NOT_FOUND');
 ELSE
 -- Same ledger-first configuration/posting lock order; identity cannot move ledgers.
 PERFORM 1 FROM lv.ledger WHERE tenant_id=a.tenant_id AND id=a.ledger_id FOR UPDATE;
 SELECT * INTO a FROM lv.account WHERE tenant_id=s.tenant_id AND id=aid FOR UPDATE;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=a.tenant_id AND id=a.ledger_id;
 IF p_match IS NULL THEN problem:=jsonb_build_object('status',428,'code','VERSION_REQUIRED');
 ELSIF p_match<>'"v'||a.version::text||'"' THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','current_version',a.version);
 ELSIF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE');
 ELSIF problem IS NOT NULL THEN NULL;
 ELSIF requested IS NULL OR jsonb_typeof(requested)<>'object' OR NOT ((requested ? 'name' AND requested-ARRAY['name']='{}'::jsonb AND jsonb_typeof(requested->'name')='string' AND length(requested->>'name')<=120 AND lv.rejection_reason_valid(requested->>'name')) OR (requested='{"active":false}'::jsonb)) THEN problem:=jsonb_build_object('status',422,'code','SCHEMA_INVALID');
 ELSIF requested ? 'active' AND (EXISTS(SELECT 1 FROM lv.account_role_binding WHERE tenant_id=a.tenant_id AND ledger_id=a.ledger_id AND account_id=a.id) OR coalesce((SELECT sum(debit_total-credit_total) FROM lv.account_balance_current WHERE tenant_id=a.tenant_id AND ledger_id=a.ledger_id AND account_id=a.id),0)<>0) THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','reason','ACCOUNT_IN_USE');
 END IF;
 END IF;
 IF problem IS NOT NULL THEN
 PERFORM lv_identity.audit_append('CONFIGURATION_CHANGE_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('resource','ACCOUNT','account_id',aid::text,'reason',problem->>'code'));
 RETURN problem;
 END IF;
 prior:=a.version;before:=jsonb_build_object('name',a.name,'active',a.active,'version',a.version);
 UPDATE lv.account SET name=CASE WHEN requested ? 'name' THEN requested->>'name' ELSE name END,active=CASE WHEN requested ? 'active' THEN false ELSE active END WHERE tenant_id=a.tenant_id AND ledger_id=a.ledger_id AND id=a.id AND version=prior RETURNING * INTO a;
 IF NOT FOUND THEN RAISE EXCEPTION 'configuration CAS lost' USING ERRCODE='40001'; END IF;
 INSERT INTO lv.configuration_change(tenant_id,ledger_id,id,account_id,previous_version,resulting_version,actor_id,session_id,before_fields,after_fields,request_id)
 VALUES(a.tenant_id,a.ledger_id,change_id,a.id,prior,a.version,s.user_id,s.id,before,jsonb_build_object('name',a.name,'active',a.active,'version',a.version),req);
 PERFORM lv_identity.audit_append('CONFIGURATION_ACCOUNT_CHANGED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('change_id',change_id::text,'account_id',a.id::text,'previous_version',prior::text,'version',a.version::text));
 RETURN jsonb_build_object('status',200,'etag','v'||a.version::text,'body',lv.configuration_account_json(a));
END $$;
REVOKE ALL ON FUNCTION lv.edit_account(bytea,uuid,text,jsonb,jsonb,text,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.edit_account(bytea,uuid,text,jsonb,jsonb,text,text) TO lv_app;
CREATE FUNCTION lv.validate_configuration_change() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE a lv.account;
BEGIN
 SELECT * INTO a FROM lv.account WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND id=NEW.account_id;
 IF NEW.tx_id<>pg_current_xact_id() OR NEW.after_fields IS DISTINCT FROM jsonb_build_object('name',a.name,'active',a.active,'version',a.version)
 OR NEW.resulting_version<>a.version OR NEW.before_fields->>'version'<>NEW.previous_version::text
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=NEW.tenant_id AND event_type='CONFIGURATION_ACCOUNT_CHANGED' AND user_id=NEW.actor_id AND session_id=NEW.session_id AND detail->>'change_id'=NEW.id::text AND detail->>'account_id'=a.id::text AND detail->>'version'=a.version::text)
 THEN RAISE EXCEPTION 'configuration change lacks its account or atomic audit' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION lv.validate_account_configuration() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF session_user='lv_app' AND (NEW.name,NEW.active) IS DISTINCT FROM (OLD.name,OLD.active)
 AND NOT EXISTS(SELECT 1 FROM lv.configuration_change WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND account_id=NEW.id AND previous_version=OLD.version AND resulting_version=NEW.version AND tx_id=pg_current_xact_id() AND before_fields=jsonb_build_object('name',OLD.name,'active',OLD.active,'version',OLD.version) AND after_fields=jsonb_build_object('name',NEW.name,'active',NEW.active,'version',NEW.version))
 THEN RAISE EXCEPTION 'runtime account change lacks immutable configuration history' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION lv.validate_configuration_change(),lv.validate_account_configuration() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER configuration_complete AFTER INSERT ON lv.configuration_change DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_configuration_change();
CREATE CONSTRAINT TRIGGER account_configuration_complete AFTER UPDATE ON lv.account DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_account_configuration();
