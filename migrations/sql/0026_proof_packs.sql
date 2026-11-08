REVOKE EXECUTE ON FUNCTION lv.authorize_proof_export(bytea,uuid,text,text,text,text,text) FROM lv_app;
CREATE TABLE lv.proof_pack(
 tenant_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),ledger_id uuid NOT NULL,proposal_id uuid NOT NULL,revision integer NOT NULL,proposal_version integer NOT NULL,simulation_id uuid NOT NULL,
 state text NOT NULL DEFAULT 'DRAFTING' CHECK(state IN ('DRAFTING','SEALED','ANCHORED','DEAD','ANCHOR_FAILED')),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 fence bigint NOT NULL DEFAULT 0,attempts integer NOT NULL DEFAULT 0,lease_owner text,lease_expires_at timestamptz,dispatched_at timestamptz,last_error text,
 content_canonical text,content_hash text,previous_chain_head text,chain_hash text,chain_seq bigint,sealed_at timestamptz,anchor_commit text,anchored_at timestamptz,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,proposal_id,revision,proposal_version),UNIQUE(tenant_id,content_hash),UNIQUE(tenant_id,chain_seq),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 CHECK(content_canonical IS NULL OR (octet_length(content_canonical)<=16777216 AND content_hash='sha256:'||encode(sha256(convert_to(content_canonical,'UTF8')),'hex') AND chain_hash='sha256:'||encode(sha256(convert_to(content_hash||previous_chain_head,'UTF8')),'hex'))),
 CHECK((content_canonical IS NULL)=(content_hash IS NULL)),CHECK((content_canonical IS NULL)=(chain_hash IS NULL)),CHECK((content_canonical IS NULL)=(previous_chain_head IS NULL)),
 CHECK((state IN ('SEALED','ANCHORED','ANCHOR_FAILED'))=(content_canonical IS NOT NULL)),CHECK((state='ANCHORED')=(anchor_commit IS NOT NULL))
);
ALTER TABLE lv.proof_pack ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.proof_pack FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.proof_pack USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE POLICY worker_inventory ON lv.proof_pack FOR SELECT TO lv_owner USING(session_user='lv_worker');
REVOKE ALL ON lv.proof_pack FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
CREATE TABLE lv.proof_pack_chain(tenant_id uuid PRIMARY KEY,seq bigint NOT NULL DEFAULT 0,head text NOT NULL DEFAULT 'sha256:0000000000000000000000000000000000000000000000000000000000000000');
ALTER TABLE lv.proof_pack_chain ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.proof_pack_chain FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.proof_pack_chain USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.proof_pack_chain FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
CREATE FUNCTION lv.guard_proof_pack() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
 IF TG_OP='DELETE' OR OLD.state='ANCHORED' OR (NEW.tenant_id,NEW.id,NEW.ledger_id,NEW.proposal_id,NEW.revision,NEW.proposal_version,NEW.simulation_id,NEW.created_at) IS DISTINCT FROM (OLD.tenant_id,OLD.id,OLD.ledger_id,OLD.proposal_id,OLD.revision,OLD.proposal_version,OLD.simulation_id,OLD.created_at) OR (OLD.content_canonical IS NOT NULL AND (NEW.content_canonical,NEW.content_hash,NEW.chain_hash,NEW.chain_seq,NEW.previous_chain_head,NEW.sealed_at) IS DISTINCT FROM (OLD.content_canonical,OLD.content_hash,OLD.chain_hash,OLD.chain_seq,OLD.previous_chain_head,OLD.sealed_at)) THEN RAISE EXCEPTION 'proof pack history is immutable' USING ERRCODE='23514';END IF;RETURN NEW;END $$;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proof_pack FOR EACH ROW EXECUTE FUNCTION lv.guard_proof_pack();
CREATE FUNCTION lv.enqueue_proof_pack(p lv.proposal) RETURNS uuid LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ DECLARE pack_id uuid;BEGIN
 IF p.latest_simulation_id IS NULL THEN RETURN NULL;END IF;
 INSERT INTO lv.proof_pack(tenant_id,ledger_id,proposal_id,revision,proposal_version,simulation_id) VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,p.latest_simulation_id) ON CONFLICT(tenant_id,proposal_id,revision,proposal_version) DO NOTHING;
 SELECT id INTO pack_id FROM lv.proof_pack WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND proposal_version=p.version;RETURN pack_id;END $$;
CREATE FUNCTION lv.auto_proof_pack() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ BEGIN
 IF NEW.state IN ('BLOCKED','REJECTED','EXECUTED','NEEDS_ATTENTION') AND (OLD.state,OLD.revision) IS DISTINCT FROM (NEW.state,NEW.revision) THEN PERFORM lv.enqueue_proof_pack(NEW);END IF;RETURN NULL;END $$;
