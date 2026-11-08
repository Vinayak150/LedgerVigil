"""Typed synthetic processor Payment documents with authoritative processor identity (SANDBOX seed path only).

Owning requirements:
- Stage 2A §5.1, rule set sp1-posting/1.0, "Payment received via processor": Dr PROCESSOR_CLEARING / Cr AR_CONTROL
  for the customer, for the payment amount, "applied and unapplied alike".
- §5.2: a Payment starts RECEIVED, and unapplied = amount − Σ applications ≥ 0.
- §11.3: the `payment_current` projection.
- §11.2: document identity and immutable versions, unique (ledger, doc_type, source_ref).
- §4.1/§4.2: DOCUMENT_POSTING entries name the exact source document version and posting rule (I-15, I-16).
- §19.1: immutable versions with a financial fingerprint, origin and seq.
- §19.2 path D and ADR-024/025: developer seeding of SANDBOX ledgers only, refused on BOOKS by trigger.
- §19.3 and Stage 2B RecordSettlementOp: settlement items are payment document ids that share the payout's
  `processor_ref` (at most 64 characters).
- DC-03: unapplied payments credit the customer's AR.

Identity rule: a processor payment is identified by (tenant, ledger, processor_ref, processor_item_id). That
pair is stored explicitly, is unique per ledger, and is never inferred from amounts, dates or text. Its
document source_ref is `processor_ref/processor_item_id` ('/' cannot occur in either part), so the existing
(ledger, doc_type, source_ref) uniqueness also holds.

Deliberately absent, because nothing in this schema backs them: invoices and payment applications
(applied is fixed at 0), settlements and inclusions, voids, operations, approvals and executions.
Migrations 0001–0004 are unchanged. 0002's `validate_seed_batch` is replaced by a version that keeps every
manual-journal check verbatim and adds a typed-payment branch. Downgrade restores the original text.
PostgreSQL cannot drop enum labels, so downgrading to 0004 leaves the unused labels PAYMENT and DOCUMENT_POSTING
in place.
"""
from pathlib import Path

from alembic import op

revision = '0005_processor_payments'
down_revision = '0004_statement_ingestion'
branch_labels = None
depends_on = None

IDENT = r'^[A-Za-z0-9_.:-]'

