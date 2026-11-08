-- Helpers operate on a deliberately narrow JSON subset: ASCII keys, strings,
-- nulls, arrays and objects. No JSON numbers cross the financial write boundary.
CREATE FUNCTION lv.canonical_seed_json(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
DECLARE result text;
BEGIN
 CASE jsonb_typeof(v)
 WHEN 'string' THEN RETURN v::text;
 WHEN 'null' THEN RETURN 'null';
 WHEN 'array' THEN
  SELECT '[' || coalesce(string_agg(lv.canonical_seed_json(value),',' ORDER BY ord),'') || ']'
   INTO result FROM jsonb_array_elements(v) WITH ORDINALITY a(value,ord);
 WHEN 'object' THEN
  IF EXISTS(SELECT 1 FROM jsonb_object_keys(v) k WHERE k !~ '^[a-z_]+$') THEN
   RAISE EXCEPTION 'unsupported canonical key' USING ERRCODE='23514';
  END IF;
  SELECT '{' || coalesce(string_agg(to_jsonb(key)::text || ':' || lv.canonical_seed_json(value),',' ORDER BY key COLLATE "C"),'') || '}'
   INTO result FROM jsonb_each(v);
 ELSE RAISE EXCEPTION 'JSON numbers and booleans are not financial input' USING ERRCODE='23514';
 END CASE;
 RETURN result;
END $$;
CREATE FUNCTION lv.parse_line_amount(value text, exponent smallint) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE parsed numeric;
BEGIN
 IF value IS NULL OR exponent IS NULL OR exponent NOT BETWEEN 0 AND 4 OR
    value !~ '^[0-9]{1,16}(\.[0-9]{1,4})?$' OR
    (position('.' IN value)>0 AND length(split_part(value,'.',2))>exponent) THEN
  RAISE EXCEPTION 'invalid exact money input' USING ERRCODE='23514';
 END IF;
 parsed := value::numeric;
 IF parsed <= 0 THEN RAISE EXCEPTION 'line amount must be positive' USING ERRCODE='23514'; END IF;
 RETURN parsed;
END $$;
CREATE TYPE lv.principal_purpose AS ENUM ('DEVELOPER_SEED');
CREATE TYPE lv.commit_kind AS ENUM ('SEED');
CREATE TYPE lv.entry_kind AS ENUM ('MANUAL_ADJUSTMENT');
CREATE TYPE lv.document_type AS ENUM ('MANUAL_JOURNAL');
CREATE TYPE lv.provenance AS ENUM ('SYNTHETIC');
CREATE TABLE lv.service_principal (
 tenant_id uuid NOT NULL REFERENCES lv.tenant(tenant_id), id uuid NOT NULL DEFAULT uuidv7(),
 name varchar(120) NOT NULL, purpose lv.principal_purpose NOT NULL,
 database_role name NOT NULL, active boolean NOT NULL DEFAULT true,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,database_role)
);
CREATE TABLE lv.ledger_counter (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL,
 last_seq bigint NOT NULL DEFAULT 0 CHECK(last_seq>=0),
 last_entry_no bigint NOT NULL DEFAULT 0 CHECK(last_entry_no>=0),
 batch_head char(64) NOT NULL DEFAULT repeat('0',64),
 event_head char(64) NOT NULL DEFAULT repeat('0',64),
 PRIMARY KEY(tenant_id,ledger_id),
 FOREIGN KEY(tenant_id,ledger_id) REFERENCES lv.ledger(tenant_id,id)
);
CREATE TABLE lv.commit_batch (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, seq bigint NOT NULL CHECK(seq>0),
 kind lv.commit_kind NOT NULL, created_by uuid NOT NULL,
 previous_seq bigint, previous_hash char(64) NOT NULL,
 canonical_content text NOT NULL, hash char(64) NOT NULL,
 created_xid text NOT NULL DEFAULT pg_current_xact_id()::text,
 committed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,ledger_id,seq), UNIQUE(tenant_id,ledger_id,seq,hash),
 FOREIGN KEY(tenant_id,ledger_id) REFERENCES lv.ledger(tenant_id,id),
 FOREIGN KEY(tenant_id,created_by) REFERENCES lv.service_principal(tenant_id,id),
 FOREIGN KEY(tenant_id,ledger_id,previous_seq,previous_hash) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq,hash),
 CHECK((seq=1 AND previous_seq IS NULL AND previous_hash=repeat('0',64)) OR (seq>1 AND previous_seq IS NOT NULL AND previous_seq=seq-1)),
 CHECK(hash=encode(sha256(convert_to(canonical_content || previous_hash,'UTF8')),'hex'))
);
CREATE TABLE lv.document (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 doc_type lv.document_type NOT NULL, source_ref varchar(128) NOT NULL,
 seq bigint NOT NULL,
 PRIMARY KEY(tenant_id,ledger_id,id), UNIQUE(tenant_id,ledger_id,doc_type,source_ref),
 FOREIGN KEY(tenant_id,ledger_id,seq) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq)
);
CREATE TABLE lv.document_version (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, document_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>0), id uuid NOT NULL DEFAULT uuidv7(),
 previous_version integer, seq bigint NOT NULL, ordinal integer NOT NULL DEFAULT 1 CHECK(ordinal>0),
 financial_fields jsonb NOT NULL, financial_fingerprint char(64) NOT NULL,
 informational_fields jsonb NOT NULL, original_request jsonb NOT NULL,
 created_by uuid NOT NULL, reason varchar(512) NOT NULL,
 origin lv.commit_kind NOT NULL, provenance lv.provenance NOT NULL,
 PRIMARY KEY(tenant_id,ledger_id,document_id,version), UNIQUE(tenant_id,ledger_id,id),
 UNIQUE(tenant_id,ledger_id,document_id,version,seq),
 FOREIGN KEY(tenant_id,ledger_id,document_id) REFERENCES lv.document(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,seq) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq),
 FOREIGN KEY(tenant_id,created_by) REFERENCES lv.service_principal(tenant_id,id),
 FOREIGN KEY(tenant_id,ledger_id,document_id,previous_version) REFERENCES lv.document_version(tenant_id,ledger_id,document_id,version),
 CHECK((version=1 AND previous_version IS NULL) OR (version>1 AND previous_version IS NOT NULL AND previous_version=version-1)),
 CHECK(financial_fingerprint=encode(sha256(convert_to(lv.canonical_seed_json(financial_fields),'UTF8')),'hex'))
);
CREATE TABLE lv.document_current (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, document_id uuid NOT NULL, version integer NOT NULL,
 PRIMARY KEY(tenant_id,ledger_id,document_id),
 FOREIGN KEY(tenant_id,ledger_id,document_id,version) REFERENCES lv.document_version(tenant_id,ledger_id,document_id,version)
);
CREATE TABLE lv.journal_entry (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 entry_no bigint NOT NULL CHECK(entry_no>0), seq bigint NOT NULL, kind lv.entry_kind NOT NULL,
 effective_date date NOT NULL, period_id uuid NOT NULL,
 currency char(3) NOT NULL, currency_exponent smallint NOT NULL,
 source_document_id uuid NOT NULL, source_document_version integer NOT NULL,
 posting_rule_id varchar(80) NOT NULL, posting_rule_version varchar(32) NOT NULL,
 provenance lv.provenance NOT NULL, created_by uuid NOT NULL,
 description varchar(512) NOT NULL, committed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,ledger_id,id), UNIQUE(tenant_id,ledger_id,entry_no),
 UNIQUE(tenant_id,ledger_id,id,seq,currency,currency_exponent),
 FOREIGN KEY(tenant_id,ledger_id,currency,currency_exponent) REFERENCES lv.ledger(tenant_id,id,currency,currency_exponent),
 FOREIGN KEY(tenant_id,ledger_id,seq) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq),
 FOREIGN KEY(tenant_id,ledger_id,period_id) REFERENCES lv.fiscal_period(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,source_document_id,source_document_version,seq) REFERENCES lv.document_version(tenant_id,ledger_id,document_id,version,seq),
 FOREIGN KEY(tenant_id,created_by) REFERENCES lv.service_principal(tenant_id,id)
);
CREATE TABLE lv.journal_line (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 entry_id uuid NOT NULL, line_no integer NOT NULL CHECK(line_no>0), seq bigint NOT NULL,
 account_id uuid NOT NULL, requires_party boolean NOT NULL, party_id uuid,
 document_id uuid, side char(1) NOT NULL CHECK(side IN ('D','C')),
 amount numeric(20,4) NOT NULL CHECK(amount>0 AND amount<10000000000000000),
 currency char(3) NOT NULL, currency_exponent smallint NOT NULL,
 signed_amount numeric(20,4) GENERATED ALWAYS AS (CASE WHEN side='D' THEN amount ELSE -amount END) STORED,
 PRIMARY KEY(tenant_id,ledger_id,id), UNIQUE(tenant_id,ledger_id,entry_id,line_no),
 FOREIGN KEY(tenant_id,ledger_id,entry_id,seq,currency,currency_exponent) REFERENCES lv.journal_entry(tenant_id,ledger_id,id,seq,currency,currency_exponent),
 FOREIGN KEY(tenant_id,ledger_id,account_id,requires_party) REFERENCES lv.account(tenant_id,ledger_id,id,requires_party),
 FOREIGN KEY(tenant_id,ledger_id,party_id) REFERENCES lv.party(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,document_id) REFERENCES lv.document(tenant_id,ledger_id,id),
 CHECK(NOT requires_party OR party_id IS NOT NULL), CHECK(amount=round(amount,currency_exponent))
);
CREATE INDEX journal_balance_at_seq ON lv.journal_line(tenant_id,ledger_id,account_id,seq) INCLUDE(side,amount);
CREATE INDEX journal_party_at_seq ON lv.journal_line(tenant_id,ledger_id,party_id,seq) WHERE party_id IS NOT NULL;
CREATE INDEX journal_accounting_date ON lv.journal_entry(tenant_id,ledger_id,effective_date);
CREATE TABLE lv.ledger_event (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, seq bigint NOT NULL,
 previous_seq bigint, previous_hash char(64) NOT NULL, hash char(64) NOT NULL,
 canonical_content text NOT NULL,
 PRIMARY KEY(tenant_id,ledger_id,seq), UNIQUE(tenant_id,ledger_id,seq,hash),
 FOREIGN KEY(tenant_id,ledger_id,seq) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq),
 FOREIGN KEY(tenant_id,ledger_id,previous_seq,previous_hash) REFERENCES lv.ledger_event(tenant_id,ledger_id,seq,hash),
 CHECK((seq=1 AND previous_seq IS NULL AND previous_hash=repeat('0',64)) OR (seq>1 AND previous_seq IS NOT NULL AND previous_seq=seq-1)),
 CHECK(hash=encode(sha256(convert_to(canonical_content || previous_hash,'UTF8')),'hex'))
);
CREATE TABLE lv.account_balance_current (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, account_id uuid NOT NULL, party_id uuid,
 debit_total numeric NOT NULL CHECK(debit_total>=0 AND debit_total<'Infinity'::numeric),
 credit_total numeric NOT NULL CHECK(credit_total>=0 AND credit_total<'Infinity'::numeric), last_seq bigint NOT NULL,
 UNIQUE NULLS NOT DISTINCT(tenant_id,ledger_id,account_id,party_id),
 FOREIGN KEY(tenant_id,ledger_id,account_id) REFERENCES lv.account(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,party_id) REFERENCES lv.party(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,last_seq) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq)
);
CREATE TABLE lv.party_balance_current (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, party_id uuid NOT NULL,
 ar_balance numeric NOT NULL CHECK(ar_balance NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)), last_seq bigint NOT NULL,
 PRIMARY KEY(tenant_id,ledger_id,party_id),
 FOREIGN KEY(tenant_id,ledger_id,party_id) REFERENCES lv.party(tenant_id,ledger_id,id),
 FOREIGN KEY(tenant_id,ledger_id,last_seq) REFERENCES lv.commit_batch(tenant_id,ledger_id,seq)
);
CREATE FUNCTION lv.reject_history_change() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'posted history is immutable' USING ERRCODE='23514'; END $$;
CREATE FUNCTION lv.require_fresh_batch() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM lv.commit_batch WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND seq=NEW.seq AND created_xid=pg_current_xact_id()::text) THEN
  RAISE EXCEPTION 'cannot append to an earlier commit' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION lv.guard_seed_batch() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$ DECLARE l lv.ledger; BEGIN
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=NEW.tenant_id AND id=NEW.ledger_id FOR SHARE;
 IF NOT FOUND OR l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' THEN
  RAISE EXCEPTION 'seed requires a native SANDBOX ledger' USING ERRCODE='23514';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.service_principal WHERE tenant_id=NEW.tenant_id AND id=NEW.created_by AND active AND purpose='DEVELOPER_SEED' AND database_role=session_user) THEN
  RAISE EXCEPTION 'seed actor does not match database principal' USING ERRCODE='42501';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER seed_guard BEFORE INSERT ON lv.commit_batch FOR EACH ROW EXECUTE FUNCTION lv.guard_seed_batch();
