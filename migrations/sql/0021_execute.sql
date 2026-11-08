CREATE FUNCTION lv.execute_native(tok bytea,pid uuid,p_match text,ikey text,rhash text,requested jsonb,p_problem jsonb,src text,req text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
END $$;
REVOKE ALL ON FUNCTION lv.execute_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.execute_native(bytea,uuid,text,text,text,jsonb,jsonb,text,text) TO lv_app;
