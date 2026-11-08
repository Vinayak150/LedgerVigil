-- 0032 Statement-to-ledger settlement reconciliation findings (first detection slice; PROPOSED product choices DET-1..DET-8,
-- docs/AI_DETECTION.md). Deterministic baseline only: no model is called, `interpretation` is always NULL.
-- Findings, runs and reviewer dispositions are evidence about the books, never changes to them: no function here writes a
-- financial table, ledger counter, proposal, approval or execution. Every new table is FORCE RLS; writes go only through
-- SECURITY DEFINER functions that take the existing identity-first lock order (0027/0028); lv_app may SELECT under RLS.

-- ---------------------------------------------------------------------------------------------------------------- tables
CREATE TABLE lv.detection_run (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 detector text NOT NULL CHECK (detector = 'lv-settlement-statement-reconciliation'),
 detector_version text NOT NULL CHECK (detector_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
 config jsonb NOT NULL CHECK (jsonb_typeof(config) = 'object'), config_hash char(64) NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
 scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'array' AND jsonb_array_length(scope) <= 200),
 snapshot_seq bigint NOT NULL CHECK (snapshot_seq >= 0),
 input_fingerprint char(64) NOT NULL CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
 status text NOT NULL CHECK (status IN ('COMPLETED', 'REJECTED')),
 rejection_code text CHECK (rejection_code IS NULL OR rejection_code ~ '^[A-Z_]{1,40}$'),
 rejection_detail jsonb,
 abstentions jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(abstentions) = 'array'),
 requested_by uuid NOT NULL REFERENCES lv_identity.app_user(id), session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 request_id text NOT NULL, idempotency_key text NOT NULL, request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id) REFERENCES lv.ledger (tenant_id, id),
 CHECK ((status = 'REJECTED') = (rejection_code IS NOT NULL AND rejection_detail IS NOT NULL))
);

-- One finding = one discrepancy kind for one staged statement under one exact set of inputs (DET-4). A rerun on identical
-- inputs reuses it (UNIQUE below), so retries and concurrent runs never duplicate a finding.
CREATE TABLE lv.detection_finding (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 detector text NOT NULL, detector_version text NOT NULL,
 kind text NOT NULL CHECK (kind IN ('SETTLEMENT_NOT_RECORDED', 'SETTLEMENT_AMOUNTS_DIFFER', 'SETTLEMENT_ITEMS_DIFFER',
   'STATEMENT_ITEM_SETTLED_ELSEWHERE', 'STATEMENT_ITEM_AMOUNT_DIFFERS', 'STATEMENT_ITEM_UNRESOLVED')),
 class text NOT NULL CHECK (class IN ('DISCREPANCY', 'EVIDENCE_GAP')),
 staged_document_id uuid NOT NULL,
 finding_key text NOT NULL CHECK (length(finding_key) <= 200),
 input_fingerprint char(64) NOT NULL CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
 inputs jsonb NOT NULL,            -- the frozen evidence set exactly as compared (lv.detection_inputs)
 observed jsonb NOT NULL,          -- SOURCE observation from the staged statement (never an authoritative fact)
 recorded jsonb NOT NULL,          -- authoritative ledger facts at the run's snapshot
 computed jsonb NOT NULL,          -- deterministic discrepancy, exact decimal strings at the ledger exponent
 interpretation jsonb CHECK (interpretation IS NULL),  -- model interpretation slot: no model in this slice (DET-1)
 evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) >= 1),
 first_run_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, ledger_id, finding_key, input_fingerprint),
 FOREIGN KEY (tenant_id, ledger_id, staged_document_id) REFERENCES lv.staged_document (tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id, first_run_id) REFERENCES lv.detection_run (tenant_id, ledger_id, id),
 CHECK (finding_key = detector || ':' || kind || ':' || staged_document_id::text),
 CHECK ((class = 'EVIDENCE_GAP') = (kind = 'STATEMENT_ITEM_UNRESOLVED'))
);

CREATE TABLE lv.detection_run_finding (
 tenant_id uuid NOT NULL, run_id uuid NOT NULL, finding_id uuid NOT NULL, created boolean NOT NULL,
 PRIMARY KEY (tenant_id, run_id, finding_id),
 FOREIGN KEY (tenant_id, run_id) REFERENCES lv.detection_run (tenant_id, id),
 FOREIGN KEY (tenant_id, finding_id) REFERENCES lv.detection_finding (tenant_id, id)
);

-- Current review pointer, advanced only by compare-and-swap inside lv.detection_dispose (If-Match "v<version>").
CREATE TABLE lv.detection_review (
 tenant_id uuid NOT NULL, finding_id uuid NOT NULL, version integer NOT NULL CHECK (version >= 1),
 status text NOT NULL CHECK (status IN ('OPEN', 'CONFIRMED', 'DISMISSED')), last_disposition_id uuid,
 PRIMARY KEY (tenant_id, finding_id), FOREIGN KEY (tenant_id, finding_id) REFERENCES lv.detection_finding (tenant_id, id),
 CHECK ((version = 1) = (last_disposition_id IS NULL)), CHECK (version > 1 OR status = 'OPEN')
);

