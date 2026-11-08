-- 0033 downgrade: refused while any 1.1.0 run exists in ANY tenant (validating CHECK ignores RLS); otherwise restores the exact
-- 0032 definitions below (copied verbatim from 0032_detection_findings.sql) and constraints.
ALTER TABLE lv.detection_run ADD CONSTRAINT no_detection_v11_downgrade CHECK (detector_version = '1.0.0');
ALTER TABLE lv.detection_run DROP CONSTRAINT no_detection_v11_downgrade;
CREATE OR REPLACE FUNCTION lv.detection_dispose(tok bytea, p_finding uuid, p_match text, ikey text, rhash text, p_action text, p_reason text, p_note text,
 p_problem jsonb, src text, req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; f lv.detection_finding; rv lv.detection_review; prior lv.api_idempotency; problem jsonb := p_problem;
 fresh text; cur text; d lv.detection_disposition; next_status text; response jsonb; route_name text := '/api/v1/detection-findings/{finding_id}/dispositions';
BEGIN
 s := lv.proposal_session(tok, src, req);
 IF NOT (lv.proposal_may(s.tenant_id, s.user_id, 'propose') OR lv.proposal_may(s.tenant_id, s.user_id, 'review')) THEN
  RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires propose or review'); END IF;
 IF ikey IS NULL OR ikey !~ '^[!-~]{1,64}$' OR rhash IS NULL OR rhash !~ '^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status', 400, 'code', 'IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-detect-dispose:' || s.tenant_id::text || ':' || s.user_id::text || ':' || ikey, 0)) THEN
  RETURN jsonb_build_object('status', 409, 'code', 'REQUEST_IN_PROGRESS', 'retry_after', 1); END IF;
 PERFORM 1 FROM lv_identity.app_user WHERE id = s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id = s.tenant_id AND user_id = s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id = s.tenant_id AND user_id = s.user_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv_identity.session WHERE id = s.id FOR UPDATE;
 s := lv.proposal_session(tok, src, req);
 IF NOT (lv.proposal_may(s.tenant_id, s.user_id, 'propose') OR lv.proposal_may(s.tenant_id, s.user_id, 'review')) THEN
  RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires propose or review'); END IF;
 PERFORM lv_identity.session_context(tok, true);
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id = s.tenant_id AND i.principal_id = s.user_id AND i.method = 'POST'
  AND i.route = route_name AND i.idempotency_key = ikey;
 IF FOUND AND prior.expires_at > statement_timestamp() THEN
  IF prior.request_hash <> rhash THEN RETURN jsonb_build_object('status', 422, 'code', 'IDEMPOTENCY_KEY_REUSED'); END IF;
  RETURN prior.response || jsonb_build_object('replayed', true);
 END IF;
 SELECT * INTO f FROM lv.detection_finding WHERE tenant_id = s.tenant_id AND id = p_finding;
 IF NOT FOUND THEN problem := jsonb_build_object('status', 404, 'code', 'NOT_FOUND', 'detail', 'finding not found');
 ELSE
  PERFORM 1 FROM lv.ledger WHERE tenant_id = f.tenant_id AND id = f.ledger_id FOR KEY SHARE;   -- same ledger -> counter order
  PERFORM 1 FROM lv.ledger_counter WHERE tenant_id = f.tenant_id AND ledger_id = f.ledger_id FOR SHARE;
  SELECT * INTO rv FROM lv.detection_review WHERE tenant_id = f.tenant_id AND finding_id = f.id FOR UPDATE;
  cur := lv.detection_fingerprint(lv.detection_inputs(f.tenant_id, f.ledger_id, f.staged_document_id));
  fresh := CASE WHEN cur = f.input_fingerprint THEN 'CURRENT' ELSE 'STALE' END;
  next_status := CASE p_action WHEN 'CONFIRMED' THEN 'CONFIRMED' WHEN 'DISMISSED' THEN 'DISMISSED' WHEN 'REOPENED' THEN 'OPEN' END;
  IF problem IS NOT NULL THEN NULL;
  ELSIF p_match IS NULL THEN problem := jsonb_build_object('status', 428, 'code', 'VERSION_REQUIRED');
  ELSIF p_match <> '"v' || rv.version::text || '"' THEN problem := jsonb_build_object('status', 412, 'code', 'VERSION_MISMATCH', 'current_version', rv.version);
  ELSIF next_status IS NULL
     OR (p_action = 'CONFIRMED' AND p_reason IS DISTINCT FROM 'VERIFIED_AGAINST_EVIDENCE')
     OR (p_action = 'REOPENED' AND p_reason IS DISTINCT FROM 'REVIEW_CORRECTION')
     OR (p_action = 'DISMISSED' AND (p_reason IS NULL OR p_reason NOT IN ('EXPECTED_TIMING', 'STATEMENT_ERROR', 'KNOWN_EXCEPTION', 'NOT_A_DISCREPANCY', 'INPUTS_CHANGED')))
     OR (p_note IS NOT NULL AND (length(p_note) NOT BETWEEN 1 AND 2000 OR p_note !~ '[^[:space:]]')) THEN
   problem := jsonb_build_object('status', 422, 'code', 'SCHEMA_INVALID', 'detail', 'action, reason_code or note is not valid',
    'errors', jsonb_build_array(jsonb_build_object('pointer', '/reason_code', 'code', 'enum')));
  ELSIF (rv.status = 'OPEN' AND p_action = 'REOPENED') OR (rv.status <> 'OPEN' AND p_action <> 'REOPENED') THEN
   problem := jsonb_build_object('status', 409, 'code', 'INVALID_STATE_TRANSITION', 'state', rv.status);
  ELSIF p_action = 'CONFIRMED' AND fresh = 'STALE' THEN
   problem := jsonb_build_object('status', 409, 'code', 'FINDING_STALE', 'detail', 'compared inputs changed since detection; run detection again or dismiss with INPUTS_CHANGED');
  END IF;
 END IF;
 IF problem IS NOT NULL THEN
  IF (problem->>'status')::integer NOT BETWEEN 400 AND 499 THEN RAISE EXCEPTION 'invalid disposition refusal' USING ERRCODE = '22023'; END IF;
  PERFORM lv_identity.audit_append('DETECTION_DISPOSITION_REFUSED', 'DENIED', s.user_id, s.tenant_id, s.id, src, req,
   jsonb_build_object('finding_id', p_finding::text, 'reason', problem->>'code'));
  response := problem;
 ELSE
  INSERT INTO lv.detection_disposition(tenant_id, ledger_id, finding_id, review_version, action, reason_code, note, freshness,
   input_fingerprint_at_decision, decided_by, session_id, request_id, idempotency_key, request_hash)
  VALUES (f.tenant_id, f.ledger_id, f.id, rv.version + 1, p_action, p_reason, p_note, fresh, cur, s.user_id, s.id, req, ikey, rhash) RETURNING * INTO d;
  UPDATE lv.detection_review SET version = rv.version + 1, status = next_status, last_disposition_id = d.id
   WHERE tenant_id = rv.tenant_id AND finding_id = rv.finding_id AND version = rv.version;
  IF NOT FOUND THEN RAISE EXCEPTION 'detection review CAS lost' USING ERRCODE = '40001'; END IF;
  PERFORM lv_identity.audit_append('DETECTION_FINDING_DISPOSED', 'ALLOWED', s.user_id, s.tenant_id, s.id, src, req,
   jsonb_build_object('disposition_id', d.id::text, 'finding_id', f.id::text, 'action', p_action, 'reason_code', p_reason,
    'review_version', d.review_version::text, 'freshness', fresh,
    'note_hash', CASE WHEN p_note IS NULL THEN NULL ELSE encode(sha256(convert_to(p_note, 'UTF8')), 'hex') END));
  SELECT * INTO f FROM lv.detection_finding WHERE tenant_id = s.tenant_id AND id = p_finding;
  response := jsonb_build_object('status', 200, 'etag', 'v' || (rv.version + 1)::text, 'body', lv.detection_finding_json(f));
 END IF;
 DELETE FROM lv.api_idempotency i WHERE i.tenant_id = s.tenant_id AND i.principal_id = s.user_id AND i.method = 'POST' AND i.route = route_name AND i.idempotency_key = ikey;
 INSERT INTO lv.api_idempotency(tenant_id, principal_id, method, route, idempotency_key, request_hash, response_status, response, expires_at)
 VALUES (s.tenant_id, s.user_id, 'POST', route_name, ikey, rhash, (response->>'status')::smallint, response, transaction_timestamp() + interval '24 hours');
 RETURN response || jsonb_build_object('replayed', false);
