"""Proposal drafts and immutable revision history (Stage 2B Family 8: POST/GET/PATCH /proposals, GET /proposals/{id}/revisions).

Scope: DRAFT proposals only, native SANDBOX ledgers only, operation type RecordSettlement only. Nothing here submits,
simulates, approves or executes, and nothing writes an authoritative financial table (documents, journal, projections,
counters, commit batches or ledger events).

- Tables (Stage 2A §11.2 workflow group, as far as drafts need them):
  * lv.proposal            one row per proposal; state is DRAFT only in this revision (CHECK); version = revision while DRAFT.
  * lv.proposal_revision   immutable content of every revision (operations, evidence, rationale), its author and session,
                           and a SHA-256 over the canonical content; revision 1 is CREATED, later ones EDITED.
  * lv.proposal_reference  one row per id a revision references, with composite (tenant, ledger, id) foreign keys to the
                           referenced row, so a revision can never point at another tenant's or another ledger's data.
- Writes happen only inside two SECURITY DEFINER functions owned by lv_owner with a pinned search_path:
  lv.proposal_create and lv.proposal_edit. They re-resolve the session from its token hash, bind tenant/user/session, re-check
  `propose`, write the rows, the identity audit event and (for create) the stored idempotent response in ONE transaction.
  lv_app gets EXECUTE on those two functions and SELECT on the new tables (FORCE RLS, tenant policy); no INSERT/UPDATE/DELETE.
- A deferred validator re-checks every inserted revision at COMMIT: contiguous revisions, proposal head = latest revision,
  maker = author of revision 1, reference rows consistent with the content, and a matching audit event.
- Create idempotency reuses lv.api_idempotency (0009) with route '/api/v1/proposals' and the Part 8.1 request hash of the
  canonical body.
"""
from alembic import op

revision = '0010_proposal_drafts'
down_revision = '0009_period_close'
branch_labels = None
depends_on = None

NEW_TABLES = ('proposal', 'proposal_revision', 'proposal_reference')
POLICY = "tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid"

