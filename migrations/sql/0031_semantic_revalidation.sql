-- User approved RG03: full native recalculation, immutable original review and auditable equivalence.
CREATE TABLE lv.balance_dependency_step(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,account_id uuid NOT NULL,version integer NOT NULL,
 tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),PRIMARY KEY(tenant_id,ledger_id,account_id,version),
 FOREIGN KEY(tenant_id,ledger_id,account_id) REFERENCES lv.account(tenant_id,ledger_id,id)
);
ALTER TABLE lv.balance_dependency_step ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.balance_dependency_step FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.balance_dependency_step USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.balance_dependency_step FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();
REVOKE ALL ON lv.balance_dependency_step FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
CREATE FUNCTION lv.touch_balance_dependency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE a uuid;ids uuid[];t uuid;l uuid;n integer;
BEGIN
 IF TG_OP='INSERT' THEN ids:=ARRAY[NEW.account_id];t:=NEW.tenant_id;l:=NEW.ledger_id;
 ELSIF TG_OP='DELETE' THEN ids:=ARRAY[OLD.account_id];t:=OLD.tenant_id;l:=OLD.ledger_id;
 ELSE ids:=ARRAY[OLD.account_id,NEW.account_id];t:=NEW.tenant_id;l:=NEW.ledger_id;END IF;
 FOR a IN SELECT id FROM lv.account WHERE tenant_id=t AND ledger_id=l AND id=ANY(ids) ORDER BY id FOR UPDATE LOOP
 UPDATE lv.account SET name=name WHERE tenant_id=t AND ledger_id=l AND id=a RETURNING version INTO n;
 INSERT INTO lv.balance_dependency_step(tenant_id,ledger_id,account_id,version) VALUES(t,l,a,n);
 END LOOP;RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION lv.touch_balance_dependency() FROM PUBLIC;
DROP TRIGGER balance_account_version ON lv.account_balance_current;
CREATE TRIGGER balance_account_version AFTER INSERT OR UPDATE OR DELETE ON lv.account_balance_current FOR EACH ROW EXECUTE FUNCTION lv.touch_balance_dependency();

CREATE TABLE lv.semantic_recheck(
 tenant_id uuid NOT NULL,ledger_id uuid NOT NULL,id uuid NOT NULL DEFAULT uuidv7(),proposal_id uuid NOT NULL,revision integer NOT NULL,
 simulation_id uuid NOT NULL,proposal_version integer NOT NULL,actor_id uuid NOT NULL REFERENCES lv_identity.app_user(id),session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 action text NOT NULL CHECK(action IN ('approve','execute','override','revalidate')),request_id text NOT NULL,
 seq bigint NOT NULL CHECK(seq>=0),tx_id xid8 NOT NULL DEFAULT pg_current_xact_id(),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 input_canonical text,outcome_canonical text,input_hash text,result_hash text,equivalent boolean,
 PRIMARY KEY(tenant_id,id),FOREIGN KEY(tenant_id,ledger_id,proposal_id,revision) REFERENCES lv.proposal_revision(tenant_id,ledger_id,proposal_id,revision),
 FOREIGN KEY(tenant_id,ledger_id,simulation_id) REFERENCES lv.simulation_run(tenant_id,ledger_id,id),
 CHECK(input_hash IS NULL OR input_hash=encode(sha256(convert_to(input_canonical,'UTF8')),'hex')),
 CHECK(result_hash IS NULL OR result_hash=encode(sha256(convert_to(outcome_canonical,'UTF8')),'hex'))
);
ALTER TABLE lv.semantic_recheck ENABLE ROW LEVEL SECURITY;ALTER TABLE lv.semantic_recheck FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lv.semantic_recheck USING(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
REVOKE ALL ON lv.semantic_recheck FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
GRANT SELECT ON lv.semantic_recheck TO lv_app;
CREATE FUNCTION lv.guard_semantic_recheck() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.equivalent IS NOT NULL OR NEW.equivalent IS NULL OR
 (to_jsonb(OLD)-ARRAY['input_canonical','outcome_canonical','input_hash','result_hash','equivalent']) IS DISTINCT FROM
 (to_jsonb(NEW)-ARRAY['input_canonical','outcome_canonical','input_hash','result_hash','equivalent']) THEN
 RAISE EXCEPTION 'semantic rechecks are immutable after evaluation' USING ERRCODE='23514';END IF;RETURN NEW;
END $$;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.semantic_recheck FOR EACH ROW EXECUTE FUNCTION lv.guard_semantic_recheck();
REVOKE ALL ON FUNCTION lv.guard_semantic_recheck() FROM PUBLIC;

CREATE FUNCTION lv.semantic_projection(o jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT (o-ARRAY['input_hash','snapshot_seq','account_deltas','verdicts','calculation'])||jsonb_build_object(
 'account_deltas',(SELECT jsonb_agg(x-ARRAY['before','after'] ORDER BY n) FROM jsonb_array_elements(o->'account_deltas') WITH ORDINALITY a(x,n)),
 'verdicts',(SELECT jsonb_agg(CASE WHEN x->>'policy_id'='P-CLR-02' THEN jsonb_set(x,'{facts}',(x->'facts')-'clearing_balance_after') ELSE x END ORDER BY n) FROM jsonb_array_elements(o->'verdicts') WITH ORDINALITY a(x,n)),
 'calculation',jsonb_set(jsonb_set(o->'calculation','{kernel}',(o->'calculation'->'kernel')-ARRAY['input_hash','snapshot_hash','envelope_sha256']),'{evidence_observations}',
 (SELECT jsonb_agg(CASE WHEN x->>'id'='E-DUP-01' THEN
 jsonb_set(jsonb_set(jsonb_set(x,'{reason}',to_jsonb(replace(x->>'reason','snapshot seq '||(o->>'snapshot_seq'),'snapshot seq CAPTURE'))),'{trace}',(x->'trace')-ARRAY['batch_head','snapshot_seq']),'{values,scope}',to_jsonb(replace(x->'values'->>'scope','snapshot seq '||(o->>'snapshot_seq'),'snapshot seq CAPTURE'))) ELSE x END ORDER BY n)
 FROM jsonb_array_elements(o->'calculation'->'evidence_observations') WITH ORDINALITY a(x,n))))
$$;
REVOKE ALL ON FUNCTION lv.semantic_projection(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.semantic_projection(jsonb) TO lv_app;

-- Full original dependency checks still govern which changes can even be recalculated.
CREATE OR REPLACE FUNCTION lv.original_proposal_freshness(p lv.proposal)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE b jsonb;changes jsonb:='[]';v record;actual integer;current_seq bigint;op jsonb;expected jsonb;observed jsonb;ref jsonb;
BEGIN
 SELECT input_canonical::jsonb INTO b FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF b IS NULL OR b->'versions'->>'simulator' IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' OR b->'versions'->>'account_read_set' IS DISTINCT FROM 'lv-account-dependencies/1' THEN
 PERFORM lv.proposal_refusal(422,'CAPABILITY_UNAVAILABLE','freshness requires the complete native dependency profile',NULL,NULL); END IF;
 op:=b->'proposal'->'operations'->0;
 SELECT last_seq INTO current_seq FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id;
 FOR v IN SELECT * FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision ORDER BY entity_type,entity_id LOOP
 actual:=NULL;
 IF v.entity_type='ACCOUNT' THEN SELECT version INTO actual FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=v.entity_id;
 ELSIF v.entity_type='DOCUMENT' THEN SELECT version INTO actual FROM lv.document_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND document_id=v.entity_id;
 ELSIF v.entity_type='PERIOD' THEN
 SELECT version INTO actual FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=v.entity_id;
 IF EXISTS(SELECT 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=v.entity_id AND status='CLOSED') THEN changes:=changes||jsonb_build_array(jsonb_build_object('reason','PERIOD_CLOSED','entity_id',v.entity_id::text)); END IF;
 END IF;
 IF actual IS DISTINCT FROM v.version THEN changes:=changes||jsonb_build_array(jsonb_build_object('reason','DEPENDENCY_VERSION_CHANGED','entity_type',v.entity_type,'entity_id',v.entity_id::text,'pinned_version',v.version,'current_version',actual)); END IF;
 END LOOP;
 SELECT coalesce(jsonb_agg(jsonb_build_object('entity_type',entity_type,'entity_id',entity_id::text,'version',version) ORDER BY entity_type,entity_id),'[]') INTO observed FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 IF observed IS DISTINCT FROM b->'read_set' THEN changes:=changes||'[{"reason":"PINNED_DEPENDENCIES_INCOMPLETE"}]'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_array(role::text,account_id::text) ORDER BY role::text),'[]') INTO observed FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id;
 SELECT coalesce(jsonb_agg(jsonb_build_array(x->>'role',x->>'account_id') ORDER BY x->>'role'),'[]') INTO expected FROM jsonb_array_elements(b->'facts'->'bindings') x;
 IF observed IS DISTINCT FROM expected THEN changes:=changes||'[{"reason":"ROLE_BINDINGS_CHANGED"}]'; END IF;
 IF EXISTS(SELECT 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id AND (profile_version IS DISTINCT FROM b->'ledger'->>'profile_version' OR currency::text IS DISTINCT FROM b->'ledger'->>'currency' OR currency_exponent IS DISTINCT FROM (b->'ledger'->>'currency_exponent')::integer)) THEN changes:=changes||'[{"reason":"LEDGER_PROFILE_CHANGED"}]'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('import_batch_id',ib.id::text,'source_file_sha256',f.sha256,'failure_code',ib.failure_code) ORDER BY ib.id),'[]') INTO observed
 FROM lv.import_batch ib JOIN lv.source_file f ON (f.tenant_id,f.ledger_id,f.id)=(ib.tenant_id,ib.ledger_id,ib.source_file_id)
 WHERE ib.tenant_id=p.tenant_id AND ib.ledger_id=p.ledger_id AND ib.status='REJECTED' AND ib.related_staged_document_id=(op->>'statement_document_id')::uuid;
 IF observed IS DISTINCT FROM b->'facts'->'statement_conflicts' THEN changes:=changes||'[{"reason":"STATEMENT_CONFLICT_RANGE_CHANGED"}]'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('processor_ref',i.processor_ref,'processor_item_id',i.processor_item_id,'document_id',i.document_id::text) ORDER BY i.processor_ref,i.processor_item_id),'[]') INTO observed FROM lv.processor_payment_identity i
 WHERE i.tenant_id=p.tenant_id AND i.ledger_id=p.ledger_id AND i.processor_item_id IN (SELECT value->>1 FROM jsonb_array_elements(b->'facts'->'statement'->'items'));
 IF observed IS DISTINCT FROM b->'facts'->'statement_identities' THEN changes:=changes||'[{"reason":"STATEMENT_PAYMENT_RANGE_CHANGED"}]'; END IF;
 IF EXISTS(SELECT 1 FROM lv.processor_settlement_identity WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND processor_ref=op->>'processor_ref' AND payout_id=op->>'payout_id') AND b->'facts'->'payout_settlement'='null'::jsonb THEN changes:=changes||'[{"reason":"PAYOUT_NOW_USED"}]'; END IF;
 IF EXISTS(SELECT 1 FROM lv.settlement_inclusion_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND item_document_id IN (SELECT (value->>'document_id')::uuid FROM jsonb_array_elements(op->'items'))) AND b->'facts'->'active_inclusions'='[]'::jsonb THEN changes:=changes||'[{"reason":"PAYMENT_NOW_INCLUDED"}]'; END IF;
 FOR ref IN SELECT value FROM jsonb_array_elements(b->'pinned_sources'->'evidence') LOOP
 IF ref->>'kind'='DOCUMENT_VERSION' AND (SELECT version FROM lv.document_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND document_id=(ref->>'document_id')::uuid) IS DISTINCT FROM (ref->>'current_version')::integer THEN changes:=changes||jsonb_build_array(jsonb_build_object('reason','EVIDENCE_CURRENT_VERSION_CHANGED','entity_id',ref->>'document_id')); END IF;
 END LOOP;
 RETURN jsonb_build_object('status',CASE WHEN changes='[]'::jsonb THEN 'FRESH' ELSE 'STALE' END,'changes',changes,'pinned_seq',(b->'snapshot'->>'seq')::bigint,'observed_seq',current_seq,'scope','native RecordSettlement dependencies; point-in-time, not approval');