CREATE TRIGGER proof_pack_requested AFTER UPDATE ON lv.proposal FOR EACH ROW EXECUTE FUNCTION lv.auto_proof_pack();
CREATE FUNCTION lv.pack_api(tok bytea,action text,subject uuid,rev integer,src text,req text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;pack lv.proof_pack;BEGIN
 s:=lv.proposal_session(tok,src,req);IF NOT lv.proposal_may(s.tenant_id,s.user_id,'read') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED');END IF;
 IF action='CREATE' THEN
  SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=subject FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status',404,'code','NOT_FOUND');END IF;
  IF p.revision IS DISTINCT FROM rev OR p.latest_simulation_id IS NULL THEN RETURN jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION');END IF;
  subject:=lv.enqueue_proof_pack(p);
 END IF;
 SELECT * INTO pack FROM lv.proof_pack WHERE tenant_id=s.tenant_id AND id=subject;
 IF NOT FOUND THEN RETURN jsonb_build_object('status',404,'code','NOT_FOUND');END IF;
 RETURN jsonb_build_object('status',CASE WHEN action='CREATE' THEN 202 ELSE 200 END,'body',jsonb_build_object('id',pack.id::text,'proposal_id',pack.proposal_id::text,'revision',pack.revision,'ledger_id',pack.ledger_id::text,'state',CASE WHEN pack.state='ANCHORED' THEN 'SEALED' ELSE pack.state END,'content_hash',pack.content_hash,'chain_hash',pack.chain_hash,'chain_seq',pack.chain_seq,'anchor_commit',pack.anchor_commit,'chain_head_exported_at',pack.anchored_at,'last_error',pack.last_error),'content',pack.content_canonical,'previous_chain_head',pack.previous_chain_head,'sealed_at',pack.sealed_at);
END $$;
CREATE FUNCTION lv.pack_due(n integer) RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ DECLARE p lv.proof_pack;q record;prop lv.proposal;BEGIN
 PERFORM lv.maintenance_context(NULL);IF n IS NULL OR n NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'invalid bound' USING ERRCODE='22023';END IF;
 -- Native completion rows are immutable notifications. Their anchored proof pack is the consumption receipt.
 FOR q IN SELECT o.tenant_id,o.native_payload FROM lv.outbox o WHERE o.native_execution_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM lv.proof_pack x WHERE x.tenant_id=o.tenant_id AND x.proposal_id::text=o.native_payload->>'proposal_id' AND x.proposal_version=(o.native_payload->>'resulting_version')::integer) ORDER BY o.id LIMIT n LOOP
  PERFORM lv.maintenance_context(q.tenant_id);SELECT * INTO prop FROM lv.proposal WHERE tenant_id=q.tenant_id AND id=(q.native_payload->>'proposal_id')::uuid AND state='EXECUTED';
  IF FOUND THEN PERFORM lv.enqueue_proof_pack(prop);END IF;
 END LOOP;
 PERFORM lv.maintenance_context(NULL);
 FOR p IN SELECT * FROM lv.proof_pack WHERE state IN ('DRAFTING','SEALED') AND (lease_expires_at IS NULL OR lease_expires_at<=clock_timestamp()) AND (dispatched_at IS NULL OR dispatched_at<clock_timestamp()-interval '30 seconds') ORDER BY created_at,id LIMIT n LOOP RETURN NEXT jsonb_build_object('tenant_id',p.tenant_id::text,'job_id',p.id::text);END LOOP;END $$;