END $$;
CREATE OR REPLACE FUNCTION lv.detection_record(tok bytea, p_ledger uuid, ikey text, rhash text, p_detector text, p_version text, p_config jsonb,
 p_results jsonb, src text, req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; seq bigint; scope jsonb := nullif(current_setting('lv.detection_scope', true), '')::jsonb;
 r jsonb; i integer; inputs jsonb; fp text; expected jsonb; violation jsonb; facts jsonb; fps text := ''; abstentions jsonb := '[]';
 run lv.detection_run; fid uuid; created boolean; f jsonb; response jsonb; route_name text := '/api/v1/ledgers/{ledger_id}/detection-runs';
BEGIN
 s := lv.proposal_session(tok, src, req);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'propose') THEN RAISE EXCEPTION 'detection record requires propose' USING ERRCODE = '42501'; END IF;
 SELECT last_seq INTO seq FROM lv.ledger_counter WHERE tenant_id = s.tenant_id AND ledger_id = p_ledger FOR SHARE;
 seq := coalesce(seq, 0);
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-detect:' || s.tenant_id::text || ':' || s.user_id::text || ':' || ikey, 0))
    OR coalesce(current_setting('lv.detection_begun', true), '') <> s.tenant_id::text || ':' || p_ledger::text || ':' || ikey || ':' || rhash || ':' || seq::text
    OR scope IS NULL THEN
  RAISE EXCEPTION 'detection record requires detection begin in this transaction on an unchanged ledger' USING ERRCODE = '42501';
 END IF;
 IF p_detector IS DISTINCT FROM 'lv-settlement-statement-reconciliation' OR p_version IS DISTINCT FROM '1.0.0' THEN
  violation := jsonb_build_object('index', NULL, 'reason', 'UNKNOWN_DETECTOR');
 ELSIF p_config IS DISTINCT FROM lv.detection_config() THEN
  violation := jsonb_build_object('index', NULL, 'reason', 'CONFIGURATION_MISMATCH');
 ELSIF p_results IS NULL OR jsonb_typeof(p_results) <> 'array' OR jsonb_array_length(p_results) <> jsonb_array_length(scope) THEN
  violation := jsonb_build_object('index', NULL, 'reason', 'SCOPE_NOT_COVERED');
 ELSE
  FOR i IN 0 .. jsonb_array_length(scope) - 1 LOOP
   r := p_results->i;
   IF jsonb_typeof(r) <> 'object' OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(r) k)
        <> ARRAY['abstention', 'findings', 'input_fingerprint', 'integrity', 'staged_document_id'] THEN
    violation := jsonb_build_object('index', i, 'reason', 'RESULT_SHAPE'); EXIT;
   END IF;
   IF r->>'staged_document_id' IS DISTINCT FROM scope->>i THEN violation := jsonb_build_object('index', i, 'reason', 'SCOPE_ORDER'); EXIT; END IF;
   inputs := lv.detection_inputs(s.tenant_id, p_ledger, (scope->>i)::uuid);
   fp := lv.detection_fingerprint(inputs);
   IF r->>'input_fingerprint' IS DISTINCT FROM fp THEN violation := jsonb_build_object('index', i, 'reason', 'INPUT_FINGERPRINT_MISMATCH'); EXIT; END IF;
   expected := lv.detection_expected(inputs);
   IF r->>'integrity' = 'FAILED' THEN
    IF r->'abstention' IS DISTINCT FROM '"SOURCE_INTEGRITY_FAILED"'::jsonb OR r->'findings' IS DISTINCT FROM '[]'::jsonb THEN
     violation := jsonb_build_object('index', i, 'reason', 'INTEGRITY_ABSTENTION_SHAPE'); EXIT; END IF;
   ELSIF r->>'integrity' IS DISTINCT FROM 'VERIFIED' THEN
    violation := jsonb_build_object('index', i, 'reason', 'INTEGRITY_VALUE'); EXIT;
   ELSIF r->'abstention' IS DISTINCT FROM expected->'abstention' OR r->'findings' IS DISTINCT FROM expected->'findings' THEN
    violation := jsonb_build_object('index', i, 'reason', 'FINDINGS_DIFFER_FROM_DATABASE_ORACLE'); EXIT;
   END IF;
   fps := fps || fp;
   IF jsonb_typeof(r->'abstention') = 'string' THEN
    abstentions := abstentions || jsonb_build_array(jsonb_build_object('staged_document_id', scope->>i, 'code', r->>'abstention'));
   END IF;
  END LOOP;
 END IF;
 IF violation IS NOT NULL THEN
  INSERT INTO lv.detection_run(tenant_id, ledger_id, detector, detector_version, config, config_hash, scope, snapshot_seq, input_fingerprint,
   status, rejection_code, rejection_detail, requested_by, session_id, request_id, idempotency_key, request_hash)
  VALUES (s.tenant_id, p_ledger, 'lv-settlement-statement-reconciliation', '1.0.0', lv.detection_config(),
   encode(sha256(convert_to(lv.detection_config()::text, 'UTF8')), 'hex'), scope, seq, encode(sha256(convert_to(fps, 'UTF8')), 'hex'),
   'REJECTED', 'DETECTOR_OUTPUT_INVALID', violation, s.user_id, s.id, req, ikey, rhash) RETURNING * INTO run;
  PERFORM lv_identity.audit_append('DETECTION_RUN_REJECTED', 'DENIED', s.user_id, s.tenant_id, s.id, src, req,
   jsonb_build_object('run_id', run.id::text, 'ledger_id', p_ledger::text, 'reason', violation->>'reason'));
  RETURN jsonb_build_object('status', 502, 'code', 'DETECTOR_OUTPUT_REJECTED', 'detail', 'detector output failed database verification; nothing was recorded as a finding',
   'run_id', run.id::text, 'replayed', false);
 END IF;
 INSERT INTO lv.detection_run(tenant_id, ledger_id, detector, detector_version, config, config_hash, scope, snapshot_seq, input_fingerprint,
  status, abstentions, requested_by, session_id, request_id, idempotency_key, request_hash)
 VALUES (s.tenant_id, p_ledger, p_detector, p_version, p_config, encode(sha256(convert_to(p_config::text, 'UTF8')), 'hex'), scope, seq,
  encode(sha256(convert_to(fps, 'UTF8')), 'hex'), 'COMPLETED', abstentions, s.user_id, s.id, req, ikey, rhash) RETURNING * INTO run;
 FOR i IN 0 .. jsonb_array_length(scope) - 1 LOOP
  r := p_results->i;
  CONTINUE WHEN jsonb_array_length(r->'findings') = 0;
  inputs := lv.detection_inputs(s.tenant_id, p_ledger, (scope->>i)::uuid);
  fp := lv.detection_fingerprint(inputs); facts := lv.detection_facts(inputs, seq);
  FOR f IN SELECT x FROM jsonb_array_elements(r->'findings') x LOOP
   INSERT INTO lv.detection_finding(tenant_id, ledger_id, detector, detector_version, kind, class, staged_document_id, finding_key,
    input_fingerprint, inputs, observed, recorded, computed, interpretation, evidence, first_run_id)
   VALUES (s.tenant_id, p_ledger, p_detector, p_version, f->>'kind', f->>'class', (scope->>i)::uuid,
    p_detector || ':' || (f->>'kind') || ':' || (scope->>i), fp, inputs, facts->'observed', facts->'recorded', f->'computed', NULL,
    facts->'evidence', run.id)
   ON CONFLICT (tenant_id, ledger_id, finding_key, input_fingerprint) DO NOTHING RETURNING id INTO fid;
   created := fid IS NOT NULL;
   IF created THEN
    INSERT INTO lv.detection_review(tenant_id, finding_id, version, status) VALUES (s.tenant_id, fid, 1, 'OPEN');
   ELSE
    SELECT id INTO fid FROM lv.detection_finding WHERE tenant_id = s.tenant_id AND ledger_id = p_ledger
     AND finding_key = p_detector || ':' || (f->>'kind') || ':' || (scope->>i) AND input_fingerprint = fp;
   END IF;
   INSERT INTO lv.detection_run_finding(tenant_id, run_id, finding_id, created) VALUES (s.tenant_id, run.id, fid, created);
  END LOOP;
 END LOOP;
 PERFORM lv_identity.audit_append('DETECTION_RUN_COMPLETED', 'ALLOWED', s.user_id, s.tenant_id, s.id, src, req,
  jsonb_build_object('run_id', run.id::text, 'ledger_id', p_ledger::text, 'detector', p_detector || '/' || p_version,
   'snapshot_seq', seq::text, 'input_fingerprint', run.input_fingerprint, 'statements', jsonb_array_length(scope)::text,
   'findings', (SELECT count(*) FROM lv.detection_run_finding WHERE tenant_id = s.tenant_id AND run_id = run.id)::text));
 response := jsonb_build_object('status', 201, 'body', lv.detection_run_json(run));
 DELETE FROM lv.api_idempotency i WHERE i.tenant_id = s.tenant_id AND i.principal_id = s.user_id AND i.method = 'POST' AND i.route = route_name AND i.idempotency_key = ikey;
 INSERT INTO lv.api_idempotency(tenant_id, principal_id, method, route, idempotency_key, request_hash, response_status, response, expires_at)
 VALUES (s.tenant_id, s.user_id, 'POST', route_name, ikey, rhash, 201, response, transaction_timestamp() + interval '24 hours');
 RETURN response || jsonb_build_object('replayed', false);