CREATE FUNCTION lv.guard_posting_entry() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$ DECLARE p lv.fiscal_period; BEGIN
 SELECT * INTO p FROM lv.fiscal_period WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND id=NEW.period_id FOR SHARE;
 IF NOT FOUND OR p.status<>'OPEN' OR NOT (NEW.effective_date>=p.start_date AND NEW.effective_date<p.end_date) THEN
  RAISE EXCEPTION 'entry date must be in an open period' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER entry_period_guard BEFORE INSERT ON lv.journal_entry FOR EACH ROW EXECUTE FUNCTION lv.guard_posting_entry();
CREATE FUNCTION lv.guard_posting_line() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$ DECLARE a lv.account; BEGIN
 SELECT * INTO a FROM lv.account WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND id=NEW.account_id FOR SHARE;
 IF NOT FOUND OR NOT a.active THEN RAISE EXCEPTION 'posting account unavailable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER line_account_guard BEFORE INSERT ON lv.journal_line FOR EACH ROW EXECUTE FUNCTION lv.guard_posting_line();
CREATE FUNCTION lv.validate_entry_balance() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog AS $$ DECLARE e uuid; n bigint; d numeric; c numeric; BEGIN
 IF TG_TABLE_NAME='journal_entry' THEN e:=NEW.id; ELSE e:=NEW.entry_id; END IF;
 SELECT count(*),coalesce(sum(amount) FILTER(WHERE side='D'),0),coalesce(sum(amount) FILTER(WHERE side='C'),0)
 INTO n,d,c FROM lv.journal_line WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND entry_id=e;
 IF n<2 OR d<=0 OR d<>c THEN RAISE EXCEPTION 'entry must have at least two balanced positive lines' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER entry_balanced AFTER INSERT ON lv.journal_entry DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_entry_balance();