CREATE FUNCTION lv.pack_dispatch(t uuid,j uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ BEGIN PERFORM lv.maintenance_context(t);UPDATE lv.proof_pack SET dispatched_at=clock_timestamp() WHERE tenant_id=t AND id=j AND state IN ('DRAFTING','SEALED');END $$;
CREATE FUNCTION lv.pack_claim(t uuid,j uuid,w text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ DECLARE p lv.proof_pack;BEGIN
 PERFORM lv.maintenance_context(t);
 SELECT * INTO p FROM lv.proof_pack WHERE tenant_id=t AND id=j FOR UPDATE;
 IF p.id IS NULL OR p.state NOT IN ('DRAFTING','SEALED') OR p.lease_expires_at>clock_timestamp() THEN RETURN NULL;END IF;
 IF p.attempts>=3 THEN UPDATE lv.proof_pack SET state=CASE WHEN content_canonical IS NULL THEN 'DEAD' ELSE 'ANCHOR_FAILED' END,last_error='LEASE_RETRIES_EXHAUSTED',lease_expires_at=NULL WHERE tenant_id=t AND id=j;PERFORM lv_identity.audit_append('PROOF_PACK_BUILD_FAILED','DENIED',NULL,t,NULL,'proof-worker',j::text,jsonb_build_object('pack_id',j::text,'error_class','LEASE_RETRIES_EXHAUSTED'));RETURN NULL;END IF;
 IF EXISTS(SELECT 1 FROM lv.proof_pack WHERE tenant_id=t AND state<>'ANCHORED' AND (created_at,id)<(p.created_at,p.id)) THEN RETURN NULL;END IF;
 UPDATE lv.proof_pack SET fence=fence+1,attempts=attempts+1,lease_owner=w,lease_expires_at=clock_timestamp()+interval '60 seconds' WHERE tenant_id=t AND id=j RETURNING * INTO p;
 RETURN jsonb_build_object('fence',p.fence,'state',p.state,'content',p.content_canonical,'content_hash',p.content_hash,'chain_hash',p.chain_hash,'previous_chain_head',p.previous_chain_head,'chain_seq',p.chain_seq,'sealed_at',p.sealed_at);
END $$;
CREATE FUNCTION lv.pack_snapshot(t uuid,j uuid,w text,f bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proof_pack;s lv.simulation_run;r lv.proposal_revision;inp jsonb;outcome jsonb;prev text;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO p FROM lv.proof_pack WHERE tenant_id=t AND id=j;
 IF p.state<>'DRAFTING' OR p.fence IS DISTINCT FROM f OR p.lease_owner IS DISTINCT FROM w OR p.lease_expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'lost pack lease' USING ERRCODE='40001';END IF;
 SELECT * INTO STRICT s FROM lv.simulation_run WHERE tenant_id=t AND id=p.simulation_id;
 SELECT * INTO STRICT r FROM lv.proposal_revision WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision;
 inp:=s.input_canonical::jsonb;outcome:=s.outcome_canonical::jsonb;
 SELECT content_hash INTO prev FROM lv.proof_pack WHERE tenant_id=t AND proposal_id=p.proposal_id AND state='ANCHORED' ORDER BY chain_seq DESC LIMIT 1;
 RETURN jsonb_build_object('tenant_id',t::text,'proof_pack_id',j::text,'proposal',jsonb_build_object('id',p.proposal_id::text,'revision',p.revision,'origin',(SELECT origin FROM lv.proposal WHERE tenant_id=t AND id=p.proposal_id)),'ledger',inp->'ledger','snapshot',inp->'snapshot','engine_versions',inp->'versions','operations',r.operations,'read_set',inp->'read_set','evidence',r.evidence_refs,
 'simulation',jsonb_build_object('result_hash',s.result_hash,'input_hash',s.input_hash,'input_canonical',s.input_canonical,'outcome_canonical',s.outcome_canonical,'predicted_postings',outcome->'predicted_postings','account_deltas',outcome->'account_deltas','verdicts',outcome->'verdicts','aggregate',outcome->'aggregate'),
 'ai',NULL,'decisions',(SELECT coalesce(jsonb_agg(v ORDER BY v->>'at',v->>'id'),'[]'::jsonb) FROM (SELECT jsonb_build_object('id',d.id::text,'type',d.kind,'actor_id',d.approved_by::text,'revision',d.revision,'at',to_char(d.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'comment_sha256',CASE WHEN d.comment IS NULL THEN NULL ELSE encode(sha256(convert_to(d.comment,'UTF8')),'hex') END) v FROM lv.proposal_decision d WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision AND resulting_version<=p.proposal_version
 UNION ALL SELECT jsonb_build_object('id',o.id::text,'type','EVIDENCE_EXCEPTION','actor_id',o.overridden_by::text,'revision',o.revision,'policy_id',o.policy_id,'at',to_char(o.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'comment_sha256',encode(sha256(convert_to(o.reason,'UTF8')),'hex')) FROM lv.proposal_override o WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision AND resulting_version<=p.proposal_version) decisions),
 'execution',(SELECT jsonb_build_object('adapter','NATIVE','completion_notification',(SELECT jsonb_build_object('id',id::text,'payload',native_payload) FROM lv.outbox WHERE tenant_id=t AND native_execution_id=e.id AND topic='execution'),'receipt',lv.native_execution_json(e),'check',(SELECT report FROM lv.native_execution_check WHERE tenant_id=t AND id=e.check_id),'batch_hash',(SELECT hash FROM lv.commit_batch WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq),'event_hash',(SELECT hash FROM lv.ledger_event WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq)) FROM lv.native_execution e WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision AND resulting_version<=p.proposal_version),
 'accounting_review_status','UNREVIEWED','previous_pack_hash',prev);
END $$;
CREATE FUNCTION lv.pack_seal(t uuid,j uuid,w text,f bigint,content text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ DECLARE p lv.proof_pack;c lv.proof_pack_chain;expected jsonb;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO p FROM lv.proof_pack WHERE tenant_id=t AND id=j FOR UPDATE;
 IF p.state<>'DRAFTING' OR p.fence IS DISTINCT FROM f OR p.lease_owner IS DISTINCT FROM w OR p.lease_expires_at<=clock_timestamp() THEN RETURN NULL;END IF;
 expected:=lv.pack_snapshot(t,j,w,f);IF content::jsonb IS DISTINCT FROM expected THEN RAISE EXCEPTION 'pack content differs from frozen source facts' USING ERRCODE='23514';END IF;
 INSERT INTO lv.proof_pack_chain(tenant_id) VALUES(t) ON CONFLICT DO NOTHING;SELECT * INTO c FROM lv.proof_pack_chain WHERE tenant_id=t FOR UPDATE;
 UPDATE lv.proof_pack SET state='SEALED',content_canonical=content,content_hash='sha256:'||encode(sha256(convert_to(content,'UTF8')),'hex'),previous_chain_head=c.head,chain_seq=c.seq+1,sealed_at=clock_timestamp(),chain_hash='sha256:'||encode(sha256(convert_to(('sha256:'||encode(sha256(convert_to(content,'UTF8')),'hex'))||c.head,'UTF8')),'hex') WHERE tenant_id=t AND id=j RETURNING * INTO p;
 UPDATE lv.proof_pack_chain SET seq=p.chain_seq,head=p.chain_hash WHERE tenant_id=t;
 PERFORM lv_identity.audit_append('PROOF_PACK_SEALED','ALLOWED',NULL,t,NULL,'proof-worker',j::text,jsonb_build_object('pack_id',j::text,'content_hash',p.content_hash,'chain_hash',p.chain_hash));
 RETURN jsonb_build_object('content_hash',p.content_hash,'chain_hash',p.chain_hash,'previous_chain_head',p.previous_chain_head,'chain_seq',p.chain_seq,'sealed_at',p.sealed_at);
END $$;
CREATE FUNCTION lv.pack_anchor(t uuid,j uuid,w text,f bigint,oid text,archive bytea) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ DECLARE p lv.proof_pack;BEGIN
 PERFORM lv.maintenance_context(t);IF oid IS NULL OR oid!~'^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'invalid Git anchor commit' USING ERRCODE='22023';END IF;
 UPDATE lv.proof_pack SET state='ANCHORED',anchor_commit=oid,bundle=archive,bundle_hash='sha256:'||encode(sha256(archive),'hex'),anchored_at=clock_timestamp(),lease_expires_at=NULL WHERE tenant_id=t AND id=j AND state='SEALED' AND fence=f AND lease_owner=w AND lease_expires_at>clock_timestamp() RETURNING * INTO p;
 IF NOT FOUND THEN RETURN false;END IF;
 PERFORM lv_identity.audit_append('PROOF_PACK_ANCHORED','ALLOWED',NULL,t,NULL,'proof-worker',j::text,jsonb_build_object('pack_id',j::text,'anchor_commit',oid,'chain_hash',p.chain_hash,'notification_id',p.content_canonical::jsonb->'execution'->'completion_notification'->>'id'));RETURN true;END $$;
REVOKE ALL ON FUNCTION lv.guard_proof_pack(),lv.enqueue_proof_pack(lv.proposal),lv.auto_proof_pack(),lv.pack_api(bytea,text,uuid,integer,text,text),lv.pack_due(integer),lv.pack_dispatch(uuid,uuid),lv.pack_claim(uuid,uuid,text),lv.pack_snapshot(uuid,uuid,text,bigint),lv.pack_seal(uuid,uuid,text,bigint,text),lv.pack_anchor(uuid,uuid,text,bigint,text,bytea) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION lv.pack_due(integer),lv.pack_dispatch(uuid,uuid),lv.pack_claim(uuid,uuid,text),lv.pack_snapshot(uuid,uuid,text,bigint),lv.pack_seal(uuid,uuid,text,bigint,text),lv.pack_anchor(uuid,uuid,text,bigint,text,bytea) TO lv_worker;
ALTER TABLE lv.proof_pack ADD COLUMN bundle bytea,ADD COLUMN bundle_hash text;
ALTER TABLE lv.proof_pack ADD CONSTRAINT anchored_bundle CHECK((state='ANCHORED')=(bundle IS NOT NULL));
ALTER TABLE lv.proof_pack ADD CONSTRAINT bundle_digest CHECK((bundle IS NULL)=(bundle_hash IS NULL));
ALTER TABLE lv.proof_pack ADD CONSTRAINT bundle_bytes CHECK(bundle IS NULL OR (octet_length(bundle)>=1 AND octet_length(bundle)<=16777216 AND bundle_hash='sha256:'||encode(sha256(bundle),'hex')));
CREATE TABLE lv.proof_pack_request(tenant_id uuid NOT NULL,user_id uuid NOT NULL,idempotency_key text NOT NULL,proposal_id uuid NOT NULL,revision integer NOT NULL,response jsonb NOT NULL,PRIMARY KEY(tenant_id,user_id,idempotency_key));
ALTER TABLE lv.proof_pack_request ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.proof_pack_request FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.proof_pack_request USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.proof_pack_request FROM PUBLIC,lv_app,lv_worker,lv_seed,lv_ingest;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proof_pack_request FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
ALTER TABLE lv.native_export_job ADD COLUMN proof_pack_id uuid,ALTER COLUMN execution_id DROP NOT NULL;
ALTER TABLE lv.native_export_job ADD CONSTRAINT pack_export_reference FOREIGN KEY(tenant_id,proof_pack_id) REFERENCES lv.proof_pack(tenant_id,id);
ALTER TABLE lv.native_export_job ADD CONSTRAINT export_subject CHECK(num_nonnulls(execution_id,proof_pack_id)=1);
ALTER TABLE lv.native_export_job DROP CONSTRAINT native_export_job_format_check;
ALTER TABLE lv.native_export_job ADD CONSTRAINT native_export_format CHECK((proof_pack_id IS NULL AND format='JSON') OR (proof_pack_id IS NOT NULL AND format='BUNDLE'));
CREATE FUNCTION lv.pack_request(tok bytea,pid uuid,rev integer,key text,src text,req text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;r lv.proof_pack_request;response jsonb;BEGIN
 s:=lv.proposal_session(tok,src,req);IF NOT lv.proposal_may(s.tenant_id,s.user_id,'read') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED');END IF;
 IF key IS NULL OR key!~'^[!-~]{1,64}$' THEN RETURN jsonb_build_object('status',422,'code','SCHEMA_INVALID');END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(s.tenant_id::text||s.user_id::text||key,0));
 SELECT * INTO r FROM lv.proof_pack_request WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND idempotency_key=key;
 IF FOUND THEN
  IF (r.proposal_id,r.revision) IS DISTINCT FROM (pid,rev) THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED');END IF;RETURN r.response;
 END IF;
 response:=lv.pack_api(tok,'CREATE',pid,rev,src,req);
 IF response->>'status'='202' THEN
  INSERT INTO lv.proof_pack_request VALUES(s.tenant_id,s.user_id,key,pid,rev,response);
  PERFORM lv_identity.audit_append('PROOF_PACK_REQUESTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('pack_id',response->'body'->>'id','proposal_id',pid::text,'revision',rev::text));
  IF NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND request_id=req AND event_type='PROOF_PACK_REQUESTED' AND detail->>'pack_id'=response->'body'->>'id') THEN RAISE EXCEPTION 'pack request lacks audit' USING ERRCODE='23514';END IF;
 END IF;RETURN response;
END $$;
CREATE FUNCTION lv.pack_export_request(tok bytea,pid uuid,key text,src text,req text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;w lv.native_export_job;new_id uuid:=uuidv7();BEGIN
 s:=lv.export_session(tok,src,req);
 IF key IS NULL OR key!~'^[!-~]{1,64}$' THEN RETURN jsonb_build_object('status',422,'code','SCHEMA_INVALID');END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.proof_pack WHERE tenant_id=s.tenant_id AND id=pid) THEN RETURN jsonb_build_object('status',404,'code','NOT_FOUND');END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.proof_pack WHERE tenant_id=s.tenant_id AND id=pid AND state='ANCHORED') THEN RETURN jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION');END IF;
 INSERT INTO lv.native_export_job(tenant_id,id,proof_pack_id,requested_by,idempotency_key,format) VALUES(s.tenant_id,new_id,pid,s.user_id,key,'BUNDLE') ON CONFLICT(tenant_id,requested_by,idempotency_key) DO NOTHING;
 SELECT * INTO w FROM lv.native_export_job WHERE tenant_id=s.tenant_id AND requested_by=s.user_id AND idempotency_key=key;
 IF w.proof_pack_id IS DISTINCT FROM pid OR w.format<>'BUNDLE' THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED');END IF;
 IF w.id=new_id THEN PERFORM lv_identity.audit_append('NATIVE_EXPORT_REQUESTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('job_id',w.id::text,'pack_id',pid::text));END IF;
 RETURN jsonb_build_object('status',202,'body',jsonb_build_object('id',w.id::text,'kind','PROOF_PACK_EXPORT','state',w.state,'proof_pack_id',pid::text));
END $$;
REVOKE ALL ON FUNCTION lv.pack_request(bytea,uuid,integer,text,text,text),lv.pack_export_request(bytea,uuid,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.pack_request(bytea,uuid,integer,text,text,text),lv.pack_export_request(bytea,uuid,text,text,text) TO lv_app;

CREATE OR REPLACE FUNCTION lv.native_export_api(tok bytea,action text,subject uuid,key text,src text,req text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;w lv.native_export_job;new_id uuid:=uuidv7();BEGIN
 s:=lv.export_session(tok,src,req);
 IF action='CREATE' THEN
  IF key IS NULL OR key!~'^[!-~]{1,64}$' THEN RETURN jsonb_build_object('status',422,'code','SCHEMA_INVALID');END IF;
  IF NOT EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=s.tenant_id AND id=subject AND state='RECONCILED') THEN RETURN jsonb_build_object('status',404,'code','NOT_FOUND');END IF;
  INSERT INTO lv.native_export_job(tenant_id,id,execution_id,requested_by,idempotency_key,format) VALUES(s.tenant_id,new_id,subject,s.user_id,key,'JSON') ON CONFLICT(tenant_id,requested_by,idempotency_key) DO NOTHING;
  SELECT * INTO w FROM lv.native_export_job WHERE tenant_id=s.tenant_id AND requested_by=s.user_id AND idempotency_key=key;
  IF w.execution_id IS DISTINCT FROM subject THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED');END IF;
  IF w.id=new_id THEN PERFORM lv_identity.audit_append('NATIVE_EXPORT_REQUESTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('release_id',new_id::text,'job_id',w.id::text,'execution_id',subject::text));END IF;
 ELSE
  SELECT * INTO w FROM lv.native_export_job WHERE tenant_id=s.tenant_id AND id=subject AND requested_by=s.user_id;
  IF NOT FOUND THEN RETURN jsonb_build_object('status',404,'code','NOT_FOUND');END IF;
 END IF;
 IF action IN ('LINK','DOWNLOAD') THEN
  IF w.state<>'DONE' THEN RETURN jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION');END IF;
  IF encode(sha256(w.artifact),'hex')<>w.content_hash THEN RAISE EXCEPTION 'stored export digest mismatch' USING ERRCODE='23514';END IF;
  PERFORM lv_identity.audit_append(CASE WHEN action='LINK' THEN 'NATIVE_EXPORT_LINK_ISSUED' ELSE 'NATIVE_EXPORT_DOWNLOADED' END,'ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('release_id',new_id::text,'job_id',w.id::text,'execution_id',w.execution_id::text,'content_hash',w.content_hash,'bytes',octet_length(w.artifact)::text));
  IF NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND session_id=s.id AND detail->>'release_id'=new_id::text AND detail->>'content_hash'=w.content_hash) THEN RAISE EXCEPTION 'export release lacks audit' USING ERRCODE='23514';END IF;
 END IF;
 IF action NOT IN ('CREATE','STATUS','LINK','DOWNLOAD') THEN RAISE EXCEPTION 'invalid export action' USING ERRCODE='22023';END IF;
 RETURN jsonb_build_object('status',CASE WHEN action='CREATE' THEN 202 ELSE 200 END,'body',jsonb_build_object('id',w.id::text,'kind',CASE WHEN w.proof_pack_id IS NULL THEN 'NATIVE_PROOF_EXPORT' ELSE 'PROOF_PACK_EXPORT' END,'proof_pack_id',w.proof_pack_id::text,'format',w.format,'state',w.state,'execution_id',w.execution_id::text,'content_hash',w.content_hash,'bytes',octet_length(w.artifact),'attempts',w.attempts,'last_error',w.last_error),'authorization_id',new_id::text,'artifact_hex',CASE WHEN action='DOWNLOAD' THEN encode(w.artifact,'hex') ELSE NULL END);
END $$;
CREATE OR REPLACE FUNCTION lv.native_export_snapshot(t uuid,j uuid,worker text,f bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.native_export_job;e lv.native_execution;l lv.ledger;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.native_export_job WHERE tenant_id=t AND id=j;
 IF w.id IS NULL OR w.state<>'CLAIMED' OR w.fence IS DISTINCT FROM f OR w.lease_owner IS DISTINCT FROM worker OR w.lease_expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'lost export lease' USING ERRCODE='40001';END IF;
 IF NOT lv.proposal_may(t,w.requested_by,'evidence.export') OR NOT EXISTS(SELECT 1 FROM lv_identity.app_user u JOIN lv_identity.membership m ON m.user_id=u.id WHERE u.id=w.requested_by AND u.disabled_at IS NULL AND m.tenant_id=t AND m.status='ACTIVE') THEN RAISE EXCEPTION 'export requester revoked' USING ERRCODE='42501';END IF;
 IF w.proof_pack_id IS NOT NULL THEN RETURN jsonb_build_object('bundle_hex',(SELECT encode(bundle,'hex') FROM lv.proof_pack WHERE tenant_id=t AND id=w.proof_pack_id AND state='ANCHORED'));END IF;
 SELECT * INTO STRICT e FROM lv.native_execution WHERE tenant_id=t AND id=w.execution_id AND state='RECONCILED';SELECT * INTO STRICT l FROM lv.ledger WHERE tenant_id=t AND id=e.ledger_id;
 IF (SELECT coalesce(sum(octet_length(financial_fields::text)+octet_length(original_request::text)+octet_length(informational_fields::text)),0) FROM lv.document_version WHERE tenant_id=t AND ledger_id=e.ledger_id AND (seq=e.commit_seq OR (document_id,version) IN (SELECT document_id,previous_version FROM lv.document_version WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq)))>4194304 OR e.commit_seq>2000 OR (SELECT octet_length(input_canonical)+octet_length(outcome_canonical) FROM lv.simulation_run WHERE tenant_id=t AND id=e.simulation_id)>4194304 OR (SELECT coalesce(sum(octet_length(b.canonical_content)+octet_length(h.canonical_content)),0) FROM lv.commit_batch b JOIN lv.ledger_event h USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=t AND b.ledger_id=e.ledger_id AND b.seq<=e.commit_seq)>4194304 THEN RAISE EXCEPTION 'export bound reached' USING ERRCODE='54000';END IF;
 RETURN jsonb_build_object('scope',jsonb_build_object('tenant_id',t::text,'ledger_id',e.ledger_id::text,'proposal_id',e.proposal_id::text,'revision',e.revision,'execution_id',e.id::text,'simulation_id',e.simulation_id::text,'commit_seq',e.commit_seq::text,'currency',btrim(l.currency),'exponent',l.currency_exponent),'artifacts',jsonb_build_object(
'revision.json',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'proposal_id',x.proposal_id,'revision',x.revision,'previous_revision',x.previous_revision,'change',x.change,'authored_by',x.authored_by,'target_adapter',x.target_adapter,'operations',x.operations,'evidence_refs',x.evidence_refs,'rationale',x.rationale,'content_hash',x.content_hash) AS v FROM lv.proposal_revision x WHERE x.tenant_id=t AND x.proposal_id=e.proposal_id AND x.revision=e.revision LIMIT 2) r),
'approval.json',jsonb_build_object('decision',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'kind',x.kind,'simulation_id',x.simulation_id,'check_id',x.check_id,'submitted_by',x.submitted_by,'approved_by',x.approved_by,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'comment',x.comment) AS v FROM lv.proposal_decision x WHERE x.tenant_id=t AND x.id=e.approval_id LIMIT 2) r),'check',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'simulation_id',x.simulation_id,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'checked_by',x.checked_by,'report',x.report,'review_granted_at',to_char(x.review_granted_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'checked_at',to_char(x.checked_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'decision_action',x.decision_action) AS v FROM lv.approval_check x WHERE x.tenant_id=t AND x.id=(SELECT check_id FROM lv.proposal_decision WHERE tenant_id=t AND id=e.approval_id) LIMIT 2) r),'overrides',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'simulation_id',x.simulation_id,'policy_id',x.policy_id,'policy_version',x.policy_version,'reason',x.reason,'overridden_by',x.overridden_by,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'freshness',x.freshness) AS v FROM lv.proposal_override x WHERE x.tenant_id=t AND x.proposal_id=e.proposal_id AND x.revision=e.revision AND x.simulation_id=e.simulation_id ORDER BY x.policy_id LIMIT 2) r)),
'simulation.json',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'status',x.status,'simulator_version',x.simulator_version,'snapshot_seq',x.snapshot_seq::text,'input_canonical',x.input_canonical,'input_hash',x.input_hash,'outcome_canonical',x.outcome_canonical,'result_hash',x.result_hash,'created_by',x.created_by) AS v FROM lv.simulation_run x WHERE x.tenant_id=t AND x.id=e.simulation_id LIMIT 2) r),
'execution.json',jsonb_build_object('receipt',lv.native_execution_json(e),'approval_id',e.approval_id::text,'check',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'simulation_id',x.simulation_id,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'checked_by',x.checked_by,'report',x.report) AS v FROM lv.native_execution_check x WHERE x.tenant_id=t AND x.id=e.check_id LIMIT 2) r),'transition',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'proposal_id',x.proposal_id,'revision',x.revision,'version',x.version,'phase',x.phase,'from_state',x.from_state,'to_state',x.to_state,'user_id',x.user_id,'action',x.action) AS v FROM lv.proposal_transition x WHERE x.tenant_id=t AND x.proposal_id=e.proposal_id AND x.revision=e.revision AND x.version=(lv.native_execution_json(e)->>'resulting_version')::integer AND x.action='execute' AND x.phase=0 LIMIT 2) r),'identity',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'revision',x.revision,'version',x.version,'state',x.state,'created_by',x.created_by,'submitted_by',x.submitted_by) AS v FROM lv.proposal x WHERE x.tenant_id=t AND x.id=e.proposal_id LIMIT 2) r),'editors',(SELECT jsonb_agg(v ORDER BY v) FROM (SELECT DISTINCT authored_by::text v FROM lv.proposal_revision WHERE tenant_id=t AND proposal_id=e.proposal_id AND revision<=e.revision) editors)),
'documents.json',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'document_id',x.document_id,'version',x.version,'id',x.id,'previous_version',x.previous_version,'seq',x.seq::text,'ordinal',x.ordinal,'financial_fields',x.financial_fields,'financial_fingerprint',x.financial_fingerprint,'informational_fields',x.informational_fields,'original_request',x.original_request,'created_by',x.created_by,'created_by_user',x.created_by_user,'reason',x.reason,'origin',x.origin,'provenance',x.provenance) AS v FROM lv.document_version x WHERE x.tenant_id=t AND x.ledger_id=e.ledger_id AND (x.seq=e.commit_seq OR (x.document_id,x.version) IN (SELECT document_id,previous_version FROM lv.document_version WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq)) ORDER BY x.document_id,x.version LIMIT 2001) r),
'journal.json',jsonb_build_object('entry',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'entry_no',x.entry_no::text,'seq',x.seq::text,'kind',x.kind,'effective_date',x.effective_date,'period_id',x.period_id,'currency',x.currency,'currency_exponent',x.currency_exponent,'source_document_id',x.source_document_id,'source_document_version',x.source_document_version,'posting_rule_id',x.posting_rule_id,'posting_rule_version',x.posting_rule_version,'provenance',x.provenance,'created_by',x.created_by,'created_by_user',x.created_by_user,'description',x.description) AS v FROM lv.journal_entry x WHERE x.tenant_id=t AND x.id=(lv.native_execution_json(e)->>'entry_id')::uuid LIMIT 2) r),'lines',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'entry_id',x.entry_id,'line_no',x.line_no,'seq',x.seq::text,'account_id',x.account_id,'requires_party',x.requires_party,'party_id',x.party_id,'document_id',x.document_id,'side',x.side,'amount',x.amount::text,'currency',x.currency,'currency_exponent',x.currency_exponent) AS v FROM lv.journal_line x WHERE x.tenant_id=t AND x.entry_id=(lv.native_execution_json(e)->>'entry_id')::uuid ORDER BY x.line_no LIMIT 4) r)),
'inclusions.json',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'settlement_document_id',x.settlement_document_id,'settlement_version',x.settlement_version,'line_no',x.line_no,'payment_document_id',x.payment_document_id,'payment_version_before',x.payment_version_before,'payment_version_after',x.payment_version_after,'kind',x.kind,'amount',x.amount::text,'seq',x.seq::text) AS v FROM lv.settlement_inclusion x WHERE x.tenant_id=t AND x.settlement_document_id=(lv.native_execution_json(e)->>'settlement_document_id')::uuid AND x.settlement_version=1 ORDER BY x.line_no LIMIT 1001) r),
'history.json',(SELECT jsonb_agg(jsonb_build_object('seq',b.seq::text,'batch_canonical',b.canonical_content,'batch_previous_hash',b.previous_hash,'batch_hash',b.hash,'event_canonical',h.canonical_content,'event_previous_hash',h.previous_hash,'event_hash',h.hash) ORDER BY b.seq) FROM lv.commit_batch b JOIN lv.ledger_event h USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=t AND b.ledger_id=e.ledger_id AND b.seq<=e.commit_seq)));END $$;
CREATE OR REPLACE FUNCTION lv.native_export_finish(t uuid,j uuid,worker text,f bigint,data bytea,error text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.native_export_job;b jsonb;BEGIN PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.native_export_job WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT FOUND OR w.state<>'CLAIMED' OR w.fence IS DISTINCT FROM f OR w.lease_owner IS DISTINCT FROM worker OR w.lease_expires_at<=clock_timestamp() THEN RETURN false;END IF;
 IF error IS NOT NULL THEN
  IF data IS NOT NULL THEN RAISE EXCEPTION 'conflicting completion' USING ERRCODE='22023';END IF;
  IF error NOT IN ('RETRYABLE','TERMINAL','REVOKED') THEN RAISE EXCEPTION 'invalid error' USING ERRCODE='22023';END IF;
  UPDATE lv.native_export_job SET state=CASE WHEN error='REVOKED' THEN 'DENIED' WHEN attempts>=3 OR error='TERMINAL' THEN 'DEAD' ELSE 'QUEUED' END,fence=fence+1,lease_owner=NULL,lease_expires_at=NULL,dispatched_at=NULL,available_at=clock_timestamp()+make_interval(secs=>least(60,power(2,attempts)::integer)),last_error=error WHERE tenant_id=t AND id=j RETURNING * INTO w;
  IF w.state IN ('DEAD','DENIED') THEN PERFORM lv_identity.audit_append('NATIVE_EXPORT_FAILED','DENIED',w.requested_by,t,NULL,'export-worker',j::text,jsonb_build_object('job_id',j::text,'error_class',error));END IF;RETURN true;
 END IF;
 IF data IS NULL OR octet_length(data) NOT BETWEEN 1 AND 16777216 THEN RAISE EXCEPTION 'invalid export size' USING ERRCODE='23514';END IF;
 IF w.proof_pack_id IS NOT NULL THEN
  IF data IS DISTINCT FROM (SELECT bundle FROM lv.proof_pack WHERE tenant_id=t AND id=w.proof_pack_id AND state='ANCHORED') THEN RAISE EXCEPTION 'export differs from sealed bundle' USING ERRCODE='23514';END IF;
 ELSE
 b:=convert_from(data,'UTF8')::jsonb;
 IF (b->>'format') IS DISTINCT FROM 'ledgervigil.native-execution-proof/1' OR (b->'scope'->>'tenant_id') IS DISTINCT FROM t::text OR (b->'scope'->>'execution_id') IS DISTINCT FROM w.execution_id::text THEN RAISE EXCEPTION 'invalid export scope' USING ERRCODE='23514';END IF;
 END IF;
 UPDATE lv.native_export_job SET state='DONE',artifact=data,content_hash=encode(sha256(data),'hex'),lease_expires_at=NULL,finished_at=clock_timestamp() WHERE tenant_id=t AND id=j;
 PERFORM lv_identity.audit_append('NATIVE_EXPORT_BUILT','ALLOWED',w.requested_by,t,NULL,'export-worker',j::text,jsonb_build_object('job_id',j::text,'execution_id',w.execution_id::text,'content_hash',encode(sha256(data),'hex'),'bytes',octet_length(data)::text));RETURN true;
END $$;
CREATE OR REPLACE FUNCTION lv.guard_native_export_job() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
 IF TG_OP='DELETE' OR OLD.state IN ('DONE','DEAD','DENIED') THEN RAISE EXCEPTION 'terminal export jobs are retained' USING ERRCODE='23514'; END IF;
 IF (NEW.tenant_id,NEW.id,NEW.execution_id,NEW.proof_pack_id,NEW.requested_by,NEW.idempotency_key,NEW.format,NEW.created_at) IS DISTINCT FROM (OLD.tenant_id,OLD.id,OLD.execution_id,OLD.proof_pack_id,OLD.requested_by,OLD.idempotency_key,OLD.format,OLD.created_at) THEN RAISE EXCEPTION 'export identity is fixed' USING ERRCODE='23514'; END IF;RETURN NEW;END $$;

CREATE FUNCTION lv.validate_proof_pack() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ BEGIN
 IF NEW.state IN ('SEALED','ANCHORED','ANCHOR_FAILED') AND (NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=NEW.tenant_id AND event_type='PROOF_PACK_SEALED' AND detail->>'pack_id'=NEW.id::text AND detail->>'content_hash'=NEW.content_hash) OR NOT EXISTS(SELECT 1 FROM lv.proof_pack_chain WHERE tenant_id=NEW.tenant_id AND seq>=NEW.chain_seq AND (seq<>NEW.chain_seq OR head=NEW.chain_hash))) THEN RAISE EXCEPTION 'sealed pack lacks chain or audit' USING ERRCODE='23514';END IF;
 IF NEW.state='ANCHORED' AND NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=NEW.tenant_id AND event_type='PROOF_PACK_ANCHORED' AND detail->>'pack_id'=NEW.id::text AND detail->>'anchor_commit'=NEW.anchor_commit) THEN RAISE EXCEPTION 'anchored pack lacks audit' USING ERRCODE='23514';END IF;RETURN NULL;END $$;
