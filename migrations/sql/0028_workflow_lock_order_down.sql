CREATE OR REPLACE FUNCTION lv.add_proposal_comment(tok bytea, pid uuid, ikey text, rhash text, p_body text, p_match text, p_problem jsonb, src text, req text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
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
END $function$;
CREATE OR REPLACE FUNCTION lv.approve_native(tok bytea, pid uuid, p_match text, ikey text, rhash text, requested jsonb, p_problem jsonb, src text, req text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE s lv_identity.session;p lv.proposal;l lv.ledger;r lv.simulation_run;prior lv.api_idempotency;problem jsonb:=p_problem;response jsonb;f jsonb;blocker text;grant_time timestamptz;check_id uuid:=uuidv7();before_version integer;previous lv.proposal_state;
 route_name text:='/api/v1/proposals/{proposal_id}/approve';
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 IF ikey IS NULL OR ikey!~'^[!-~]{1,64}$' OR rhash IS NULL OR rhash!~'^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-approve:'||s.tenant_id::text||':'||s.user_id::text||':'||ikey,0)) THEN RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1); END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid FOR UPDATE;
 IF p.id IS NOT NULL THEN
 blocker:=lv.review_blocker(p,s.user_id);
 IF blocker IS NOT NULL THEN RETURN jsonb_build_object('status',403,'code','SEGREGATION_OF_DUTIES','reason',blocker); END IF;
 END IF;
 SELECT * INTO prior FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
 IF prior.request_hash<>rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
 RETURN prior.response||jsonb_build_object('replayed',true); END IF;
 IF p.id IS NULL THEN problem:=jsonb_build_object('status',404,'code','NOT_FOUND');
 ELSIF problem IS NOT NULL THEN NULL;
 ELSIF p_match IS NULL THEN problem:=jsonb_build_object('status',428,'code','VERSION_REQUIRED');
 ELSIF requested IS NULL OR jsonb_typeof(requested)<>'object' OR NOT requested?'revision' OR (requested-ARRAY['revision','comment'])<>'{}'::jsonb OR jsonb_typeof(requested->'revision') IS DISTINCT FROM 'number' OR (requested->>'revision')!~'^[1-9][0-9]*$' OR (requested?'comment' AND jsonb_typeof(requested->'comment') NOT IN ('string','null')) OR length(requested->>'comment')>4000 THEN problem:=jsonb_build_object('status',422,'code','SCHEMA_INVALID');
 ELSIF requested->>'revision'<>p.revision::text THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','reason','REVISION_MISMATCH','current_version',p.version);
 ELSIF EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN problem:=jsonb_build_object('status',409,'code','ALREADY_DECIDED');
 ELSIF p_match<>'"v'||p.version::text||'"' THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','current_version',p.version);
 ELSIF p.state<>'READY_FOR_REVIEW' THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','state',p.state);
 END IF;
 IF problem IS NULL THEN
 PERFORM 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' OR r.revision<>p.revision OR r.proposal_id<>p.id THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE');
 ELSE
 f:=lv.proposal_freshness(p);
 IF f->>'status'='FRESH' AND f->'pinned_seq' IS DISTINCT FROM f->'observed_seq' THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE','reason','FULL_CAPTURE_CHANGED'); END IF;
 END IF;
 END IF;
 IF problem IS NULL THEN
 before_version:=p.version;previous:=p.state;
 SELECT max(mr.granted_at) INTO grant_time FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=s.user_id AND rp.permission='review';
 INSERT INTO lv.approval_check(tenant_id,ledger_id,id,proposal_id,revision,simulation_id,observed_version,resulting_version,checked_by,session_id,report,review_granted_at,comment,idempotency_key,request_hash,request_id)
 VALUES(p.tenant_id,p.ledger_id,check_id,p.id,p.revision,r.id,p.version,p.version+1,s.user_id,s.id,f,grant_time,requested->>'comment',ikey,rhash,req);
 IF f->>'status'='STALE' THEN
 UPDATE lv.proposal SET state='STALE',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 WITH released AS(UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id) SELECT tenant_id,ledger_id,staged_document_id,version,'RELEASED',p.id,p.revision,s.user_id,s.id FROM released;
 response:=jsonb_build_object('status',409,'code','PROPOSAL_STALE','reason','DEPENDENCIES_CHANGED','current_version',p.version);
 ELSE
 INSERT INTO lv.proposal_decision(tenant_id,ledger_id,proposal_id,revision,kind,simulation_id,check_id,submitted_by,approved_by,session_id,observed_version,resulting_version,comment)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,'APPROVED',r.id,check_id,p.created_by,s.user_id,s.id,p.version,p.version+1,requested->>'comment');
 UPDATE lv.proposal SET state='APPROVED',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 response:=jsonb_build_object('status',200,'etag','v'||p.version::text,'body',lv.proposal_json(p));
 END IF;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,previous,p.state,s.user_id,s.id,'approve',ikey,rhash);
 PERFORM lv_identity.audit_append(CASE WHEN p.state='APPROVED' THEN 'PROPOSAL_APPROVED' ELSE 'PROPOSAL_APPROVAL_STALE' END,CASE WHEN p.state='APPROVED' THEN 'ALLOWED' ELSE 'DENIED' END,s.user_id,p.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'check_id',check_id::text));
 ELSE
 PERFORM lv_identity.audit_append('PROPOSAL_APPROVAL_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',pid::text,'reason',problem->>'code'));response:=problem;
 END IF;
 DELETE FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at) VALUES(s.tenant_id,s.user_id,'POST',route_name,ikey,rhash,(response->>'status')::smallint,response,transaction_timestamp()+interval '24 hours');
 RETURN response||jsonb_build_object('replayed',false);
