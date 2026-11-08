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
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv_identity.session WHERE id=s.id FOR UPDATE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,'review') OR NOT EXISTS(SELECT 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND role='CONTROLLER') THEN RETURN jsonb_build_object('status',403,'code','PERMISSION_DENIED'); END IF;
 PERFORM lv_identity.session_context(tok,true);
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

CREATE OR REPLACE FUNCTION lv.validate_evidence_override()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p lv.proposal;v lv.verdict;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id;
 SELECT * INTO v FROM lv.verdict WHERE tenant_id=NEW.tenant_id AND simulation_id=NEW.simulation_id AND policy_id=NEW.policy_id;
 IF NEW.tx_id<>pg_current_xact_id() OR p.state<>'READY_FOR_REVIEW' OR p.revision<>NEW.revision OR p.version<>NEW.resulting_version OR p.latest_simulation_id<>NEW.simulation_id
 OR NEW.overridden_by=p.created_by OR v.body->>'outcome'<>'INSUFFICIENT_EVIDENCE' OR v.body->>'policy_version'<>NEW.policy_version OR NOT lv.override_covers(p)
 OR NOT EXISTS(SELECT 1 FROM lv_identity.membership_role WHERE tenant_id=NEW.tenant_id AND user_id=NEW.overridden_by AND role='CONTROLLER')
 OR NOT EXISTS(SELECT 1 FROM lv.proposal_transition t WHERE t.tenant_id=NEW.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.revision=p.revision AND t.action='overrides' AND t.tx_id=pg_current_xact_id() AND t.user_id=NEW.overridden_by AND t.session_id=NEW.session_id AND t.request_hash=NEW.request_hash AND t.idempotency_key=NEW.idempotency_key)
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event a WHERE a.tenant_id=NEW.tenant_id AND a.user_id=NEW.overridden_by AND a.session_id=NEW.session_id AND a.request_id=NEW.request_id AND a.event_type='PROPOSAL_OVERRIDDEN' AND a.detail->>'override_id'=NEW.id::text AND a.detail->>'proposal_id'=p.id::text AND a.detail->>'version'=p.version::text AND a.detail->>'reason_hash'=encode(sha256(convert_to(NEW.reason,'UTF8')),'hex'))
 OR NOT EXISTS(SELECT 1 FROM lv.api_idempotency i WHERE i.tenant_id=NEW.tenant_id AND i.principal_id=NEW.overridden_by AND i.method='POST' AND i.route='/api/v1/proposals/{proposal_id}/overrides' AND i.idempotency_key=NEW.idempotency_key AND i.request_hash=NEW.request_hash AND i.response_status=200 AND i.response->'body'=lv.proposal_json(p))
 THEN RAISE EXCEPTION 'override lacks immutable verdict, controller, transition, audit or cache proof' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $function$;