END $function$;
REVOKE ALL ON FUNCTION lv.original_proposal_freshness(lv.proposal) FROM PUBLIC;
CREATE FUNCTION lv.semantic_eligible(p lv.proposal) RETURNS boolean LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE f jsonb;ch jsonb;lo integer;hi integer;aid uuid;
BEGIN
 f:=lv.original_proposal_freshness(p);
 FOR ch IN SELECT value FROM jsonb_array_elements(f->'changes') LOOP
 IF ch->>'reason' IS DISTINCT FROM 'DEPENDENCY_VERSION_CHANGED' OR ch->>'entity_type' IS DISTINCT FROM 'ACCOUNT' THEN RETURN false;END IF;
 lo:=(ch->>'pinned_version')::integer;hi:=(ch->>'current_version')::integer;aid:=(ch->>'entity_id')::uuid;
 IF lo IS NULL OR hi IS NULL OR hi<=lo OR (SELECT count(*) FROM lv.balance_dependency_step WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND account_id=aid AND version>lo AND version<=hi)<>hi-lo THEN RETURN false;END IF;
 END LOOP;RETURN true;
END $$;
REVOKE ALL ON FUNCTION lv.semantic_eligible(lv.proposal) FROM PUBLIC;

CREATE OR REPLACE FUNCTION lv.proposal_freshness(p lv.proposal) RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE f jsonb;r lv.semantic_recheck;b jsonb;v jsonb;actual integer;
BEGIN
 f:=lv.original_proposal_freshness(p);
 SELECT * INTO r FROM lv.semantic_recheck WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND simulation_id=p.latest_simulation_id AND seq=(f->>'observed_seq')::bigint AND equivalent IS NOT NULL ORDER BY created_at DESC,id DESC LIMIT 1;
 IF r.id IS NULL OR NOT lv.semantic_eligible(p) THEN RETURN f;END IF;
 b:=r.input_canonical::jsonb;
 FOR v IN SELECT value FROM jsonb_array_elements(b->'read_set') LOOP
 actual:=NULL;
 IF v->>'entity_type'='ACCOUNT' THEN SELECT version INTO actual FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=(v->>'entity_id')::uuid;
 ELSIF v->>'entity_type'='PERIOD' THEN SELECT version INTO actual FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=(v->>'entity_id')::uuid;
 ELSIF v->>'entity_type'='DOCUMENT' THEN SELECT version INTO actual FROM lv.document_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND document_id=(v->>'entity_id')::uuid;
 END IF;
 IF actual IS DISTINCT FROM (v->>'version')::integer THEN RETURN f;END IF;
 END LOOP;
 IF r.equivalent THEN RETURN f||jsonb_build_object('status','FRESH','changes','[]'::jsonb,'original_pinned_seq',f->'pinned_seq','pinned_seq',r.seq,'semantic_recheck_id',r.id::text,'scope','native semantic equivalence; original simulation and review unchanged');END IF;
 RETURN f||jsonb_build_object('status','STALE','changes',jsonb_build_array(jsonb_build_object('reason','SEMANTIC_OUTCOME_CHANGED','recheck_id',r.id::text)));