UPGRADE = rf"""
ALTER TYPE lv.document_type ADD VALUE IF NOT EXISTS 'PAYMENT';
ALTER TYPE lv.entry_kind ADD VALUE IF NOT EXISTS 'DOCUMENT_POSTING';

-- Authoritative processor identity of a typed Payment (document-level, stable across versions).
CREATE TABLE lv.processor_payment_identity (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, document_id uuid NOT NULL,
 processor_ref varchar(64) NOT NULL CHECK (processor_ref ~ '{IDENT}{{1,64}}$'),
 processor_item_id varchar(63) NOT NULL CHECK (processor_item_id ~ '{IDENT}{{1,63}}$'),
 seq bigint NOT NULL,
 PRIMARY KEY (tenant_id, ledger_id, document_id),
 UNIQUE (tenant_id, ledger_id, processor_ref, processor_item_id),
 FOREIGN KEY (tenant_id, ledger_id, document_id) REFERENCES lv.document (tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id, seq) REFERENCES lv.commit_batch (tenant_id, ledger_id, seq)
);

-- 2A §11.3 payment_current. Applications are not implemented, so applied = 0 and unapplied = amount.
-- There is no settlement column: no settlement or inclusion structure exists to reference.
CREATE TABLE lv.payment_current (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, document_id uuid NOT NULL, version integer NOT NULL,
 party_id uuid NOT NULL, currency char(3) NOT NULL, currency_exponent smallint NOT NULL,
 amount numeric(20,4) NOT NULL CHECK (amount > 0 AND amount < 10000000000000000),
 applied numeric(20,4) NOT NULL CHECK (applied = 0),
 unapplied numeric(20,4) NOT NULL,
 status varchar(16) NOT NULL CHECK (status = 'RECEIVED'),
 last_seq bigint NOT NULL,
 PRIMARY KEY (tenant_id, ledger_id, document_id),
 CHECK (amount = round(amount, currency_exponent)),
 CHECK (unapplied = amount - applied AND unapplied >= 0),
 FOREIGN KEY (tenant_id, ledger_id, document_id, version) REFERENCES lv.document_version (tenant_id, ledger_id, document_id, version),
 FOREIGN KEY (tenant_id, ledger_id, party_id) REFERENCES lv.party (tenant_id, ledger_id, id),
 FOREIGN KEY (tenant_id, ledger_id, currency, currency_exponent) REFERENCES lv.ledger (tenant_id, id, currency, currency_exponent),
 FOREIGN KEY (tenant_id, ledger_id, last_seq) REFERENCES lv.commit_batch (tenant_id, ledger_id, seq)
);

-- Canonical ledger event for a typed-payment seed commit, built only from committed rows. The seed function
-- hashes it when writing, and the commit validator rebuilds it for comparison.
CREATE FUNCTION lv.payment_seed_event(p_tenant uuid, p_ledger uuid, p_seq bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE b lv.commit_batch; d lv.document; v lv.document_version; e lv.journal_entry; lines jsonb;
BEGIN
 SELECT * INTO b FROM lv.commit_batch WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO d FROM lv.document WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO v FROM lv.document_version WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO e FROM lv.journal_entry WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT jsonb_agg(jsonb_build_object('line_no', line_no::text, 'account_id', account_id::text, 'side', side::text,
        'amount', amount::text, 'party_id', party_id::text) ORDER BY line_no)
   INTO lines FROM lv.journal_line WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND entry_id = e.id;
 RETURN jsonb_build_object('tenant_id', p_tenant::text, 'ledger_id', p_ledger::text, 'seq', p_seq::text, 'kind', 'SEED',
  'created_by', b.created_by::text, 'document_type', d.doc_type::text, 'document_id', d.id::text, 'document_version', v.version::text,
  'source_ref', d.source_ref, 'entry_id', e.id::text, 'entry_no', e.entry_no::text,
  'posting_rule', e.posting_rule_id || '/' || e.posting_rule_version, 'financial_fields', v.financial_fields,
  'postings', jsonb_build_object('effective_date', e.effective_date::text, 'currency', e.currency::text, 'lines', lines),
  'description', e.description, 'provenance', 'SYNTHETIC');
END $$;

-- Developer-only SANDBOX seed of one typed processor Payment (2A §19.2 path D). It uses the same protections as
-- seed_manual_journal: tenant context, session-derived actor, exact-money parser, counter lock, open period,
-- complete chart, deferred validation, and an idempotent replay or conflict refusal.
CREATE FUNCTION lv.seed_processor_payment(p_ledger uuid, p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
<<seed_payment>>
DECLARE
 t uuid := nullif(current_setting('app.tenant_id', true), '')::uuid;
 l lv.ledger; actor uuid; counter lv.ledger_counter; current_period lv.fiscal_period;
 existing lv.processor_payment_identity; existing_request jsonb;
 doc_id uuid := uuidv7(); entry_id uuid := uuidv7(); customer uuid; amount_value numeric; received date;
 clr lv.account; ar lv.account; financial jsonb; batch_content text; event_content text; bh text; eh text;
 next_seq bigint; next_entry bigint;
 keys text[] := ARRAY['processor_ref', 'processor_item_id', 'party_id', 'currency', 'amount', 'received_date', 'description'];
BEGIN
 IF t IS NULL THEN RAISE EXCEPTION 'tenant context required' USING ERRCODE = '42501'; END IF;
 SELECT id INTO actor FROM lv.service_principal
  WHERE tenant_id = t AND database_role = session_user AND active AND purpose = 'DEVELOPER_SEED' FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'active developer seed principal required' USING ERRCODE = '42501'; END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id = t AND id = p_ledger FOR SHARE;
 IF NOT FOUND OR l.purpose <> 'SANDBOX' OR l.source_kind <> 'NATIVE' THEN
  RAISE EXCEPTION 'seed requires a native SANDBOX ledger' USING ERRCODE = '23514';
 END IF;
 IF p_request IS NULL OR jsonb_typeof(p_request) <> 'object' OR octet_length(p_request::text) > 16384
    OR NOT p_request ?& keys OR (p_request - keys) <> '{{}}'::jsonb
    OR EXISTS (SELECT 1 FROM jsonb_each(p_request) WHERE jsonb_typeof(value) <> 'string') THEN
  RAISE EXCEPTION 'invalid typed payment request' USING ERRCODE = '23514';
 END IF;
 IF (p_request->>'processor_ref') !~ '{IDENT}{{1,64}}$' OR (p_request->>'processor_item_id') !~ '{IDENT}{{1,63}}$'
    OR (p_request->>'party_id') !~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
    OR (p_request->>'received_date') !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$' OR length(p_request->>'description') > 512 THEN
  RAISE EXCEPTION 'invalid typed payment request' USING ERRCODE = '23514';
 END IF;
 IF p_request->>'currency' IS DISTINCT FROM l.currency::text THEN
  RAISE EXCEPTION 'payment currency must equal the ledger functional currency' USING ERRCODE = '23514';
 END IF;
 amount_value := lv.parse_line_amount(p_request->>'amount', l.currency_exponent);   -- exact; refuses before any cast
 received := (p_request->>'received_date')::date;
 customer := (p_request->>'party_id')::uuid;
 -- Serialize per ledger before identity detection, so simultaneous duplicates converge.
 INSERT INTO lv.ledger_counter(tenant_id, ledger_id) VALUES (t, p_ledger) ON CONFLICT DO NOTHING;
 SELECT * INTO counter FROM lv.ledger_counter WHERE tenant_id = t AND ledger_id = p_ledger FOR UPDATE;
 SELECT * INTO existing FROM lv.processor_payment_identity
  WHERE tenant_id = t AND ledger_id = p_ledger AND processor_ref = p_request->>'processor_ref' AND processor_item_id = p_request->>'processor_item_id';
 IF FOUND THEN
  SELECT original_request INTO existing_request FROM lv.document_version
   WHERE tenant_id = t AND ledger_id = p_ledger AND document_id = existing.document_id AND version = 1;
  IF existing_request IS DISTINCT FROM p_request THEN
   RAISE EXCEPTION 'processor payment identity already recorded with different content' USING ERRCODE = '23505';
  END IF;
  SELECT id INTO entry_id FROM lv.journal_entry
   WHERE tenant_id = t AND ledger_id = p_ledger AND source_document_id = existing.document_id AND source_document_version = 1;
  RETURN jsonb_build_object('seq', existing.seq::text, 'document_id', existing.document_id::text, 'entry_id', entry_id::text, 'replayed', true);
 END IF;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id = t AND ledger_id = p_ledger ORDER BY role FOR SHARE;
 IF (SELECT count(*) FROM lv.account_role_binding WHERE tenant_id = t AND ledger_id = p_ledger) <> 7 THEN
  RAISE EXCEPTION 'complete SP-1 account-role configuration required' USING ERRCODE = '23514';
 END IF;
 SELECT a.* INTO clr FROM lv.account a JOIN lv.account_role_binding b ON (b.tenant_id, b.ledger_id, b.account_id) = (a.tenant_id, a.ledger_id, a.id)
  WHERE b.tenant_id = t AND b.ledger_id = p_ledger AND b.role = 'PROCESSOR_CLEARING' FOR SHARE OF a;
 SELECT a.* INTO ar FROM lv.account a JOIN lv.account_role_binding b ON (b.tenant_id, b.ledger_id, b.account_id) = (a.tenant_id, a.ledger_id, a.id)
  WHERE b.tenant_id = t AND b.ledger_id = p_ledger AND b.role = 'AR_CONTROL' FOR SHARE OF a;
 IF NOT clr.active OR NOT ar.active THEN RAISE EXCEPTION 'posting account unavailable' USING ERRCODE = '23514'; END IF;
 PERFORM 1 FROM lv.party WHERE tenant_id = t AND ledger_id = p_ledger AND id = customer FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'customer is not a party of this ledger' USING ERRCODE = '23503'; END IF;
 SELECT * INTO current_period FROM lv.fiscal_period
  WHERE tenant_id = t AND ledger_id = p_ledger AND received >= start_date AND received < end_date FOR SHARE;
 IF NOT FOUND OR current_period.status <> 'OPEN' THEN RAISE EXCEPTION 'open period required' USING ERRCODE = '23514'; END IF;

 financial := jsonb_build_object('document_type', 'PAYMENT', 'method', 'PROCESSOR', 'processor_ref', p_request->>'processor_ref',
  'processor_item_id', p_request->>'processor_item_id', 'party_id', customer::text, 'amount', round(amount_value, l.currency_exponent)::text,
  'currency', l.currency::text, 'received_date', received::text);
 next_seq := counter.last_seq + 1; next_entry := counter.last_entry_no + 1;
 batch_content := lv.canonical_seed_json(jsonb_build_object('tenant_id', t::text, 'ledger_id', p_ledger::text, 'seq', next_seq::text,
  'kind', 'SEED', 'created_by', actor::text, 'request_hash', encode(sha256(convert_to(lv.canonical_seed_json(p_request), 'UTF8')), 'hex')));
 bh := encode(sha256(convert_to(batch_content || counter.batch_head, 'UTF8')), 'hex');
 INSERT INTO lv.commit_batch(tenant_id, ledger_id, seq, kind, created_by, previous_seq, previous_hash, canonical_content, hash)
 VALUES (t, p_ledger, next_seq, 'SEED', actor, nullif(next_seq - 1, 0), counter.batch_head, batch_content, bh);
 INSERT INTO lv.document(tenant_id, ledger_id, id, doc_type, source_ref, seq)
 VALUES (t, p_ledger, doc_id, 'PAYMENT', (p_request->>'processor_ref') || '/' || (p_request->>'processor_item_id'), next_seq);
 INSERT INTO lv.document_version(tenant_id, ledger_id, document_id, version, seq, financial_fields, financial_fingerprint, informational_fields,
  original_request, created_by, reason, origin, provenance)
 VALUES (t, p_ledger, doc_id, 1, next_seq, financial, encode(sha256(convert_to(lv.canonical_seed_json(financial), 'UTF8')), 'hex'),
  jsonb_build_object('description', p_request->>'description'), p_request, actor, 'Synthetic developer fixture: typed processor payment', 'SEED', 'SYNTHETIC');
 INSERT INTO lv.document_current VALUES (t, p_ledger, doc_id, 1);
 INSERT INTO lv.processor_payment_identity VALUES (t, p_ledger, doc_id, p_request->>'processor_ref', p_request->>'processor_item_id', next_seq);
 INSERT INTO lv.journal_entry(tenant_id, ledger_id, id, entry_no, seq, kind, effective_date, period_id, currency, currency_exponent,
  source_document_id, source_document_version, posting_rule_id, posting_rule_version, provenance, created_by, description)
 VALUES (t, p_ledger, entry_id, next_entry, next_seq, 'DOCUMENT_POSTING', received, current_period.id, l.currency, l.currency_exponent,
  doc_id, 1, 'sp1-posting', '1.0', 'SYNTHETIC', actor, p_request->>'description');
 INSERT INTO lv.journal_line(tenant_id, ledger_id, entry_id, line_no, seq, account_id, requires_party, party_id, document_id, side, amount, currency, currency_exponent)
 VALUES (t, p_ledger, entry_id, 1, next_seq, clr.id, clr.requires_party, NULL, doc_id, 'D', amount_value, l.currency, l.currency_exponent),
        (t, p_ledger, entry_id, 2, next_seq, ar.id, ar.requires_party, customer, doc_id, 'C', amount_value, l.currency, l.currency_exponent);
 INSERT INTO lv.payment_current VALUES (t, p_ledger, doc_id, 1, customer, l.currency, l.currency_exponent, amount_value, 0, amount_value, 'RECEIVED', next_seq);
 INSERT INTO lv.account_balance_current(tenant_id, ledger_id, account_id, party_id, debit_total, credit_total, last_seq)
 SELECT t, p_ledger, account_id, party_id, coalesce(sum(amount) FILTER (WHERE side = 'D'), 0), coalesce(sum(amount) FILTER (WHERE side = 'C'), 0), next_seq
   FROM lv.journal_line j WHERE j.tenant_id = t AND j.ledger_id = p_ledger AND j.entry_id = seed_payment.entry_id GROUP BY account_id, party_id
 ON CONFLICT (tenant_id, ledger_id, account_id, party_id) DO UPDATE SET
  debit_total = lv.account_balance_current.debit_total + EXCLUDED.debit_total,
  credit_total = lv.account_balance_current.credit_total + EXCLUDED.credit_total, last_seq = EXCLUDED.last_seq;
 INSERT INTO lv.party_balance_current(tenant_id, ledger_id, party_id, ar_balance, last_seq)
 SELECT t, p_ledger, j.party_id, sum(j.signed_amount), next_seq FROM lv.journal_line j JOIN lv.account_role_binding b
   ON b.tenant_id = j.tenant_id AND b.ledger_id = j.ledger_id AND b.account_id = j.account_id AND b.role = 'AR_CONTROL'
  WHERE j.tenant_id = t AND j.ledger_id = p_ledger AND j.entry_id = seed_payment.entry_id GROUP BY j.party_id
 ON CONFLICT (tenant_id, ledger_id, party_id) DO UPDATE SET ar_balance = lv.party_balance_current.ar_balance + EXCLUDED.ar_balance, last_seq = EXCLUDED.last_seq;
 event_content := lv.canonical_seed_json(lv.payment_seed_event(t, p_ledger, next_seq));
 eh := encode(sha256(convert_to(event_content || counter.event_head, 'UTF8')), 'hex');
 INSERT INTO lv.ledger_event VALUES (t, p_ledger, next_seq, nullif(next_seq - 1, 0), counter.event_head, eh, event_content);
 UPDATE lv.ledger_counter SET last_seq = next_seq, last_entry_no = next_entry, batch_head = bh, event_head = eh
  WHERE tenant_id = t AND ledger_id = p_ledger;
 RETURN jsonb_build_object('seq', next_seq::text, 'document_id', doc_id::text, 'entry_id', entry_id::text, 'replayed', false);
END $$;

-- Commit-time validation of a typed-payment seed batch (called from validate_seed_batch, as the owner).
-- Document, version, identity, projection and postings must agree, and the postings must be exactly the
-- sp1-posting/1.0 processor-payment rule applied to the version (I-15).
CREATE FUNCTION lv.validate_payment_seed(p_tenant uuid, p_ledger uuid, p_seq bigint, p_actor uuid, p_batch_content text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE d lv.document; v lv.document_version; e lv.journal_entry; i lv.processor_payment_identity; p lv.payment_current;
 clr uuid; ar uuid; actual jsonb; expected jsonb; ok boolean;
BEGIN
 IF (SELECT count(*) FROM lv.document WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.document_version WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.journal_entry WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.ledger_event WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.processor_payment_identity WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 THEN
  RAISE EXCEPTION 'incomplete typed payment commit' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO d FROM lv.document WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO v FROM lv.document_version WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO e FROM lv.journal_entry WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO i FROM lv.processor_payment_identity WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO p FROM lv.payment_current WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND document_id = d.id;
 SELECT account_id INTO clr FROM lv.account_role_binding WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND role = 'PROCESSOR_CLEARING';
 SELECT account_id INTO ar FROM lv.account_role_binding WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND role = 'AR_CONTROL';
 SELECT jsonb_agg(jsonb_build_object('line_no', line_no, 'account_id', account_id, 'party_id', party_id, 'document_id', document_id,
        'side', side, 'amount', amount) ORDER BY line_no) INTO actual
   FROM lv.journal_line WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND entry_id = e.id;
 expected := jsonb_build_array(
   jsonb_build_object('line_no', 1, 'account_id', clr, 'party_id', NULL, 'document_id', d.id, 'side', 'D', 'amount', p.amount),
   jsonb_build_object('line_no', 2, 'account_id', ar, 'party_id', p.party_id, 'document_id', d.id, 'side', 'C', 'amount', p.amount));
 ok := p.document_id IS NOT NULL AND p.version = 1 AND p.last_seq = p_seq AND p.applied = 0 AND p.unapplied = p.amount AND p.status = 'RECEIVED'
   AND d.doc_type::text = 'PAYMENT' AND i.document_id = d.id AND d.source_ref = i.processor_ref || '/' || i.processor_item_id
   AND v.document_id = d.id AND v.version = 1 AND v.origin = 'SEED' AND v.provenance = 'SYNTHETIC' AND v.created_by = p_actor
   AND EXISTS (SELECT 1 FROM lv.document_current c WHERE c.tenant_id = p_tenant AND c.ledger_id = p_ledger AND c.document_id = d.id AND c.version = 1)
   AND v.financial_fields = jsonb_build_object('document_type', 'PAYMENT', 'method', 'PROCESSOR', 'processor_ref', i.processor_ref,
        'processor_item_id', i.processor_item_id, 'party_id', p.party_id::text, 'amount', round(p.amount, p.currency_exponent)::text,
        'currency', p.currency::text, 'received_date', e.effective_date::text)
   AND v.original_request->>'processor_ref' = i.processor_ref AND v.original_request->>'processor_item_id' = i.processor_item_id
   AND v.original_request->>'party_id' = p.party_id::text AND (v.original_request->>'received_date')::date = e.effective_date
   AND (v.original_request->>'amount')::numeric = p.amount
   AND e.kind::text = 'DOCUMENT_POSTING' AND e.source_document_id = d.id AND e.source_document_version = 1
   AND e.posting_rule_id = 'sp1-posting' AND e.posting_rule_version = '1.0' AND e.provenance = 'SYNTHETIC' AND e.created_by = p_actor
   AND e.currency = p.currency AND actual = expected;
 IF NOT coalesce(ok, false) THEN
  RAISE EXCEPTION 'typed payment, version, identity, projection and postings diverge' USING ERRCODE = '23514';
 END IF;
 IF p_batch_content IS DISTINCT FROM lv.canonical_seed_json(jsonb_build_object('tenant_id', p_tenant::text, 'ledger_id', p_ledger::text,
      'seq', p_seq::text, 'kind', 'SEED', 'created_by', p_actor::text,
      'request_hash', encode(sha256(convert_to(lv.canonical_seed_json(v.original_request), 'UTF8')), 'hex')))
    OR (SELECT canonical_content FROM lv.ledger_event WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq)
       IS DISTINCT FROM lv.canonical_seed_json(lv.payment_seed_event(p_tenant, p_ledger, p_seq)) THEN
  RAISE EXCEPTION 'history content differs from committed facts' USING ERRCODE = '23514';
 END IF;
END $$;
"""