# NOTE: executed with exec_driver_sql, so the text must not contain a percent sign.
UPGRADE = r"""
CREATE TYPE lv.proposal_state AS ENUM ('DRAFT', 'SUBMITTED', 'BLOCKED', 'READY_FOR_REVIEW', 'APPROVED', 'REJECTED', 'STALE',
 'EXECUTING', 'EXECUTED', 'EXECUTION_FAILED', 'NEEDS_ATTENTION', 'WITHDRAWN');

CREATE TABLE lv.proposal (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 state lv.proposal_state NOT NULL DEFAULT 'DRAFT' CHECK (state = 'DRAFT'),
 origin text NOT NULL DEFAULT 'HUMAN' CHECK (origin = 'HUMAN'),
 target_adapter text NOT NULL CHECK (target_adapter = 'NATIVE'),
 revision integer NOT NULL CHECK (revision > 0),
 version integer NOT NULL CHECK (version > 0),
 created_by uuid NOT NULL REFERENCES lv_identity.app_user(id),
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id, id),
 UNIQUE (tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id) REFERENCES lv.ledger(tenant_id, id),
 CHECK (state <> 'DRAFT' OR version = revision)
);
CREATE INDEX proposal_queue ON lv.proposal (tenant_id, ledger_id, state);
CREATE INDEX proposal_newest ON lv.proposal (tenant_id, id DESC);

-- Canonical text of proposal content: sorted keys (bytewise), no whitespace, jsonb's own text for strings, numbers and
-- booleans. Separate from lv.canonical_seed_json, which deliberately refuses numbers at the financial posting boundary.
CREATE FUNCTION lv.proposal_canonical_json(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
DECLARE result text;
BEGIN
 CASE jsonb_typeof(v)
 WHEN 'array' THEN
  SELECT '[' || coalesce(string_agg(lv.proposal_canonical_json(value), ',' ORDER BY ord), '') || ']'
   INTO result FROM jsonb_array_elements(v) WITH ORDINALITY a(value, ord);
 WHEN 'object' THEN
  SELECT '{' || coalesce(string_agg(to_jsonb(key)::text || ':' || lv.proposal_canonical_json(value), ',' ORDER BY key COLLATE "C"), '') || '}'
   INTO result FROM jsonb_each(v);
 ELSE result := v::text;
 END CASE;
 RETURN result;
END $$;

CREATE TABLE lv.proposal_revision (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, proposal_id uuid NOT NULL,
 revision integer NOT NULL CHECK (revision > 0), previous_revision integer,
 change text NOT NULL CHECK (change IN ('CREATED', 'EDITED')),
 authored_by uuid NOT NULL REFERENCES lv_identity.app_user(id),
 session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 target_adapter text NOT NULL CHECK (target_adapter = 'NATIVE'),
 operations jsonb NOT NULL CHECK (jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) BETWEEN 1 AND 10),
 evidence_refs jsonb NOT NULL CHECK (jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) <= 100),
 rationale text CHECK (rationale IS NULL OR length(rationale) <= 4000),
 content_hash char(64) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id, proposal_id, revision),
 UNIQUE (tenant_id, ledger_id, proposal_id, revision),
 FOREIGN KEY (tenant_id, ledger_id, proposal_id) REFERENCES lv.proposal(tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, proposal_id, previous_revision) REFERENCES lv.proposal_revision(tenant_id, proposal_id, revision),
 CHECK ((revision = 1 AND previous_revision IS NULL AND change = 'CREATED')
     OR (revision > 1 AND previous_revision = revision - 1 AND change = 'EDITED')),
 CHECK (content_hash = encode(sha256(convert_to(lv.proposal_canonical_json(jsonb_build_object('target_adapter', target_adapter,
        'operations', operations, 'evidence_refs', evidence_refs, 'rationale', coalesce(to_jsonb(rationale), 'null'::jsonb))), 'UTF8')), 'hex'))
);

CREATE TABLE lv.proposal_reference (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, proposal_id uuid NOT NULL, revision integer NOT NULL,
 ordinal integer NOT NULL CHECK (ordinal > 0), pointer text NOT NULL CHECK (length(pointer) BETWEEN 1 AND 200),
 kind text NOT NULL CHECK (kind IN ('SETTLEMENT_ITEM_PAYMENT', 'SETTLEMENT_STATEMENT', 'EVIDENCE_SOURCE_RECORD',
                                    'EVIDENCE_DOCUMENT_VERSION', 'EVIDENCE_STAGED_DOCUMENT')),
 document_id uuid, staged_document_id uuid, source_record_id uuid, document_version_id uuid,
 PRIMARY KEY (tenant_id, proposal_id, revision, ordinal),
 FOREIGN KEY (tenant_id, ledger_id, proposal_id, revision) REFERENCES lv.proposal_revision(tenant_id, ledger_id, proposal_id, revision),
 FOREIGN KEY (tenant_id, ledger_id, document_id) REFERENCES lv.document(tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id, staged_document_id) REFERENCES lv.staged_document(tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id, source_record_id) REFERENCES lv.source_record(tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id, document_version_id) REFERENCES lv.document_version(tenant_id, ledger_id, id),
 CHECK (num_nonnulls(document_id, staged_document_id, source_record_id, document_version_id) = 1),
 CHECK ((kind = 'SETTLEMENT_ITEM_PAYMENT') = (document_id IS NOT NULL)
    AND (kind IN ('SETTLEMENT_STATEMENT', 'EVIDENCE_STAGED_DOCUMENT')) = (staged_document_id IS NOT NULL)
    AND (kind = 'EVIDENCE_SOURCE_RECORD') = (source_record_id IS NOT NULL)
    AND (kind = 'EVIDENCE_DOCUMENT_VERSION') = (document_version_id IS NOT NULL))
);

CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proposal_revision FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.proposal_reference FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();

-- The proposal row changes only by advancing to a new revision; identity, ledger, maker and state never change here.
CREATE FUNCTION lv.guard_proposal_update() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'proposals are retained, not deleted' USING ERRCODE = '23514'; END IF;
 IF (NEW.tenant_id, NEW.ledger_id, NEW.id, NEW.state, NEW.origin, NEW.target_adapter, NEW.created_by, NEW.created_at)
    IS DISTINCT FROM (OLD.tenant_id, OLD.ledger_id, OLD.id, OLD.state, OLD.origin, OLD.target_adapter, OLD.created_by, OLD.created_at)
    OR NEW.revision <> OLD.revision + 1 OR NEW.version <> OLD.version + 1 THEN
  RAISE EXCEPTION 'a draft changes only by advancing one revision and one version' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard BEFORE UPDATE OR DELETE ON lv.proposal FOR EACH ROW EXECUTE FUNCTION lv.guard_proposal_update();

-- COMMIT-time validator: a revision is complete only with its proposal head, its references and its audit event.
CREATE FUNCTION lv.validate_proposal_revision() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE p lv.proposal; n integer; items integer; ops integer; evidence integer;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id = NEW.tenant_id AND id = NEW.proposal_id;
 SELECT count(*), coalesce(max(revision), 0) INTO n, ops FROM lv.proposal_revision WHERE tenant_id = NEW.tenant_id AND proposal_id = NEW.proposal_id;
 IF p.id IS NULL OR n <> ops OR p.revision <> n OR p.ledger_id <> NEW.ledger_id THEN
  RAISE EXCEPTION 'proposal head does not match its revision history' USING ERRCODE = '23514';
 END IF;
 IF NEW.revision = 1 AND p.created_by <> NEW.authored_by THEN
  RAISE EXCEPTION 'the maker must be the author of revision 1' USING ERRCODE = '23514';
 END IF;
 SELECT coalesce(sum(jsonb_array_length(o -> 'items')), 0), count(*) INTO items, ops FROM jsonb_array_elements(NEW.operations) o;
 SELECT jsonb_array_length(NEW.evidence_refs) INTO evidence;
 IF (SELECT count(*) FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision AND r.kind = 'SETTLEMENT_ITEM_PAYMENT') <> items
    OR (SELECT count(*) FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision AND r.kind = 'SETTLEMENT_STATEMENT') <> ops THEN
  RAISE EXCEPTION 'revision references do not match its content' USING ERRCODE = '23514';
 END IF;
 IF (SELECT count(*) FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision AND r.kind IN ('EVIDENCE_SOURCE_RECORD', 'EVIDENCE_DOCUMENT_VERSION', 'EVIDENCE_STAGED_DOCUMENT')) <> evidence THEN
  RAISE EXCEPTION 'revision evidence references do not match its content' USING ERRCODE = '23514';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM lv_identity.audit_event a WHERE a.chain_key = NEW.tenant_id AND a.tenant_id = NEW.tenant_id
                AND a.event_type = CASE WHEN NEW.revision = 1 THEN 'PROPOSAL_CREATED' ELSE 'PROPOSAL_EDITED' END
                AND a.user_id = NEW.authored_by AND a.session_id = NEW.session_id
                AND a.detail ->> 'proposal_id' = NEW.proposal_id::text AND a.detail ->> 'revision' = NEW.revision::text
                AND a.detail ->> 'content_hash' = NEW.content_hash) THEN
  RAISE EXCEPTION 'a proposal revision must commit with its audit event' USING ERRCODE = '23514';
 END IF;
 -- Verify identity and pointer as well as counts: same-ledger substitutions must not pass.
 IF EXISTS (
  WITH expected AS (
   SELECT 'SETTLEMENT_ITEM_PAYMENT'::text AS kind,
          '/operations/' || (o.ord - 1)::text || '/items/' || (i.ord - 1)::text || '/document_id' AS pointer,
          (i.item ->> 'document_id')::uuid AS document_id, NULL::uuid AS staged_document_id,
          NULL::uuid AS source_record_id, NULL::uuid AS document_version_id
    FROM jsonb_array_elements(NEW.operations) WITH ORDINALITY o(op, ord)
    CROSS JOIN LATERAL jsonb_array_elements(o.op -> 'items') WITH ORDINALITY i(item, ord)
   UNION ALL
   SELECT 'SETTLEMENT_STATEMENT', '/operations/' || (o.ord - 1)::text || '/statement_document_id',
          NULL::uuid, (o.op ->> 'statement_document_id')::uuid, NULL::uuid, NULL::uuid
    FROM jsonb_array_elements(NEW.operations) WITH ORDINALITY o(op, ord)
   UNION ALL
   SELECT 'EVIDENCE_' || (e.ref ->> 'kind'), '/evidence_refs/' || (e.ord - 1)::text || '/id', NULL::uuid,
          CASE WHEN e.ref ->> 'kind' = 'STAGED_DOCUMENT' THEN (e.ref ->> 'id')::uuid END,
          CASE WHEN e.ref ->> 'kind' = 'SOURCE_RECORD' THEN (e.ref ->> 'id')::uuid END,
          CASE WHEN e.ref ->> 'kind' = 'DOCUMENT_VERSION' THEN (e.ref ->> 'id')::uuid END
    FROM jsonb_array_elements(NEW.evidence_refs) WITH ORDINALITY e(ref, ord)
  ), actual AS (
   SELECT r.kind, r.pointer, r.document_id, r.staged_document_id, r.source_record_id, r.document_version_id
    FROM lv.proposal_reference r WHERE r.tenant_id = NEW.tenant_id AND r.proposal_id = NEW.proposal_id
      AND r.revision = NEW.revision
  )
  SELECT 1 FROM ((SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
                 UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)) mismatch
 ) THEN
  RAISE EXCEPTION 'revision reference identities do not match its content' USING ERRCODE = '23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER revision_complete AFTER INSERT ON lv.proposal_revision DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION lv.validate_proposal_revision();

-- Refusals inside the definer functions: SQLSTATE LVR02 with the problem as DETAIL; the enclosing block rolls back.
CREATE FUNCTION lv.proposal_refusal(p_status integer, p_code text, p_detail text, p_errors jsonb, p_extra jsonb) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 RAISE EXCEPTION USING MESSAGE = 'proposal refused: ' || p_code, ERRCODE = 'LVR02',
  DETAIL = (jsonb_build_object('status', p_status, 'code', p_code, 'detail', p_detail)
            || CASE WHEN p_errors IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('errors', p_errors) END
            || coalesce(p_extra, '{}'::jsonb))::text;
END $$;

-- Session binding shared by both entry points (same rules as lv.close_period).
CREATE FUNCTION lv.proposal_session(p_token_hash bytea, p_source text, p_request text) RETURNS lv_identity.session
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; v_problem text;
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
 IF coalesce(current_setting('app.tenant_id', true), '') NOT IN ('', s.tenant_id::text)
    OR coalesce(current_setting('app.user_id', true), '') NOT IN ('', s.user_id::text)
    OR coalesce(current_setting('app.session_id', true), '') NOT IN ('', s.id::text) THEN
  RAISE EXCEPTION 'transaction context belongs to a different session' USING ERRCODE = '42501';
 END IF;
 PERFORM set_config('app.tenant_id', s.tenant_id::text, true);
 PERFORM set_config('app.user_id', s.user_id::text, true);
 PERFORM set_config('app.session_id', s.id::text, true);
 RETURN s;
END $$;

CREATE FUNCTION lv.proposal_may(p_tenant uuid, p_user uuid, p_permission text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM lv_identity.membership_role r JOIN lv_identity.role_permission rp ON rp.role = r.role
                WHERE r.tenant_id = p_tenant AND r.user_id = p_user AND rp.permission::text = p_permission);
$$;

-- Money backstop (the API validates first, with field pointers): string amount, currency = ledger, scale <= exponent.
CREATE FUNCTION lv.proposal_money_ok(m jsonb, p_currency text, p_exponent smallint) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT CASE WHEN jsonb_typeof(m) IS DISTINCT FROM 'object' THEN false ELSE coalesce(
    (SELECT count(*) FROM jsonb_object_keys(m)) = 2
    AND jsonb_typeof(m -> 'amount') = 'string' AND (m ->> 'currency') = p_currency
    AND CASE WHEN p_exponent = 0 THEN (m ->> 'amount') ~ '^[0-9]{1,16}$'
             ELSE (m ->> 'amount') ~ '^[0-9]{1,16}[.][0-9]+$' AND length(split_part(m ->> 'amount', '.', 2)) = p_exponent END, false) END;
$$;

-- Resolve every reference of one draft WITHOUT writing: returns {refs: [...], evidence: [...]} or raises a refusal.
-- Every lookup is (tenant, proposal ledger, id), so absent ids, other tenants' ids and other ledgers' ids are one answer.
CREATE FUNCTION lv.proposal_resolve(t uuid, l lv.ledger, p_operations jsonb, p_evidence jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE o jsonb; i jsonb; e jsonb; oi integer; ii integer; ei integer; ptr text; ref uuid; hit boolean;
        v_type text; v_amount numeric; v_hash text; v_prov text;
        refs jsonb := '[]'::jsonb; enriched jsonb := '[]'::jsonb;
        missing jsonb := '[]'::jsonb; wrong jsonb := '[]'::jsonb; partial jsonb := '[]'::jsonb;
BEGIN
 FOR o, oi IN SELECT value, (ordinality - 1)::integer FROM jsonb_array_elements(p_operations) WITH ORDINALITY LOOP
  IF o ->> 'type' IS DISTINCT FROM 'RecordSettlement' OR (o -> 'schema_version') IS DISTINCT FROM '1'::jsonb THEN
   PERFORM lv.proposal_refusal(422, 'CAPABILITY_UNAVAILABLE', 'only RecordSettlement (schema_version 1) drafts are available', NULL, NULL);
  END IF;
  IF NOT (lv.proposal_money_ok(o -> 'gross', l.currency::text, l.currency_exponent)
          AND lv.proposal_money_ok(o -> 'fee', l.currency::text, l.currency_exponent)
          AND lv.proposal_money_ok(o -> 'net', l.currency::text, l.currency_exponent))
     OR jsonb_typeof(o -> 'items') IS DISTINCT FROM 'array' THEN
   PERFORM lv.proposal_refusal(422, 'SCHEMA_INVALID', 'operation is not a valid RecordSettlement of the ledger currency',
     jsonb_build_array(jsonb_build_object('pointer', '/operations/' || oi, 'code', 'invalid')), NULL);
  END IF;
  ptr := '/operations/' || oi || '/statement_document_id';
  BEGIN ref := (o ->> 'statement_document_id')::uuid; EXCEPTION WHEN invalid_text_representation THEN ref := NULL; END;
  SELECT s.doc_type::text INTO v_type FROM lv.staged_document s WHERE s.tenant_id = t AND s.ledger_id = l.id AND s.id = ref;
  hit := FOUND;
  IF NOT hit THEN
   missing := missing || jsonb_build_object('pointer', ptr, 'code', 'not_found');
  ELSIF v_type <> 'SETTLEMENT_STATEMENT' THEN
   wrong := wrong || jsonb_build_object('pointer', ptr, 'code', 'wrong_reference_type');
  ELSE
   refs := refs || jsonb_build_object('kind', 'SETTLEMENT_STATEMENT', 'pointer', ptr, 'staged_document_id', ref::text);
  END IF;
  FOR i, ii IN SELECT value, (ordinality - 1)::integer FROM jsonb_array_elements(o -> 'items') WITH ORDINALITY LOOP
   ptr := '/operations/' || oi || '/items/' || ii || '/document_id';
   IF i ->> 'kind' IS DISTINCT FROM 'PAYMENT' THEN
    PERFORM lv.proposal_refusal(422, 'CAPABILITY_UNAVAILABLE', 'native refund documents do not exist; only PAYMENT items are available', NULL, NULL);
   END IF;
   IF NOT lv.proposal_money_ok(i -> 'amount', l.currency::text, l.currency_exponent) THEN
    PERFORM lv.proposal_refusal(422, 'SCHEMA_INVALID', 'item amount is not Money of the ledger currency at its exponent',
      jsonb_build_array(jsonb_build_object('pointer', '/operations/' || oi || '/items/' || ii || '/amount', 'code', 'invalid')), NULL);
   END IF;
   BEGIN ref := (i ->> 'document_id')::uuid; EXCEPTION WHEN invalid_text_representation THEN ref := NULL; END;
   SELECT dd.doc_type::text, pc.amount INTO v_type, v_amount FROM lv.document dd
    LEFT JOIN lv.payment_current pc ON (pc.tenant_id, pc.ledger_id, pc.document_id) = (dd.tenant_id, dd.ledger_id, dd.id)
    WHERE dd.tenant_id = t AND dd.ledger_id = l.id AND dd.id = ref;
   hit := FOUND;
   IF NOT hit THEN
    missing := missing || jsonb_build_object('pointer', ptr, 'code', 'not_found');
   ELSIF v_type <> 'PAYMENT' OR v_amount IS NULL THEN
    wrong := wrong || jsonb_build_object('pointer', ptr, 'code', 'wrong_reference_type');
   ELSE
    -- Stage 2A §19.3: every item settles at its full amount; settling part of one item is unsupported by SP-1.
    IF v_amount <> (i -> 'amount' ->> 'amount')::numeric THEN
     partial := partial || jsonb_build_object('pointer', '/operations/' || oi || '/items/' || ii || '/amount', 'code', 'PARTIAL_ITEM_SETTLEMENT');
    END IF;
    refs := refs || jsonb_build_object('kind', 'SETTLEMENT_ITEM_PAYMENT', 'pointer', ptr, 'document_id', ref::text);
   END IF;
  END LOOP;
 END LOOP;
 FOR e, ei IN SELECT value, (ordinality - 1)::integer FROM jsonb_array_elements(p_evidence) WITH ORDINALITY LOOP
  ptr := '/evidence_refs/' || ei || '/id';
  BEGIN ref := (e ->> 'id')::uuid; EXCEPTION WHEN invalid_text_representation THEN ref := NULL; END;
  hit := false;
  IF e ->> 'kind' = 'SOURCE_RECORD' THEN
   SELECT r.content_hash INTO v_hash FROM lv.source_record r WHERE r.tenant_id = t AND r.ledger_id = l.id AND r.id = ref;
   hit := FOUND;
   IF hit THEN
    refs := refs || jsonb_build_object('kind', 'EVIDENCE_SOURCE_RECORD', 'pointer', ptr, 'source_record_id', ref::text);
    enriched := enriched || jsonb_build_object('kind', 'SOURCE_RECORD', 'id', ref::text, 'sha256', v_hash);
   END IF;
  ELSIF e ->> 'kind' = 'DOCUMENT_VERSION' THEN
   SELECT v.provenance::text INTO v_prov FROM lv.document_version v WHERE v.tenant_id = t AND v.ledger_id = l.id AND v.id = ref;
   hit := FOUND;
   IF hit THEN
    refs := refs || jsonb_build_object('kind', 'EVIDENCE_DOCUMENT_VERSION', 'pointer', ptr, 'document_version_id', ref::text);
    enriched := enriched || jsonb_build_object('kind', 'DOCUMENT_VERSION', 'id', ref::text, 'provenance', v_prov);
   END IF;
  ELSIF e ->> 'kind' = 'STAGED_DOCUMENT' THEN
   SELECT s.provenance::text INTO v_prov FROM lv.staged_document s WHERE s.tenant_id = t AND s.ledger_id = l.id AND s.id = ref;
   hit := FOUND;
   IF hit THEN
    refs := refs || jsonb_build_object('kind', 'EVIDENCE_STAGED_DOCUMENT', 'pointer', ptr, 'staged_document_id', ref::text);
    enriched := enriched || jsonb_build_object('kind', 'STAGED_DOCUMENT', 'id', ref::text, 'provenance', v_prov);
   END IF;
  ELSE
   PERFORM lv.proposal_refusal(422, 'CAPABILITY_UNAVAILABLE', 'evidence of this kind cannot be referenced yet', NULL, NULL);
  END IF;
  IF NOT hit THEN missing := missing || jsonb_build_object('pointer', ptr, 'code', 'not_found'); END IF;
 END LOOP;
 IF jsonb_array_length(missing) > 0 THEN
  PERFORM lv.proposal_refusal(404, 'NOT_FOUND', 'a referenced record was not found in this ledger', missing, NULL);
 END IF;
 IF jsonb_array_length(wrong) > 0 THEN
  PERFORM lv.proposal_refusal(422, 'SCHEMA_INVALID', 'a referenced record has the wrong type', wrong, NULL);
 END IF;
 IF jsonb_array_length(partial) > 0 THEN
  PERFORM lv.proposal_refusal(422, 'UNSUPPORTED_BY_PROFILE', 'SP-1 settles each item at its full amount', partial,
    jsonb_build_object('reason', 'PARTIAL_ITEM_SETTLEMENT'));
 END IF;
 RETURN jsonb_build_object('refs', refs, 'evidence', enriched);
END $$;

CREATE FUNCTION lv.proposal_content_hash(p_target text, p_operations jsonb, p_evidence jsonb, p_rationale text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT encode(sha256(convert_to(lv.proposal_canonical_json(jsonb_build_object('target_adapter', p_target, 'operations', p_operations,
   'evidence_refs', p_evidence, 'rationale', coalesce(to_jsonb(p_rationale), 'null'::jsonb))), 'UTF8')), 'hex');
$$;

-- Representation (SECURITY INVOKER: callable by lv_app under RLS; also used by the writers so replays are identical).
CREATE FUNCTION lv.proposal_json(p lv.proposal) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
 SELECT jsonb_build_object(
  'id', p.id::text, 'ledger_id', p.ledger_id::text, 'ledger_purpose', l.purpose::text, 'state', p.state::text,
  'revision', p.revision, 'version', p.version, 'origin', p.origin, 'ai_draft_id', NULL, 'target_adapter', p.target_adapter,
  'operations', r.operations, 'evidence_refs', r.evidence_refs, 'rationale', r.rationale,
  'snapshot', NULL, 'read_set', '[]'::jsonb, 'latest_simulation_id', NULL,
  'created_by', p.created_by::text, 'submitted_by', NULL,
  'editors', (SELECT jsonb_agg(a.authored_by::text ORDER BY a.first) FROM (SELECT x.authored_by, min(x.revision) AS first
              FROM lv.proposal_revision x WHERE x.tenant_id = p.tenant_id AND x.proposal_id = p.id GROUP BY x.authored_by) a),
  'created_at', to_char(p.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'updated_at', to_char(p.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'accounting_status', 'UNREVIEWED',
  'data_label', CASE WHEN l.purpose::text = 'SANDBOX' THEN 'SANDBOX — not accounting records' END)
 FROM lv.ledger l JOIN lv.proposal_revision r ON r.tenant_id = l.tenant_id
 WHERE l.tenant_id = p.tenant_id AND l.id = p.ledger_id AND r.proposal_id = p.id AND r.revision = p.revision;
$$;

-- Write revision p_revision of proposal p_proposal (creating the proposal at revision 1). Raises LVR02 refusals.
CREATE FUNCTION lv.proposal_write_revision(s lv_identity.session, l lv.ledger, p_proposal uuid, p_revision integer,
                                           p_draft jsonb, p_source text, p_request text) RETURNS lv.proposal
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE t uuid := s.tenant_id; u uuid := s.user_id; p lv.proposal; res jsonb; h text; v_rationale text; x jsonb; n integer;
BEGIN
 IF l.purpose::text <> 'SANDBOX' OR l.source_kind::text <> 'NATIVE' THEN
  PERFORM lv.proposal_refusal(422, 'CAPABILITY_UNAVAILABLE', 'proposal drafts are available for native SANDBOX ledgers only', NULL, NULL);
 END IF;
 IF p_draft ->> 'target_adapter' IS DISTINCT FROM 'NATIVE' THEN
  PERFORM lv.proposal_refusal(422, 'CAPABILITY_UNAVAILABLE', 'only the NATIVE adapter is available', NULL, NULL);
 END IF;
 IF jsonb_typeof(p_draft -> 'operations') IS DISTINCT FROM 'array' OR jsonb_array_length(p_draft -> 'operations') NOT BETWEEN 1 AND 10
    OR jsonb_typeof(p_draft -> 'evidence_refs') IS DISTINCT FROM 'array' OR jsonb_array_length(p_draft -> 'evidence_refs') > 100
    OR jsonb_typeof(p_draft -> 'rationale') NOT IN ('string', 'null') THEN
  PERFORM lv.proposal_refusal(422, 'SCHEMA_INVALID', 'operations, evidence_refs or rationale has the wrong shape', NULL, NULL);
 END IF;
 v_rationale := p_draft ->> 'rationale';
 res := lv.proposal_resolve(t, l, p_draft -> 'operations', p_draft -> 'evidence_refs');
 h := lv.proposal_content_hash('NATIVE', p_draft -> 'operations', res -> 'evidence', v_rationale);
 IF p_revision = 1 THEN
  INSERT INTO lv.proposal(tenant_id, ledger_id, id, target_adapter, revision, version, created_by)
  VALUES (t, l.id, p_proposal, 'NATIVE', 1, 1, u) RETURNING * INTO p;
 ELSE
  UPDATE lv.proposal SET revision = revision + 1, version = version + 1, updated_at = transaction_timestamp()
   WHERE tenant_id = t AND id = p_proposal AND revision = p_revision - 1 RETURNING * INTO p;
  IF NOT FOUND THEN RAISE EXCEPTION 'proposal head moved during an edit' USING ERRCODE = '40001'; END IF;
 END IF;
 INSERT INTO lv.proposal_revision(tenant_id, ledger_id, proposal_id, revision, previous_revision, change, authored_by, session_id,
                                  target_adapter, operations, evidence_refs, rationale, content_hash)
 VALUES (t, l.id, p_proposal, p_revision, nullif(p_revision - 1, 0), CASE WHEN p_revision = 1 THEN 'CREATED' ELSE 'EDITED' END,
         u, s.id, 'NATIVE', p_draft -> 'operations', res -> 'evidence', v_rationale, h);
 n := 0;
 FOR x IN SELECT value FROM jsonb_array_elements(res -> 'refs') LOOP
  n := n + 1;
  INSERT INTO lv.proposal_reference(tenant_id, ledger_id, proposal_id, revision, ordinal, pointer, kind,
                                    document_id, staged_document_id, source_record_id, document_version_id)
  VALUES (t, l.id, p_proposal, p_revision, n, x ->> 'pointer', x ->> 'kind', (x ->> 'document_id')::uuid,
          (x ->> 'staged_document_id')::uuid, (x ->> 'source_record_id')::uuid, (x ->> 'document_version_id')::uuid);
 END LOOP;
 PERFORM lv_identity.audit_append(CASE WHEN p_revision = 1 THEN 'PROPOSAL_CREATED' ELSE 'PROPOSAL_EDITED' END, 'ALLOWED', u, t, s.id,
  p_source, p_request, jsonb_build_object('proposal_id', p_proposal::text, 'revision', p_revision::text, 'ledger_id', l.id::text,
  'content_hash', h, 'operation_types', (SELECT jsonb_agg(o ->> 'type') FROM jsonb_array_elements(p_draft -> 'operations') o),
  'reference_count', n::text));
 RETURN p;
END $$;

-- POST /api/v1/proposals (Stage 2B Family 8; Part 8.1 idempotency).
CREATE FUNCTION lv.proposal_create(p_token_hash bytea, p_idempotency_key text, p_request_hash text, p_draft jsonb,
                                   p_problem jsonb, p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_route constant text := '/api/v1/proposals'; s lv_identity.session; t uuid; u uuid;
        prior lv.api_idempotency; result jsonb; refusal text; l lv.ledger; p lv.proposal; lid uuid;
BEGIN
 s := lv.proposal_session(p_token_hash, p_source, p_request); t := s.tenant_id; u := s.user_id;
 IF NOT lv.proposal_may(t, u, 'propose') THEN
  PERFORM lv_identity.audit_append('PROPOSAL_CREATE_DENIED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('reason', 'PERMISSION_DENIED'));
  RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires propose', 'stored', false);
 END IF;
 IF p_idempotency_key IS NULL OR p_idempotency_key !~ '^[!-~]{1,64}$' OR p_request_hash IS NULL OR p_request_hash !~ '^[0-9a-f]{64}$' THEN
  RAISE EXCEPTION 'invalid idempotency key or request hash' USING ERRCODE = '22023';
 END IF;
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
  ELSIF prior.request_hash <> p_request_hash THEN
   RETURN jsonb_build_object('status', 422, 'code', 'IDEMPOTENCY_KEY_REUSED', 'detail', 'this Idempotency-Key was used with a different request',
                             'stored', false);
  ELSE
   RETURN prior.response || jsonb_build_object('replayed', true, 'stored', true);
  END IF;
 END IF;
 BEGIN
  IF p_problem IS NOT NULL THEN
   PERFORM lv.proposal_refusal((p_problem ->> 'status')::integer, p_problem ->> 'code', p_problem ->> 'detail', p_problem -> 'errors',
     p_problem - ARRAY['status', 'code', 'detail', 'errors']);
  END IF;
  BEGIN lid := (p_draft ->> 'ledger_id')::uuid; EXCEPTION WHEN invalid_text_representation THEN lid := NULL; END;
  SELECT * INTO l FROM lv.ledger WHERE tenant_id = t AND id = lid FOR SHARE;
  IF NOT FOUND THEN
   PERFORM lv.proposal_refusal(404, 'NOT_FOUND', 'ledger not found', jsonb_build_array(jsonb_build_object('pointer', '/ledger_id', 'code', 'not_found')), NULL);
  END IF;
  p := lv.proposal_write_revision(s, l, uuidv7(), 1, p_draft, p_source, p_request);
  result := jsonb_build_object('status', 201, 'etag', 'v' || p.version::text, 'body', lv.proposal_json(p));
 EXCEPTION WHEN SQLSTATE 'LVR02' THEN
  GET STACKED DIAGNOSTICS refusal = PG_EXCEPTION_DETAIL;
  result := refusal::jsonb;
  PERFORM lv_identity.audit_append('PROPOSAL_CREATE_REFUSED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('reason', result ->> 'code'));
 END;
 INSERT INTO lv.api_idempotency(tenant_id, principal_id, method, route, idempotency_key, request_hash, response_status, response, expires_at)
 VALUES (t, u, 'POST', v_route, p_idempotency_key, p_request_hash, (result ->> 'status')::smallint, result, transaction_timestamp() + interval '24 hours');
 RETURN result || jsonb_build_object('replayed', false, 'stored', true);
END $$;

-- PATCH /api/v1/proposals/{id}: version-guarded (If-Match), not idempotency-keyed (Family 8: "N (version-guarded)").
CREATE FUNCTION lv.proposal_edit(p_token_hash bytea, p_proposal uuid, p_if_match text, p_patch jsonb, p_problem jsonb,
                                 p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; t uuid; u uuid; result jsonb; refusal text; cur lv.proposal; r lv.proposal_revision;
        l lv.ledger; p lv.proposal; draft jsonb;
BEGIN
 s := lv.proposal_session(p_token_hash, p_source, p_request); t := s.tenant_id; u := s.user_id;
 IF NOT lv.proposal_may(t, u, 'propose') THEN
  PERFORM lv_identity.audit_append('PROPOSAL_EDIT_DENIED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('proposal_id', p_proposal::text, 'reason', 'PERMISSION_DENIED'));
  RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires propose');
 END IF;
 IF p_if_match IS NOT NULL AND length(p_if_match) > 64 THEN
  RAISE EXCEPTION 'invalid If-Match' USING ERRCODE = '22023';
 END IF;
 BEGIN
  IF p_if_match IS NULL THEN
   PERFORM lv.proposal_refusal(428, 'VERSION_REQUIRED', 'If-Match is required', NULL, NULL);
  END IF;
  SELECT * INTO cur FROM lv.proposal WHERE tenant_id = t AND id = p_proposal FOR UPDATE;
  IF NOT FOUND THEN PERFORM lv.proposal_refusal(404, 'NOT_FOUND', 'proposal not found', NULL, NULL); END IF;
  IF cur.state <> 'DRAFT' THEN
   PERFORM lv.proposal_refusal(409, 'INVALID_STATE_TRANSITION', 'only a DRAFT can be edited', NULL, jsonb_build_object('state', cur.state::text));
  END IF;
  IF p_if_match <> '"v' || cur.version::text || '"' THEN
   PERFORM lv.proposal_refusal(412, 'VERSION_MISMATCH', 'the proposal has changed', NULL, jsonb_build_object('current_version', cur.version));
  END IF;
  IF p_problem IS NOT NULL THEN
   PERFORM lv.proposal_refusal((p_problem ->> 'status')::integer, p_problem ->> 'code', p_problem ->> 'detail', p_problem -> 'errors',
     p_problem - ARRAY['status', 'code', 'detail', 'errors']);
  END IF;
  SELECT * INTO r FROM lv.proposal_revision WHERE tenant_id = t AND proposal_id = cur.id AND revision = cur.revision;
  -- Merge against the LOCKED head, never against what the caller read earlier.
  draft := jsonb_build_object('target_adapter', cur.target_adapter,
   'operations', CASE WHEN p_patch ? 'operations' THEN p_patch -> 'operations' ELSE r.operations END,
   'evidence_refs', CASE WHEN p_patch ? 'evidence_refs' THEN p_patch -> 'evidence_refs' ELSE
      coalesce((SELECT jsonb_agg(jsonb_build_object('kind', x ->> 'kind', 'id', x ->> 'id') ORDER BY ord)
                FROM jsonb_array_elements(r.evidence_refs) WITH ORDINALITY AS z(x, ord)), '[]'::jsonb) END,
   'rationale', CASE WHEN p_patch ? 'rationale' THEN p_patch -> 'rationale' ELSE coalesce(to_jsonb(r.rationale), 'null'::jsonb) END);
  SELECT * INTO l FROM lv.ledger WHERE tenant_id = t AND id = cur.ledger_id FOR SHARE;
  p := lv.proposal_write_revision(s, l, cur.id, cur.revision + 1, draft, p_source, p_request);
  result := jsonb_build_object('status', 200, 'etag', 'v' || p.version::text, 'body', lv.proposal_json(p));
 EXCEPTION WHEN SQLSTATE 'LVR02' THEN
  GET STACKED DIAGNOSTICS refusal = PG_EXCEPTION_DETAIL;
  result := refusal::jsonb;
  PERFORM lv_identity.audit_append('PROPOSAL_EDIT_REFUSED', 'DENIED', u, t, s.id, p_source, p_request,
   jsonb_build_object('proposal_id', p_proposal::text, 'reason', result ->> 'code'));
 END;
 RETURN result;
END $$;
"""