END $$;

CREATE FUNCTION lv.semantic_begin(tok bytea,pid uuid,act text,match text,ikey text,src text,req text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;p lv.proposal;permission text;prefix text;rid uuid;f jsonb;blocker text;
BEGIN
 IF act NOT IN ('approve','execute','override','revalidate') OR ikey IS NULL OR ikey!~'^[!-~]{1,64}$' THEN RETURN NULL;END IF;
 s:=lv.proposal_session(tok,src,req);permission:=CASE WHEN act='execute' THEN 'execute' WHEN act='revalidate' THEN 'propose' ELSE 'review' END;
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,permission) THEN RETURN NULL;END IF;
 prefix:=CASE WHEN act='revalidate' THEN 'lv-idem:'||s.tenant_id::text||':'||s.user_id::text||':POST:/api/v1/proposals/{proposal_id}/revalidate:'||ikey ELSE 'lv-'||act||':'||s.tenant_id::text||':'||s.user_id::text||':'||ikey END;
 IF NOT pg_try_advisory_xact_lock(hashtextextended(prefix,0)) THEN RETURN NULL;END IF;
 PERFORM 1 FROM lv_identity.app_user WHERE id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership WHERE tenant_id=s.tenant_id AND user_id=s.user_id FOR SHARE;
 PERFORM 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv_identity.session WHERE id=s.id FOR UPDATE;
 s:=lv.proposal_session(tok,src,req);
 IF NOT lv.proposal_may(s.tenant_id,s.user_id,permission) THEN RETURN NULL;END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=pid FOR UPDATE;
 IF p.id IS NULL OR match IS DISTINCT FROM '"v'||p.version::text||'"' OR (act='execute' AND p.state<>'APPROVED') OR (act='approve' AND p.state<>'READY_FOR_REVIEW') OR (act='override' AND p.state<>'BLOCKED') OR (act='revalidate' AND (p.created_by<>s.user_id OR p.state NOT IN ('BLOCKED','READY_FOR_REVIEW','APPROVED'))) THEN RETURN NULL;END IF;
 IF act='execute' THEN blocker:=lv.executor_blocker(p,s.user_id);
 ELSIF act IN ('approve','override') THEN blocker:=lv.review_blocker(p,s.user_id);END IF;
 IF blocker IS NOT NULL OR (act='override' AND NOT EXISTS(SELECT 1 FROM lv_identity.membership_role WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND role='CONTROLLER')) THEN RETURN NULL;END IF;
 -- Do not turn an idempotent retry into a new calculation/effect.
 IF EXISTS(SELECT 1 FROM lv.api_idempotency WHERE tenant_id=s.tenant_id AND principal_id=s.user_id AND method='POST' AND route='/api/v1/proposals/{proposal_id}/'||CASE WHEN act='override' THEN 'overrides' ELSE act END AND idempotency_key=ikey AND expires_at>statement_timestamp()) THEN RETURN NULL;END IF;
 PERFORM 1 FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id FOR UPDATE;
 PERFORM 1 FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 PERFORM 1 FROM lv.account_role_binding WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY role FOR SHARE;
 PERFORM 1 FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id ORDER BY id FOR SHARE;
 IF NOT lv.semantic_eligible(p) THEN RETURN NULL;END IF;
 f:=lv.proposal_freshness(p);
 IF f->>'status'='FRESH' AND f->'pinned_seq'=f->'observed_seq' THEN RETURN NULL;END IF;
 INSERT INTO lv.semantic_recheck(tenant_id,ledger_id,proposal_id,revision,simulation_id,proposal_version,actor_id,session_id,action,request_id,seq)
 VALUES(p.tenant_id,p.ledger_id,p.id,p.revision,p.latest_simulation_id,p.version,s.user_id,s.id,act,req,(f->>'observed_seq')::bigint) RETURNING id INTO rid;
 RETURN jsonb_build_object('id',rid::text,'revision',p.revision,'state',p.state::text);
