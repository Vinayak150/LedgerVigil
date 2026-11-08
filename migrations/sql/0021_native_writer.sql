CREATE FUNCTION lv.settlement_execution_event(p_tenant uuid, p_ledger uuid, p_seq bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE b lv.commit_batch; d lv.document; v lv.document_version; e lv.journal_entry; lines jsonb; payments jsonb;
BEGIN
 SELECT * INTO b FROM lv.commit_batch WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO d FROM lv.document WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO v FROM lv.document_version WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND document_id = d.id AND version = 1;
 SELECT * INTO e FROM lv.journal_entry WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT jsonb_agg(jsonb_build_object('line_no', line_no::text, 'account_id', account_id::text, 'side', side::text,
        'amount', amount::text, 'party_id', party_id::text) ORDER BY line_no)
   INTO lines FROM lv.journal_line WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND entry_id = e.id;
 SELECT jsonb_agg(jsonb_build_object('line_no', i.line_no::text, 'document_id', i.payment_document_id::text,
        'version_before', i.payment_version_before::text, 'version_after', i.payment_version_after::text,
        'financial_fingerprint_after', pv.financial_fingerprint) ORDER BY i.line_no)
   INTO payments FROM lv.settlement_inclusion i JOIN lv.document_version pv
     ON (pv.tenant_id, pv.ledger_id, pv.document_id, pv.version) = (i.tenant_id, i.ledger_id, i.payment_document_id, i.payment_version_after)
  WHERE i.tenant_id = p_tenant AND i.ledger_id = p_ledger AND i.seq = p_seq;
 RETURN jsonb_build_object('tenant_id', p_tenant::text, 'ledger_id', p_ledger::text, 'seq', p_seq::text, 'kind', 'EXECUTION',
  'created_by_user', b.created_by_user::text, 'document_type', d.doc_type::text, 'document_id', d.id::text, 'document_version', v.version::text,
  'source_ref', d.source_ref, 'entry_id', e.id::text, 'entry_no', e.entry_no::text,
  'posting_rule', e.posting_rule_id || '/' || e.posting_rule_version, 'financial_fields', v.financial_fields,
  'postings', jsonb_build_object('effective_date', e.effective_date::text, 'currency', e.currency::text, 'lines', lines),
  'payment_versions', payments, 'description', e.description, 'provenance', 'SYNTHETIC');
END $$;
CREATE FUNCTION lv.post_native_settlement(p_execution uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
<<runtime_settlement>>
DECLARE
 p_ledger uuid; p_request jsonb; ex lv.native_execution; operation jsonb;
 t uuid := nullif(current_setting('app.tenant_id', true), '')::uuid;
 l lv.ledger; actor uuid; counter lv.ledger_counter; current_period lv.fiscal_period;
 existing lv.processor_settlement_identity; existing_request jsonb;
 doc_id uuid := uuidv7(); entry_id uuid := uuidv7();
 gross_value numeric; fee_value numeric; net_value numeric; payout date; item_total numeric := 0;
 bank lv.account; fee_account lv.account; clr lv.account; item record; pay record; financial jsonb; items_json jsonb := '[]'::jsonb;
 batch_content text; event_content text; bh text; eh text; next_seq bigint; next_entry bigint; line_counter integer := 0;
 keys text[] := ARRAY['processor_ref', 'payout_id', 'payout_date', 'currency', 'gross', 'fee', 'net', 'items', 'description'];
BEGIN
 IF t IS NULL THEN RAISE EXCEPTION 'tenant context required' USING ERRCODE = '42501'; END IF;
 SELECT * INTO ex FROM lv.native_execution WHERE tenant_id=t AND id=p_execution;
 IF ex.id IS NULL OR ex.state<>'BUILDING' OR ex.tx_id<>pg_current_xact_id() THEN RAISE EXCEPTION 'same-transaction execution proof required' USING ERRCODE='42501'; END IF;
 actor:=ex.executed_by;p_ledger:=ex.ledger_id;
 SELECT operations->0 INTO operation FROM lv.proposal_revision WHERE tenant_id=t AND proposal_id=ex.proposal_id AND revision=ex.revision;
 p_request:=jsonb_build_object('processor_ref',operation->>'processor_ref','payout_id',operation->>'payout_id','payout_date',operation->>'payout_date','currency',operation->'gross'->>'currency',
 'gross',operation->'gross'->>'amount','fee',operation->'fee'->>'amount','net',operation->'net'->>'amount','description','Approved native SANDBOX settlement',
 'items',(SELECT jsonb_agg(jsonb_build_object('document_id',x->>'document_id','kind',x->>'kind','amount',x->'amount'->>'amount') ORDER BY n) FROM jsonb_array_elements(operation->'items') WITH ORDINALITY a(x,n)));
 SELECT * INTO l FROM lv.ledger WHERE tenant_id = t AND id = p_ledger FOR SHARE;
 IF NOT FOUND OR l.purpose <> 'SANDBOX' OR l.source_kind <> 'NATIVE' THEN
  RAISE EXCEPTION 'seed requires a native SANDBOX ledger' USING ERRCODE = '23514';
 END IF;
 IF p_request IS NULL OR jsonb_typeof(p_request) <> 'object' OR octet_length(p_request::text) > 262144
    OR NOT p_request ?& keys OR (p_request - keys) <> '{}'::jsonb
    OR EXISTS (SELECT 1 FROM jsonb_each(p_request) WHERE key <> 'items' AND jsonb_typeof(value) <> 'string')
    OR jsonb_typeof(p_request->'items') <> 'array' OR jsonb_array_length(p_request->'items') NOT BETWEEN 1 AND 1000
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_request->'items') x WHERE jsonb_typeof(x) <> 'object'
               OR NOT x ?& ARRAY['document_id', 'kind', 'amount'] OR (x - ARRAY['document_id', 'kind', 'amount']) <> '{}'::jsonb
               OR EXISTS (SELECT 1 FROM jsonb_each(x) WHERE jsonb_typeof(value) <> 'string')) THEN
  RAISE EXCEPTION 'invalid typed settlement request' USING ERRCODE = '23514';
 END IF;
 IF (p_request->>'processor_ref') !~ '^[A-Za-z0-9_.:-]{1,64}$' OR (p_request->>'payout_id') !~ '^[A-Za-z0-9_.:-]{1,63}$'
    OR (p_request->>'payout_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' OR length(p_request->>'description') > 512
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_request->'items') x WHERE x->>'document_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
  RAISE EXCEPTION 'invalid typed settlement request' USING ERRCODE = '23514';
 END IF;
 IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_request->'items') x WHERE x->>'kind' = 'REFUND') THEN
  RAISE EXCEPTION 'settlements containing refunds are not supported' USING ERRCODE = '0A000';
 END IF;
 IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_request->'items') x WHERE x->>'kind' <> 'PAYMENT') THEN
  RAISE EXCEPTION 'invalid typed settlement request' USING ERRCODE = '23514';
 END IF;
 IF (SELECT count(DISTINCT x->>'document_id') FROM jsonb_array_elements(p_request->'items') x) <> jsonb_array_length(p_request->'items') THEN
  RAISE EXCEPTION 'a payment is repeated within the settlement request' USING ERRCODE = '23514';
 END IF;
 IF p_request->>'currency' IS DISTINCT FROM l.currency::text THEN
  RAISE EXCEPTION 'settlement currency must equal the ledger functional currency' USING ERRCODE = '23514';
 END IF;
 gross_value := lv.parse_line_amount(p_request->>'gross', l.currency_exponent);          -- exact, > 0
 fee_value := lv.parse_nonnegative_amount(p_request->>'fee', l.currency_exponent);       -- exact, >= 0
 net_value := lv.parse_nonnegative_amount(p_request->>'net', l.currency_exponent);       -- exact, >= 0
 IF net_value <> gross_value - fee_value THEN
  RAISE EXCEPTION 'net must equal gross minus fee, with fee and net not negative' USING ERRCODE = '23514';
 END IF;
 FOR item IN SELECT x->>'amount' AS amount FROM jsonb_array_elements(p_request->'items') x LOOP
  item_total := item_total + lv.parse_line_amount(item.amount, l.currency_exponent);     -- exact, > 0, validated before use
 END LOOP;
 IF item_total <> gross_value THEN
  RAISE EXCEPTION 'gross must equal the sum of included payment amounts' USING ERRCODE = '23514';
 END IF;
 payout := (p_request->>'payout_date')::date;
 -- Serialize per ledger before identity and inclusion detection, so simultaneous duplicates and competing inclusions converge.
 INSERT INTO lv.ledger_counter(tenant_id, ledger_id) VALUES (t, p_ledger) ON CONFLICT DO NOTHING;
 SELECT * INTO counter FROM lv.ledger_counter WHERE tenant_id = t AND ledger_id = p_ledger FOR UPDATE;
 SELECT * INTO existing FROM lv.processor_settlement_identity
  WHERE tenant_id = t AND ledger_id = p_ledger AND processor_ref = p_request->>'processor_ref' AND payout_id = p_request->>'payout_id';
 IF FOUND THEN RAISE EXCEPTION 'payout already recorded' USING ERRCODE='23505'; END IF;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id = t AND ledger_id = p_ledger ORDER BY role FOR SHARE;
 IF (SELECT count(*) FROM lv.account_role_binding WHERE tenant_id = t AND ledger_id = p_ledger) <> 7 THEN
  RAISE EXCEPTION 'complete SP-1 account-role configuration required' USING ERRCODE = '23514';
 END IF;
 SELECT a.* INTO bank FROM lv.account a JOIN lv.account_role_binding b ON (b.tenant_id, b.ledger_id, b.account_id) = (a.tenant_id, a.ledger_id, a.id)
  WHERE b.tenant_id = t AND b.ledger_id = p_ledger AND b.role = 'BANK_OPERATING' FOR SHARE OF a;
 SELECT a.* INTO fee_account FROM lv.account a JOIN lv.account_role_binding b ON (b.tenant_id, b.ledger_id, b.account_id) = (a.tenant_id, a.ledger_id, a.id)
  WHERE b.tenant_id = t AND b.ledger_id = p_ledger AND b.role = 'PROCESSOR_FEE_EXPENSE' FOR SHARE OF a;
 SELECT a.* INTO clr FROM lv.account a JOIN lv.account_role_binding b ON (b.tenant_id, b.ledger_id, b.account_id) = (a.tenant_id, a.ledger_id, a.id)
  WHERE b.tenant_id = t AND b.ledger_id = p_ledger AND b.role = 'PROCESSOR_CLEARING' FOR SHARE OF a;
 IF NOT bank.active OR NOT fee_account.active OR NOT clr.active THEN RAISE EXCEPTION 'posting account unavailable' USING ERRCODE = '23514'; END IF;
 SELECT * INTO current_period FROM lv.fiscal_period
  WHERE tenant_id = t AND ledger_id = p_ledger AND payout >= start_date AND payout < end_date FOR SHARE;
 IF NOT FOUND OR current_period.status <> 'OPEN' THEN RAISE EXCEPTION 'open period required' USING ERRCODE = '23514'; END IF;
 -- Every item: a typed processor payment of THIS tenant and ledger, same processor, RECEIVED, full amount, not included.
 FOR item IN SELECT (x->>'document_id')::uuid AS document_id, x->>'amount' AS amount, n::integer AS line_no
             FROM jsonb_array_elements(p_request->'items') WITH ORDINALITY AS a(x, n) ORDER BY n LOOP
  SELECT i.processor_ref, c.version AS current_version, p.version AS projected_version, p.amount, p.currency, p.status,
         v.financial_fields, v.informational_fields
    INTO pay
    FROM lv.processor_payment_identity i
    JOIN lv.document_current c ON (c.tenant_id, c.ledger_id, c.document_id) = (i.tenant_id, i.ledger_id, i.document_id)
    JOIN lv.payment_current p ON (p.tenant_id, p.ledger_id, p.document_id) = (i.tenant_id, i.ledger_id, i.document_id)
    JOIN lv.document_version v ON (v.tenant_id, v.ledger_id, v.document_id, v.version) = (c.tenant_id, c.ledger_id, c.document_id, c.version)
   WHERE i.tenant_id = t AND i.ledger_id = p_ledger AND i.document_id = item.document_id
   FOR UPDATE OF c, p;
  IF NOT FOUND THEN RAISE EXCEPTION 'typed processor payment not found in this ledger' USING ERRCODE = '23503'; END IF;
  IF EXISTS (SELECT 1 FROM lv.settlement_inclusion_current WHERE tenant_id = t AND ledger_id = p_ledger AND item_document_id = item.document_id) THEN
   RAISE EXCEPTION 'payment already included in an active settlement' USING ERRCODE = '23505';
  END IF;
  IF pay.processor_ref <> p_request->>'processor_ref' THEN
   RAISE EXCEPTION 'payment belongs to another processor' USING ERRCODE = '23514';
  END IF;
  IF pay.currency::text <> l.currency::text OR pay.status <> 'RECEIVED' OR pay.current_version <> pay.projected_version THEN
   RAISE EXCEPTION 'payment is not a settleable RECEIVED payment in the ledger currency' USING ERRCODE = '23514';
  END IF;
  IF pay.amount <> lv.parse_line_amount(item.amount, l.currency_exponent) THEN
   RAISE EXCEPTION 'each payment settles at its full amount' USING ERRCODE = '23514';
  END IF;
  items_json := items_json || jsonb_build_array(jsonb_build_object('line_no', item.line_no::text, 'document_id', item.document_id::text,
                 'kind', 'PAYMENT', 'amount', round(pay.amount, l.currency_exponent)::text, 'payment_version', pay.current_version::text));
 END LOOP;

 financial := jsonb_build_object('document_type', 'SETTLEMENT', 'processor_ref', p_request->>'processor_ref', 'payout_id', p_request->>'payout_id',
  'payout_date', payout::text, 'currency', l.currency::text, 'gross', round(gross_value, l.currency_exponent)::text,
  'fee', round(fee_value, l.currency_exponent)::text, 'net', round(net_value, l.currency_exponent)::text,
  'bank_role', 'BANK_OPERATING', 'items', items_json);
 next_seq := counter.last_seq + 1; next_entry := counter.last_entry_no + 1;
 batch_content := lv.canonical_seed_json(jsonb_build_object('tenant_id', t::text, 'ledger_id', p_ledger::text, 'seq', next_seq::text,
  'kind', 'EXECUTION', 'created_by_user', actor::text, 'request_hash', encode(sha256(convert_to(lv.canonical_seed_json(p_request), 'UTF8')), 'hex')));
 bh := encode(sha256(convert_to(batch_content || counter.batch_head, 'UTF8')), 'hex');
 INSERT INTO lv.commit_batch(tenant_id, ledger_id, seq, kind, created_by_user, previous_seq, previous_hash, canonical_content, hash)
 VALUES (t, p_ledger, next_seq, 'EXECUTION', actor, nullif(next_seq - 1, 0), counter.batch_head, batch_content, bh);
 INSERT INTO lv.document(tenant_id, ledger_id, id, doc_type, source_ref, seq)
 VALUES (t, p_ledger, doc_id, 'SETTLEMENT', (p_request->>'processor_ref') || '/' || (p_request->>'payout_id'), next_seq);
 INSERT INTO lv.document_version(tenant_id, ledger_id, document_id, version, seq, ordinal, financial_fields, financial_fingerprint,
  informational_fields, original_request, created_by_user, reason, origin, provenance)
 VALUES (t, p_ledger, doc_id, 1, next_seq, 1, financial, encode(sha256(convert_to(lv.canonical_seed_json(financial), 'UTF8')), 'hex'),
  jsonb_build_object('description', p_request->>'description'), p_request, actor, 'Approved native SANDBOX settlement', 'EXECUTION', 'SYNTHETIC');
 INSERT INTO lv.document_current VALUES (t, p_ledger, doc_id, 1);
 INSERT INTO lv.processor_settlement_identity VALUES (t, p_ledger, doc_id, p_request->>'processor_ref', p_request->>'payout_id', next_seq);
 INSERT INTO lv.settlement_current VALUES (t, p_ledger, doc_id, 1, p_request->>'processor_ref', p_request->>'payout_id', payout,
  l.currency, l.currency_exponent, gross_value, fee_value, net_value, jsonb_array_length(p_request->'items'), 'RECORDED', next_seq);
 INSERT INTO lv.journal_entry(tenant_id, ledger_id, id, entry_no, seq, kind, effective_date, period_id, currency, currency_exponent,
  source_document_id, source_document_version, posting_rule_id, posting_rule_version, provenance, created_by_user, description)
 VALUES (t, p_ledger, entry_id, next_entry, next_seq, 'DOCUMENT_POSTING', payout, current_period.id, l.currency, l.currency_exponent,
  doc_id, 1, 'sp1-posting', '1.0', 'SYNTHETIC', actor, p_request->>'description');
 -- §5.1 / §19.3: Dr BANK net; Dr FEE fee (omitted when zero); Cr CLR gross. A zero net line is omitted too: lines are > 0 (I-02).
 IF net_value > 0 THEN
  line_counter := line_counter + 1;
  INSERT INTO lv.journal_line(tenant_id, ledger_id, entry_id, line_no, seq, account_id, requires_party, party_id, document_id, side, amount, currency, currency_exponent)
  VALUES (t, p_ledger, entry_id, line_counter, next_seq, bank.id, bank.requires_party, NULL, doc_id, 'D', net_value, l.currency, l.currency_exponent);
 END IF;
 IF fee_value > 0 THEN
  line_counter := line_counter + 1;
  INSERT INTO lv.journal_line(tenant_id, ledger_id, entry_id, line_no, seq, account_id, requires_party, party_id, document_id, side, amount, currency, currency_exponent)
  VALUES (t, p_ledger, entry_id, line_counter, next_seq, fee_account.id, fee_account.requires_party, NULL, doc_id, 'D', fee_value, l.currency, l.currency_exponent);
 END IF;
 line_counter := line_counter + 1;
 INSERT INTO lv.journal_line(tenant_id, ledger_id, entry_id, line_no, seq, account_id, requires_party, party_id, document_id, side, amount, currency, currency_exponent)
 VALUES (t, p_ledger, entry_id, line_counter, next_seq, clr.id, clr.requires_party, NULL, doc_id, 'C', gross_value, l.currency, l.currency_exponent);
 -- Each included payment: inclusion history, active inclusion, a new immutable payment version (status SETTLED + settlement
 -- link), the current pointer advanced by compare-and-swap, and the projection. Payment postings are unchanged
 -- (the payment rule does not depend on status or links), so no reversal or re-posting is written (§5.1 supersession).
 FOR item IN SELECT (x->>'document_id')::uuid AS document_id, n::integer AS line_no
             FROM jsonb_array_elements(financial->'items') WITH ORDINALITY AS a(x, n) ORDER BY n LOOP
  SELECT c.version AS before_version, p.amount, v.financial_fields, v.informational_fields INTO pay
    FROM lv.document_current c
    JOIN lv.payment_current p ON (p.tenant_id, p.ledger_id, p.document_id) = (c.tenant_id, c.ledger_id, c.document_id)
    JOIN lv.document_version v ON (v.tenant_id, v.ledger_id, v.document_id, v.version) = (c.tenant_id, c.ledger_id, c.document_id, c.version)
   WHERE c.tenant_id = t AND c.ledger_id = p_ledger AND c.document_id = item.document_id;
  INSERT INTO lv.document_version(tenant_id, ledger_id, document_id, version, previous_version, seq, ordinal, financial_fields,
   financial_fingerprint, informational_fields, original_request, created_by_user, reason, origin, provenance)
  SELECT t, p_ledger, item.document_id, pay.before_version + 1, pay.before_version, next_seq, 1 + item.line_no, f,
         encode(sha256(convert_to(lv.canonical_seed_json(f), 'UTF8')), 'hex'), pay.informational_fields, p_request, actor,
         'Approved native settlement inclusion ' || (p_request->>'processor_ref') || '/' || (p_request->>'payout_id'), 'EXECUTION', 'SYNTHETIC'
    FROM (SELECT pay.financial_fields || jsonb_build_object('status', 'SETTLED', 'settlement_document_id', doc_id::text, 'settlement_version', '1') AS f) s;
  INSERT INTO lv.settlement_inclusion VALUES (t, p_ledger, doc_id, 1, item.line_no, item.document_id, pay.before_version, pay.before_version + 1,
   'PAYMENT', pay.amount, next_seq);
  INSERT INTO lv.settlement_inclusion_current VALUES (t, p_ledger, item.document_id, doc_id, 1, next_seq);
  UPDATE lv.document_current SET version = pay.before_version + 1
   WHERE tenant_id = t AND ledger_id = p_ledger AND document_id = item.document_id AND version = pay.before_version;
  IF NOT FOUND THEN RAISE EXCEPTION 'payment version changed concurrently' USING ERRCODE = '40001'; END IF;
  UPDATE lv.payment_current SET version = pay.before_version + 1, status = 'SETTLED', settlement_document_id = doc_id, last_seq = next_seq
   WHERE tenant_id = t AND ledger_id = p_ledger AND document_id = item.document_id AND version = pay.before_version AND status = 'RECEIVED';
  IF NOT FOUND THEN RAISE EXCEPTION 'payment projection changed concurrently' USING ERRCODE = '40001'; END IF;
 END LOOP;
 INSERT INTO lv.account_balance_current(tenant_id, ledger_id, account_id, party_id, debit_total, credit_total, last_seq)
 SELECT t, p_ledger, account_id, party_id, coalesce(sum(amount) FILTER (WHERE side = 'D'), 0), coalesce(sum(amount) FILTER (WHERE side = 'C'), 0), next_seq
   FROM lv.journal_line j WHERE j.tenant_id = t AND j.ledger_id = p_ledger AND j.entry_id = runtime_settlement.entry_id GROUP BY account_id, party_id
 ON CONFLICT (tenant_id, ledger_id, account_id, party_id) DO UPDATE SET
  debit_total = lv.account_balance_current.debit_total + EXCLUDED.debit_total,
  credit_total = lv.account_balance_current.credit_total + EXCLUDED.credit_total, last_seq = EXCLUDED.last_seq;
 UPDATE lv.native_execution SET commit_seq=next_seq,settlement_document_id=doc_id,entry_id=runtime_settlement.entry_id WHERE tenant_id=t AND id=p_execution;
 event_content := lv.canonical_seed_json(lv.settlement_execution_event(t, p_ledger, next_seq));
 eh := encode(sha256(convert_to(event_content || counter.event_head, 'UTF8')), 'hex');
 INSERT INTO lv.ledger_event VALUES (t, p_ledger, next_seq, nullif(next_seq - 1, 0), counter.event_head, eh, event_content);
 UPDATE lv.ledger_counter SET last_seq = next_seq, last_entry_no = next_entry, batch_head = bh, event_head = eh
  WHERE tenant_id = t AND ledger_id = p_ledger;
 RETURN jsonb_build_object('seq', next_seq::text, 'document_id', doc_id::text, 'entry_id', entry_id::text, 'replayed', false);