-- Immutable reviewer actions (DET-3). The note is untrusted user text, stored but never interpreted.
CREATE TABLE lv.detection_disposition (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(), finding_id uuid NOT NULL,
 review_version integer NOT NULL CHECK (review_version >= 2),
 action text NOT NULL CHECK (action IN ('CONFIRMED', 'DISMISSED', 'REOPENED')),
 reason_code text NOT NULL CHECK (reason_code IN ('VERIFIED_AGAINST_EVIDENCE', 'EXPECTED_TIMING', 'STATEMENT_ERROR', 'KNOWN_EXCEPTION',
   'NOT_A_DISCREPANCY', 'INPUTS_CHANGED', 'REVIEW_CORRECTION')),
 -- Flat comparisons, not BETWEEN: BETWEEN nested inside AND deparses differently after pg_dump/pg_restore, which the recovery
 -- drill's semantic catalog comparison (tools.verify_recovery.schema_catalog) would report as RECOVERY_SCHEMA_MISMATCH.
 note text CHECK (note IS NULL OR (length(note) >= 1 AND length(note) <= 2000 AND note ~ '[^[:space:]]')),
 freshness text NOT NULL CHECK (freshness IN ('CURRENT', 'STALE')),
 input_fingerprint_at_decision char(64) NOT NULL CHECK (input_fingerprint_at_decision ~ '^[0-9a-f]{64}$'),
 decided_by uuid NOT NULL REFERENCES lv_identity.app_user(id), session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 request_id text NOT NULL, idempotency_key text NOT NULL, request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
 PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, finding_id, review_version),
 FOREIGN KEY (tenant_id, finding_id) REFERENCES lv.detection_finding (tenant_id, id),
 CHECK (action <> 'CONFIRMED' OR (freshness = 'CURRENT' AND reason_code = 'VERIFIED_AGAINST_EVIDENCE')),
 CHECK (action <> 'REOPENED' OR reason_code = 'REVIEW_CORRECTION'),
 CHECK (action <> 'DISMISSED' OR reason_code NOT IN ('VERIFIED_AGAINST_EVIDENCE', 'REVIEW_CORRECTION'))
);

DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['detection_run', 'detection_finding', 'detection_run_finding', 'detection_review', 'detection_disposition'] LOOP
  -- No format(): migration SQL runs through exec_driver_sql, where a percent sign is a parameter placeholder.
  EXECUTE 'ALTER TABLE lv.' || quote_ident(t) || ' ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE lv.' || quote_ident(t) || ' FORCE ROW LEVEL SECURITY';
  EXECUTE 'CREATE POLICY tenant_scope ON lv.' || quote_ident(t) || ' USING (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) '
          'WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)';
  EXECUTE 'REVOKE ALL ON lv.' || quote_ident(t) || ' FROM PUBLIC, lv_app, lv_seed, lv_ingest, lv_worker';
  EXECUTE 'GRANT SELECT ON lv.' || quote_ident(t) || ' TO lv_app';
 END LOOP;
END $$;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.detection_run FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.detection_finding FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.detection_run_finding FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.detection_disposition FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();

CREATE FUNCTION lv.guard_detection_review() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF TG_OP = 'DELETE' OR NEW.tenant_id <> OLD.tenant_id OR NEW.finding_id <> OLD.finding_id OR NEW.version <> OLD.version + 1 THEN
  RAISE EXCEPTION 'detection review advances only by compare-and-swap' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER review_cas BEFORE UPDATE OR DELETE ON lv.detection_review FOR EACH ROW EXECUTE FUNCTION lv.guard_detection_review();
REVOKE ALL ON FUNCTION lv.guard_detection_review() FROM PUBLIC;

