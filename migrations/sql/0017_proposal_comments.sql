CREATE TABLE lv.proposal_comment (
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,proposal_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),
 position bigint NOT NULL CHECK(position>0),revision integer NOT NULL,proposal_version integer NOT NULL CHECK(proposal_version>0),
 authored_by uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 body text NOT NULL CHECK(length(body) BETWEEN 1 AND 4000 AND body~'[^[:space:]]'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 idempotency_key text NOT NULL,request_hash text NOT NULL CHECK(request_hash~'^[0-9a-f]{64}$'),request_id text NOT NULL,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,proposal_id,position),
 FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision)
);
ALTER TABLE lv.proposal_comment ENABLE ROW LEVEL SECURITY;
ALTER TABLE lv.proposal_comment FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.proposal_comment
 USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
 WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proposal_comment FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
REVOKE ALL ON lv.proposal_comment FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
GRANT SELECT ON lv.proposal_comment TO lv_app;

CREATE FUNCTION lv.comment_json(c lv.proposal_comment) RETURNS jsonb
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',c.id::text,'proposal_id',c.proposal_id::text,'ledger_id',c.ledger_id::text,
  'position',c.position,'revision',c.revision,'proposal_version',c.proposal_version,'authored_by',c.authored_by::text,
  'body',c.body,'created_at',to_char(c.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
$$;
REVOKE ALL ON FUNCTION lv.comment_json(lv.proposal_comment) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.comment_json(lv.proposal_comment) TO lv_app;

CREATE FUNCTION lv.add_proposal_comment(tok bytea,pid uuid,ikey text,rhash text,p_body text,p_match text,p_problem jsonb,src text,req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;c lv.proposal_comment;prior lv.api_idempotency;response jsonb;v_problem jsonb;
 route_name text:='/api/v1/proposals/{proposal_id}/comments';n bigint;
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT (lv.proposal_may(s.tenant_id,s.user_id,'propose') OR lv.proposal_may(s.tenant_id,s.user_id,'review')) THEN
  RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires propose or review'); END IF;
 IF ikey IS NULL OR ikey!~'^[!-~]{1,64}$' OR rhash IS NULL OR rhash!~'^[0-9a-f]{64}$' THEN
  RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-comment:'||s.tenant_id::text||':'||s.user_id::text||':'||ikey,0)) THEN
  RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1); END IF;
 -- Touch session after the nonblocking key lock, then lock live identity through commit.
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT (lv.proposal_may(s.tenant_id,s.user_id,'propose') OR lv.proposal_may(s.tenant_id,s.user_id,'review')) THEN
  RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','requires propose or review'); END IF;
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
  IF prior.request_hash<>rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
  RETURN prior.response||jsonb_build_object('replayed',true);
 END IF;
 -- The row lock serializes comment positions and pins the revision being discussed; it does not update the proposal.
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid FOR UPDATE;
 IF NOT FOUND THEN v_problem:=jsonb_build_object('status',404,'code','NOT_FOUND','detail','proposal not found');
 ELSIF p_problem IS NOT NULL THEN v_problem:=p_problem;
 ELSIF p_body IS NULL OR length(p_body) NOT BETWEEN 1 AND 4000 OR p_body!~'[^[:space:]]' THEN
  v_problem:=jsonb_build_object('status',422,'code','SCHEMA_INVALID','detail','body must contain 1–4000 characters and non-whitespace text');
 ELSIF p_match IS NOT NULL AND p_match<>'"v'||p.version::text||'"' THEN
  v_problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','detail','proposal changed; reload before commenting','current_version',p.version);
 END IF;
 IF v_problem IS NOT NULL THEN
  IF (v_problem->>'status')::integer NOT BETWEEN 400 AND 499 THEN RAISE EXCEPTION 'invalid comment refusal' USING ERRCODE='22023'; END IF;
  response:=v_problem;
  PERFORM lv_identity.audit_append('PROPOSAL_COMMENT_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',pid::text,'reason',v_problem->>'code'));
 ELSE
  SELECT coalesce(max(position),0)+1 INTO n FROM lv.proposal_comment WHERE tenant_id=p.tenant_id AND proposal_id=p.id;
  INSERT INTO lv.proposal_comment(tenant_id,ledger_id,proposal_id,position,revision,proposal_version,authored_by,session_id,body,idempotency_key,request_hash,request_id)
   VALUES(p.tenant_id,p.ledger_id,p.id,n,p.revision,p.version,s.user_id,s.id,p_body,ikey,rhash,req) RETURNING * INTO c;
  PERFORM lv_identity.audit_append('PROPOSAL_COMMENTED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,
   jsonb_build_object('comment_id',c.id::text,'proposal_id',p.id::text,'revision',p.revision::text,'position',n::text,'body_hash',encode(sha256(convert_to(c.body,'UTF8')),'hex')));
  response:=jsonb_build_object('status',201,'body',lv.comment_json(c));
 END IF;
 DELETE FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=ikey;
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at)
 VALUES(s.tenant_id,s.user_id,'POST',route_name,ikey,rhash,(response->>'status')::smallint,response,transaction_timestamp()+interval '24 hours');
 RETURN response||jsonb_build_object('replayed',false);
END $$;
REVOKE ALL ON FUNCTION lv.add_proposal_comment(bytea,uuid,text,text,text,text,jsonb,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.add_proposal_comment(bytea,uuid,text,text,text,text,jsonb,text,text) TO lv_app;

CREATE FUNCTION lv.validate_proposal_comment() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF NEW.tx_id<>pg_current_xact_id()
 OR NOT EXISTS(SELECT 1 FROM lv.proposal p WHERE p.tenant_id=NEW.tenant_id AND p.id=NEW.proposal_id AND p.revision=NEW.revision AND p.version=NEW.proposal_version)
 OR (NEW.position>1 AND NOT EXISTS(SELECT 1 FROM lv.proposal_comment c WHERE c.tenant_id=NEW.tenant_id AND c.proposal_id=NEW.proposal_id AND c.position=NEW.position-1))
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event a WHERE a.tenant_id=NEW.tenant_id AND a.user_id=NEW.authored_by AND a.session_id=NEW.session_id
  AND a.event_type='PROPOSAL_COMMENTED' AND a.outcome='ALLOWED' AND a.request_id=NEW.request_id
  AND a.detail->>'comment_id'=NEW.id::text AND a.detail->>'proposal_id'=NEW.proposal_id::text AND a.detail->>'revision'=NEW.revision::text
  AND a.detail->>'position'=NEW.position::text AND a.detail->>'body_hash'=encode(sha256(convert_to(NEW.body,'UTF8')),'hex'))
 OR NOT EXISTS(SELECT 1 FROM lv.api_idempotency i WHERE i.tenant_id=NEW.tenant_id AND i.principal_id=NEW.authored_by AND i.method='POST'
  AND i.route='/api/v1/proposals/{proposal_id}/comments' AND i.idempotency_key=NEW.idempotency_key AND i.request_hash=NEW.request_hash
  AND i.response_status=201 AND i.response->'body'=lv.comment_json(NEW))
 THEN RAISE EXCEPTION 'proposal comment lacks atomic revision/audit/cache' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER comment_complete AFTER INSERT ON lv.proposal_comment DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_proposal_comment();
REVOKE ALL ON FUNCTION lv.validate_proposal_comment() FROM PUBLIC;