# validate_seed_batch: the 0002 body with a typed-payment branch. The manual-journal checks are copied verbatim.
VALIDATE = r"""
CREATE OR REPLACE FUNCTION lv.validate_seed_batch() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE e lv.journal_entry; v lv.document_version; d lv.document; ev lv.ledger_event;
 actual_lines jsonb; expected_event jsonb; expected_batch jsonb; seed_doc_type text;
BEGIN
 SELECT doc_type::text INTO seed_doc_type FROM lv.document WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq LIMIT 1;
 IF seed_doc_type = 'PAYMENT' THEN
  PERFORM lv.validate_payment_seed(NEW.tenant_id, NEW.ledger_id, NEW.seq, NEW.created_by, NEW.canonical_content);
 ELSE
 IF (SELECT count(*) FROM lv.journal_entry WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq)<>1 OR
    (SELECT count(*) FROM lv.document_version WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq)<>1 OR
    (SELECT count(*) FROM lv.document WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq)<>1 OR
    (SELECT count(*) FROM lv.ledger_event WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq)<>1 THEN
  RAISE EXCEPTION 'incomplete seed commit' USING ERRCODE='23514';
 END IF;
 SELECT * INTO e FROM lv.journal_entry WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq;
 SELECT * INTO v FROM lv.document_version WHERE tenant_id=e.tenant_id AND ledger_id=e.ledger_id AND document_id=e.source_document_id AND version=e.source_document_version;
 SELECT * INTO d FROM lv.document WHERE tenant_id=v.tenant_id AND ledger_id=v.ledger_id AND id=v.document_id;
 SELECT * INTO ev FROM lv.ledger_event WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq;
 SELECT jsonb_agg(jsonb_build_object('line_no',line_no::text,'account_id',account_id::text,'side',side::text,'amount',amount::text,'party_id',party_id::text) ORDER BY line_no)
 INTO actual_lines FROM lv.journal_line WHERE tenant_id=e.tenant_id AND ledger_id=e.ledger_id AND entry_id=e.id;
 IF v.financial_fields IS DISTINCT FROM jsonb_build_object('effective_date',e.effective_date::text,'currency',e.currency::text,'lines',actual_lines)
    OR v.version<>1 OR v.origin<>'SEED' OR v.created_by<>NEW.created_by OR e.created_by<>NEW.created_by
    OR NOT EXISTS(SELECT 1 FROM lv.document_current WHERE tenant_id=v.tenant_id AND ledger_id=v.ledger_id AND document_id=v.document_id AND version=v.version) THEN
  RAISE EXCEPTION 'document and postings diverge' USING ERRCODE='23514';
 END IF;
 expected_event:=jsonb_build_object('tenant_id',NEW.tenant_id::text,'ledger_id',NEW.ledger_id::text,'seq',NEW.seq::text,'kind','SEED','created_by',NEW.created_by::text,'entry_id',e.id::text,'entry_no',e.entry_no::text,'document_id',v.document_id::text,'document_version','1','financial_fields',v.financial_fields,'source_ref',d.source_ref,'description',e.description,'provenance','SYNTHETIC');
 expected_batch:=jsonb_build_object('tenant_id',NEW.tenant_id::text,'ledger_id',NEW.ledger_id::text,'seq',NEW.seq::text,'kind','SEED','created_by',NEW.created_by::text,'request_hash',encode(sha256(convert_to(lv.canonical_seed_json(v.original_request),'UTF8')),'hex'));
 IF ev.canonical_content IS DISTINCT FROM lv.canonical_seed_json(expected_event) OR NEW.canonical_content IS DISTINCT FROM lv.canonical_seed_json(expected_batch) THEN
  RAISE EXCEPTION 'history content differs from committed facts' USING ERRCODE='23514';
 END IF;
 END IF;
 IF EXISTS(
  WITH expected AS (
   SELECT account_id,party_id,coalesce(sum(amount) FILTER(WHERE side='D'),0) AS debit_total,coalesce(sum(amount) FILTER(WHERE side='C'),0) AS credit_total,max(seq) AS last_seq
   FROM lv.journal_line WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id GROUP BY account_id,party_id
  ), actual AS (SELECT account_id,party_id,debit_total,credit_total,last_seq FROM lv.account_balance_current WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id)
  (SELECT * FROM expected EXCEPT SELECT * FROM actual) UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)
 ) THEN RAISE EXCEPTION 'account projection differs from postings' USING ERRCODE='23514'; END IF;
 IF EXISTS(
  WITH expected AS (
   SELECT j.party_id,sum(j.signed_amount) AS ar_balance,max(j.seq) AS last_seq FROM lv.journal_line j JOIN lv.account_role_binding b
   ON b.tenant_id=j.tenant_id AND b.ledger_id=j.ledger_id AND b.account_id=j.account_id AND b.role='AR_CONTROL'
   WHERE j.tenant_id=NEW.tenant_id AND j.ledger_id=NEW.ledger_id GROUP BY j.party_id
  ), actual AS (SELECT party_id,ar_balance,last_seq FROM lv.party_balance_current WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id)
  (SELECT * FROM expected EXCEPT SELECT * FROM actual) UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)
 ) THEN RAISE EXCEPTION 'party projection differs from postings' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.ledger_counter c JOIN lv.commit_batch b ON (b.tenant_id,b.ledger_id,b.seq,b.hash)=(c.tenant_id,c.ledger_id,c.last_seq,c.batch_head)
  JOIN lv.ledger_event h ON (h.tenant_id,h.ledger_id,h.seq,h.hash)=(c.tenant_id,c.ledger_id,c.last_seq,c.event_head)
  WHERE c.tenant_id=NEW.tenant_id AND c.ledger_id=NEW.ledger_id AND c.last_seq=(SELECT max(seq) FROM lv.commit_batch WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id)
  AND c.last_entry_no=(SELECT max(entry_no) FROM lv.journal_entry WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id)) THEN
  RAISE EXCEPTION 'counter and history heads diverge' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
"""