END $$;
CREATE OR REPLACE FUNCTION lv.detection_finding_json(f lv.detection_finding) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 WITH cur AS (SELECT lv.detection_fingerprint(lv.detection_inputs(f.tenant_id, f.ledger_id, f.staged_document_id)) AS fp)
 SELECT jsonb_build_object('id', f.id::text, 'ledger_id', f.ledger_id::text, 'detector', f.detector, 'detector_version', f.detector_version,
  'kind', f.kind, 'class', f.class, 'staged_document_id', f.staged_document_id::text, 'finding_key', f.finding_key,
  'basis', 'DETERMINISTIC_COMPARISON', 'verified', false, 'accounting_review_status', 'UNREVIEWED',
  'observed', f.observed, 'recorded', f.recorded, 'computed', f.computed,
  'interpretation', jsonb_build_object('status', 'NOT_AVAILABLE', 'reason', 'No model is configured for this detector (DET-1).', 'content', NULL),
  'evidence', f.evidence, 'input_fingerprint', f.input_fingerprint,
  'freshness', jsonb_build_object('status', CASE WHEN cur.fp = f.input_fingerprint THEN 'CURRENT' ELSE 'STALE' END, 'current_input_fingerprint', cur.fp),
  'first_run_id', f.first_run_id::text, 'created_at', to_char(f.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'review', (SELECT jsonb_build_object('version', rv.version, 'status', rv.status, 'etag', 'v' || rv.version::text) FROM lv.detection_review rv
     WHERE rv.tenant_id = f.tenant_id AND rv.finding_id = f.id),
  'dispositions', (SELECT coalesce(jsonb_agg(jsonb_build_object('id', d.id::text, 'review_version', d.review_version, 'action', d.action,
     'reason_code', d.reason_code, 'note', d.note, 'freshness', d.freshness, 'input_fingerprint_at_decision', d.input_fingerprint_at_decision,
     'decided_by', d.decided_by::text, 'created_at', to_char(d.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
     ORDER BY d.review_version), '[]') FROM lv.detection_disposition d WHERE d.tenant_id = f.tenant_id AND d.finding_id = f.id))
 FROM cur
$$;
CREATE OR REPLACE FUNCTION lv.detection_facts(p jsonb, p_seq bigint) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object(
  'observed', jsonb_build_object('label', 'OBSERVATION from a staged SYNTHETIC source record; not an authoritative ledger fact',
    'processor_ref', p->'processor_ref', 'payout_id', p->'payout_id', 'payout_date', p->'payout_date', 'currency', p->'currency',
    'gross', p->'gross', 'fee', p->'fee', 'net', p->'net',
    'items', (SELECT coalesce(jsonb_agg(jsonb_build_object('line_no', x->'line_no', 'item_ref', x->'item_ref', 'kind', x->'kind',
       'amount', x->'amount') ORDER BY (x->>'line_no')::integer), '[]') FROM jsonb_array_elements(p->'items') x)),
  'recorded', jsonb_build_object('label', 'AUTHORITATIVE ledger state at snapshot seq ' || p_seq::text, 'snapshot_seq', p_seq::text,
    'settlement', p->'settlement',
    'payments', (SELECT coalesce(jsonb_agg(x->'payment' ORDER BY (x->>'line_no')::integer), '[]') FROM jsonb_array_elements(p->'items') x
       WHERE jsonb_typeof(x->'payment') = 'object')),
  'evidence', jsonb_build_array(
    jsonb_build_object('type', 'STAGED_STATEMENT', 'id', p->'staged_document_id', 'financial_fingerprint', p->'financial_fingerprint'),
    jsonb_build_object('type', 'SOURCE_RECORD', 'id', p->'source_record_id', 'content_hash', p->'source_record_hash'),
    jsonb_build_object('type', 'SOURCE_FILE', 'id', p->'source_file_id', 'sha256', p->'source_file_sha256'))
   || (SELECT coalesce(jsonb_agg(jsonb_build_object('type', 'PAYMENT_VERSION', 'document_id', x->'payment'->'document_id',
        'version', x->'payment'->'version') ORDER BY (x->>'line_no')::integer), '[]') FROM jsonb_array_elements(p->'items') x
        WHERE jsonb_typeof(x->'payment') = 'object')
   || CASE WHEN jsonb_typeof(p->'settlement') = 'object'
       THEN jsonb_build_array(jsonb_build_object('type', 'SETTLEMENT_VERSION', 'document_id', p->'settlement'->'document_id',
              'version', p->'settlement'->'version'))
       ELSE jsonb_build_array(jsonb_build_object('type', 'SETTLEMENT_ABSENT', 'processor_ref', p->'processor_ref', 'payout_id', p->'payout_id',
              'as_of_seq', p_seq::text)) END)
$$;
CREATE OR REPLACE FUNCTION lv.detection_config() RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object('identity', 'processor_ref+processor_item_id (statement processor namespace only)',
  'amount_comparison', 'exact decimal at the ledger exponent', 'max_statements', 200, 'model', 'none',
  'abstain', jsonb_build_array('AMBIGUOUS_STATEMENT', 'REFUND_ITEMS_NOT_SUPPORTED', 'SOURCE_INTEGRITY_FAILED'))
$$;
DROP FUNCTION lv.detection_expected(jsonb);
ALTER FUNCTION lv.detection_expected_v1(jsonb) RENAME TO detection_expected;
DROP FUNCTION lv.detection_current_fingerprint(lv.detection_finding);
DROP FUNCTION lv.detection_inputs_v2(uuid, uuid, uuid);
ALTER TABLE lv.detection_finding DROP CONSTRAINT detection_finding_conflict_class_check;
ALTER TABLE lv.detection_finding DROP CONSTRAINT detection_finding_class_check;
ALTER TABLE lv.detection_finding ADD CONSTRAINT detection_finding_class_check CHECK (class IN ('DISCREPANCY', 'EVIDENCE_GAP'));
ALTER TABLE lv.detection_finding DROP CONSTRAINT detection_finding_kind_check;
ALTER TABLE lv.detection_finding ADD CONSTRAINT detection_finding_kind_check CHECK (kind IN ('SETTLEMENT_NOT_RECORDED', 'SETTLEMENT_AMOUNTS_DIFFER',
 'SETTLEMENT_ITEMS_DIFFER', 'STATEMENT_ITEM_SETTLED_ELSEWHERE', 'STATEMENT_ITEM_AMOUNT_DIFFERS', 'STATEMENT_ITEM_UNRESOLVED'));