CREATE CONSTRAINT TRIGGER lines_balanced AFTER INSERT ON lv.journal_line DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_entry_balance();
CREATE FUNCTION lv.guard_posted_configuration() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$ BEGIN
 IF TG_TABLE_NAME='ledger' THEN
  IF (NEW.source_kind,NEW.profile_version,NEW.currency,NEW.currency_exponent) IS DISTINCT FROM (OLD.source_kind,OLD.profile_version,OLD.currency,OLD.currency_exponent)
    AND EXISTS(SELECT 1 FROM lv.commit_batch WHERE tenant_id=OLD.tenant_id AND ledger_id=OLD.id) THEN
   RAISE EXCEPTION 'posted ledger interpretation cannot change' USING ERRCODE='23514';
  END IF;
 ELSE
  IF (NEW.type,NEW.is_contra,NEW.requires_party,NEW.currency,NEW.currency_exponent) IS DISTINCT FROM (OLD.type,OLD.is_contra,OLD.requires_party,OLD.currency,OLD.currency_exponent)
    AND EXISTS(SELECT 1 FROM lv.journal_line WHERE tenant_id=OLD.tenant_id AND ledger_id=OLD.ledger_id AND account_id=OLD.id) THEN
   RAISE EXCEPTION 'posted account interpretation cannot change' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER posted_ledger_guard BEFORE UPDATE ON lv.ledger FOR EACH ROW EXECUTE FUNCTION lv.guard_posted_configuration();
