CREATE OR REPLACE FUNCTION lv.edit_account(tok bytea,aid uuid,p_match text,requested jsonb,p_problem jsonb,src text,req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;a lv.account;l lv.ledger;problem jsonb:=p_problem;before jsonb;change_id uuid:=uuidv7();prior integer;
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'administer') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv_identity.session WHERE id=s.id FOR UPDATE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'administer') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 PERFORM lv_identity.session_context(tok,true);
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
