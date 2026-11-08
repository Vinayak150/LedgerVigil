"""RG-02 first slice: permission and durable audit before native proof release."""
from alembic import op
revision='0024_proof_export_authority'
down_revision='0023_configuration_boundary'
branch_labels=None
depends_on=None

def upgrade():
 op.execute(r'''
CREATE FUNCTION lv.authorize_proof_export(tok bytea,eid uuid,digest text,size_bytes text,response_status text,src text,req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; export_id uuid:=uuidv7();
BEGIN
 s:=lv.proposal_session(tok,src,req);
 -- No session write is acquired before these identity locks. This new path avoids RG-05 F1's inverse order.
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv_identity.session WHERE id=s.id FOR UPDATE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'evidence.export') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=s.tenant_id AND id=eid AND state='RECONCILED') THEN RETURN jsonb_build_object('status',404,'code','NOT_FOUND'); END IF;
 IF digest IS NULL OR digest !~ '^[0-9a-f]{64}$' OR size_bytes IS NULL OR size_bytes !~ '^[1-9][0-9]{0,8}$' OR response_status IS NULL OR response_status NOT IN ('200','304') THEN RAISE EXCEPTION 'invalid proof export metadata' USING ERRCODE='22023'; END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM lv_identity.audit_append('NATIVE_PROOF_EXPORT_AUTHORIZED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('export_id',export_id::text,'execution_id',eid::text,'content_hash',digest,'bytes',size_bytes,'response_status',response_status,'format','ledgervigil.native-execution-proof/1'));
 IF NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND session_id=s.id AND event_type='NATIVE_PROOF_EXPORT_AUTHORIZED' AND detail->>'export_id'=export_id::text AND detail->>'content_hash'=digest) THEN RAISE EXCEPTION 'proof export authorization lacks its audit' USING ERRCODE='23514'; END IF;
 RETURN jsonb_build_object('status',200,'export_id',export_id::text);
END $$;
REVOKE ALL ON FUNCTION lv.authorize_proof_export(bytea,uuid,text,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.authorize_proof_export(bytea,uuid,text,text,text,text,text) TO lv_app;
''')

def downgrade():
 op.execute("ALTER TABLE lv_identity.audit_event ADD CONSTRAINT no_export_downgrade CHECK(event_type<>'NATIVE_PROOF_EXPORT_AUTHORIZED')")
 op.execute('ALTER TABLE lv_identity.audit_event DROP CONSTRAINT no_export_downgrade')
 op.execute('DROP FUNCTION lv.authorize_proof_export(bytea,uuid,text,text,text,text,text)')