def _original_validate_seed_batch() -> str:
    """The exact 0002 definition, read from the preserved 0002 SQL file, used to restore it on downgrade."""
    sql = (Path(__file__).parents[1] / 'sql/0002_posting.sql').read_text()
    start = sql.index('CREATE FUNCTION lv.validate_seed_batch()')
    end = sql.index('CREATE CONSTRAINT TRIGGER seed_commit_complete')
    return sql[start:end].replace('CREATE FUNCTION', 'CREATE OR REPLACE FUNCTION', 1)


def upgrade():
    bind = op.get_bind()
    bind.exec_driver_sql(UPGRADE)
    bind.exec_driver_sql(VALIDATE)
    for name in ('processor_payment_identity', 'payment_current'):
        p = "tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid"
        op.execute(f'ALTER TABLE lv.{name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE lv.{name} FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_scope ON lv.{name} USING ({p}) WITH CHECK ({p})')
    op.execute('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.processor_payment_identity FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change()')
    op.execute('CREATE TRIGGER fresh_batch BEFORE INSERT ON lv.processor_payment_identity FOR EACH ROW EXECUTE FUNCTION lv.require_fresh_batch()')
    op.execute('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lv FROM PUBLIC')
    op.execute('GRANT SELECT ON lv.processor_payment_identity, lv.payment_current TO lv_app, lv_seed')
    op.execute('GRANT EXECUTE ON FUNCTION lv.seed_processor_payment(uuid, jsonb) TO lv_seed')


