-- 0033 Detection 1.1.0: two advisory rules (PROPOSED DET-9, DET-10; docs/AI_DETECTION.md §8). Deterministic rules only; no model.
--  * PAYMENT_CLAIMED_BY_OTHER_PAYOUT (class POTENTIAL_CONFLICT): a RECEIVED, unsettled payment, resolved by exact identity
--    (processor_ref + processor_item_id in the statement's namespace), that another staged statement for a DIFFERENT, still
--    unrecorded payout of the same tenant, ledger and processor also lists. Both claims are cited. Not proof of anything.
--  * PAYOUT_DATE_DIFFERS (class DISCREPANCY): the statement's payout date differs from the recorded settlement's payout date
--    (the same meaning: SOURCE_READS.md maps StagedDocument.payout_date to RecordSettlement.payout_date). Calendar dates as
--    recorded; no timezone conversion, business-day tolerance or cutoff. Ingestion time and posting dates are never compared.
-- Versioning: new runs use lv-detection-inputs/2 (v1 plus other_claims). Existing 1.0.0 findings keep their v1 fingerprint,
-- so the upgrade itself makes nothing stale; a 1.1.0 rerun creates NEW findings (new review, no carried confirmation).
-- No change to approval, execution, accounting, permissions or the ledger -> counter lock order.

ALTER TABLE lv.detection_finding DROP CONSTRAINT detection_finding_kind_check;
ALTER TABLE lv.detection_finding ADD CONSTRAINT detection_finding_kind_check CHECK (kind IN ('SETTLEMENT_NOT_RECORDED',
 'SETTLEMENT_AMOUNTS_DIFFER', 'SETTLEMENT_ITEMS_DIFFER', 'STATEMENT_ITEM_SETTLED_ELSEWHERE', 'STATEMENT_ITEM_AMOUNT_DIFFERS',
 'STATEMENT_ITEM_UNRESOLVED', 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'PAYOUT_DATE_DIFFERS'));
ALTER TABLE lv.detection_finding DROP CONSTRAINT detection_finding_class_check;
ALTER TABLE lv.detection_finding ADD CONSTRAINT detection_finding_class_check CHECK (class IN ('DISCREPANCY', 'EVIDENCE_GAP', 'POTENTIAL_CONFLICT'));
ALTER TABLE lv.detection_finding ADD CONSTRAINT detection_finding_conflict_class_check
 CHECK ((class = 'POTENTIAL_CONFLICT') = (kind = 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT'));

-- v2 evidence set = v1 unchanged + every other staged statement of the same tenant, ledger and processor, for a different
-- payout, that lists one of this statement's PAYMENT item identities. Facts about that other claim are included so any change
-- to it (new file, conflict, refund line, its payout being recorded) changes this statement's fingerprint (DET-5 staleness).
CREATE FUNCTION lv.detection_inputs_v2(p_tenant uuid, p_ledger uuid, p_staged uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 WITH s AS (SELECT * FROM lv.staged_document WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND id = p_staged),
 claims AS (
  SELECT it.item_ref::text AS item_ref, o.id AS other_id, o.payout_id AS other_payout, oi.line_no AS other_line,
   round(oi.amount::numeric, s.currency_exponent)::text AS other_amount, r.content_hash AS other_record_hash,
   EXISTS (SELECT 1 FROM lv.import_batch b WHERE b.tenant_id = o.tenant_id AND b.ledger_id = o.ledger_id AND b.status = 'REJECTED'
     AND b.related_staged_document_id = o.id) AS other_conflicting,
   EXISTS (SELECT 1 FROM lv.staged_statement_item x WHERE (x.tenant_id, x.ledger_id, x.staged_document_id) = (o.tenant_id, o.ledger_id, o.id)
     AND x.kind::text <> 'PAYMENT') AS other_unsupported,
   EXISTS (SELECT 1 FROM lv.processor_settlement_identity si WHERE si.tenant_id = o.tenant_id AND si.ledger_id = o.ledger_id
     AND si.processor_ref = o.processor_ref AND si.payout_id = o.payout_id) AS other_recorded
  FROM s
  JOIN lv.staged_statement_item it ON (it.tenant_id, it.ledger_id, it.staged_document_id) = (s.tenant_id, s.ledger_id, s.id) AND it.kind::text = 'PAYMENT'
  JOIN lv.staged_document o ON o.tenant_id = s.tenant_id AND o.ledger_id = s.ledger_id AND o.processor_ref = s.processor_ref
   AND o.payout_id <> s.payout_id AND o.id <> s.id
  JOIN lv.staged_statement_item oi ON (oi.tenant_id, oi.ledger_id, oi.staged_document_id) = (o.tenant_id, o.ledger_id, o.id)
   AND oi.item_ref = it.item_ref AND oi.kind::text = 'PAYMENT'
  JOIN lv.source_record r ON (r.tenant_id, r.ledger_id, r.id) = (o.tenant_id, o.ledger_id, o.source_record_id))
 SELECT lv.detection_inputs(p_tenant, p_ledger, p_staged) || jsonb_build_object('schema', 'lv-detection-inputs/2', 'other_claims',
  (SELECT coalesce(jsonb_agg(jsonb_build_object('item_ref', item_ref, 'staged_document_id', other_id::text, 'payout_id', other_payout,
     'line_no', other_line, 'statement_amount', other_amount, 'source_record_hash', other_record_hash, 'conflicting', other_conflicting,
     'unsupported', other_unsupported, 'payout_recorded', other_recorded) ORDER BY item_ref COLLATE "C", other_payout COLLATE "C", other_id), '[]')
   FROM claims))
 FROM s
$$;

-- Freshness uses the evidence-set version the finding was made with (1.0.0 findings: v1; 1.1.0 findings: v2).
CREATE FUNCTION lv.detection_current_fingerprint(f lv.detection_finding) RETURNS char(64)
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT lv.detection_fingerprint(CASE WHEN f.inputs->>'schema' = 'lv-detection-inputs/2'
   THEN lv.detection_inputs_v2(f.tenant_id, f.ledger_id, f.staged_document_id)
   ELSE lv.detection_inputs(f.tenant_id, f.ledger_id, f.staged_document_id) END)
$$;

-- The 1.0.0 oracle is kept unchanged under a new name; the dispatcher keeps the old name (used by the commit-time validator).
ALTER FUNCTION lv.detection_expected(jsonb) RENAME TO detection_expected_v1;

CREATE FUNCTION lv.detection_expected(p jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE base jsonb := lv.detection_expected_v1(p); findings jsonb; st jsonb := p->'settlement'; it jsonb; pay jsonb;
 claims jsonb := '[]'; valid jsonb; excluded jsonb; e integer := (p->>'exponent')::integer;
BEGIN
 IF p->>'schema' IS DISTINCT FROM 'lv-detection-inputs/2' OR base->>'abstention' IS NOT NULL THEN RETURN base; END IF;
 findings := base->'findings';
 IF jsonb_typeof(st) = 'object' AND p->>'payout_date' IS NOT NULL AND st->>'payout_date' IS NOT NULL
    AND (p->>'payout_date')::date <> (st->>'payout_date')::date THEN
  findings := findings || jsonb_build_array(jsonb_build_object('kind', 'PAYOUT_DATE_DIFFERS', 'class', 'DISCREPANCY', 'computed', jsonb_build_object(
   'settlement_document_id', st->'document_id', 'settlement_version', st->'version',
   'statement_payout_date', p->>'payout_date', 'ledger_payout_date', st->>'payout_date',
   'calendar_days_statement_minus_ledger', (p->>'payout_date')::date - (st->>'payout_date')::date,
   'date_semantics', 'payout date as stated by the source and as recorded; calendar dates; no timezone, business-day or cutoff rule')));
 END IF;
 IF st IS NULL OR jsonb_typeof(st) = 'null' THEN
  FOR it IN SELECT x FROM jsonb_array_elements(p->'items') x ORDER BY (x->>'line_no')::integer LOOP
   pay := it->'payment';
   CONTINUE WHEN jsonb_array_length(it->'links') <> 1 OR pay IS NULL OR jsonb_typeof(pay) <> 'object'
     OR pay->>'status' IS DISTINCT FROM 'RECEIVED' OR pay->>'settlement_document_id' IS NOT NULL;
   SELECT coalesce(jsonb_agg(jsonb_build_object('staged_document_id', c->'staged_document_id', 'payout', jsonb_build_array(p->'processor_ref', c->'payout_id'),
      'line_no', c->'line_no', 'statement_amount', c->'statement_amount', 'source_record_hash', c->'source_record_hash')
      ORDER BY c->>'payout_id' COLLATE "C", c->>'staged_document_id' COLLATE "C"), '[]') INTO valid
    FROM jsonb_array_elements(p->'other_claims') c WHERE c->>'item_ref' = it->>'item_ref'
     AND NOT (c->>'conflicting')::boolean AND NOT (c->>'unsupported')::boolean AND NOT (c->>'payout_recorded')::boolean;
   SELECT coalesce(jsonb_agg(jsonb_build_object('staged_document_id', c->'staged_document_id', 'payout', jsonb_build_array(p->'processor_ref', c->'payout_id'),
      'line_no', c->'line_no', 'reason', CASE WHEN (c->>'payout_recorded')::boolean THEN 'OTHER_PAYOUT_RECORDED'
        WHEN (c->>'conflicting')::boolean THEN 'OTHER_STATEMENT_AMBIGUOUS' ELSE 'OTHER_STATEMENT_UNSUPPORTED' END)
      ORDER BY c->>'payout_id' COLLATE "C", c->>'staged_document_id' COLLATE "C"), '[]') INTO excluded
    FROM jsonb_array_elements(p->'other_claims') c WHERE c->>'item_ref' = it->>'item_ref'
     AND ((c->>'conflicting')::boolean OR (c->>'unsupported')::boolean OR (c->>'payout_recorded')::boolean);
   IF jsonb_array_length(valid) > 0 THEN
    claims := claims || jsonb_build_array(jsonb_build_object('line_no', it->'line_no', 'item_ref', it->'item_ref',
      'payment_document_id', pay->'document_id', 'payment_version', pay->'version', 'payment_amount', pay->'amount',
      'statement_amount', round((it->>'amount')::numeric, e)::text, 'other_claims', valid, 'excluded_claims', excluded));
   END IF;
  END LOOP;
  IF jsonb_array_length(claims) > 0 THEN
   findings := findings || jsonb_build_array(jsonb_build_object('kind', 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'class', 'POTENTIAL_CONFLICT',
     'computed', jsonb_build_object('statement_payout', jsonb_build_array(p->'processor_ref', p->'payout_id'), 'claims', claims,
       'basis', 'exact payment identity listed by two distinct, unrecorded payouts; not a conclusion about cause')));
  END IF;
 END IF;
 RETURN jsonb_build_object('abstention', NULL, 'findings',
   (SELECT coalesce(jsonb_agg(x ORDER BY x->>'kind' COLLATE "C"), '[]') FROM jsonb_array_elements(findings) x));
END $$;

CREATE OR REPLACE FUNCTION lv.detection_config() RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object('version', '1.1.0', 'inputs', 'lv-detection-inputs/2',
  'rules', jsonb_build_array('SETTLEMENT_NOT_RECORDED', 'SETTLEMENT_AMOUNTS_DIFFER', 'SETTLEMENT_ITEMS_DIFFER', 'STATEMENT_ITEM_SETTLED_ELSEWHERE',
   'STATEMENT_ITEM_AMOUNT_DIFFERS', 'STATEMENT_ITEM_UNRESOLVED', 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'PAYOUT_DATE_DIFFERS'),
  'date_comparison', 'statement payout date vs recorded settlement payout date; calendar dates as recorded; no timezone, business-day tolerance or cutoff',
  'overlap', 'same tenant, ledger and processor; distinct payout ids; both unrecorded; payment RECEIVED and unsettled; exact identity only',
  'identity', 'processor_ref+processor_item_id (statement processor namespace only)',
  'amount_comparison', 'exact decimal at the ledger exponent', 'max_statements', 200, 'model', 'none',
  'abstain', jsonb_build_array('AMBIGUOUS_STATEMENT', 'REFUND_ITEMS_NOT_SUPPORTED', 'SOURCE_INTEGRITY_FAILED'))
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
              'as_of_seq', p_seq::text)) END
   || (SELECT coalesce(jsonb_agg(jsonb_build_object('type', 'OTHER_PAYOUT_CLAIM', 'staged_document_id', c->'staged_document_id',
        'payout_id', c->'payout_id', 'line_no', c->'line_no', 'item_ref', c->'item_ref', 'content_hash', c->'source_record_hash')
        ORDER BY c->>'item_ref' COLLATE "C", c->>'payout_id' COLLATE "C", c->>'staged_document_id'), '[]') FROM jsonb_array_elements(coalesce(p->'other_claims', '[]')) c))
$$;

CREATE OR REPLACE FUNCTION lv.detection_finding_json(f lv.detection_finding) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 WITH cur AS (SELECT lv.detection_current_fingerprint(f) AS fp)
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
 IF p_detector IS DISTINCT FROM 'lv-settlement-statement-reconciliation' OR p_version IS DISTINCT FROM '1.1.0' THEN
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
   inputs := lv.detection_inputs_v2(s.tenant_id, p_ledger, (scope->>i)::uuid);
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
  VALUES (s.tenant_id, p_ledger, 'lv-settlement-statement-reconciliation', '1.1.0', lv.detection_config(),
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
  inputs := lv.detection_inputs_v2(s.tenant_id, p_ledger, (scope->>i)::uuid);
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
  cur := lv.detection_current_fingerprint(f);
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

REVOKE ALL ON FUNCTION lv.detection_inputs_v2(uuid, uuid, uuid), lv.detection_current_fingerprint(lv.detection_finding), lv.detection_expected(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.detection_inputs_v2(uuid, uuid, uuid), lv.detection_current_fingerprint(lv.detection_finding), lv.detection_expected(jsonb) TO lv_app;