END $$;
REVOKE ALL ON FUNCTION lv.semantic_begin(bytea,uuid,text,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.semantic_begin(bytea,uuid,text,text,text,text,text) TO lv_app;

CREATE FUNCTION lv.semantic_finish(tok bytea,rid uuid,inp text,outp text,src text,req text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE s lv_identity.session;r lv.semantic_recheck;p lv.proposal;original lv.simulation_run;b jsonb:=inp::jsonb;o jsonb:=outp::jsonb;equal boolean;clearing_after numeric;clr jsonb;
BEGIN
 s:=lv.proposal_session(tok,src,req);
 SELECT * INTO r FROM lv.semantic_recheck WHERE tenant_id=s.tenant_id AND id=rid FOR UPDATE;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=s.tenant_id AND id=r.proposal_id;
 SELECT * INTO original FROM lv.simulation_run WHERE tenant_id=s.tenant_id AND id=r.simulation_id;
 IF r.id IS NULL OR r.actor_id<>s.user_id OR r.session_id<>s.id OR r.tx_id<>pg_current_xact_id() OR r.equivalent IS NOT NULL OR r.request_id<>req OR p.version<>r.proposal_version OR p.revision<>r.revision OR p.latest_simulation_id<>r.simulation_id
 OR b->>'tenant_id' IS DISTINCT FROM p.tenant_id::text OR b->'proposal'->>'id' IS DISTINCT FROM p.id::text OR b->'proposal'->>'revision' IS DISTINCT FROM p.revision::text
 OR b->'proposal'->'operations' IS DISTINCT FROM original.input_canonical::jsonb->'proposal'->'operations'
 OR b->'proposal'->'evidence_refs' IS DISTINCT FROM original.input_canonical::jsonb->'proposal'->'evidence_refs'
 OR b->'read_set' IS DISTINCT FROM (SELECT jsonb_agg(jsonb_build_object('entity_type',rs.entity_type,'entity_id',rs.entity_id::text,'version',CASE rs.entity_type WHEN 'ACCOUNT' THEN (SELECT version FROM lv.account WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=rs.entity_id) WHEN 'DOCUMENT' THEN (SELECT version FROM lv.document_current WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND document_id=rs.entity_id) WHEN 'PERIOD' THEN (SELECT version FROM lv.fiscal_period WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=rs.entity_id) END) ORDER BY rs.entity_type,rs.entity_id) FROM lv.proposal_read_set rs WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision)
 OR b->'versions' IS DISTINCT FROM original.input_canonical::jsonb->'versions' OR b->'ledger' IS DISTINCT FROM original.input_canonical::jsonb->'ledger'
 OR b->'pinned_sources' IS DISTINCT FROM original.input_canonical::jsonb->'pinned_sources'
 OR b->'facts'->'bindings' IS DISTINCT FROM original.input_canonical::jsonb->'facts'->'bindings'
 OR (b->'snapshot'->>'seq')::bigint IS DISTINCT FROM r.seq OR r.seq IS DISTINCT FROM (SELECT last_seq FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id)
 OR o->>'input_hash' IS DISTINCT FROM encode(sha256(convert_to(inp,'UTF8')),'hex') OR NOT lv.semantic_eligible(p)
 THEN RAISE EXCEPTION 'semantic refresh is not bound to locked inputs and original review' USING ERRCODE='23514';END IF;
 IF o->>'status'='COMPLETED' THEN
 SELECT coalesce(sum(ab.debit_total-ab.credit_total),0)-(original.input_canonical::jsonb->'proposal'->'operations'->0->'gross'->>'amount')::numeric INTO clearing_after
 FROM lv.account_balance_current ab JOIN lv.account_role_binding rb ON(rb.tenant_id,rb.ledger_id,rb.account_id)=(ab.tenant_id,ab.ledger_id,ab.account_id)
 WHERE ab.tenant_id=p.tenant_id AND ab.ledger_id=p.ledger_id AND rb.role::text='PROCESSOR_CLEARING';
 SELECT value INTO clr FROM jsonb_array_elements(o->'verdicts') WHERE value->>'policy_id'='P-CLR-02';
 IF clr IS NULL OR clr->>'outcome' IS DISTINCT FROM (CASE WHEN clearing_after<0 THEN 'FAIL' ELSE 'PASS' END) OR (clr->'facts'->'clearing_balance_after'->>'amount')::numeric IS DISTINCT FROM clearing_after THEN
 RAISE EXCEPTION 'fresh clearing policy disagrees with authoritative balance' USING ERRCODE='23514';END IF;
 END IF;
 equal:=o->>'status'='COMPLETED' AND lv.semantic_projection(o)=lv.semantic_projection(original.outcome_canonical::jsonb);
 UPDATE lv.semantic_recheck SET input_canonical=inp,outcome_canonical=outp,input_hash=encode(sha256(convert_to(inp,'UTF8')),'hex'),result_hash=encode(sha256(convert_to(outp,'UTF8')),'hex'),equivalent=equal WHERE tenant_id=s.tenant_id AND id=rid;
 PERFORM lv_identity.audit_append('PROPOSAL_SEMANTIC_RECHECKED','ALLOWED',s.user_id,s.tenant_id,s.id,src,req,jsonb_build_object('proposal_id',p.id::text,'revision',p.revision::text,'version',p.version::text,'recheck_id',rid::text,'equivalent',equal::text,'original_input_hash',original.input_hash,'input_hash',encode(sha256(convert_to(inp,'UTF8')),'hex'),'result_hash',encode(sha256(convert_to(outp,'UTF8')),'hex'),'seq',r.seq::text));
 RETURN equal;
END $$;
REVOKE ALL ON FUNCTION lv.semantic_finish(bytea,uuid,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.semantic_finish(bytea,uuid,text,text,text,text) TO lv_app;
CREATE FUNCTION lv.validate_semantic_recheck() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE r lv.semantic_recheck;
BEGIN
 SELECT * INTO r FROM lv.semantic_recheck WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
 IF r.tx_id<>pg_current_xact_id() OR r.equivalent IS NULL OR r.input_canonical IS NULL OR r.outcome_canonical IS NULL
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=r.tenant_id AND user_id=r.actor_id AND session_id=r.session_id AND event_type='PROPOSAL_SEMANTIC_RECHECKED' AND request_id=r.request_id AND detail->>'recheck_id'=r.id::text AND detail->>'input_hash'=r.input_hash AND detail->>'result_hash'=r.result_hash)
 THEN RAISE EXCEPTION 'semantic refresh lacks atomic evaluation and audit' USING ERRCODE='23514';END IF;RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER semantic_complete AFTER INSERT OR UPDATE ON lv.semantic_recheck DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lv.validate_semantic_recheck();
REVOKE ALL ON FUNCTION lv.validate_semantic_recheck() FROM PUBLIC;

CREATE OR REPLACE FUNCTION lv.approval_available(pid uuid, actor uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p lv.proposal;r lv.simulation_run;l lv.ledger;f jsonb;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid AND id=pid;
 IF p.id IS NULL OR p.state<>'READY_FOR_REVIEW' OR lv.review_blocker(p,actor) IS NOT NULL THEN RETURN false; END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' THEN RETURN false; END IF;
 IF EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN RETURN false; END IF;
 f:=lv.proposal_freshness(p);RETURN ((f->>'status'='FRESH' AND f->'pinned_seq'=f->'observed_seq') OR lv.semantic_eligible(p));
END $function$;

CREATE OR REPLACE FUNCTION lv.execution_available(pid uuid, actor uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p lv.proposal;l lv.ledger;r lv.simulation_run;f jsonb;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid AND id=pid;
 IF p.id IS NULL OR p.state<>'APPROVED' OR lv.executor_blocker(p,actor) IS NOT NULL THEN RETURN false; END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' THEN RETURN false; END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND kind='APPROVED' AND simulation_id=r.id) OR EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN RETURN false; END IF;
 f:=lv.proposal_freshness(p);RETURN ((f->>'status'='FRESH' AND f->'pinned_seq'=f->'observed_seq') OR lv.semantic_eligible(p));
END $function$;

CREATE OR REPLACE FUNCTION lv.pack_snapshot(t uuid,j uuid,w text,f bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proof_pack;s lv.simulation_run;r lv.proposal_revision;inp jsonb;outcome jsonb;prev text;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO p FROM lv.proof_pack WHERE tenant_id=t AND id=j;
 IF p.state<>'DRAFTING' OR p.fence IS DISTINCT FROM f OR p.lease_owner IS DISTINCT FROM w OR p.lease_expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'lost pack lease' USING ERRCODE='40001';END IF;
 SELECT * INTO STRICT s FROM lv.simulation_run WHERE tenant_id=t AND id=p.simulation_id;
 SELECT * INTO STRICT r FROM lv.proposal_revision WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision;
 inp:=s.input_canonical::jsonb;outcome:=s.outcome_canonical::jsonb;
 SELECT content_hash INTO prev FROM lv.proof_pack WHERE tenant_id=t AND proposal_id=p.proposal_id AND state='ANCHORED' ORDER BY chain_seq DESC LIMIT 1;
 RETURN jsonb_build_object('tenant_id',t::text,'proof_pack_id',j::text,'proposal',jsonb_build_object('id',p.proposal_id::text,'revision',p.revision,'origin',(SELECT origin FROM lv.proposal WHERE tenant_id=t AND id=p.proposal_id)),'ledger',inp->'ledger','snapshot',inp->'snapshot','engine_versions',inp->'versions','operations',r.operations,'read_set',inp->'read_set','evidence',r.evidence_refs,
 'simulation',jsonb_build_object('result_hash',s.result_hash,'input_hash',s.input_hash,'input_canonical',s.input_canonical,'outcome_canonical',s.outcome_canonical,'predicted_postings',outcome->'predicted_postings','account_deltas',outcome->'account_deltas','verdicts',outcome->'verdicts','aggregate',outcome->'aggregate'),
 'ai',NULL,'decisions',(SELECT coalesce(jsonb_agg(v ORDER BY v->>'at',v->>'id'),'[]'::jsonb) FROM (SELECT jsonb_build_object('id',d.id::text,'type',d.kind,'actor_id',d.approved_by::text,'revision',d.revision,'at',to_char(d.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'comment_sha256',CASE WHEN d.comment IS NULL THEN NULL ELSE encode(sha256(convert_to(d.comment,'UTF8')),'hex') END) v FROM lv.proposal_decision d WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision AND resulting_version<=p.proposal_version
 UNION ALL SELECT jsonb_build_object('id',o.id::text,'type','EVIDENCE_EXCEPTION','actor_id',o.overridden_by::text,'revision',o.revision,'policy_id',o.policy_id,'at',to_char(o.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'comment_sha256',encode(sha256(convert_to(o.reason,'UTF8')),'hex')) FROM lv.proposal_override o WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision AND resulting_version<=p.proposal_version) decisions),
 'execution',(SELECT jsonb_build_object('adapter','NATIVE','completion_notification',(SELECT jsonb_build_object('id',id::text,'payload',native_payload) FROM lv.outbox WHERE tenant_id=t AND native_execution_id=e.id AND topic='execution'),'receipt',lv.native_execution_json(e),'check',(SELECT report FROM lv.native_execution_check WHERE tenant_id=t AND id=e.check_id),'batch_hash',(SELECT hash FROM lv.commit_batch WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq),'event_hash',(SELECT hash FROM lv.ledger_event WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq)) FROM lv.native_execution e WHERE tenant_id=t AND proposal_id=p.proposal_id AND revision=p.revision AND resulting_version<=p.proposal_version),
 'accounting_review_status','UNREVIEWED','previous_pack_hash',prev) || CASE WHEN EXISTS(SELECT jsonb_agg(jsonb_build_object('id',q.id::text,'actor_id',q.actor_id::text,'action',q.action,'revision',q.revision,'proposal_version',q.proposal_version,'seq',q.seq::text,'equivalent',q.equivalent,'input_hash',q.input_hash,'result_hash',q.result_hash,'input_canonical',q.input_canonical,'outcome_canonical',q.outcome_canonical) ORDER BY q.created_at,q.id) FROM lv.semantic_recheck q WHERE q.tenant_id=t AND q.proposal_id=p.proposal_id AND q.revision=p.revision AND q.simulation_id=p.simulation_id AND q.proposal_version<=p.proposal_version AND q.created_at<=p.created_at AND q.equivalent IS NOT NULL HAVING count(*)>0) THEN jsonb_build_object('semantic_rechecks',(SELECT jsonb_agg(jsonb_build_object('id',q.id::text,'actor_id',q.actor_id::text,'action',q.action,'revision',q.revision,'proposal_version',q.proposal_version,'seq',q.seq::text,'equivalent',q.equivalent,'input_hash',q.input_hash,'result_hash',q.result_hash,'input_canonical',q.input_canonical,'outcome_canonical',q.outcome_canonical) ORDER BY q.created_at,q.id) FROM lv.semantic_recheck q WHERE q.tenant_id=t AND q.proposal_id=p.proposal_id AND q.revision=p.revision AND q.simulation_id=p.simulation_id AND q.proposal_version<=p.proposal_version AND q.created_at<=p.created_at AND q.equivalent IS NOT NULL)) ELSE '{}'::jsonb END;
END $$;

CREATE OR REPLACE FUNCTION lv.native_export_snapshot(t uuid,j uuid,worker text,f bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.native_export_job;e lv.native_execution;l lv.ledger;BEGIN
 PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.native_export_job WHERE tenant_id=t AND id=j;
 IF w.id IS NULL OR w.state<>'CLAIMED' OR w.fence IS DISTINCT FROM f OR w.lease_owner IS DISTINCT FROM worker OR w.lease_expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'lost export lease' USING ERRCODE='40001';END IF;
 IF NOT lv.proposal_may(t,w.requested_by,'evidence.export') OR NOT EXISTS(SELECT 1 FROM lv_identity.app_user u JOIN lv_identity.membership m ON m.user_id=u.id WHERE u.id=w.requested_by AND u.disabled_at IS NULL AND m.tenant_id=t AND m.status='ACTIVE') THEN RAISE EXCEPTION 'export requester revoked' USING ERRCODE='42501';END IF;
 IF w.proof_pack_id IS NOT NULL THEN RETURN jsonb_build_object('bundle_hex',(SELECT encode(bundle,'hex') FROM lv.proof_pack WHERE tenant_id=t AND id=w.proof_pack_id AND state='ANCHORED'));END IF;
 SELECT * INTO STRICT e FROM lv.native_execution WHERE tenant_id=t AND id=w.execution_id AND state='RECONCILED';SELECT * INTO STRICT l FROM lv.ledger WHERE tenant_id=t AND id=e.ledger_id;
 IF (SELECT coalesce(sum(octet_length(financial_fields::text)+octet_length(original_request::text)+octet_length(informational_fields::text)),0) FROM lv.document_version WHERE tenant_id=t AND ledger_id=e.ledger_id AND (seq=e.commit_seq OR (document_id,version) IN (SELECT document_id,previous_version FROM lv.document_version WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq)))>4194304 OR e.commit_seq>2000 OR (SELECT octet_length(input_canonical)+octet_length(outcome_canonical) FROM lv.simulation_run WHERE tenant_id=t AND id=e.simulation_id)>4194304 OR (SELECT coalesce(sum(octet_length(b.canonical_content)+octet_length(h.canonical_content)),0) FROM lv.commit_batch b JOIN lv.ledger_event h USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=t AND b.ledger_id=e.ledger_id AND b.seq<=e.commit_seq)>4194304 THEN RAISE EXCEPTION 'export bound reached' USING ERRCODE='54000';END IF;
 RETURN jsonb_build_object('scope',jsonb_build_object('tenant_id',t::text,'ledger_id',e.ledger_id::text,'proposal_id',e.proposal_id::text,'revision',e.revision,'execution_id',e.id::text,'simulation_id',e.simulation_id::text,'commit_seq',e.commit_seq::text,'currency',btrim(l.currency),'exponent',l.currency_exponent),'artifacts',jsonb_build_object(
'revision.json',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'proposal_id',x.proposal_id,'revision',x.revision,'previous_revision',x.previous_revision,'change',x.change,'authored_by',x.authored_by,'target_adapter',x.target_adapter,'operations',x.operations,'evidence_refs',x.evidence_refs,'rationale',x.rationale,'content_hash',x.content_hash) AS v FROM lv.proposal_revision x WHERE x.tenant_id=t AND x.proposal_id=e.proposal_id AND x.revision=e.revision LIMIT 2) r),
'approval.json',jsonb_build_object('decision',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'kind',x.kind,'simulation_id',x.simulation_id,'check_id',x.check_id,'submitted_by',x.submitted_by,'approved_by',x.approved_by,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'comment',x.comment) AS v FROM lv.proposal_decision x WHERE x.tenant_id=t AND x.id=e.approval_id LIMIT 2) r),'check',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'simulation_id',x.simulation_id,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'checked_by',x.checked_by,'report',x.report,'review_granted_at',to_char(x.review_granted_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'checked_at',to_char(x.checked_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'decision_action',x.decision_action) AS v FROM lv.approval_check x WHERE x.tenant_id=t AND x.id=(SELECT check_id FROM lv.proposal_decision WHERE tenant_id=t AND id=e.approval_id) LIMIT 2) r),'overrides',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'simulation_id',x.simulation_id,'policy_id',x.policy_id,'policy_version',x.policy_version,'reason',x.reason,'overridden_by',x.overridden_by,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'freshness',x.freshness) AS v FROM lv.proposal_override x WHERE x.tenant_id=t AND x.proposal_id=e.proposal_id AND x.revision=e.revision AND x.simulation_id=e.simulation_id ORDER BY x.policy_id LIMIT 2) r)),
'simulation.json',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'status',x.status,'simulator_version',x.simulator_version,'snapshot_seq',x.snapshot_seq::text,'input_canonical',x.input_canonical,'input_hash',x.input_hash,'outcome_canonical',x.outcome_canonical,'result_hash',x.result_hash,'created_by',x.created_by)||CASE WHEN EXISTS(SELECT 1 FROM lv.semantic_recheck q WHERE q.tenant_id=t AND q.proposal_id=e.proposal_id AND q.revision=e.revision AND q.simulation_id=e.simulation_id AND q.equivalent IS NOT NULL AND q.created_at<=e.created_at) THEN jsonb_build_object('semantic_rechecks',(SELECT jsonb_agg(jsonb_build_object('id',q.id::text,'actor_id',q.actor_id::text,'action',q.action,'revision',q.revision,'proposal_id',q.proposal_id::text,'simulation_id',q.simulation_id::text,'seq',q.seq::text,'equivalent',q.equivalent,'input_hash',q.input_hash,'result_hash',q.result_hash,'input_canonical',q.input_canonical,'outcome_canonical',q.outcome_canonical) ORDER BY q.created_at,q.id) FROM lv.semantic_recheck q WHERE q.tenant_id=t AND q.proposal_id=e.proposal_id AND q.revision=e.revision AND q.simulation_id=e.simulation_id AND q.equivalent IS NOT NULL AND q.created_at<=e.created_at)) ELSE '{}'::jsonb END AS v FROM lv.simulation_run x WHERE x.tenant_id=t AND x.id=e.simulation_id LIMIT 2) r),
'execution.json',jsonb_build_object('receipt',lv.native_execution_json(e),'approval_id',e.approval_id::text,'check',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'proposal_id',x.proposal_id,'revision',x.revision,'simulation_id',x.simulation_id,'observed_version',x.observed_version,'resulting_version',x.resulting_version,'checked_by',x.checked_by,'report',x.report) AS v FROM lv.native_execution_check x WHERE x.tenant_id=t AND x.id=e.check_id LIMIT 2) r),'transition',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'proposal_id',x.proposal_id,'revision',x.revision,'version',x.version,'phase',x.phase,'from_state',x.from_state,'to_state',x.to_state,'user_id',x.user_id,'action',x.action) AS v FROM lv.proposal_transition x WHERE x.tenant_id=t AND x.proposal_id=e.proposal_id AND x.revision=e.revision AND x.version=(lv.native_execution_json(e)->>'resulting_version')::integer AND x.action='execute' AND x.phase=0 LIMIT 2) r),'identity',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'revision',x.revision,'version',x.version,'state',x.state,'created_by',x.created_by,'submitted_by',x.submitted_by) AS v FROM lv.proposal x WHERE x.tenant_id=t AND x.id=e.proposal_id LIMIT 2) r),'editors',(SELECT jsonb_agg(v ORDER BY v) FROM (SELECT DISTINCT authored_by::text v FROM lv.proposal_revision WHERE tenant_id=t AND proposal_id=e.proposal_id AND revision<=e.revision) editors)),
'documents.json',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'document_id',x.document_id,'version',x.version,'id',x.id,'previous_version',x.previous_version,'seq',x.seq::text,'ordinal',x.ordinal,'financial_fields',x.financial_fields,'financial_fingerprint',x.financial_fingerprint,'informational_fields',x.informational_fields,'original_request',x.original_request,'created_by',x.created_by,'created_by_user',x.created_by_user,'reason',x.reason,'origin',x.origin,'provenance',x.provenance) AS v FROM lv.document_version x WHERE x.tenant_id=t AND x.ledger_id=e.ledger_id AND (x.seq=e.commit_seq OR (x.document_id,x.version) IN (SELECT document_id,previous_version FROM lv.document_version WHERE tenant_id=t AND ledger_id=e.ledger_id AND seq=e.commit_seq)) ORDER BY x.document_id,x.version LIMIT 2001) r),
'journal.json',jsonb_build_object('entry',(SELECT v FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'entry_no',x.entry_no::text,'seq',x.seq::text,'kind',x.kind,'effective_date',x.effective_date,'period_id',x.period_id,'currency',x.currency,'currency_exponent',x.currency_exponent,'source_document_id',x.source_document_id,'source_document_version',x.source_document_version,'posting_rule_id',x.posting_rule_id,'posting_rule_version',x.posting_rule_version,'provenance',x.provenance,'created_by',x.created_by,'created_by_user',x.created_by_user,'description',x.description) AS v FROM lv.journal_entry x WHERE x.tenant_id=t AND x.id=(lv.native_execution_json(e)->>'entry_id')::uuid LIMIT 2) r),'lines',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'id',x.id,'entry_id',x.entry_id,'line_no',x.line_no,'seq',x.seq::text,'account_id',x.account_id,'requires_party',x.requires_party,'party_id',x.party_id,'document_id',x.document_id,'side',x.side,'amount',x.amount::text,'currency',x.currency,'currency_exponent',x.currency_exponent) AS v FROM lv.journal_line x WHERE x.tenant_id=t AND x.entry_id=(lv.native_execution_json(e)->>'entry_id')::uuid ORDER BY x.line_no LIMIT 4) r)),
'inclusions.json',(SELECT coalesce(jsonb_agg(v),'[]'::jsonb) FROM (SELECT jsonb_build_object('tenant_id',x.tenant_id,'ledger_id',x.ledger_id,'settlement_document_id',x.settlement_document_id,'settlement_version',x.settlement_version,'line_no',x.line_no,'payment_document_id',x.payment_document_id,'payment_version_before',x.payment_version_before,'payment_version_after',x.payment_version_after,'kind',x.kind,'amount',x.amount::text,'seq',x.seq::text) AS v FROM lv.settlement_inclusion x WHERE x.tenant_id=t AND x.settlement_document_id=(lv.native_execution_json(e)->>'settlement_document_id')::uuid AND x.settlement_version=1 ORDER BY x.line_no LIMIT 1001) r),
'history.json',(SELECT jsonb_agg(jsonb_build_object('seq',b.seq::text,'batch_canonical',b.canonical_content,'batch_previous_hash',b.previous_hash,'batch_hash',b.hash,'event_canonical',h.canonical_content,'event_previous_hash',h.previous_hash,'event_hash',h.hash) ORDER BY b.seq) FROM lv.commit_batch b JOIN lv.ledger_event h USING(tenant_id,ledger_id,seq) WHERE b.tenant_id=t AND b.ledger_id=e.ledger_id AND b.seq<=e.commit_seq)));END $$;