CREATE TRIGGER posted_account_guard BEFORE UPDATE ON lv.account FOR EACH ROW EXECUTE FUNCTION lv.guard_posted_configuration();
CREATE FUNCTION lv.seed_manual_journal(p_ledger uuid, p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
<<seed_write>>
DECLARE
 t uuid:=nullif(current_setting('app.tenant_id',true),'')::uuid;
 l lv.ledger; actor uuid; counter lv.ledger_counter; current_period lv.fiscal_period;
 existing lv.document_version; doc_id uuid:=uuidv7(); entry_id uuid:=uuidv7();
 item jsonb; a lv.account; v_party uuid; amount_value numeric; normalized jsonb:='[]';
 financial jsonb; event_content text; batch_content text; bh text; eh text; request_hash text;
 n integer:=0; next_seq bigint; next_entry bigint; effective date;
BEGIN
 IF t IS NULL THEN RAISE EXCEPTION 'tenant context required' USING ERRCODE='42501'; END IF;
 SELECT id INTO actor FROM lv.service_principal WHERE tenant_id=t AND database_role=session_user AND active AND purpose='DEVELOPER_SEED' FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'active developer seed principal required' USING ERRCODE='42501'; END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=t AND id=p_ledger FOR SHARE;
 IF NOT FOUND OR l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' THEN
  RAISE EXCEPTION 'seed requires a native SANDBOX ledger' USING ERRCODE='23514';
 END IF;
 IF p_request IS NULL OR jsonb_typeof(p_request)<>'object' OR octet_length(p_request::text)>262144 THEN
  RAISE EXCEPTION 'invalid seed request' USING ERRCODE='23514';
 END IF;
 IF NOT p_request ?& ARRAY['source_ref','effective_date','description','lines'] OR
    (p_request - ARRAY['source_ref','effective_date','description','lines']) <> '{}'::jsonb OR
    jsonb_typeof(p_request->'source_ref')<>'string' OR
    (p_request->>'source_ref') !~ '^[A-Za-z0-9_.:/-]{1,128}$' OR
    jsonb_typeof(p_request->'effective_date')<>'string' OR
    (p_request->>'effective_date') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' OR
    jsonb_typeof(p_request->'description')<>'string' OR length(p_request->>'description')>512 OR
    jsonb_typeof(p_request->'lines')<>'array' THEN
  RAISE EXCEPTION 'invalid typed seed request' USING ERRCODE='23514';
 END IF;
 IF jsonb_array_length(p_request->'lines') NOT BETWEEN 2 AND 1000 THEN
  RAISE EXCEPTION 'seed requires 2 to 1000 lines' USING ERRCODE='23514';
 END IF;
 effective:=(p_request->>'effective_date')::date;
 -- Serialize even duplicate detection on the same ledger; failed work rolls this back.
 INSERT INTO lv.ledger_counter(tenant_id,ledger_id) VALUES(t,p_ledger) ON CONFLICT DO NOTHING;
 SELECT * INTO counter FROM lv.ledger_counter WHERE tenant_id=t AND ledger_id=p_ledger FOR UPDATE;
 SELECT v.* INTO existing FROM lv.document d JOIN lv.document_version v
 ON (v.tenant_id,v.ledger_id,v.document_id,v.version)=(d.tenant_id,d.ledger_id,d.id,1)
 WHERE d.tenant_id=t AND d.ledger_id=p_ledger AND d.doc_type='MANUAL_JOURNAL' AND d.source_ref=p_request->>'source_ref';
 IF FOUND THEN
  IF existing.original_request IS DISTINCT FROM p_request THEN
   RAISE EXCEPTION 'fixture source reference already has a different body' USING ERRCODE='23505';
  END IF;
  SELECT id INTO entry_id FROM lv.journal_entry WHERE tenant_id=t AND ledger_id=p_ledger AND source_document_id=existing.document_id AND source_document_version=1;
  RETURN jsonb_build_object('seq',existing.seq::text,'document_id',existing.document_id::text,'entry_id',entry_id::text,'replayed',true);
 END IF;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=t AND ledger_id=p_ledger ORDER BY role FOR SHARE;
 IF (SELECT count(*) FROM lv.account_role_binding WHERE tenant_id=t AND ledger_id=p_ledger)<>7 THEN
  RAISE EXCEPTION 'complete SP-1 account-role configuration required' USING ERRCODE='23514';
 END IF;
 SELECT * INTO current_period FROM lv.fiscal_period WHERE tenant_id=t AND ledger_id=p_ledger AND effective>=start_date AND effective<end_date FOR SHARE;
 IF NOT FOUND OR current_period.status<>'OPEN' THEN RAISE EXCEPTION 'open period required' USING ERRCODE='23514'; END IF;
 -- Stable account locking order also protects unbound accounts against deactivation.
 PERFORM 1 FROM lv.account WHERE tenant_id=t AND ledger_id=p_ledger AND id IN
  (SELECT (value->>'account_id')::uuid FROM jsonb_array_elements(p_request->'lines')) ORDER BY id FOR SHARE;
 FOR item IN SELECT value FROM jsonb_array_elements(p_request->'lines') LOOP
  n:=n+1;
  IF jsonb_typeof(item)<>'object' OR NOT item ?& ARRAY['account_id','side','amount'] OR
     (item - ARRAY['account_id','side','amount','party_id'])<>'{}'::jsonb OR
     jsonb_typeof(item->'account_id')<>'string' OR jsonb_typeof(item->'side')<>'string' OR
     (item->>'side') NOT IN ('D','C') OR jsonb_typeof(item->'amount')<>'string' OR
     (item ? 'party_id' AND jsonb_typeof(item->'party_id') NOT IN ('string','null')) THEN
   RAISE EXCEPTION 'invalid typed journal line' USING ERRCODE='23514';
  END IF;
  SELECT * INTO a FROM lv.account WHERE tenant_id=t AND ledger_id=p_ledger AND id=(item->>'account_id')::uuid;
  IF NOT FOUND OR NOT a.active THEN RAISE EXCEPTION 'posting account unavailable' USING ERRCODE='23514'; END IF;
  -- Parse unconstrained numeric only after lexical/scale checks. Cast AFTER validation.
  amount_value:=lv.parse_line_amount(item->>'amount',l.currency_exponent);
  v_party:=(item->>'party_id')::uuid;
  IF a.requires_party AND v_party IS NULL THEN RAISE EXCEPTION 'account requires a party' USING ERRCODE='23514'; END IF;
  IF v_party IS NOT NULL AND NOT EXISTS(SELECT 1 FROM lv.party WHERE tenant_id=t AND ledger_id=p_ledger AND id=v_party) THEN
   RAISE EXCEPTION 'party is outside the ledger' USING ERRCODE='23503';
  END IF;
  normalized:=normalized || jsonb_build_array(jsonb_build_object('line_no',n::text,'account_id',a.id::text,'side',item->>'side','amount',(amount_value::numeric(20,4))::text,'party_id',v_party::text));
 END LOOP;
 financial:=jsonb_build_object('effective_date',effective::text,'currency',l.currency::text,'lines',normalized);
 request_hash:=encode(sha256(convert_to(lv.canonical_seed_json(p_request),'UTF8')),'hex');
 next_seq:=counter.last_seq+1;next_entry:=counter.last_entry_no+1;
 batch_content:=lv.canonical_seed_json(jsonb_build_object('tenant_id',t::text,'ledger_id',p_ledger::text,'seq',next_seq::text,'kind','SEED','created_by',actor::text,'request_hash',request_hash));
 bh:=encode(sha256(convert_to(batch_content || counter.batch_head,'UTF8')),'hex');
 INSERT INTO lv.commit_batch(tenant_id,ledger_id,seq,kind,created_by,previous_seq,previous_hash,canonical_content,hash)
 VALUES(t,p_ledger,next_seq,'SEED',actor,nullif(next_seq-1,0),counter.batch_head,batch_content,bh);
 INSERT INTO lv.document(tenant_id,ledger_id,id,doc_type,source_ref,seq)
 VALUES(t,p_ledger,doc_id,'MANUAL_JOURNAL',p_request->>'source_ref',next_seq);
 INSERT INTO lv.document_version(tenant_id,ledger_id,document_id,version,seq,financial_fields,financial_fingerprint,informational_fields,original_request,created_by,reason,origin,provenance)
 VALUES(t,p_ledger,doc_id,1,next_seq,financial,encode(sha256(convert_to(lv.canonical_seed_json(financial),'UTF8')),'hex'),jsonb_build_object('description',p_request->>'description'),p_request,actor,'Synthetic developer fixture','SEED','SYNTHETIC');
 INSERT INTO lv.document_current VALUES(t,p_ledger,doc_id,1);
 INSERT INTO lv.journal_entry(tenant_id,ledger_id,id,entry_no,seq,kind,effective_date,period_id,currency,currency_exponent,source_document_id,source_document_version,posting_rule_id,posting_rule_version,provenance,created_by,description)
 VALUES(t,p_ledger,entry_id,next_entry,next_seq,'MANUAL_ADJUSTMENT',effective,current_period.id,l.currency,l.currency_exponent,doc_id,1,'seed-manual','1','SYNTHETIC',actor,p_request->>'description');
 FOR item IN SELECT value FROM jsonb_array_elements(normalized) LOOP
  SELECT * INTO a FROM lv.account WHERE tenant_id=t AND ledger_id=p_ledger AND id=(item->>'account_id')::uuid;
  INSERT INTO lv.journal_line(tenant_id,ledger_id,entry_id,line_no,seq,account_id,requires_party,party_id,document_id,side,amount,currency,currency_exponent)
  VALUES(t,p_ledger,entry_id,(item->>'line_no')::integer,next_seq,a.id,a.requires_party,(item->>'party_id')::uuid,doc_id,item->>'side',(item->>'amount')::numeric(20,4),l.currency,l.currency_exponent);
 END LOOP;
 INSERT INTO lv.account_balance_current(tenant_id,ledger_id,account_id,party_id,debit_total,credit_total,last_seq)
 SELECT t,p_ledger,account_id,party_id,coalesce(sum(amount) FILTER(WHERE side='D'),0),coalesce(sum(amount) FILTER(WHERE side='C'),0),next_seq
 FROM lv.journal_line j WHERE j.tenant_id=t AND j.ledger_id=p_ledger AND j.entry_id=seed_write.entry_id GROUP BY account_id,party_id
 ON CONFLICT(tenant_id,ledger_id,account_id,party_id) DO UPDATE SET
 debit_total=lv.account_balance_current.debit_total+EXCLUDED.debit_total,
 credit_total=lv.account_balance_current.credit_total+EXCLUDED.credit_total,last_seq=EXCLUDED.last_seq;
 INSERT INTO lv.party_balance_current(tenant_id,ledger_id,party_id,ar_balance,last_seq)
 SELECT t,p_ledger,j.party_id,sum(j.signed_amount),next_seq FROM lv.journal_line j JOIN lv.account_role_binding b
 ON b.tenant_id=j.tenant_id AND b.ledger_id=j.ledger_id AND b.account_id=j.account_id AND b.role='AR_CONTROL'
 WHERE j.tenant_id=t AND j.ledger_id=p_ledger AND j.entry_id=seed_write.entry_id GROUP BY j.party_id
 ON CONFLICT(tenant_id,ledger_id,party_id) DO UPDATE SET ar_balance=lv.party_balance_current.ar_balance+EXCLUDED.ar_balance,last_seq=EXCLUDED.last_seq;
 event_content:=lv.canonical_seed_json(jsonb_build_object('tenant_id',t::text,'ledger_id',p_ledger::text,'seq',next_seq::text,'kind','SEED','created_by',actor::text,'entry_id',entry_id::text,'entry_no',next_entry::text,'document_id',doc_id::text,'document_version','1','financial_fields',financial,'source_ref',p_request->>'source_ref','description',p_request->>'description','provenance','SYNTHETIC'));
 eh:=encode(sha256(convert_to(event_content || counter.event_head,'UTF8')),'hex');
 INSERT INTO lv.ledger_event VALUES(t,p_ledger,next_seq,nullif(next_seq-1,0),counter.event_head,eh,event_content);
 UPDATE lv.ledger_counter SET last_seq=next_seq,last_entry_no=next_entry,batch_head=bh,event_head=eh WHERE tenant_id=t AND ledger_id=p_ledger;
 RETURN jsonb_build_object('seq',next_seq::text,'document_id',doc_id::text,'entry_id',entry_id::text,'replayed',false);
END $$;
CREATE FUNCTION lv.validate_seed_batch() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog AS $$
DECLARE e lv.journal_entry; v lv.document_version; d lv.document; ev lv.ledger_event;
 actual_lines jsonb; expected_event jsonb; expected_batch jsonb;
BEGIN
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
CREATE CONSTRAINT TRIGGER seed_commit_complete AFTER INSERT ON lv.commit_batch DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_seed_batch();
CREATE FUNCTION lv.guard_posted_binding() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
 IF NEW IS DISTINCT FROM OLD AND EXISTS(SELECT 1 FROM lv.commit_batch WHERE tenant_id=OLD.tenant_id AND ledger_id=OLD.ledger_id) THEN
  RAISE EXCEPTION 'posted role configuration requires a future audited transition' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER posted_binding_guard BEFORE UPDATE ON lv.account_role_binding FOR EACH ROW EXECUTE FUNCTION lv.guard_posted_binding();