REVOKE ALL ON FUNCTION lv.validate_proof_pack() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER proof_pack_complete AFTER INSERT OR UPDATE ON lv.proof_pack DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_proof_pack();
CREATE FUNCTION lv.pack_chain_check(tok bytea,anchor_seq bigint,anchor_hash text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proof_pack;seq bigint:=0;prev text:='sha256:0000000000000000000000000000000000000000000000000000000000000000';BEGIN
 s:=lv.proposal_session(tok,'proof-verify','proof-verify');IF NOT lv.proposal_may(s.tenant_id,s.user_id,'read') THEN RETURN false;END IF;
 FOR p IN SELECT * FROM lv.proof_pack WHERE tenant_id=s.tenant_id AND chain_seq<=anchor_seq ORDER BY chain_seq LOOP
  seq:=seq+1;
  IF p.chain_seq<>seq OR p.previous_chain_head<>prev OR p.content_hash<>'sha256:'||encode(sha256(convert_to(p.content_canonical,'UTF8')),'hex') OR p.chain_hash<>'sha256:'||encode(sha256(convert_to(p.content_hash||prev,'UTF8')),'hex') THEN RETURN false;END IF;
  prev:=p.chain_hash;
 END LOOP;
 RETURN seq=anchor_seq AND prev=anchor_hash;
END $$;
REVOKE ALL ON FUNCTION lv.pack_chain_check(bytea,bigint,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.pack_chain_check(bytea,bigint,text) TO lv_app;

CREATE FUNCTION lv.pack_fail(t uuid,j uuid,w text,f bigint,reason text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ DECLARE p lv.proof_pack;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO p FROM lv.proof_pack WHERE tenant_id=t AND id=j FOR UPDATE;
 IF p.id IS NULL OR p.state NOT IN ('DRAFTING','SEALED') OR p.fence IS DISTINCT FROM f OR p.lease_owner IS DISTINCT FROM w OR p.lease_expires_at<=clock_timestamp() THEN RETURN;END IF;
 UPDATE lv.proof_pack SET lease_expires_at=NULL,dispatched_at=clock_timestamp(),last_error=left(reason,80),state=CASE WHEN attempts>=3 THEN CASE WHEN content_canonical IS NULL THEN 'DEAD' ELSE 'ANCHOR_FAILED' END ELSE state END WHERE tenant_id=t AND id=j;
 PERFORM lv_identity.audit_append('PROOF_PACK_BUILD_FAILED','DENIED',NULL,t,NULL,'proof-worker',j::text,jsonb_build_object('pack_id',j::text,'error_class',left(reason,80)));END $$;
REVOKE ALL ON FUNCTION lv.pack_fail(uuid,uuid,text,bigint,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.pack_fail(uuid,uuid,text,bigint,text) TO lv_worker;
CREATE FUNCTION lv.check_pack_prefix(t uuid,anchor_seq bigint,anchor_hash text) RETURNS boolean LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proof_pack;seq bigint:=0;prev text:='sha256:0000000000000000000000000000000000000000000000000000000000000000';c jsonb;e jsonb;facts jsonb;lid uuid;n bigint;bh text;eh text;actual_bh text;actual_eh text;BEGIN
 IF anchor_seq IS NULL OR anchor_seq<1 OR anchor_hash IS NULL THEN RETURN false;END IF;
 FOR p IN SELECT * FROM lv.proof_pack WHERE tenant_id=t AND chain_seq<=anchor_seq ORDER BY chain_seq LOOP
  seq:=seq+1;
  IF p.chain_seq<>seq OR p.previous_chain_head<>prev OR p.content_hash<>'sha256:'||encode(sha256(convert_to(p.content_canonical,'UTF8')),'hex') OR p.chain_hash<>'sha256:'||encode(sha256(convert_to(p.content_hash||prev,'UTF8')),'hex') THEN RETURN false;END IF;
  prev:=p.chain_hash;c:=p.content_canonical::jsonb;e:=c->'execution';facts:=(c->'simulation'->>'input_canonical')::jsonb->'facts';lid:=(c->'ledger'->>'id')::uuid;
  IF e IS NOT NULL AND e<>'null'::jsonb THEN n:=(e->'receipt'->>'commit_seq')::bigint;bh:=e->>'batch_hash';eh:=e->>'event_hash';ELSE n:=(c->'snapshot'->>'seq')::bigint;bh:=facts->>'batch_head';eh:=facts->>'event_head';END IF;
  IF n IS NULL OR (SELECT last_seq FROM lv.ledger_counter WHERE tenant_id=t AND ledger_id=lid)<n THEN RETURN false;END IF;
  IF n=0 THEN actual_bh:=repeat('0',64);actual_eh:=repeat('0',64);ELSE SELECT b.hash,h.hash INTO actual_bh,actual_eh FROM lv.commit_batch b JOIN lv.ledger_event h USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=t AND b.ledger_id=lid AND b.seq=n;END IF;
  IF (actual_bh,actual_eh) IS DISTINCT FROM (bh,eh) THEN RETURN false;END IF;
 END LOOP;RETURN seq=anchor_seq AND prev=anchor_hash;
END $$;
CREATE OR REPLACE FUNCTION lv.pack_chain_check(tok bytea,anchor_seq bigint,anchor_hash text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ DECLARE s lv_identity.session;BEGIN
 s:=lv.proposal_session(tok,'proof-verify','proof-verify');IF NOT lv.proposal_may(s.tenant_id,s.user_id,'read') THEN RETURN false;END IF;
 RETURN lv.check_pack_prefix(s.tenant_id,anchor_seq,anchor_hash);END $$;
CREATE FUNCTION lv.pack_anchor_preflight(t uuid,anchor_seq bigint,anchor_hash text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ BEGIN PERFORM lv.maintenance_context(t);RETURN lv.check_pack_prefix(t,anchor_seq,anchor_hash);END $$;
REVOKE ALL ON FUNCTION lv.check_pack_prefix(uuid,bigint,text),lv.pack_anchor_preflight(uuid,bigint,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.pack_anchor_preflight(uuid,bigint,text) TO lv_worker;

CREATE FUNCTION lv.pack_status(tok bytea,pid uuid,src text,req text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ BEGIN RETURN lv.pack_api(tok,'STATUS',pid,NULL,src,req);END $$;
REVOKE ALL ON FUNCTION lv.pack_status(bytea,uuid,text,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.pack_status(bytea,uuid,text,text) TO lv_app;