CREATE FUNCTION lv.semantic_needed(pid uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;f jsonb;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid AND id=pid;
 IF p.id IS NULL OR p.state NOT IN ('BLOCKED','READY_FOR_REVIEW','APPROVED') OR p.latest_simulation_id IS NULL THEN RETURN false;END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id AND simulator_version='lv-native-recordsettlement/0.3.0') THEN RETURN false;END IF;
 f:=lv.proposal_freshness(p);RETURN NOT (f->>'status'='FRESH' AND f->'pinned_seq'=f->'observed_seq') AND lv.semantic_eligible(p);
END $$;
REVOKE ALL ON FUNCTION lv.semantic_needed(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lv.semantic_needed(uuid) TO lv_app;

CREATE OR REPLACE FUNCTION lv.validate_submission()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p lv.proposal;r lv.simulation_run;ls lv.ledger_snapshot;b jsonb;o jsonb;rs jsonb;vs jsonb;act text;
BEGIN
 IF TG_TABLE_NAME='proposal' THEN SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
 ELSE SELECT * INTO p FROM lv.proposal WHERE tenant_id=NEW.tenant_id AND id=NEW.proposal_id; END IF;
 IF p.state='DRAFT' THEN
 IF p.submitted_by IS NOT NULL OR p.latest_simulation_id IS NOT NULL OR p.snapshot_id IS NOT NULL THEN RAISE EXCEPTION 'draft has submitted artifacts' USING ERRCODE='23514'; END IF;RETURN NULL;
 END IF;
 IF p.state='SUBMITTED' THEN RAISE EXCEPTION 'incomplete submission cannot commit' USING ERRCODE='23514'; END IF;
 IF p.state='EXECUTED' THEN
 IF NOT EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND resulting_version=p.version AND state='RECONCILED' AND tx_id=pg_current_xact_id()) THEN RAISE EXCEPTION 'executed proposal lacks completion' USING ERRCODE='23514'; END IF;RETURN NULL;END IF;
 IF p.state='REJECTED' THEN
 IF NOT EXISTS(SELECT 1 FROM lv.proposal_decision d JOIN lv.approval_check c ON (c.tenant_id,c.id)=(d.tenant_id,d.check_id)
 WHERE d.tenant_id=p.tenant_id AND d.proposal_id=p.id AND d.revision=p.revision AND d.resulting_version=p.version AND d.kind='REJECTED' AND d.tx_id=pg_current_xact_id() AND c.tx_id=pg_current_xact_id() AND c.decision_action='reject' AND c.report->>'status'='FRESH')
 OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'rejection lacks decision or reservation release' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
 IF p.state='STALE' THEN
 IF EXISTS(SELECT 1 FROM lv.native_execution_check WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version AND report->>'status'='STALE' AND tx_id=pg_current_xact_id()) THEN RETURN NULL;END IF;
 IF EXISTS(SELECT 1 FROM lv.approval_check c JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.revision,t.version)=(c.tenant_id,c.proposal_id,c.revision,c.resulting_version)
 WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.report->>'status'='STALE' AND c.tx_id=pg_current_xact_id() AND t.tx_id=pg_current_xact_id() AND t.action=c.decision_action AND t.user_id=c.checked_by AND t.session_id=c.session_id)
 THEN
 IF EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'approval stale left reservations' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
 IF EXISTS(SELECT 1 FROM lv.proposal_freshness_check c JOIN lv.proposal_transition t ON (t.tenant_id,t.proposal_id,t.revision,t.version)=(c.tenant_id,c.proposal_id,c.revision,c.resulting_version)
 WHERE c.tenant_id=p.tenant_id AND c.proposal_id=p.id AND c.resulting_version=p.version AND c.report->>'status'='STALE' AND c.tx_id=pg_current_xact_id() AND t.tx_id=pg_current_xact_id() AND t.action='revalidate' AND t.user_id=p.created_by AND t.session_id=c.session_id)
 THEN
 IF EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'freshness transition left reservations' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
 IF session_user<>'lv_worker' OR NOT EXISTS(SELECT 1 FROM lv.proposal_transition t JOIN lv.work_item w ON (w.tenant_id,w.id)=(t.tenant_id,t.worker_job_id)
 JOIN lv.maintenance_event e ON (e.tenant_id,e.job_id,e.proposal_id,e.proposal_version,e.fencing_token)=(t.tenant_id,t.worker_job_id,t.proposal_id,t.version,t.worker_fencing_token)
 WHERE t.tenant_id=p.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.to_state='STALE' AND t.action='stale' AND t.tx_id=pg_current_xact_id() AND e.tx_id=pg_current_xact_id()
 AND w.state='CLAIMED' AND w.fencing_token=t.worker_fencing_token AND w.lease_expires_at>clock_timestamp())
 OR EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED')
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event WHERE tenant_id=p.tenant_id AND event_type='PROPOSAL_STALED' AND user_id IS NULL AND detail->>'proposal_id'=p.id::text AND detail->>'version'=p.version::text)
 THEN RAISE EXCEPTION 'stale transition lacks its fenced system proof, release or audit' USING ERRCODE='23514'; END IF;
 RETURN NULL;
 END IF;
 act:=CASE WHEN p.state='WITHDRAWN' THEN 'withdraw' ELSE coalesce((SELECT action FROM lv.proposal_transition WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND version=p.version AND phase=0 AND tx_id=pg_current_xact_id()),'submit') END;
 IF NOT EXISTS(SELECT 1 FROM lv.proposal_transition t JOIN lv.api_idempotency i ON i.tenant_id=t.tenant_id AND i.principal_id=t.user_id
 AND i.method='POST' AND i.route='/api/v1/proposals/{proposal_id}/'||t.action AND i.idempotency_key=t.idempotency_key AND i.request_hash=t.request_hash AND i.response_status=200
 WHERE t.tenant_id=p.tenant_id AND t.proposal_id=p.id AND t.version=p.version AND t.to_state=p.state AND t.tx_id=pg_current_xact_id() AND (t.user_id=p.created_by OR (t.action='approve' AND EXISTS(SELECT 1 FROM lv.proposal_decision d WHERE d.tenant_id=p.tenant_id AND d.proposal_id=p.id AND d.resulting_version=p.version AND d.approved_by=t.user_id AND d.session_id=t.session_id AND d.tx_id=pg_current_xact_id())) OR (t.action='overrides' AND EXISTS(SELECT 1 FROM lv.proposal_override ov WHERE ov.tenant_id=p.tenant_id AND ov.proposal_id=p.id AND ov.resulting_version=p.version AND ov.overridden_by=t.user_id AND ov.session_id=t.session_id AND ov.tx_id=pg_current_xact_id()))))
 OR NOT EXISTS(SELECT 1 FROM lv_identity.audit_event a WHERE a.chain_key=p.tenant_id AND a.tenant_id=p.tenant_id AND a.user_id=CASE WHEN act='approve' THEN (SELECT approved_by FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version) WHEN act='overrides' THEN (SELECT overridden_by FROM lv.proposal_override WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND resulting_version=p.version) ELSE p.created_by END
 AND a.event_type=CASE WHEN act='withdraw' THEN 'PROPOSAL_WITHDRAWN' WHEN act='resimulate' THEN 'PROPOSAL_RESIMULATED' WHEN act='overrides' THEN 'PROPOSAL_OVERRIDDEN' WHEN act='approve' THEN 'PROPOSAL_APPROVED' ELSE 'PROPOSAL_SUBMITTED' END AND a.detail->>'proposal_id'=p.id::text AND a.detail->>'version'=p.version::text)
 THEN RAISE EXCEPTION 'workflow transition lacks cache or audit' USING ERRCODE='23514'; END IF;
 IF p.state='WITHDRAWN' THEN
 IF EXISTS(SELECT 1 FROM lv.staged_workflow WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND status='RESERVED') THEN RAISE EXCEPTION 'withdrawal left a reservation' USING ERRCODE='23514'; END IF; RETURN NULL; END IF;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 SELECT * INTO ls FROM lv.ledger_snapshot WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id AND id=p.snapshot_id;
 IF r.id IS NULL OR ls.id IS NULL OR r.proposal_id<>p.id OR r.revision<>p.revision OR ls.proposal_id<>p.id OR ls.revision<>p.revision OR ls.simulation_id<>r.id OR p.submitted_by<>p.created_by THEN RAISE EXCEPTION 'submission artifacts do not match head' USING ERRCODE='23514'; END IF;
 b:=r.input_canonical::jsonb;o:=r.outcome_canonical::jsonb;
 SELECT coalesce(jsonb_agg(jsonb_build_object('entity_type',entity_type,'entity_id',entity_id::text,'version',version) ORDER BY entity_type,entity_id),'[]'::jsonb) INTO rs FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision;
 SELECT coalesce(jsonb_agg(body ORDER BY policy_id),'[]'::jsonb) INTO vs FROM lv.verdict WHERE tenant_id=p.tenant_id AND simulation_id=r.id;
 IF rs IS DISTINCT FROM b->'read_set' OR vs IS DISTINCT FROM o->'verdicts' OR ls.balances_canonical::jsonb IS DISTINCT FROM b->'facts'->'projection'
 OR ls.snapshot_ref IS DISTINCT FROM (((b->'snapshot')-ARRAY['batch_head','event_head'])||jsonb_build_object('seq',r.snapshot_seq)) OR ls.seq<>r.snapshot_seq OR (p.state IN ('READY_FOR_REVIEW','APPROVED')) IS DISTINCT FROM (o->>'aggregate'='CLEAR' OR lv.override_covers(p)) THEN RAISE EXCEPTION 'incomplete pinned submission' USING ERRCODE='23514'; END IF;
 IF (ls.seq IS DISTINCT FROM (SELECT last_seq FROM lv.ledger_counter WHERE tenant_id=p.tenant_id AND ledger_id=p.ledger_id)
 OR EXISTS(
 WITH expected AS (
 SELECT 'ACCOUNT'::text AS typ,a.id,a.version FROM lv.account a JOIN lv.account_role_binding rb ON (rb.tenant_id,rb.ledger_id,rb.account_id)=(a.tenant_id,a.ledger_id,a.id) WHERE a.tenant_id=p.tenant_id AND a.ledger_id=p.ledger_id
 UNION ALL SELECT DISTINCT 'DOCUMENT',dc.document_id,dc.version FROM lv.document_current dc JOIN lv.proposal_reference ref ON (ref.tenant_id,ref.ledger_id,ref.document_id)=(dc.tenant_id,dc.ledger_id,dc.document_id) WHERE ref.tenant_id=p.tenant_id AND ref.proposal_id=p.id AND ref.revision=p.revision AND ref.kind='SETTLEMENT_ITEM_PAYMENT'
 UNION ALL SELECT 'PERIOD',fp.id,fp.version FROM lv.fiscal_period fp WHERE fp.tenant_id=p.tenant_id AND fp.ledger_id=p.ledger_id AND fp.start_date<=(b->'proposal'->'operations'->0->>'payout_date')::date AND (b->'proposal'->'operations'->0->>'payout_date')::date<fp.end_date
 ), actual AS (SELECT entity_type AS typ,entity_id AS id,version FROM lv.proposal_read_set WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision)
 SELECT 1 FROM ((SELECT * FROM expected EXCEPT ALL SELECT * FROM actual) UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)) delta
 )) AND NOT (lv.proposal_freshness(p)->>'status'='FRESH' AND lv.proposal_freshness(p)?'semantic_recheck_id') THEN RAISE EXCEPTION 'submission dependency set changed or is incomplete' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM lv.proposal_reference ref WHERE ref.tenant_id=p.tenant_id AND ref.proposal_id=p.id AND ref.revision=p.revision AND ref.staged_document_id IS NOT NULL
 AND NOT EXISTS(SELECT 1 FROM lv.staged_workflow w WHERE w.tenant_id=p.tenant_id AND w.ledger_id=p.ledger_id AND w.staged_document_id=ref.staged_document_id AND w.proposal_id=p.id AND w.revision=p.revision AND w.status='RESERVED')) THEN RAISE EXCEPTION 'submission lacks its reservations' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $function$;