END $function$;
CREATE OR REPLACE FUNCTION lv.reject_native(tok bytea, pid uuid, p_match text, ikey text, rhash text, requested jsonb, p_problem jsonb, src text, req text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE s lv_identity.session;p lv.proposal;l lv.ledger;r lv.simulation_run;prior lv.api_idempotency;problem jsonb:=p_problem;response jsonb;f jsonb;blocker text;grant_time timestamptz;check_id uuid:=uuidv7();before_version integer;previous lv.proposal_state;
 route_name text:='/api/v1/proposals/{proposal_id}/reject';
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 IF ikey IS NULL OR ikey!~'^[!-~]{1,64}$' OR rhash IS NULL OR rhash!~'^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-reject:'||s.tenant_id::text||':'||s.user_id::text||':'||ikey,0)) THEN RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1); END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid FOR UPDATE;
 IF p.id IS NOT NULL THEN
 blocker:=lv.review_blocker(p,s.user_id);
 IF blocker IS NOT NULL THEN RETURN jsonb_build_object('status',403,'code','SEGREGATION_OF_DUTIES','reason',blocker); END IF;
 END IF;
 SELECT * INTO prior FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
 IF prior.request_hash<>rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
 RETURN prior.response||jsonb_build_object('replayed',true); END IF;
 IF p.id IS NULL THEN problem:=jsonb_build_object('status',404,'code','NOT_FOUND');
 ELSIF problem IS NOT NULL THEN NULL;
 ELSIF p_match IS NULL THEN problem:=jsonb_build_object('status',428,'code','VERSION_REQUIRED');
 ELSIF requested IS NULL OR jsonb_typeof(requested)<>'object' OR NOT requested?'revision' OR NOT requested?'comment' OR (requested-ARRAY['revision','comment'])<>'{}'::jsonb OR jsonb_typeof(requested->'revision') IS DISTINCT FROM 'number' OR (requested->>'revision')!~'^[1-9][0-9]*$' OR jsonb_typeof(requested->'comment') IS DISTINCT FROM 'string' OR NOT lv.rejection_reason_valid(requested->>'comment') OR length(requested->>'comment')>4000 THEN problem:=jsonb_build_object('status',422,'code','SCHEMA_INVALID');
 ELSIF requested->>'revision'<>p.revision::text THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','reason','REVISION_MISMATCH','current_version',p.version);
 ELSIF EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN problem:=jsonb_build_object('status',409,'code','ALREADY_DECIDED');
 ELSIF p_match<>'"v'||p.version::text||'"' THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','current_version',p.version);
 ELSIF p.state<>'READY_FOR_REVIEW' THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','state',p.state);
 END IF;
 IF problem IS NULL THEN
 PERFORM 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' OR r.revision<>p.revision OR r.proposal_id<>p.id THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE');
 ELSE
 f:=lv.proposal_freshness(p);
 IF f->>'status'='FRESH' AND f->'pinned_seq' IS DISTINCT FROM f->'observed_seq' THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE','reason','FULL_CAPTURE_CHANGED'); END IF;
 END IF;
 END IF;
 IF problem IS NULL THEN
 before_version:=p.version;previous:=p.state;
 SELECT max(mr.granted_at) INTO grant_time FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=s.user_id AND rp.permission='review';
 INSERT INTO lv.approval_check(tenant_id,ledger_id,id,proposal_id,revision,simulation_id,observed_version,resulting_version,checked_by,session_id,report,review_granted_at,decision_action,comment,idempotency_key,request_hash,request_id)
 VALUES(p.tenant_id,p.ledger_id,check_id,p.id,p.revision,r.id,p.version,p.version+1,s.user_id,s.id,f,grant_time,'reject',requested->>'comment',ikey,rhash,req);
 IF f->>'status'='STALE' THEN
 UPDATE lv.proposal SET state='STALE',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 WITH released AS(UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id) SELECT tenant_id,ledger_id,staged_document_id,version,'RELEASED',p.id,p.revision,s.user_id,s.id FROM released;
 response:=jsonb_build_object('status',409,'code','PROPOSAL_STALE','reason','DEPENDENCIES_CHANGED','current_version',p.version);
 ELSE
 INSERT INTO lv.proposal_decision(tenant_id,ledger_id,proposal_id,revision,kind,simulation_id,check_id,submitted_by,approved_by,session_id,observed_version,resulting_version,comment)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,'REJECTED',r.id,check_id,p.created_by,s.user_id,s.id,p.version,p.version+1,requested->>'comment');
 UPDATE lv.proposal SET state='REJECTED',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
  WITH released AS(UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id) SELECT tenant_id,ledger_id,staged_document_id,version,'RELEASED',p.id,p.revision,s.user_id,s.id FROM released;
 response:=jsonb_build_object('status',200,'etag','v'||p.version::text,'body',lv.proposal_json(p));
 END IF;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,previous,p.state,s.user_id,s.id,'reject',ikey,rhash);
 PERFORM lv_identity.audit_append(CASE WHEN p.state='REJECTED' THEN 'PROPOSAL_REJECTED' ELSE 'PROPOSAL_REJECTION_STALE' END,CASE WHEN p.state='REJECTED' THEN 'ALLOWED' ELSE 'DENIED' END,s.user_id,p.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'check_id',check_id::text));
 ELSE
 PERFORM lv_identity.audit_append('PROPOSAL_REJECTION_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',pid::text,'reason',problem->>'code'));response:=problem;
 END IF;
 DELETE FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at) VALUES(s.tenant_id,s.user_id,'POST',route_name,ikey,rhash,(response->>'status')::smallint,response,transaction_timestamp()+interval '24 hours');
 RETURN response||jsonb_build_object('replayed',false);