-- --------------------------------------------------------------------------------------- frozen evidence set (authoritative)
-- Everything the comparison may depend on for one staged statement, read from current committed state. SECURITY INVOKER:
-- lv_app reads it under its own RLS scope; the writers call it after taking the ledger counter lock, so it is stable there.
-- Identity is the persisted (processor_ref, processor_item_id) pair in the statement's processor namespace only; amounts,
-- dates and memo text never establish identity (Stage 2A §19.3; kernel_adapter.evidence E-ITEM-01).
CREATE FUNCTION lv.detection_inputs(p_tenant uuid, p_ledger uuid, p_staged uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 WITH s AS (
  SELECT s.*, r.content_hash AS record_hash, f.id AS file_id, f.sha256 AS file_sha256
  FROM lv.staged_document s
  JOIN lv.source_record r ON (r.tenant_id, r.ledger_id, r.id, r.import_batch_id) = (s.tenant_id, s.ledger_id, s.source_record_id, s.import_batch_id)
  JOIN lv.import_batch b ON (b.tenant_id, b.ledger_id, b.id) = (s.tenant_id, s.ledger_id, s.import_batch_id)
  JOIN lv.source_file f ON (f.tenant_id, f.ledger_id, f.id) = (b.tenant_id, b.ledger_id, b.source_file_id)
  WHERE s.tenant_id = p_tenant AND s.ledger_id = p_ledger AND s.id = p_staged),
 st AS (
  SELECT sc.document_id, sc.version, sc.payout_date, sc.gross, sc.fee, sc.net, sc.status, sc.currency_exponent
  FROM s JOIN lv.processor_settlement_identity i ON i.tenant_id = s.tenant_id AND i.ledger_id = s.ledger_id
    AND i.processor_ref = s.processor_ref AND i.payout_id = s.payout_id
  JOIN lv.settlement_current sc ON (sc.tenant_id, sc.ledger_id, sc.document_id) = (i.tenant_id, i.ledger_id, i.document_id)),
 items AS (
  SELECT it.line_no, it.item_ref, it.kind::text AS kind, it.amount,
   (SELECT coalesce(jsonb_agg(pi.document_id::text ORDER BY pi.document_id), '[]') FROM lv.processor_payment_identity pi
     WHERE pi.tenant_id = s.tenant_id AND pi.ledger_id = s.ledger_id AND pi.processor_ref = s.processor_ref AND pi.processor_item_id = it.item_ref) AS links,
   (SELECT coalesce(jsonb_agg(DISTINCT pi.processor_ref ORDER BY pi.processor_ref), '[]') FROM lv.processor_payment_identity pi
     WHERE pi.tenant_id = s.tenant_id AND pi.ledger_id = s.ledger_id AND pi.processor_ref <> s.processor_ref AND pi.processor_item_id = it.item_ref) AS other_namespaces,
   (SELECT jsonb_build_object('document_id', pc.document_id::text, 'version', pc.version, 'currency', pc.currency,
      'amount', round(pc.amount, pc.currency_exponent)::text, 'status', pc.status,
      'settlement_document_id', pc.settlement_document_id::text)
     FROM lv.processor_payment_identity pi JOIN lv.payment_current pc ON (pc.tenant_id, pc.ledger_id, pc.document_id) = (pi.tenant_id, pi.ledger_id, pi.document_id)
     WHERE pi.tenant_id = s.tenant_id AND pi.ledger_id = s.ledger_id AND pi.processor_ref = s.processor_ref AND pi.processor_item_id = it.item_ref
     ORDER BY pi.document_id LIMIT 1) AS payment
  FROM s JOIN lv.staged_statement_item it ON (it.tenant_id, it.ledger_id, it.staged_document_id) = (s.tenant_id, s.ledger_id, s.id))
 SELECT jsonb_build_object(
  'schema', 'lv-detection-inputs/1',
  'staged_document_id', s.id::text, 'processor_ref', s.processor_ref, 'payout_id', s.payout_id, 'payout_date', s.payout_date::text,
  'currency', s.currency, 'exponent', s.currency_exponent, 'gross', s.gross, 'fee', s.fee, 'net', s.net,
  'financial_fingerprint', s.financial_fingerprint, 'source_record_id', s.source_record_id::text, 'source_record_hash', s.record_hash,
  'source_file_id', s.file_id::text, 'source_file_sha256', s.file_sha256,
  'conflicting_import_batches', (SELECT coalesce(jsonb_agg(b.id::text ORDER BY b.id), '[]') FROM lv.import_batch b
     WHERE b.tenant_id = s.tenant_id AND b.ledger_id = s.ledger_id AND b.status = 'REJECTED' AND b.related_staged_document_id = s.id),
  'items', (SELECT coalesce(jsonb_agg(jsonb_build_object('line_no', line_no, 'item_ref', item_ref, 'kind', kind, 'amount', amount,
     'links', links, 'other_namespaces', other_namespaces, 'payment', payment) ORDER BY line_no), '[]') FROM items),
  'settlement', (SELECT jsonb_build_object('document_id', st.document_id::text, 'version', st.version, 'payout_date', st.payout_date::text,
     'gross', round(st.gross, st.currency_exponent)::text, 'fee', round(st.fee, st.currency_exponent)::text,
     'net', round(st.net, st.currency_exponent)::text, 'status', st.status,
     'included_payment_document_ids', (SELECT coalesce(jsonb_agg(ic.item_document_id::text ORDER BY ic.item_document_id), '[]')
        FROM lv.settlement_inclusion_current ic WHERE ic.tenant_id = p_tenant AND ic.ledger_id = p_ledger AND ic.settlement_document_id = st.document_id))
     FROM st))
 FROM s
$$;

CREATE FUNCTION lv.detection_fingerprint(p_inputs jsonb) RETURNS char(64)
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$ SELECT encode(sha256(convert_to(p_inputs::text, 'UTF8')), 'hex') $$;

-- ----------------------------------------------------------------------------------------- independent database oracle
-- The detector's configuration is fixed server-side: a run whose detector reports any other configuration is rejected.
CREATE FUNCTION lv.detection_config() RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object('identity', 'processor_ref+processor_item_id (statement processor namespace only)',
  'amount_comparison', 'exact decimal at the ledger exponent', 'max_statements', 200, 'model', 'none',
  'abstain', jsonb_build_array('AMBIGUOUS_STATEMENT', 'REFUND_ITEMS_NOT_SUPPORTED', 'SOURCE_INTEGRITY_FAILED'))
$$;

-- Expected findings for one frozen evidence set, written from the requirements independently of the Python detector
-- (ledgervigil.detection.detector). The record function accepts detector output only if it equals this exactly, so a
-- malformed, partial or fabricated detector result is rejected rather than persisted (ADR-017: invalid output never
-- becomes a finding). Amounts are exact NUMERIC rounded to the ledger exponent for display only; nothing is rounded away
-- because every stored amount already has at most that scale.
CREATE FUNCTION lv.detection_expected(p jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE e integer := (p->>'exponent')::integer; it jsonb; findings jsonb := '[]'; unresolved jsonb := '[]'; differs jsonb := '[]';
 elsewhere jsonb := '[]'; received jsonb := '[]'; resolved jsonb := '[]'; received_total numeric := 0; unresolved_total numeric := 0;
 st jsonb := p->'settlement'; pay jsonb; fields jsonb := '[]'; f text; missing jsonb; extra jsonb; reason text;
BEGIN
 IF jsonb_array_length(p->'conflicting_import_batches') > 0 THEN
  RETURN jsonb_build_object('abstention', 'AMBIGUOUS_STATEMENT', 'findings', '[]'::jsonb);
 END IF;
 IF EXISTS (SELECT 1 FROM jsonb_array_elements(p->'items') x WHERE x->>'kind' <> 'PAYMENT') THEN
  RETURN jsonb_build_object('abstention', 'REFUND_ITEMS_NOT_SUPPORTED', 'findings', '[]'::jsonb);
 END IF;
 FOR it IN SELECT x FROM jsonb_array_elements(p->'items') x ORDER BY (x->>'line_no')::integer LOOP
  pay := it->'payment';
  IF jsonb_array_length(it->'links') <> 1 OR pay IS NULL OR jsonb_typeof(pay) = 'null' THEN
   reason := CASE WHEN jsonb_array_length(it->'links') > 1 THEN 'AMBIGUOUS_LINK'
                  WHEN jsonb_array_length(it->'other_namespaces') > 0 THEN 'OTHER_NAMESPACE_ONLY' ELSE 'NO_IDENTITY_LINK' END;
   unresolved := unresolved || jsonb_build_array(jsonb_build_object('line_no', it->'line_no', 'item_ref', it->'item_ref',
     'statement_amount', round((it->>'amount')::numeric, e)::text, 'reason', reason, 'other_namespaces', it->'other_namespaces'));
   unresolved_total := unresolved_total + (it->>'amount')::numeric;
   CONTINUE;
  END IF;
  resolved := resolved || to_jsonb(pay->>'document_id');
  IF (it->>'amount')::numeric <> (pay->>'amount')::numeric THEN
   differs := differs || jsonb_build_array(jsonb_build_object('line_no', it->'line_no', 'item_ref', it->'item_ref',
     'payment_document_id', pay->'document_id', 'payment_version', pay->'version',
     'statement_amount', round((it->>'amount')::numeric, e)::text, 'ledger_amount', round((pay->>'amount')::numeric, e)::text,
     'difference', round((it->>'amount')::numeric - (pay->>'amount')::numeric, e)::text));
  END IF;
  IF pay->>'settlement_document_id' IS NOT NULL AND (st IS NULL OR jsonb_typeof(st) = 'null' OR pay->>'settlement_document_id' <> st->>'document_id') THEN
   elsewhere := elsewhere || jsonb_build_array(jsonb_build_object('line_no', it->'line_no', 'item_ref', it->'item_ref',
     'payment_document_id', pay->'document_id', 'settled_in_document_id', pay->'settlement_document_id'));
  END IF;
  IF pay->>'status' = 'RECEIVED' THEN
   received := received || to_jsonb(pay->>'document_id'); received_total := received_total + (pay->>'amount')::numeric;
  END IF;
 END LOOP;
 IF st IS NULL OR jsonb_typeof(st) = 'null' THEN
  findings := findings || jsonb_build_array(jsonb_build_object('kind', 'SETTLEMENT_NOT_RECORDED', 'class', 'DISCREPANCY', 'computed', jsonb_build_object(
   'payout', jsonb_build_array(p->'processor_ref', p->'payout_id'),
   'statement_gross', round((p->>'gross')::numeric, e)::text, 'statement_fee', round((p->>'fee')::numeric, e)::text,
   'statement_net', round((p->>'net')::numeric, e)::text,
   'received_payment_document_ids', (SELECT coalesce(jsonb_agg(v ORDER BY v COLLATE "C"), '[]') FROM jsonb_array_elements_text(received) v),
   'received_total', round(received_total, e)::text,
   'resolved_item_count', jsonb_array_length(resolved), 'unresolved_item_count', jsonb_array_length(unresolved))));
 ELSE
  FOREACH f IN ARRAY ARRAY['gross', 'fee', 'net'] LOOP
   IF (p->>f)::numeric <> (st->>f)::numeric THEN
    fields := fields || jsonb_build_array(jsonb_build_object('field', f, 'statement', round((p->>f)::numeric, e)::text,
      'ledger', round((st->>f)::numeric, e)::text, 'difference', round((p->>f)::numeric - (st->>f)::numeric, e)::text));
   END IF;
  END LOOP;
  IF jsonb_array_length(fields) > 0 THEN
   findings := findings || jsonb_build_array(jsonb_build_object('kind', 'SETTLEMENT_AMOUNTS_DIFFER', 'class', 'DISCREPANCY', 'computed',
     jsonb_build_object('settlement_document_id', st->'document_id', 'settlement_version', st->'version', 'fields', fields)));
  END IF;
  SELECT coalesce(jsonb_agg(v ORDER BY v COLLATE "C"), '[]') INTO missing FROM (SELECT DISTINCT v FROM jsonb_array_elements_text(resolved) v
    EXCEPT SELECT v FROM jsonb_array_elements_text(st->'included_payment_document_ids') v) m;
  SELECT coalesce(jsonb_agg(v ORDER BY v COLLATE "C"), '[]') INTO extra FROM (SELECT v FROM jsonb_array_elements_text(st->'included_payment_document_ids') v
    EXCEPT SELECT v FROM jsonb_array_elements_text(resolved) v) x;
  IF jsonb_array_length(missing) > 0 OR jsonb_array_length(extra) > 0 THEN
   findings := findings || jsonb_build_array(jsonb_build_object('kind', 'SETTLEMENT_ITEMS_DIFFER', 'class', 'DISCREPANCY', 'computed',
     jsonb_build_object('settlement_document_id', st->'document_id', 'settlement_version', st->'version',
       'on_statement_not_in_settlement', missing, 'in_settlement_not_on_statement', extra)));
  END IF;
 END IF;
 IF jsonb_array_length(elsewhere) > 0 THEN
  findings := findings || jsonb_build_array(jsonb_build_object('kind', 'STATEMENT_ITEM_SETTLED_ELSEWHERE', 'class', 'DISCREPANCY',
    'computed', jsonb_build_object('items', elsewhere)));
 END IF;
 IF jsonb_array_length(differs) > 0 THEN
  findings := findings || jsonb_build_array(jsonb_build_object('kind', 'STATEMENT_ITEM_AMOUNT_DIFFERS', 'class', 'DISCREPANCY',
    'computed', jsonb_build_object('items', differs)));
 END IF;
 IF jsonb_array_length(unresolved) > 0 THEN
  findings := findings || jsonb_build_array(jsonb_build_object('kind', 'STATEMENT_ITEM_UNRESOLVED', 'class', 'EVIDENCE_GAP',
    'computed', jsonb_build_object('items', unresolved, 'unresolved_total', round(unresolved_total, e)::text)));
 END IF;
 RETURN jsonb_build_object('abstention', NULL, 'findings',
   (SELECT coalesce(jsonb_agg(x ORDER BY x->>'kind' COLLATE "C"), '[]') FROM jsonb_array_elements(findings) x));
END $$;

-- Observed facts (statement), recorded facts (ledger) and citations, all derived from the frozen evidence set by the
-- database, never accepted from the detector.
CREATE FUNCTION lv.detection_facts(p jsonb, p_seq bigint) RETURNS jsonb
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

-- ------------------------------------------------------------------------------------------------------- representations
CREATE FUNCTION lv.detection_run_json(r lv.detection_run) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object('id', r.id::text, 'ledger_id', r.ledger_id::text, 'detector', r.detector, 'detector_version', r.detector_version,
  'config', r.config, 'config_hash', r.config_hash, 'status', r.status, 'rejection_code', r.rejection_code, 'rejection_detail', r.rejection_detail,
  'snapshot_seq', r.snapshot_seq, 'input_fingerprint', r.input_fingerprint, 'scope', r.scope, 'abstentions', r.abstentions,
  'findings', (SELECT coalesce(jsonb_agg(jsonb_build_object('id', f.id::text, 'kind', f.kind, 'class', f.class,
     'staged_document_id', f.staged_document_id::text, 'created_by_this_run', rf.created) ORDER BY f.staged_document_id, f.kind), '[]')
     FROM lv.detection_run_finding rf JOIN lv.detection_finding f ON (f.tenant_id, f.id) = (rf.tenant_id, rf.finding_id)
     WHERE rf.tenant_id = r.tenant_id AND rf.run_id = r.id),
  'finding_count', (SELECT count(*) FROM lv.detection_run_finding rf WHERE rf.tenant_id = r.tenant_id AND rf.run_id = r.id),
  'new_finding_count', (SELECT count(*) FROM lv.detection_run_finding rf WHERE rf.tenant_id = r.tenant_id AND rf.run_id = r.id AND rf.created),
  'requested_by', r.requested_by::text, 'created_at', to_char(r.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'basis', 'DETERMINISTIC_COMPARISON', 'model', 'NONE', 'accounting_review_status', 'UNREVIEWED')
$$;

-- Freshness is recomputed from current committed state on every read (DET-5): STALE as soon as any compared input changes.
CREATE FUNCTION lv.detection_finding_json(f lv.detection_finding) RETURNS jsonb
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

-- ------------------------------------------------------------------------------------------------------------- writers
-- Phase 1 of a run. Verified session, propose permission, nonblocking idempotency key lock, then the 0027/0028 lock order
-- (user -> membership -> ordered roles -> session), revalidation, touch, replay; then the ledger row FOR KEY SHARE and the
-- ledger counter FOR SHARE (the writers' ledger -> counter order), which
-- every financial commit takes FOR UPDATE, so the ledger cannot change until this transaction ends. Returns the scope.
CREATE FUNCTION lv.detection_begin(tok bytea, p_ledger uuid, ikey text, rhash text, p_scope jsonb, p_problem jsonb, src text, req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; l lv.ledger; prior lv.api_idempotency; problem jsonb := p_problem; scope jsonb; seq bigint := 0; missing jsonb;
 route_name text := '/api/v1/ledgers/{ledger_id}/detection-runs';
BEGIN
 s := lv.proposal_session(tok, src, req);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'propose') THEN RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires propose'); END IF;
 IF ikey IS NULL OR ikey !~ '^[!-~]{1,64}$' OR rhash IS NULL OR rhash !~ '^[0-9a-f]{64}$' THEN RETURN jsonb_build_object('status', 400, 'code', 'IDEMPOTENCY_KEY_REQUIRED'); END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('lv-detect:' || s.tenant_id::text || ':' || s.user_id::text || ':' || ikey, 0)) THEN
  RETURN jsonb_build_object('status', 409, 'code', 'REQUEST_IN_PROGRESS', 'retry_after', 1); END IF;
 PERFORM 1 FROM lv_identity.app_user WHERE id = s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id = s.tenant_id AND user_id = s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id = s.tenant_id AND user_id = s.user_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv_identity.session WHERE id = s.id FOR UPDATE;
 s := lv.proposal_session(tok, src, req);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'propose') THEN RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires propose'); END IF;
 PERFORM lv_identity.session_context(tok, true);
 SELECT * INTO prior FROM lv.api_idempotency i WHERE i.tenant_id = s.tenant_id AND i.principal_id = s.user_id AND i.method = 'POST'
  AND i.route = route_name AND i.idempotency_key = ikey;
 IF FOUND AND prior.expires_at > statement_timestamp() THEN
  IF prior.request_hash <> rhash THEN RETURN jsonb_build_object('status', 422, 'code', 'IDEMPOTENCY_KEY_REUSED'); END IF;
  RETURN prior.response || jsonb_build_object('replayed', true);
 END IF;
 -- Ledger row before the counter: the order every financial/workflow writer uses (ledger FOR UPDATE, then counter FOR UPDATE).
 -- FOR KEY SHARE also covers detection_run's foreign key, which would otherwise need the row after the counter (a deadlock).
 SELECT * INTO l FROM lv.ledger WHERE tenant_id = s.tenant_id AND id = p_ledger FOR KEY SHARE;
 IF NOT FOUND THEN problem := jsonb_build_object('status', 404, 'code', 'NOT_FOUND', 'detail', 'ledger not found');
 ELSIF problem IS NOT NULL THEN NULL;
 ELSIF l.purpose <> 'SANDBOX' OR l.source_kind <> 'NATIVE' THEN
  problem := jsonb_build_object('status', 422, 'code', 'CAPABILITY_UNAVAILABLE', 'detail', 'detection reads synthetic statements of native SANDBOX ledgers only');
 ELSE
  SELECT last_seq INTO seq FROM lv.ledger_counter WHERE tenant_id = s.tenant_id AND ledger_id = l.id FOR SHARE;
  seq := coalesce(seq, 0);
  IF p_scope IS NULL THEN
   SELECT coalesce(jsonb_agg(d.id::text ORDER BY d.id), '[]') INTO scope FROM lv.staged_document d WHERE d.tenant_id = s.tenant_id AND d.ledger_id = l.id;
   IF jsonb_array_length(scope) > 200 THEN
    problem := jsonb_build_object('status', 422, 'code', 'SCOPE_TOO_LARGE', 'detail', 'more than 200 staged statements; list staged_document_ids explicitly');
   END IF;
  ELSE
   SELECT coalesce(jsonb_agg(jsonb_build_object('pointer', '/staged_document_ids/' || (x.ord - 1)::text, 'code', 'not_found') ORDER BY x.ord), '[]')
    INTO missing FROM jsonb_array_elements_text(p_scope) WITH ORDINALITY x(v, ord)
    WHERE NOT EXISTS (SELECT 1 FROM lv.staged_document d WHERE d.tenant_id = s.tenant_id AND d.ledger_id = l.id AND d.id::text = x.v);
   IF jsonb_array_length(missing) > 0 THEN
    problem := jsonb_build_object('status', 404, 'code', 'NOT_FOUND', 'detail', 'staged statement not found in this ledger', 'errors', missing);
   ELSE
    SELECT jsonb_agg(v ORDER BY v) INTO scope FROM (SELECT DISTINCT v FROM jsonb_array_elements_text(p_scope) v) u;
   END IF;
  END IF;
 END IF;
 IF problem IS NOT NULL THEN
  IF (problem->>'status')::integer NOT BETWEEN 400 AND 499 THEN RAISE EXCEPTION 'invalid detection refusal' USING ERRCODE = '22023'; END IF;
  PERFORM lv_identity.audit_append('DETECTION_RUN_REFUSED', 'DENIED', s.user_id, s.tenant_id, s.id, src, req,
   jsonb_build_object('ledger_id', p_ledger::text, 'reason', problem->>'code'));
  DELETE FROM lv.api_idempotency i WHERE i.tenant_id = s.tenant_id AND i.principal_id = s.user_id AND i.method = 'POST' AND i.route = route_name AND i.idempotency_key = ikey;
  INSERT INTO lv.api_idempotency(tenant_id, principal_id, method, route, idempotency_key, request_hash, response_status, response, expires_at)
  VALUES (s.tenant_id, s.user_id, 'POST', route_name, ikey, rhash, (problem->>'status')::smallint, problem, transaction_timestamp() + interval '24 hours');
  RETURN problem || jsonb_build_object('replayed', false);
 END IF;
 PERFORM set_config('lv.detection_begun', s.tenant_id::text || ':' || l.id::text || ':' || ikey || ':' || rhash || ':' || seq::text, true);
 PERFORM set_config('lv.detection_scope', scope::text, true);
 RETURN jsonb_build_object('status', 100, 'code', 'PROCEED', 'tenant_id', s.tenant_id::text, 'snapshot_seq', seq, 'scope', scope,
  'config', lv.detection_config());
END $$;

-- Phase 2 of a run, in the same transaction. Re-checks identity and locks (re-entrant), then treats the detector's output
-- as untrusted: it must cover exactly the scope, carry the database-computed input fingerprint of each statement, and
-- equal lv.detection_expected exactly. Otherwise the run is stored as REJECTED (counted, audited, no finding) and the
-- caller gets 502. Valid output becomes deduplicated findings with database-derived facts and citations.
CREATE FUNCTION lv.detection_record(tok bytea, p_ledger uuid, ikey text, rhash text, p_detector text, p_version text, p_config jsonb,
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

-- Reviewer disposition (DET-3). propose or review; If-Match on the review version; CONFIRMED only while CURRENT. Changes the
-- review pointer and appends an immutable disposition; never touches the finding, a financial table or a proposal.
CREATE FUNCTION lv.detection_dispose(tok bytea, p_finding uuid, p_match text, ikey text, rhash text, p_action text, p_reason text, p_note text,
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

-- ---------------------------------------------------------------------------------------- commit-time completeness checks
CREATE FUNCTION lv.validate_detection_run() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF NEW.tx_id <> pg_current_xact_id()
 OR NOT EXISTS (SELECT 1 FROM lv_identity.audit_event a WHERE a.tenant_id = NEW.tenant_id AND a.user_id = NEW.requested_by AND a.session_id = NEW.session_id
   AND a.request_id = NEW.request_id AND a.detail->>'run_id' = NEW.id::text
   AND ((NEW.status = 'COMPLETED' AND a.event_type = 'DETECTION_RUN_COMPLETED' AND a.outcome = 'ALLOWED'
         AND a.detail->>'findings' = (SELECT count(*) FROM lv.detection_run_finding rf WHERE rf.tenant_id = NEW.tenant_id AND rf.run_id = NEW.id)::text)
     OR (NEW.status = 'REJECTED' AND a.event_type = 'DETECTION_RUN_REJECTED' AND a.outcome = 'DENIED')))
 OR (NEW.status = 'COMPLETED' AND NOT EXISTS (SELECT 1 FROM lv.api_idempotency i WHERE i.tenant_id = NEW.tenant_id AND i.principal_id = NEW.requested_by
   AND i.method = 'POST' AND i.route = '/api/v1/ledgers/{ledger_id}/detection-runs' AND i.idempotency_key = NEW.idempotency_key
   AND i.request_hash = NEW.request_hash AND i.response_status = 201 AND i.response->'body' = lv.detection_run_json(NEW)))
 THEN RAISE EXCEPTION 'detection run lacks its atomic audit/cache' USING ERRCODE = '23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER detection_run_complete AFTER INSERT ON lv.detection_run DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION lv.validate_detection_run();

CREATE FUNCTION lv.validate_detection_finding() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF NEW.tx_id <> pg_current_xact_id() OR NEW.input_fingerprint <> lv.detection_fingerprint(NEW.inputs)
 OR NOT EXISTS (SELECT 1 FROM lv.detection_run r WHERE r.tenant_id = NEW.tenant_id AND r.id = NEW.first_run_id AND r.tx_id = NEW.tx_id AND r.status = 'COMPLETED')
 OR NOT EXISTS (SELECT 1 FROM lv.detection_run_finding rf WHERE rf.tenant_id = NEW.tenant_id AND rf.run_id = NEW.first_run_id AND rf.finding_id = NEW.id AND rf.created)
 OR NOT EXISTS (SELECT 1 FROM lv.detection_review rv WHERE rv.tenant_id = NEW.tenant_id AND rv.finding_id = NEW.id)
 OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(lv.detection_expected(NEW.inputs)->'findings') x
     WHERE x->>'kind' = NEW.kind AND x->>'class' = NEW.class AND x->'computed' = NEW.computed)
 THEN RAISE EXCEPTION 'detection finding lacks its run, review or database-verified computation' USING ERRCODE = '23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER detection_finding_complete AFTER INSERT ON lv.detection_finding DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION lv.validate_detection_finding();

CREATE FUNCTION lv.validate_detection_disposition() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF NEW.tx_id <> pg_current_xact_id()
 OR NOT EXISTS (SELECT 1 FROM lv.detection_review rv WHERE rv.tenant_id = NEW.tenant_id AND rv.finding_id = NEW.finding_id
   AND rv.version = NEW.review_version AND rv.last_disposition_id = NEW.id
   AND rv.status = CASE NEW.action WHEN 'REOPENED' THEN 'OPEN' ELSE NEW.action END)
 OR NOT EXISTS (SELECT 1 FROM lv_identity.audit_event a WHERE a.tenant_id = NEW.tenant_id AND a.user_id = NEW.decided_by AND a.session_id = NEW.session_id
   AND a.event_type = 'DETECTION_FINDING_DISPOSED' AND a.outcome = 'ALLOWED' AND a.request_id = NEW.request_id
   AND a.detail->>'disposition_id' = NEW.id::text AND a.detail->>'action' = NEW.action AND a.detail->>'review_version' = NEW.review_version::text)
 OR NOT EXISTS (SELECT 1 FROM lv.api_idempotency i WHERE i.tenant_id = NEW.tenant_id AND i.principal_id = NEW.decided_by AND i.method = 'POST'
   AND i.route = '/api/v1/detection-findings/{finding_id}/dispositions' AND i.idempotency_key = NEW.idempotency_key
   AND i.request_hash = NEW.request_hash AND i.response_status = 200)
 THEN RAISE EXCEPTION 'detection disposition lacks its review CAS, audit or cache' USING ERRCODE = '23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER detection_disposition_complete AFTER INSERT ON lv.detection_disposition DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION lv.validate_detection_disposition();

-- ------------------------------------------------------------------------------------------------------------- grants
REVOKE ALL ON FUNCTION lv.detection_inputs(uuid, uuid, uuid), lv.detection_fingerprint(jsonb), lv.detection_config(), lv.detection_expected(jsonb),
 lv.detection_facts(jsonb, bigint), lv.detection_run_json(lv.detection_run), lv.detection_finding_json(lv.detection_finding),
 lv.detection_begin(bytea, uuid, text, text, jsonb, jsonb, text, text),
 lv.detection_record(bytea, uuid, text, text, text, text, jsonb, jsonb, text, text),
 lv.detection_dispose(bytea, uuid, text, text, text, text, text, text, jsonb, text, text),
 lv.validate_detection_run(), lv.validate_detection_finding(), lv.validate_detection_disposition() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.detection_inputs(uuid, uuid, uuid), lv.detection_fingerprint(jsonb), lv.detection_config(), lv.detection_expected(jsonb),
 lv.detection_facts(jsonb, bigint), lv.detection_run_json(lv.detection_run), lv.detection_finding_json(lv.detection_finding),
 lv.detection_begin(bytea, uuid, text, text, jsonb, jsonb, text, text),
 lv.detection_record(bytea, uuid, text, text, text, text, jsonb, jsonb, text, text),
 lv.detection_dispose(bytea, uuid, text, text, text, text, text, text, jsonb, text, text) TO lv_app;
