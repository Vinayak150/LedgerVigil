DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM lv_identity.audit_event WHERE left(event_type,9)='MFA_RESET')
 OR EXISTS (SELECT 1 FROM lv_identity.session WHERE revoke_reason='MFA_RESET') THEN
  RAISE EXCEPTION 'MFA reset history exists; refusing downgrade' USING ERRCODE='55000';
 END IF;
END $$;
CREATE OR REPLACE FUNCTION lv_identity.mfa_prepare(p_hash bytea,p_action text,p_source text,p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; f lv_identity.mfa_factor; since timestamptz := statement_timestamp()-interval '15 minutes';
BEGIN
 PERFORM lv_identity.check_request(p_source,p_request);
 IF p_action IS NULL OR p_action NOT IN ('STATUS','START','CONFIRM','CHALLENGE','RECOVERY','REGENERATE') THEN
  RAISE EXCEPTION 'invalid MFA action' USING ERRCODE='22023';
 END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('status','INVALID'); END IF;
 IF p_action<>'STATUS' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('mfa-source:'||p_source,0));
  PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR UPDATE;
  SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash FOR UPDATE;
  IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN jsonb_build_object('status','INVALID'); END IF;
  IF (SELECT count(*) FROM lv_identity.mfa_attempt WHERE user_id=s.user_id AND at>since)>=10
     OR (SELECT count(*) FROM lv_identity.mfa_attempt WHERE source_key=p_source AND at>since)>=40 THEN
   PERFORM lv_identity.audit_append('MFA_THROTTLED','DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
   RETURN jsonb_build_object('status','THROTTLED');
  END IF;
  INSERT INTO lv_identity.mfa_attempt(user_id,source_key) VALUES(s.user_id,p_source);
 END IF;
 SELECT * INTO f FROM lv_identity.mfa_factor WHERE user_id=s.user_id;
 IF p_action='STATUS' THEN
  RETURN jsonb_build_object('status','OK','enrolled',f.last_step IS NOT NULL,'verified_at',s.mfa_verified_at);
 END IF;
 IF p_action IN ('START','REGENERATE') AND NOT EXISTS (
   SELECT 1 FROM lv_identity.reauthentication r WHERE r.session_id=s.id AND r.at>statement_timestamp()-interval '15 minutes'
 ) THEN
  PERFORM lv_identity.audit_append('MFA_'||p_action,'DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
  RETURN jsonb_build_object('status','REAUTH_REQUIRED');
 END IF;
 IF (p_action='START' AND f.last_step IS NOT NULL)
 OR (p_action='CONFIRM' AND (f.pending_session IS DISTINCT FROM s.id OR f.expires_at<=statement_timestamp()))
 OR (p_action IN ('CHALLENGE','RECOVERY','REGENERATE') AND f.last_step IS NULL) THEN
  PERFORM lv_identity.audit_append('MFA_'||p_action,'DENIED',s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
  RETURN jsonb_build_object('status','WRONG_STATE');
 END IF;
 RETURN jsonb_build_object('status','OK','user_id',s.user_id,'enrollment',f.enrollment,
                           'envelope',encode(f.envelope,'hex'));
END $$;

-- The issuer attests the cryptographic match. No other application role may call this.
CREATE OR REPLACE FUNCTION lv_identity.mfa_finish(p_hash bytea,p_action text,p_source text,p_request text,
 p_enrollment uuid,p_envelope bytea,p_step bigint,p_recovery text,p_new_codes text[]) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session; f lv_identity.mfa_factor; ok boolean := false;
BEGIN
 PERFORM lv_identity.check_request(p_source,p_request);
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN false; END IF;
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR UPDATE;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash=p_hash FOR UPDATE;
 IF lv_identity.session_problem(s) IS NOT NULL THEN RETURN false; END IF;
 SELECT * INTO f FROM lv_identity.mfa_factor WHERE user_id=s.user_id;
 IF p_action='START' AND f.last_step IS NULL AND p_envelope IS NOT NULL AND p_enrollment IS NOT NULL THEN
  INSERT INTO lv_identity.mfa_factor(user_id,enrollment,envelope,pending_session,expires_at)
   VALUES(s.user_id,p_enrollment,p_envelope,s.id,statement_timestamp()+interval '10 minutes')
   ON CONFLICT(user_id) DO UPDATE SET enrollment=excluded.enrollment,envelope=excluded.envelope,
    pending_session=excluded.pending_session,expires_at=excluded.expires_at;
  ok := true;
 ELSIF p_enrollment=f.enrollment THEN
  IF p_action='CONFIRM' AND f.pending_session=s.id AND f.expires_at>statement_timestamp() AND p_step>=0 THEN
   UPDATE lv_identity.mfa_factor SET pending_session=NULL,expires_at=NULL,last_step=p_step,recovery=p_new_codes WHERE user_id=s.user_id;
   ok := true;
  ELSIF p_action IN ('CHALLENGE','REGENERATE') AND p_step>f.last_step THEN
   UPDATE lv_identity.mfa_factor SET last_step=p_step,recovery=CASE WHEN p_action='REGENERATE' THEN p_new_codes ELSE recovery END WHERE user_id=s.user_id;
   ok := true;
  ELSIF p_action='RECOVERY' AND p_recovery=ANY(f.recovery) THEN
   UPDATE lv_identity.mfa_factor SET recovery=array_remove(recovery,p_recovery) WHERE user_id=s.user_id;
   ok := true;
  END IF;
 END IF;
 IF ok AND p_action<>'START' THEN
  UPDATE lv_identity.session SET mfa_verified_at=statement_timestamp() WHERE id=s.id;
 END IF;
 PERFORM lv_identity.audit_append('MFA_'||p_action,CASE WHEN ok THEN 'ALLOWED' ELSE 'DENIED' END,
  s.user_id,s.tenant_id,s.id,p_source,p_request,'{}');
 RETURN ok;
END $$;

ALTER TABLE lv_identity.mfa_factor DROP COLUMN reset_after;
ALTER TABLE lv_identity.session DROP CONSTRAINT session_revoke_reason_check;
ALTER TABLE lv_identity.session ADD CONSTRAINT session_revoke_reason_check CHECK (revoke_reason IN
 ('LOGOUT','ROTATED_LOGIN','TENANT_SWITCH','ADMIN_REVOKED','USER_DISABLED','MEMBERSHIP_REVOKED',
 'ROLES_CHANGED','PASSWORD_RESET','USER_REVOKED_ALL'));

CREATE OR REPLACE FUNCTION lv_identity.session_info(p_session uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object(
  'session_id', s.id::text,
  'user', jsonb_build_object('id', u.id::text, 'email', u.email, 'display_name', u.display_name),
  'tenant_id', s.tenant_id::text,
  'roles', coalesce((SELECT jsonb_agg(r.role::text ORDER BY r.role) FROM lv_identity.membership_role r
                     WHERE r.tenant_id = s.tenant_id AND r.user_id = s.user_id), '[]'::jsonb),
  'permissions', coalesce((SELECT jsonb_agg(DISTINCT p.permission::text) FROM lv_identity.membership_role r
                           JOIN lv_identity.role_permission p ON p.role = r.role
                           WHERE r.tenant_id = s.tenant_id AND r.user_id = s.user_id), '[]'::jsonb),
  'available_tenant_ids', coalesce((SELECT jsonb_agg(m.tenant_id::text ORDER BY m.tenant_id) FROM lv_identity.membership m
                                    WHERE m.user_id = s.user_id AND m.status = 'ACTIVE'), '[]'::jsonb),
  'tenant_selection_required', s.tenant_id IS NULL,
  'idle_expires_at', to_char((s.last_seen_at + s.idle_timeout) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'absolute_expires_at', to_char(s.absolute_expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
 FROM lv_identity.session s JOIN lv_identity.app_user u ON u.id = s.user_id WHERE s.id = p_session
$$;
CREATE OR REPLACE FUNCTION lv.proposal_may(p_tenant uuid, p_user uuid, p_permission text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM lv_identity.membership_role r JOIN lv_identity.role_permission rp ON rp.role = r.role
                WHERE r.tenant_id = p_tenant AND r.user_id = p_user AND rp.permission::text = p_permission);
$$;

CREATE OR REPLACE FUNCTION lv_identity.switch_tenant(p_token_hash bytea, p_tenant uuid, p_new_token_hash bytea, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; problem text; sid uuid;
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_new_token_hash IS NULL OR octet_length(p_new_token_hash) <> 32 OR p_tenant IS NULL THEN
  RAISE EXCEPTION 'invalid tenant selection' USING ERRCODE = '22023';
 END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash = p_token_hash FOR UPDATE;
 problem := lv_identity.session_problem(s);
 -- A session whose current tenant membership was revoked may still select another tenant it is a member of.
 IF problem IS NOT NULL AND problem <> 'MEMBERSHIP_INACTIVE' THEN RETURN jsonb_build_object('valid', false, 'reason', problem); END IF;
 IF NOT EXISTS (SELECT 1 FROM lv_identity.membership WHERE tenant_id = p_tenant AND user_id = s.user_id AND status = 'ACTIVE') THEN
  PERFORM lv_identity.audit_append('TENANT_SELECTION_DENIED', 'DENIED', s.user_id, NULL, s.id, p_source, p_request,
    jsonb_build_object('requested_tenant_id', p_tenant::text));
  RETURN jsonb_build_object('valid', true, 'selected', false);
 END IF;
 UPDATE lv_identity.session SET revoked_at = statement_timestamp(), revoke_reason = 'TENANT_SWITCH' WHERE id = s.id;
 INSERT INTO lv_identity.session(token_hash, user_id, tenant_id, created_at, last_seen_at, idle_timeout, absolute_expires_at, source_key)
 VALUES (p_new_token_hash, s.user_id, p_tenant, statement_timestamp(), statement_timestamp(), interval '30 minutes', s.absolute_expires_at, p_source)
 RETURNING id INTO sid;
 UPDATE lv_identity.session SET replaced_by = sid WHERE id = s.id;
 PERFORM lv_identity.audit_append('TENANT_SELECTED', 'ALLOWED', s.user_id, p_tenant, sid, p_source, p_request,
   jsonb_build_object('previous_session_id', s.id::text, 'previous_tenant_id', s.tenant_id::text));
 RETURN jsonb_build_object('valid', true, 'selected', true, 'session', lv_identity.session_info(sid));
END $$;

CREATE OR REPLACE FUNCTION lv.close_period(p_token_hash bytea, p_period uuid, p_if_match text, p_idempotency_key text,
                                p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
 v_route constant text := '/api/v1/periods/{period_id}/close';
 s lv_identity.session; v_problem text; t uuid; u uuid;
 req_hash text; prior lv.api_idempotency; result jsonb; refusal text;
 p lv.fiscal_period; l lv.ledger; counter lv.ledger_counter; wanted integer;
 next_seq bigint; batch_content text; event_content text; bh text; eh text;
 job uuid := uuidv7(); v_now timestamptz := statement_timestamp();
BEGIN
 PERFORM lv_identity.check_request(p_source, p_request);
 IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN
  RAISE EXCEPTION 'an authenticated session is required' USING ERRCODE = '42501';
 END IF;
 SELECT * INTO s FROM lv_identity.session WHERE token_hash = p_token_hash;
 v_problem := lv_identity.session_problem(s);
 IF v_problem IS NOT NULL OR s.tenant_id IS NULL THEN
  RAISE EXCEPTION 'an authenticated session with a selected tenant is required' USING ERRCODE = '42501';
 END IF;
 t := s.tenant_id; u := s.user_id;
 -- Never adopt a context the caller set for someone else; bind this transaction to the verified session.
 IF coalesce(current_setting('app.tenant_id', true), '') NOT IN ('', t::text)
    OR coalesce(current_setting('app.user_id', true), '') NOT IN ('', u::text)
    OR coalesce(current_setting('app.session_id', true), '') NOT IN ('', s.id::text) THEN
  RAISE EXCEPTION 'transaction context belongs to a different session' USING ERRCODE = '42501';
 END IF;
 PERFORM set_config('app.tenant_id', t::text, true);
 PERFORM set_config('app.user_id', u::text, true);
 PERFORM set_config('app.session_id', s.id::text, true);
 IF NOT EXISTS (SELECT 1 FROM lv_identity.membership_role r JOIN lv_identity.role_permission rp ON rp.role = r.role
                WHERE r.tenant_id = t AND r.user_id = u AND rp.permission = 'administer') THEN
  PERFORM lv_identity.audit_append('PERIOD_CLOSE_DENIED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('period_id', p_period::text, 'reason', 'PERMISSION_DENIED'));
  RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires administer', 'stored', false);
 END IF;
 IF p_idempotency_key IS NULL OR p_idempotency_key !~ '^[!-~]{1,64}$' THEN
  RAISE EXCEPTION 'invalid idempotency key' USING ERRCODE = '22023';
 END IF;
 IF p_if_match IS NOT NULL AND length(p_if_match) > 64 THEN
  RAISE EXCEPTION 'invalid If-Match' USING ERRCODE = '22023';
 END IF;
 req_hash := encode(sha256(convert_to(lv.canonical_seed_json(jsonb_build_object('method', 'POST', 'route', v_route,
              'period_id', p_period::text, 'if_match', p_if_match)), 'UTF8')), 'hex');
 -- Part 8.1: a repeat while the first is still running is 409 REQUEST_IN_PROGRESS (never a second execution).
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-idem:' || t::text || ':' || u::text || ':POST:' || v_route || ':' || p_idempotency_key, 0)) THEN
  RETURN jsonb_build_object('status', 409, 'code', 'REQUEST_IN_PROGRESS', 'detail', 'the first request with this Idempotency-Key is still running',
                            'retry_after', 1, 'stored', false);
 END IF;
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id = t AND i.principal_id = u AND i.method = 'POST'
  AND i.route = v_route AND i.idempotency_key = p_idempotency_key;
 IF FOUND THEN
  IF prior.expires_at <= statement_timestamp() THEN
   DELETE FROM lv.api_idempotency i WHERE i.tenant_id = t AND i.principal_id = u AND i.method = 'POST'
    AND i.route = v_route AND i.idempotency_key = p_idempotency_key;
  ELSIF prior.request_hash <> req_hash THEN
   RETURN jsonb_build_object('status', 422, 'code', 'IDEMPOTENCY_KEY_REUSED', 'detail', 'this Idempotency-Key was used with a different request',
                             'stored', false);
  ELSE
   RETURN prior.response || jsonb_build_object('replayed', true, 'stored', true);
  END IF;
 END IF;

 -- Every effect happens inside this block; any refusal rolls the block back (including locks and the counter row it may
 -- have created) before the refusal itself is stored and audited.
 BEGIN
  IF p_if_match IS NULL THEN
   PERFORM lv.period_close_refusal(428, 'VERSION_REQUIRED', 'If-Match is required', NULL);
  END IF;
  SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id = t AND id = p_period;
  IF NOT FOUND THEN PERFORM lv.period_close_refusal(404, 'NOT_FOUND', 'period not found', NULL); END IF;
  -- Lock order shared with every posting path: ledger (share) -> counter (update) -> period.
  SELECT * INTO l FROM lv.ledger WHERE tenant_id = t AND id = p.ledger_id FOR SHARE;
  IF l.purpose <> 'SANDBOX' OR l.source_kind <> 'NATIVE' THEN
   PERFORM lv.period_close_refusal(422, 'CAPABILITY_UNAVAILABLE', 'period close is enabled for native SANDBOX ledgers only', NULL);
  END IF;
  INSERT INTO lv.ledger_counter(tenant_id, ledger_id) VALUES (t, l.id) ON CONFLICT DO NOTHING;
  SELECT * INTO counter FROM lv.ledger_counter WHERE tenant_id = t AND ledger_id = l.id FOR UPDATE;
  PERFORM pg_advisory_xact_lock(hashtextextended('lv-period-config:' || t::text || ':' || l.id::text, 0));
  SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id = t AND ledger_id = l.id AND id = p_period FOR UPDATE;
  wanted := CASE WHEN p_if_match ~ '^"?v[0-9]{1,9}"?$' THEN substring(p_if_match FROM 'v([0-9]+)')::integer ELSE -1 END;
  IF wanted <> p.version THEN
   PERFORM lv.period_close_refusal(412, 'VERSION_MISMATCH', 'If-Match does not match the current period version',
    jsonb_build_object('current_version', p.version));
  END IF;
  IF p.status <> 'OPEN' THEN
   PERFORM lv.period_close_refusal(409, 'INVALID_STATE_TRANSITION', 'the period is already closed',
    jsonb_build_object('state', p.status::text, 'current_version', p.version));
  END IF;
  IF EXISTS (SELECT 1 FROM lv.fiscal_period WHERE tenant_id = t AND ledger_id = l.id AND status = 'OPEN' AND start_date < p.start_date) THEN
   PERFORM lv.period_close_refusal(409, 'INVALID_STATE_TRANSITION', 'an earlier period is still open',
    jsonb_build_object('state', p.status::text, 'current_version', p.version));
  END IF;

  next_seq := counter.last_seq + 1;
  batch_content := lv.canonical_seed_json(jsonb_build_object('tenant_id', t::text, 'ledger_id', l.id::text, 'seq', next_seq::text,
   'kind', 'PERIOD_CLOSE', 'created_by_user', u::text, 'period_id', p.id::text, 'request_hash', req_hash));
  bh := encode(sha256(convert_to(batch_content || counter.batch_head, 'UTF8')), 'hex');
  INSERT INTO lv.commit_batch(tenant_id, ledger_id, seq, kind, created_by, created_by_user, previous_seq, previous_hash, canonical_content, hash)
  VALUES (t, l.id, next_seq, 'PERIOD_CLOSE', NULL, u, nullif(next_seq - 1, 0), counter.batch_head, batch_content, bh);
  UPDATE lv.fiscal_period SET status = 'CLOSED', version = p.version + 1, closed_seq = next_seq, closed_at = v_now,
   closed_by = u, close_provenance = 'AUDITED_COMMIT'
  WHERE tenant_id = t AND ledger_id = l.id AND id = p.id AND status = 'OPEN' AND version = p.version;
  INSERT INTO lv.work_item(tenant_id, id, kind, subject_type, subject_id, ledger_id, trigger_seq)
  VALUES (t, job, 'STALENESS_SCAN', 'PERIOD', p.id, l.id, next_seq);
  INSERT INTO lv.outbox(tenant_id, topic, work_item_id, dedupe_key)
  VALUES (t, 'maintenance', job, 'STALENESS_SCAN:' || l.id::text || ':' || p.id::text || ':' || next_seq::text);
  event_content := lv.period_close_event(t, l.id, next_seq, s.id, p_request);
  eh := encode(sha256(convert_to(event_content || counter.event_head, 'UTF8')), 'hex');
  INSERT INTO lv.ledger_event VALUES (t, l.id, next_seq, nullif(next_seq - 1, 0), counter.event_head, eh, event_content);
  UPDATE lv.ledger_counter SET last_seq = next_seq, batch_head = bh, event_head = eh WHERE tenant_id = t AND ledger_id = l.id;
  PERFORM lv_identity.audit_append('PERIOD_CLOSED', 'ALLOWED', u, t, s.id, p_source, p_request,
   jsonb_build_object('ledger_id', l.id::text, 'period_id', p.id::text, 'seq', next_seq::text, 'start_date', p.start_date::text,
                      'end_date', p.end_date::text, 'period_version', (p.version + 1)::text, 'staleness_work_item_id', job::text));
  SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id = t AND ledger_id = l.id AND id = p_period;
  result := jsonb_build_object('status', 200, 'etag', 'v' || p.version::text,
   'body', lv.period_json(p) || jsonb_build_object('staleness_job', jsonb_build_object('id', job::text, 'kind', 'STALENESS_SCAN', 'state', 'QUEUED')));
 EXCEPTION WHEN SQLSTATE 'LVR01' THEN
  GET STACKED DIAGNOSTICS refusal = PG_EXCEPTION_DETAIL;
  result := refusal::jsonb;
  PERFORM lv_identity.audit_append('PERIOD_CLOSE_REFUSED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('period_id', p_period::text, 'reason', result ->> 'code'));
 END;
 -- Part 8.1: success and 4xx refusals are stored in the same transaction and replayed for the same key and request.
 INSERT INTO lv.api_idempotency(tenant_id, principal_id, method, route, idempotency_key, request_hash, response_status, response, expires_at)
 VALUES (t, u, 'POST', v_route, p_idempotency_key, req_hash, (result ->> 'status')::smallint, result, transaction_timestamp() + interval '24 hours');
 RETURN result || jsonb_build_object('replayed', false, 'stored', true);
END $$;