END $function$;
CREATE OR REPLACE FUNCTION lv.execute_native(tok bytea, pid uuid, p_match text, ikey text, rhash text, requested jsonb, p_problem jsonb, src text, req text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
<<execute_native>>
DECLARE s lv_identity.session;p lv.proposal;l lv.ledger;r lv.simulation_run;prior lv.api_idempotency;problem jsonb:=p_problem;response jsonb;f jsonb;blocker text;grant_time timestamptz;check_id uuid:=uuidv7();before_version integer;previous lv.proposal_state;ex lv.native_execution;eid uuid:=uuidv7();posted jsonb;comparison jsonb;
 route_name text:='/api/v1/proposals/{proposal_id}/execute';
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'execute') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 IF ikey IS NULL OR ikey!~'^[!-~]{1,64}$' OR rhash IS NULL OR rhash!~'^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-execute:'||s.tenant_id::text||':'||s.user_id::text||':'||ikey,0)) THEN RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1); END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'execute') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid FOR UPDATE;
 IF p.id IS NOT NULL THEN
 blocker:=lv.executor_blocker(p,s.user_id);
 IF blocker IS NOT NULL THEN RETURN jsonb_build_object('status',403,'code','SEGREGATION_OF_DUTIES','reason',blocker); END IF;
 END IF;
 SELECT * INTO prior FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
 IF prior.request_hash<>rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
 RETURN prior.response||jsonb_build_object('replayed',true); END IF;
 IF p.id IS NULL THEN problem:=jsonb_build_object('status',404,'code','NOT_FOUND');
 ELSIF problem IS NOT NULL THEN NULL;
 ELSIF p_match IS NULL THEN problem:=jsonb_build_object('status',428,'code','VERSION_REQUIRED');
 ELSIF requested IS NULL OR jsonb_typeof(requested)<>'object' OR (requested-ARRAY['revision'])<>'{}'::jsonb OR jsonb_typeof(requested->'revision') IS DISTINCT FROM 'number' OR (requested->>'revision')!~'^[1-9][0-9]*$' THEN problem:=jsonb_build_object('status',422,'code','SCHEMA_INVALID');
 ELSIF requested->>'revision'<>p.revision::text THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','reason','REVISION_MISMATCH','current_version',p.version);
 ELSIF EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','reason','ALREADY_EXECUTED');
 ELSIF p_match<>'"v'||p.version::text||'"' THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','current_version',p.version);
 ELSIF p.state<>'APPROVED' THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','state',p.state);
 ELSIF NOT EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND kind='APPROVED' AND simulation_id=p.latest_simulation_id) THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','reason','APPROVAL_MISSING');

 END IF;
 IF problem IS NULL THEN
 PERFORM 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' OR r.revision<>p.revision OR r.proposal_id<>p.id THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE');
 ELSE
 f:=lv.proposal_freshness(p);
 IF f->>'status'='FRESH' AND f->'pinned_seq' IS DISTINCT FROM f->'observed_seq' THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE','reason','FULL_CAPTURE_CHANGED'); END IF;
 END IF;
 END IF;
 IF problem IS NULL THEN
 before_version:=p.version;previous:=p.state;
 SELECT max(mr.granted_at) INTO grant_time FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=s.user_id AND rp.permission='execute';
 INSERT INTO lv.native_execution_check(tenant_id,ledger_id,id,proposal_id,revision,simulation_id,observed_version,resulting_version,checked_by,session_id,report,idempotency_key,request_hash,request_id)
 VALUES(p.tenant_id,p.ledger_id,check_id,p.id,p.revision,r.id,p.version,p.version+1,s.user_id,s.id,f,ikey,rhash,req);
 IF f->>'status'='STALE' THEN
 UPDATE lv.proposal SET state='STALE',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 WITH released AS(UPDATE lv.staged_workflow SET status='VALID',version=version+1,proposal_id=NULL,revision=NULL WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id) SELECT tenant_id,ledger_id,staged_document_id,version,'RELEASED',p.id,p.revision,s.user_id,s.id FROM released;
 response:=jsonb_build_object('status',409,'code','PROPOSAL_STALE','current_version',p.version);
 ELSE
 INSERT INTO lv.native_execution(tenant_id,ledger_id,id,proposal_id,revision,simulation_id,check_id,approval_id,executed_by,session_id,observed_version,resulting_version,state)
 VALUES(p.tenant_id,p.ledger_id,eid,p.id,p.revision,r.id,check_id,(SELECT id FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND kind='APPROVED'),s.user_id,s.id,p.version,p.version+1,'BUILDING');
 posted:=lv.post_native_settlement(eid);
 SELECT * INTO ex FROM lv.native_execution WHERE tenant_id=p.tenant_id AND id=eid;
 comparison:=lv.native_posting_match(ex);
 UPDATE lv.native_execution SET state='RECONCILED',comparison=execute_native.comparison,predicted_hash=execute_native.comparison->>'predicted_hash',derived_hash=execute_native.comparison->>'derived_hash' WHERE tenant_id=p.tenant_id AND id=eid RETURNING * INTO ex;
 UPDATE lv.proposal SET state='EXECUTED',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 WITH completed AS(UPDATE lv.staged_workflow SET status='POSTED',version=version+1 WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND proposal_id=p.id AND revision=p.revision AND status='RESERVED' RETURNING *)
 INSERT INTO lv.reservation_event(tenant_id,ledger_id,staged_document_id,version,event,proposal_id,revision,user_id,session_id) SELECT tenant_id,ledger_id,staged_document_id,version,'POSTED',p.id,p.revision,s.user_id,s.id FROM completed;
 INSERT INTO lv.outbox(tenant_id,topic,dedupe_key,native_execution_id,native_payload,native_tx_id) VALUES(p.tenant_id,'execution','native-completed:'||ex.id::text,ex.id,lv.native_execution_json(ex),pg_current_xact_id());
 response:=jsonb_build_object('status',200,'etag','v'||p.version::text,'body',lv.native_execution_json(ex));
 END IF;

 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,previous,p.state,s.user_id,s.id,'execute',ikey,rhash);
 PERFORM lv_identity.audit_append(CASE WHEN p.state='EXECUTED' THEN 'PROPOSAL_EXECUTED' ELSE 'PROPOSAL_EXECUTION_STALE' END,CASE WHEN p.state='EXECUTED' THEN 'ALLOWED' ELSE 'DENIED' END,s.user_id,p.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'check_id',check_id::text));
 ELSE
 PERFORM lv_identity.audit_append('PROPOSAL_EXECUTION_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',pid::text,'reason',problem->>'code'));response:=problem;
 END IF;
 DELETE FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route=route_name AND idempotency_key=ikey;
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at) VALUES(s.tenant_id,s.user_id,'POST',route_name,ikey,rhash,(response->>'status')::smallint,response,transaction_timestamp()+interval '24 hours');
 RETURN response||jsonb_build_object('replayed',false);
