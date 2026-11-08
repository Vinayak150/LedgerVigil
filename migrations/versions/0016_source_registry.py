"""Proposed SR-1: authenticated registry for one pinned public admission plan."""
from alembic import op
revision='0016_source_registry'
down_revision='0015_resimulation'
branch_labels=None
depends_on=None
DDL="""
CREATE TABLE lv.source_plan_registration(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),
 manifest_sha256 text NOT NULL CHECK(manifest_sha256='3c7dbcb169405d9d0f8abcff095c9f30df46ca6b1b38fb31f894ed1657ea0523'),
 created_by uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,ledger_id,manifest_sha256),
 FOREIGN KEY(tenant_id,ledger_id) REFERENCES lv.ledger(tenant_id,id)
);
ALTER TABLE lv.source_plan_registration ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv.source_plan_registration FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.source_plan_registration USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.source_plan_registration FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
REVOKE ALL ON lv.source_plan_registration FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
GRANT SELECT ON lv.source_plan_registration TO lv_app;
CREATE FUNCTION lv.register_source_plan(tok bytea,lid uuid,manifest text,ikey text,request_hash text,src text,req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;l lv.ledger;r lv.source_plan_registration;prior lv.api_idempotency;response jsonb;v_route text:='/api/v1/source-plan-registrations';
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'source.import') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 IF ikey IS NULL OR ikey!~'^[!-~]{1,64}$' OR request_hash!~'^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-source-plan:'||s.tenant_id::text||s.user_id::text||ikey,0)) THEN RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS'); END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'source.import') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 SELECT * INTO prior FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND api_idempotency.route=v_route AND idempotency_key=ikey;
 IF FOUND AND prior.expires_at>transaction_timestamp() THEN
 IF prior.request_hash<>request_hash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
 RETURN prior.response||jsonb_build_object('replayed',true);
 END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=s.tenant_id AND id=lid FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status',404,'code','NOT_FOUND'); END IF;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR l.currency<>'GBP' THEN RETURN jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE'); END IF;
 IF manifest IS DISTINCT FROM '3c7dbcb169405d9d0f8abcff095c9f30df46ca6b1b38fb31f894ed1657ea0523' THEN RETURN jsonb_build_object('status',422,'code','UNVERIFIED_SOURCE_PLAN'); END IF;
 INSERT INTO lv.source_plan_registration(tenant_id,ledger_id,manifest_sha256,created_by,session_id) VALUES(s.tenant_id,lid,manifest,s.user_id,s.id) ON CONFLICT(tenant_id,ledger_id,manifest_sha256) DO NOTHING;
 SELECT * INTO r FROM lv.source_plan_registration WHERE tenant_id=s.tenant_id AND ledger_id=lid AND manifest_sha256=manifest;
 PERFORM lv_identity.audit_append('SOURCE_PLAN_REGISTERED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('registration_id',r.id::text,'manifest_hash',manifest));
 response:=jsonb_build_object('status',200,'id',r.id::text);
 DELETE FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND api_idempotency.route=v_route AND idempotency_key=ikey;
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at) VALUES(s.tenant_id,s.user_id,'POST',v_route,ikey,request_hash,200,response,transaction_timestamp()+interval '24 hours');
 RETURN response;
END $$;
CREATE FUNCTION lv.validate_source_registration() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF NEW.tx_id<>pg_current_xact_id() OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=NEW.tenant_id AND user_id=NEW.created_by AND session_id=NEW.session_id AND event_type='SOURCE_PLAN_REGISTERED' AND detail->>'registration_id'=NEW.id::text AND detail->>'manifest_hash'=NEW.manifest_sha256)
 OR NOT EXISTS(SELECT 1 FROM lv.api_idempotency WHERE tenant_id=NEW.tenant_id AND principal_id=NEW.created_by AND route='/api/v1/source-plan-registrations' AND response_status=200 AND response->>'id'=NEW.id::text AND created_at=NEW.created_at)
 THEN RAISE EXCEPTION 'source registration lacks atomic audit/cache' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER source_registration_complete AFTER INSERT ON lv.source_plan_registration DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_source_registration();
REVOKE ALL ON FUNCTION lv.validate_source_registration() FROM PUBLIC;
REVOKE ALL ON FUNCTION lv.register_source_plan(bytea,uuid,text,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.register_source_plan(bytea,uuid,text,text,text,text,text) TO lv_app;
"""
def upgrade():op.get_bind().exec_driver_sql(DDL)
def downgrade():
 op.execute('ALTER TABLE lv.source_plan_registration ADD CONSTRAINT empty_for_downgrade CHECK(false)')
 op.execute('DROP FUNCTION lv.register_source_plan(bytea,uuid,text,text,text,text,text)')
 op.execute('DROP TABLE lv.source_plan_registration')
 op.execute('DROP FUNCTION lv.validate_source_registration()')
 op.execute("DELETE FROM lv.api_idempotency WHERE route='/api/v1/source-plan-registrations'")