CREATE OR REPLACE FUNCTION lv.native_export_finish(t uuid,j uuid,worker text,f bigint,data bytea,error text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE w lv.native_export_job;b jsonb;BEGIN PERFORM lv.maintenance_context(t);SELECT * INTO w FROM lv.native_export_job WHERE tenant_id=t AND id=j FOR UPDATE;
 IF NOT FOUND OR w.state<>'CLAIMED' OR w.fence IS DISTINCT FROM f OR w.lease_owner IS DISTINCT FROM worker OR w.lease_expires_at<=clock_timestamp() THEN RETURN false;END IF;
 IF error IS NOT NULL THEN
  IF data IS NOT NULL THEN RAISE EXCEPTION 'conflicting completion' USING ERRCODE='22023';END IF;
  IF error NOT IN ('RETRYABLE','TERMINAL','REVOKED') THEN RAISE EXCEPTION 'invalid error' USING ERRCODE='22023';END IF;
  UPDATE lv.native_export_job SET state=CASE WHEN error='REVOKED' THEN 'DENIED' WHEN attempts>=3 OR error='TERMINAL' THEN 'DEAD' ELSE 'QUEUED' END,fence=fence+1,lease_owner=NULL,lease_expires_at=NULL,dispatched_at=NULL,available_at=clock_timestamp()+make_interval(secs=>least(60,power(2,attempts)::integer)),last_error=error WHERE tenant_id=t AND id=j RETURNING * INTO w;
  IF w.state IN ('DEAD','DENIED') THEN PERFORM lv_identity.audit_append('NATIVE_EXPORT_FAILED','DENIED',w.requested_by,t,NULL,'export-worker',j::text,jsonb_build_object('job_id',j::text,'error_class',error));END IF;RETURN true;
 END IF;
 IF data IS NULL OR octet_length(data) NOT BETWEEN 1 AND 16777216 THEN RAISE EXCEPTION 'invalid export size' USING ERRCODE='23514';END IF;
 IF w.proof_pack_id IS NOT NULL THEN
  IF data IS DISTINCT FROM (SELECT bundle FROM lv.proof_pack WHERE tenant_id=t AND id=w.proof_pack_id AND state='ANCHORED') THEN RAISE EXCEPTION 'export differs from sealed bundle' USING ERRCODE='23514';END IF;
 ELSE
 b:=convert_from(data,'UTF8')::jsonb;
 IF (b->>'format') IS DISTINCT FROM (CASE WHEN b->'artifacts'->'simulation.json'?'semantic_rechecks' THEN 'ledgervigil.native-execution-proof/2' ELSE 'ledgervigil.native-execution-proof/1' END) OR (b->'scope'->>'tenant_id') IS DISTINCT FROM t::text OR (b->'scope'->>'execution_id') IS DISTINCT FROM w.execution_id::text THEN RAISE EXCEPTION 'invalid export scope' USING ERRCODE='23514';END IF;
 END IF;
 UPDATE lv.native_export_job SET state='DONE',artifact=data,content_hash=encode(sha256(data),'hex'),lease_expires_at=NULL,finished_at=clock_timestamp() WHERE tenant_id=t AND id=j;
 PERFORM lv_identity.audit_append('NATIVE_EXPORT_BUILT','ALLOWED',w.requested_by,t,NULL,'export-worker',j::text,jsonb_build_object('job_id',j::text,'execution_id',w.execution_id::text,'content_hash',encode(sha256(data),'hex'),'bytes',octet_length(data)::text));RETURN true;
END $$;