END $function$;
CREATE OR REPLACE FUNCTION lv.override_evidence(tok bytea, pid uuid, p_match text, ikey text, rhash text, requested jsonb, p_problem jsonb, src text, req text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE s lv_identity.session;p lv.proposal;l lv.ledger;r lv.simulation_run;v lv.verdict;o lv.proposal_override;prior lv.api_idempotency;
 response jsonb;problem jsonb:=p_problem;report jsonb;route_name text:='/api/v1/proposals/{proposal_id}/overrides';before_version integer;reason_text text;
BEGIN
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') OR NOT EXISTS(SELECT 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND role='CONTROLLER') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED','detail','controller review permission required'); END IF;
 IF ikey IS NULL OR ikey!~'^[!-~]{1,64}$' OR rhash IS NULL OR rhash!~'^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status',400,'code','IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-override:'||s.tenant_id::text||':'||s.user_id::text||':'||ikey,0)) THEN RETURN jsonb_build_object('status',409,'code','REQUEST_IN_PROGRESS','retry_after',1); END IF;
 PERFORM lv_identity.session_context(tok,true);
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') OR NOT EXISTS(SELECT 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND role='CONTROLLER') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=ikey;
 IF FOUND AND prior.expires_at>statement_timestamp() THEN
 IF prior.request_hash<>rhash THEN RETURN jsonb_build_object('status',422,'code','IDEMPOTENCY_KEY_REUSED'); END IF;
 RETURN prior.response||jsonb_build_object('replayed',true); END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid FOR UPDATE;
 IF NOT FOUND THEN problem:=jsonb_build_object('status',404,'code','NOT_FOUND','detail','proposal not found');
 ELSIF p.created_by=s.user_id THEN problem:=jsonb_build_object('status',403,'code','SEGREGATION_OF_DUTIES','detail','the maker cannot override their proposal');
 ELSIF problem IS NOT NULL THEN NULL;
 ELSIF p_match IS NULL THEN problem:=jsonb_build_object('status',428,'code','VERSION_REQUIRED','detail','If-Match required');
 ELSIF p_match<>'"v'||p.version::text||'"' THEN problem:=jsonb_build_object('status',412,'code','VERSION_MISMATCH','current_version',p.version);
 ELSIF p.state<>'BLOCKED' THEN problem:=jsonb_build_object('status',409,'code','INVALID_STATE_TRANSITION','state',p.state);
 ELSIF requested IS NULL OR jsonb_typeof(requested)<>'object' OR (SELECT count(*) FROM jsonb_object_keys(requested))<>2 OR requested->'policy_ids' IS DISTINCT FROM '["P-EVD-01"]'::jsonb
 OR jsonb_typeof(requested->'reason') IS DISTINCT FROM 'string' OR length(requested->>'reason') NOT BETWEEN 1 AND 4000 OR (requested->>'reason')!~'[^[:space:]]' THEN
 problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE','detail','only the native P-EVD-01 exception with a written reason is supported');
 END IF;
 IF problem IS NULL THEN
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id AND proposal_id=p.id AND revision=p.revision;
 SELECT * INTO v FROM lv.verdict WHERE tenant_id=p.tenant_id AND simulation_id=r.id AND policy_id='P-EVD-01';
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.id IS NULL OR r.simulator_version<>'lv-native-recordsettlement/0.3.0' THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE');
 ELSIF EXISTS(SELECT 1 FROM lv.verdict WHERE tenant_id=p.tenant_id AND simulation_id=r.id AND body->>'outcome'='FAIL') THEN problem:=jsonb_build_object('status',422,'code','PRECONDITION_FAILED','reason','FAIL_CANNOT_BE_OVERRIDDEN');
 ELSIF v.policy_id IS NULL OR v.body->>'outcome'<>'INSUFFICIENT_EVIDENCE' OR EXISTS(SELECT 1 FROM lv.verdict WHERE tenant_id=p.tenant_id AND simulation_id=r.id AND body->>'outcome'='INSUFFICIENT_EVIDENCE' AND policy_id<>'P-EVD-01') THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE','detail','only a sole P-EVD-01 insufficiency is supported');
 ELSE
 report:=lv.proposal_freshness(p);
 IF report->>'status'<>'FRESH' THEN problem:=jsonb_build_object('status',409,'code','PROPOSAL_STALE','detail','the maker must revalidate the changed dependencies before an override');
 ELSIF report->'pinned_seq' IS DISTINCT FROM report->'observed_seq' THEN problem:=jsonb_build_object('status',422,'code','CAPABILITY_UNAVAILABLE','reason','FULL_CAPTURE_CHANGED','detail','this bounded override requires the original full-ledger capture; no global stale transition is inferred');
 END IF;
 END IF;
 END IF;
 IF problem IS NULL THEN
 before_version:=p.version;reason_text:=requested->>'reason';
 INSERT INTO lv.proposal_override(tenant_id,ledger_id,proposal_id,revision,simulation_id,policy_id,policy_version,reason,overridden_by,session_id,observed_version,resulting_version,freshness,idempotency_key,request_hash,request_id)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,r.id,'P-EVD-01',v.body->>'policy_version',reason_text,s.user_id,s.id,p.version,p.version+1,report,ikey,rhash,req) RETURNING * INTO o;
 UPDATE lv.proposal SET state='READY_FOR_REVIEW',version=version+1,updated_at=clock_timestamp() WHERE tenant_id=p.tenant_id AND id=p.id RETURNING * INTO p;
 INSERT INTO lv.proposal_transition(tenant_id,ledger_id,proposal_id,revision,version,phase,from_state,to_state,user_id,session_id,action,idempotency_key,request_hash)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.version,0,'BLOCKED','READY_FOR_REVIEW',s.user_id,s.id,'overrides',ikey,rhash);
 PERFORM lv_identity.audit_append('PROPOSAL_OVERRIDDEN','ALLOWED',s.user_id,p.tenant_id,s.id,src,req,jsonb_build_object('override_id',o.id::text,'proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'policy_id','P-EVD-01','reason_hash',encode(sha256(convert_to(reason_text,'UTF8')),'hex')));
 response:=jsonb_build_object('status',200,'etag','v'||p.version::text,'body',lv.proposal_json(p));
 ELSE
 IF (problem->>'status')::integer NOT BETWEEN 400 AND 499 THEN RAISE EXCEPTION 'invalid override refusal' USING ERRCODE='22023'; END IF;
 PERFORM lv_identity.audit_append('PROPOSAL_OVERRIDE_REFUSED','DENIED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',pid::text,'reason',problem->>'code'));response:=problem;
 END IF;
 DELETE FROM lv.api_idempotency i WHERE i.tenant_id=s.tenant_id AND i.principal_id=s.user_id AND i.method='POST' AND i.route=route_name AND i.idempotency_key=ikey;
 INSERT INTO lv.api_idempotency(tenant_id,principal_id,method,route,idempotency_key,request_hash,response_status,response,expires_at)
 VALUES(s.tenant_id,s.user_id,'POST',route_name,ikey,rhash,(response->>'status')::smallint,response,transaction_timestamp()+interval '24 hours');
 RETURN response||jsonb_build_object('replayed',false);
END $function$;
CREATE OR REPLACE FUNCTION lv.register_source_plan(tok bytea, lid uuid, manifest text, ikey text, request_hash text, src text, req text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
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
END $function$;