def downgrade():
    # The migration runs as the table owner under FORCE ROW LEVEL SECURITY with no tenant context, so a plain query
    # would see no rows and the guard would pass vacuously. Lift FORCE for this check only, inside the same transaction.
    op.execute('ALTER TABLE lv.document NO FORCE ROW LEVEL SECURITY')
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM lv.document WHERE doc_type::text = 'PAYMENT') THEN
        RAISE EXCEPTION 'typed PAYMENT documents exist; downgrade is for disposable databases without them' USING ERRCODE = '55000';
      END IF; END $$""")
    op.execute('ALTER TABLE lv.document FORCE ROW LEVEL SECURITY')
    op.get_bind().exec_driver_sql(_original_validate_seed_batch())
    op.execute('ALTER FUNCTION lv.validate_seed_batch() SET search_path = pg_catalog, pg_temp')   # state after 0003
    op.execute('DROP FUNCTION lv.seed_processor_payment(uuid, jsonb)')
    op.execute('DROP FUNCTION lv.validate_payment_seed(uuid, uuid, bigint, uuid, text)')
    op.execute('DROP FUNCTION lv.payment_seed_event(uuid, uuid, bigint)')
    op.execute('DROP TABLE lv.payment_current')
    op.execute('DROP TABLE lv.processor_payment_identity')