FUNCTIONS = ('lv.proposal_create(bytea, text, text, jsonb, jsonb, text, text)',
             'lv.proposal_edit(bytea, uuid, text, jsonb, jsonb, text, text)')


def upgrade():
    bind = op.get_bind()
    bind.exec_driver_sql(UPGRADE)
    for name in NEW_TABLES:
        op.execute(f'ALTER TABLE lv.{name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE lv.{name} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_scope ON lv.{name} USING ({POLICY}) WITH CHECK ({POLICY})')
        op.execute(f'REVOKE ALL ON lv.{name} FROM PUBLIC, lv_app, lv_seed, lv_ingest')
        op.execute(f'GRANT SELECT ON lv.{name} TO lv_app')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC')
    for signature in FUNCTIONS:
        op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO lv_app')
    op.execute('GRANT EXECUTE ON FUNCTION lv.proposal_json(lv.proposal) TO lv_app')


DOWNGRADE = r"""
DROP FUNCTION lv.proposal_edit(bytea, uuid, text, jsonb, jsonb, text, text);
DROP FUNCTION lv.proposal_create(bytea, text, text, jsonb, jsonb, text, text);
DROP FUNCTION lv.proposal_write_revision(lv_identity.session, lv.ledger, uuid, integer, jsonb, text, text);
DROP FUNCTION lv.proposal_json(lv.proposal);
DROP FUNCTION lv.proposal_content_hash(text, jsonb, jsonb, text);
DROP FUNCTION lv.proposal_resolve(uuid, lv.ledger, jsonb, jsonb);
DROP FUNCTION lv.proposal_money_ok(jsonb, text, smallint);
DROP FUNCTION lv.proposal_may(uuid, uuid, text);
DROP FUNCTION lv.proposal_session(bytea, text, text);
DROP FUNCTION lv.proposal_refusal(integer, text, text, jsonb, jsonb);
DROP TABLE lv.proposal_reference;
DROP TABLE lv.proposal_revision;
DROP TABLE lv.proposal;
DROP FUNCTION lv.proposal_canonical_json(jsonb);
DROP FUNCTION lv.validate_proposal_revision();
DROP FUNCTION lv.guard_proposal_update();
DROP TYPE lv.proposal_state;
DELETE FROM lv.api_idempotency WHERE route = '/api/v1/proposals';
"""


def downgrade():
    bind = op.get_bind()
    # Downgrade is for disposable test databases (tools.dev_db refuses it elsewhere). Stored proposal responses are removed.
    bind.exec_driver_sql("ALTER TABLE lv.api_idempotency NO FORCE ROW LEVEL SECURITY")
    bind.exec_driver_sql(DOWNGRADE)
    bind.exec_driver_sql("ALTER TABLE lv.api_idempotency FORCE ROW LEVEL SECURITY")