END $$;
CREATE FUNCTION lv.validate_settlement_execution(p_tenant uuid, p_ledger uuid, p_seq bigint, p_actor uuid, p_batch_content text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE d lv.document; v lv.document_version; e lv.journal_entry; i lv.processor_settlement_identity; s lv.settlement_current;
 n integer; bank uuid; fee uuid; clr uuid; ar uuid; actual jsonb; expected jsonb; expected_items jsonb; ok boolean;
BEGIN
 SELECT count(*) INTO n FROM lv.settlement_inclusion WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 IF (SELECT count(*) FROM lv.document WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.journal_entry WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.ledger_event WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.processor_settlement_identity WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 1 OR
    (SELECT count(*) FROM lv.processor_payment_identity WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> 0 OR
    (SELECT count(*) FROM lv.settlement_inclusion_current WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND last_seq = p_seq) <> n OR
    (SELECT count(*) FROM lv.document_version WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq) <> n + 1 OR n < 1 THEN
  RAISE EXCEPTION 'incomplete typed settlement commit' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO d FROM lv.document WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO v FROM lv.document_version WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND document_id = d.id AND version = 1;
 SELECT * INTO e FROM lv.journal_entry WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO i FROM lv.processor_settlement_identity WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq;
 SELECT * INTO s FROM lv.settlement_current WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND document_id = d.id;
 SELECT account_id INTO bank FROM lv.account_role_binding WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND role = 'BANK_OPERATING';
 SELECT account_id INTO fee FROM lv.account_role_binding WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND role = 'PROCESSOR_FEE_EXPENSE';
 SELECT account_id INTO clr FROM lv.account_role_binding WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND role = 'PROCESSOR_CLEARING';
 SELECT account_id INTO ar FROM lv.account_role_binding WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND role = 'AR_CONTROL';
 SELECT jsonb_agg(jsonb_build_object('line_no', inc.line_no::text, 'document_id', inc.payment_document_id::text, 'kind', 'PAYMENT',
        'amount', round(inc.amount, s.currency_exponent)::text, 'payment_version', inc.payment_version_before::text) ORDER BY inc.line_no)
   INTO expected_items FROM lv.settlement_inclusion inc WHERE inc.tenant_id = p_tenant AND inc.ledger_id = p_ledger AND inc.seq = p_seq;
 -- Derived lines, rebuilt from the settlement row and the bindings: Dr BANK net (if > 0); Dr FEE fee (if > 0); Cr CLR gross.
 SELECT jsonb_agg(x ORDER BY ord) INTO expected FROM (
   SELECT row_number() OVER (ORDER BY ord) AS line_no, ord, account_id, side, amount FROM (VALUES
     (1, bank, 'D', s.net), (2, fee, 'D', s.fee), (3, clr, 'C', s.gross)) AS r(ord, account_id, side, amount) WHERE amount > 0) q,
   LATERAL (SELECT jsonb_build_object('line_no', q.line_no, 'account_id', q.account_id, 'party_id', NULL, 'document_id', d.id,
            'side', q.side, 'amount', q.amount) AS x) z;
 SELECT jsonb_agg(jsonb_build_object('line_no', line_no, 'account_id', account_id, 'party_id', party_id, 'document_id', document_id,
        'side', side, 'amount', amount) ORDER BY line_no) INTO actual
   FROM lv.journal_line WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND entry_id = e.id;
 ok := d.doc_type::text = 'SETTLEMENT' AND d.source_ref = i.processor_ref || '/' || i.payout_id AND i.document_id = d.id
   AND s.document_id IS NOT NULL AND s.version = 1 AND s.last_seq = p_seq AND s.status = 'RECORDED' AND s.item_count = n
   AND (s.processor_ref, s.payout_id) = (i.processor_ref, i.payout_id) AND s.currency = e.currency AND s.payout_date = e.effective_date
   AND s.gross = (SELECT sum(amount) FROM lv.settlement_inclusion WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq)
   AND s.net = s.gross - s.fee
   AND v.seq = p_seq AND v.ordinal = 1 AND v.previous_version IS NULL AND v.origin = 'EXECUTION' AND v.provenance = 'SYNTHETIC' AND v.created_by_user = p_actor
   AND EXISTS (SELECT 1 FROM lv.document_current c WHERE c.tenant_id = p_tenant AND c.ledger_id = p_ledger AND c.document_id = d.id AND c.version = 1)
   AND v.financial_fields = jsonb_build_object('document_type', 'SETTLEMENT', 'processor_ref', s.processor_ref, 'payout_id', s.payout_id,
        'payout_date', s.payout_date::text, 'currency', s.currency::text, 'gross', round(s.gross, s.currency_exponent)::text,
        'fee', round(s.fee, s.currency_exponent)::text, 'net', round(s.net, s.currency_exponent)::text, 'bank_role', 'BANK_OPERATING',
        'items', expected_items)
   AND v.original_request->>'processor_ref' = s.processor_ref AND v.original_request->>'payout_id' = s.payout_id
   AND (v.original_request->>'payout_date')::date = s.payout_date AND (v.original_request->>'gross')::numeric = s.gross
   AND (v.original_request->>'fee')::numeric = s.fee AND (v.original_request->>'net')::numeric = s.net
   AND e.kind::text = 'DOCUMENT_POSTING' AND e.source_document_id = d.id AND e.source_document_version = 1
   AND e.posting_rule_id = 'sp1-posting' AND e.posting_rule_version = '1.0' AND e.provenance = 'SYNTHETIC' AND e.created_by_user = p_actor
   AND actual = expected
   -- Every inclusion: same processor; new payment version = previous + status/link; pointer, projection and active
   -- inclusion agree; the payment's own posting is untouched and still equals its rule applied to the new version (I-15).
   AND NOT EXISTS (
     SELECT 1 FROM lv.settlement_inclusion inc
     JOIN lv.processor_payment_identity pi ON (pi.tenant_id, pi.ledger_id, pi.document_id) = (inc.tenant_id, inc.ledger_id, inc.payment_document_id)
     JOIN lv.document_version pb ON (pb.tenant_id, pb.ledger_id, pb.document_id, pb.version) = (inc.tenant_id, inc.ledger_id, inc.payment_document_id, inc.payment_version_before)
     LEFT JOIN lv.document_version pa ON (pa.tenant_id, pa.ledger_id, pa.document_id, pa.version) = (inc.tenant_id, inc.ledger_id, inc.payment_document_id, inc.payment_version_after)
     LEFT JOIN lv.document_current pc ON (pc.tenant_id, pc.ledger_id, pc.document_id) = (inc.tenant_id, inc.ledger_id, inc.payment_document_id)
     LEFT JOIN lv.payment_current pp ON (pp.tenant_id, pp.ledger_id, pp.document_id) = (inc.tenant_id, inc.ledger_id, inc.payment_document_id)
     LEFT JOIN lv.settlement_inclusion_current ic ON (ic.tenant_id, ic.ledger_id, ic.item_document_id) = (inc.tenant_id, inc.ledger_id, inc.payment_document_id)
     WHERE inc.tenant_id = p_tenant AND inc.ledger_id = p_ledger AND inc.seq = p_seq AND NOT coalesce(
       inc.settlement_document_id = d.id AND inc.settlement_version = 1 AND pi.processor_ref = s.processor_ref
       AND pb.seq < p_seq AND pa.seq = p_seq AND pa.previous_version = pb.version AND pa.ordinal = 1 + inc.line_no
       AND pa.origin = 'EXECUTION' AND pa.provenance = 'SYNTHETIC' AND pa.created_by_user = p_actor AND pa.original_request = v.original_request
       AND pb.financial_fields->>'status' IS DISTINCT FROM 'SETTLED'
       AND pa.financial_fields = pb.financial_fields || jsonb_build_object('status', 'SETTLED', 'settlement_document_id', d.id::text, 'settlement_version', '1')
       AND pa.informational_fields = pb.informational_fields
       AND pc.version = pa.version AND pp.version = pa.version AND pp.status = 'SETTLED' AND pp.settlement_document_id = d.id
       AND pp.last_seq = p_seq AND pp.amount = inc.amount AND pp.amount = (pb.financial_fields->>'amount')::numeric
       AND ic.settlement_document_id = d.id AND ic.settlement_version = 1 AND ic.last_seq = p_seq
       AND (SELECT count(*) FROM lv.journal_entry pe WHERE pe.tenant_id = p_tenant AND pe.ledger_id = p_ledger AND pe.source_document_id = inc.payment_document_id) = 1
       AND EXISTS (SELECT 1 FROM lv.journal_entry pe WHERE pe.tenant_id = p_tenant AND pe.ledger_id = p_ledger
                   AND pe.source_document_id = inc.payment_document_id AND pe.source_document_version < pa.version AND pe.seq < p_seq
                   AND (SELECT jsonb_agg(jsonb_build_object('a', pl.account_id, 's', pl.side, 'm', pl.amount, 'p', pl.party_id) ORDER BY pl.line_no)
                          FROM lv.journal_line pl WHERE pl.tenant_id = p_tenant AND pl.ledger_id = p_ledger AND pl.entry_id = pe.id)
                       = jsonb_build_array(jsonb_build_object('a', clr, 's', 'D', 'm', (pa.financial_fields->>'amount')::numeric, 'p', NULL),
                                           jsonb_build_object('a', ar, 's', 'C', 'm', (pa.financial_fields->>'amount')::numeric,
                                                              'p', (pa.financial_fields->>'party_id')::uuid)))
     , false));
 IF NOT coalesce(ok, false) THEN
  RAISE EXCEPTION 'typed settlement, versions, inclusions, projections and postings diverge' USING ERRCODE = '23514';
 END IF;
 IF p_batch_content IS DISTINCT FROM lv.canonical_seed_json(jsonb_build_object('tenant_id', p_tenant::text, 'ledger_id', p_ledger::text,
      'seq', p_seq::text, 'kind', 'EXECUTION', 'created_by_user', p_actor::text,
      'request_hash', encode(sha256(convert_to(lv.canonical_seed_json(v.original_request), 'UTF8')), 'hex')))
    OR (SELECT canonical_content FROM lv.ledger_event WHERE tenant_id = p_tenant AND ledger_id = p_ledger AND seq = p_seq)
       IS DISTINCT FROM lv.canonical_seed_json(lv.settlement_execution_event(p_tenant, p_ledger, p_seq)) THEN
  RAISE EXCEPTION 'history content differs from committed facts' USING ERRCODE = '23514';
 END IF;
END $$;

REVOKE ALL ON FUNCTION lv.post_native_settlement(uuid),lv.validate_settlement_execution(uuid,uuid,bigint,uuid,text),lv.settlement_execution_event(uuid,uuid,bigint) FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
