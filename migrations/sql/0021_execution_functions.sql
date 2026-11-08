CREATE FUNCTION lv.executor_blocker(p lv.proposal,actor uuid) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE latest timestamptz;
BEGIN
 IF NOT lv.proposal_may(p.tenant_id,actor,'execute') THEN RETURN 'NO_EXECUTE_PERMISSION'; END IF;
 IF actor=p.created_by OR actor=p.submitted_by THEN RETURN 'MAKER'; END IF;
 IF EXISTS(SELECT 1 FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=actor AND rp.permission='execute' AND mr.granted_by=actor) THEN RETURN 'SELF_GRANT'; END IF;
 SELECT max(mr.granted_at) INTO latest FROM lv_identity.membership_role mr JOIN lv_identity.role_permission rp ON rp.role=mr.role WHERE mr.tenant_id=p.tenant_id AND mr.user_id=actor AND rp.permission='execute';
 IF latest IS NULL OR latest>statement_timestamp()-interval '24 hours' THEN RETURN 'EXECUTE_COOLING_OFF'; END IF;RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION lv.executor_blocker(lv.proposal,uuid) FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
CREATE FUNCTION lv.native_execution_json(e lv.native_execution) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('id',e.id::text,'proposal_id',e.proposal_id::text,'ledger_id',e.ledger_id::text,'revision',e.revision,'simulation_id',e.simulation_id::text,'state',e.state,'proposal_state','EXECUTED','executed_by',e.executed_by::text,'observed_version',e.observed_version,'resulting_version',e.resulting_version,'commit_seq',e.commit_seq::text,'settlement_document_id',e.settlement_document_id::text,'entry_id',e.entry_id::text,'comparison',e.comparison,'accounting_status','UNREVIEWED','data_label','SANDBOX — not accounting records')
$$;
REVOKE ALL ON FUNCTION lv.native_execution_json(lv.native_execution) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.native_execution_json(lv.native_execution) TO lv_app;
CREATE FUNCTION lv.execution_posting_proof(e lv.native_execution) RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;r lv.simulation_run;l lv.ledger;f jsonb;expected jsonb;derived jsonb;scope jsonb;rule text;rule_version text;effective text;operation jsonb;wanted_request jsonb;actual_request jsonb;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=e.tenant_id AND id=e.proposal_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=e.tenant_id AND id=e.simulation_id;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=e.tenant_id AND id=e.ledger_id;
 SELECT operations->0 INTO operation FROM lv.proposal_revision WHERE tenant_id=e.tenant_id AND proposal_id=e.proposal_id AND revision=e.revision;
 SELECT original_request INTO actual_request FROM lv.document_version WHERE tenant_id=e.tenant_id AND ledger_id=e.ledger_id AND document_id=e.settlement_document_id AND version=1;
 SELECT financial_fields INTO f FROM lv.document_version WHERE tenant_id=e.tenant_id AND ledger_id=e.ledger_id AND document_id=e.settlement_document_id AND version=1;
 SELECT posting_rule_id,posting_rule_version,effective_date::text INTO rule,rule_version,effective FROM lv.journal_entry WHERE tenant_id=e.tenant_id AND ledger_id=e.ledger_id AND id=e.entry_id;
 wanted_request:=jsonb_build_object('processor_ref',operation->>'processor_ref','payout_id',operation->>'payout_id','payout_date',operation->>'payout_date','currency',operation->'gross'->>'currency',
 'gross',operation->'gross'->>'amount','fee',operation->'fee'->>'amount','net',operation->'net'->>'amount','description','Approved native SANDBOX settlement',
 'items',(SELECT jsonb_agg(jsonb_build_object('document_id',x->>'document_id','kind',x->>'kind','amount',x->'amount'->>'amount') ORDER BY n) FROM jsonb_array_elements(operation->'items') WITH ORDINALITY a(x,n)));
 IF actual_request IS DISTINCT FROM wanted_request OR effective IS DISTINCT FROM operation->>'payout_date' THEN RAISE EXCEPTION 'new documents do not implement the approved operation' USING ERRCODE='23514'; END IF;
 IF f IS NULL OR r.simulator_version<>'lv-native-recordsettlement/0.3.0' OR f->>'payout_date' IS DISTINCT FROM effective OR rule IS DISTINCT FROM 'sp1-posting' OR rule_version IS DISTINCT FROM '1.0' THEN RAISE EXCEPTION 'unsupported native posting proof' USING ERRCODE='23514'; END IF;
 scope:=jsonb_build_object('tenant_id',e.tenant_id::text,'ledger_id',e.ledger_id::text,'proposal_id',e.proposal_id::text,'revision',e.revision,'simulation_id',e.simulation_id::text,'currency',l.currency::text,'exponent',l.currency_exponent,'effective_date',effective,'posting_rule',rule,'posting_rule_version',rule_version);
 SELECT jsonb_agg(jsonb_build_object('account_id',v->>'account_id','party_id',v->'party_id','side',v->>'side','amount',v->'amount'->>'amount','currency',v->'amount'->>'currency') ORDER BY v->>'account_id',coalesce(v->>'party_id',''),v->>'side') INTO expected FROM jsonb_array_elements(r.outcome_canonical::jsonb->'predicted_postings') v;
 SELECT jsonb_agg(jsonb_build_object('account_id',b.account_id::text,'party_id',NULL,'side',x.side,'amount',round(x.amount,l.currency_exponent)::text,'currency',l.currency::text) ORDER BY b.account_id::text,x.side) INTO derived
 FROM (VALUES('BANK_OPERATING','D',(f->>'net')::numeric),('PROCESSOR_FEE_EXPENSE','D',(f->>'fee')::numeric),('PROCESSOR_CLEARING','C',(f->>'gross')::numeric)) x(role,side,amount)
 JOIN lv.account_role_binding b ON b.tenant_id=e.tenant_id AND b.ledger_id=e.ledger_id AND b.role::text=x.role WHERE x.amount>0;
 RETURN jsonb_build_object('scope',scope,'predicted',expected,'derived',derived);
END $$;
REVOKE ALL ON FUNCTION lv.execution_posting_proof(lv.native_execution) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.execution_posting_proof(lv.native_execution) TO lv_app;
CREATE FUNCTION lv.native_posting_match(e lv.native_execution) RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE proof jsonb;predicted jsonb;derived jsonb;ph text;dh text;
BEGIN
 proof:=lv.execution_posting_proof(e);
 SELECT jsonb_agg(jsonb_build_array(v->>'account_id',v->'party_id',v->>'side',v->>'amount',v->>'currency') ORDER BY v->>'account_id',coalesce(v->>'party_id',''),v->>'side') INTO predicted FROM jsonb_array_elements(proof->'predicted') v;
 SELECT jsonb_agg(jsonb_build_array(v->>'account_id',v->'party_id',v->>'side',v->>'amount',v->>'currency') ORDER BY v->>'account_id',coalesce(v->>'party_id',''),v->>'side') INTO derived FROM jsonb_array_elements(proof->'derived') v;
 IF predicted IS NULL OR derived IS NULL OR predicted IS DISTINCT FROM derived THEN RAISE EXCEPTION 'new-version postings differ from approved simulation' USING ERRCODE='23514'; END IF;
 ph:=encode(sha256(convert_to(lv.proposal_canonical_json(jsonb_build_object('format','lv-native-posting-match/1','scope',proof->'scope','lines',predicted)),'UTF8')),'hex');
 dh:=encode(sha256(convert_to(lv.proposal_canonical_json(jsonb_build_object('format','lv-native-posting-match/1','scope',proof->'scope','lines',derived)),'UTF8')),'hex');
 RETURN jsonb_build_object('format','lv-native-posting-match/1','comparison','MATCHED','predicted_hash',ph,'derived_hash',dh,'line_count',jsonb_array_length(predicted),'accounting_status','UNREVIEWED','authorizes_execution',false);
END $$;
REVOKE ALL ON FUNCTION lv.native_posting_match(lv.native_execution) FROM PUBLIC,lv_app,lv_seed,lv_ingest,lv_worker;
CREATE FUNCTION lv.guard_execution_batch() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF NEW.created_by IS NOT NULL OR NOT EXISTS(SELECT 1 FROM lv.native_execution e JOIN lv.native_execution_check c ON(c.tenant_id,c.id)=(e.tenant_id,e.check_id)
 WHERE e.tenant_id=NEW.tenant_id AND e.ledger_id=NEW.ledger_id AND e.executed_by=NEW.created_by_user AND e.state='BUILDING' AND e.tx_id=pg_current_xact_id() AND c.tx_id=pg_current_xact_id() AND c.report->>'status'='FRESH' AND (c.report->>'observed_seq')::bigint+1=NEW.seq)
 THEN RAISE EXCEPTION 'execution batch lacks authorized same-transaction proof' USING ERRCODE='23514'; END IF;RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION lv.guard_execution_batch() FROM PUBLIC;
CREATE TRIGGER execution_guard BEFORE INSERT ON lv.commit_batch FOR EACH ROW WHEN(NEW.kind::text='EXECUTION') EXECUTE FUNCTION lv.guard_execution_batch();
CREATE FUNCTION lv.execution_available(pid uuid,actor uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p lv.proposal;l lv.ledger;r lv.simulation_run;f jsonb;
BEGIN
 SELECT * INTO p FROM lv.proposal WHERE tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid AND id=pid;
 IF p.id IS NULL OR p.state<>'APPROVED' OR lv.executor_blocker(p,actor) IS NOT NULL THEN RETURN false; END IF;
 SELECT * INTO l FROM lv.ledger WHERE tenant_id=p.tenant_id AND id=p.ledger_id;
 SELECT * INTO r FROM lv.simulation_run WHERE tenant_id=p.tenant_id AND id=p.latest_simulation_id;
 IF l.purpose<>'SANDBOX' OR l.source_kind<>'NATIVE' OR r.simulator_version IS DISTINCT FROM 'lv-native-recordsettlement/0.3.0' THEN RETURN false; END IF;
 IF NOT EXISTS(SELECT 1 FROM lv.proposal_decision WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision AND kind='APPROVED' AND simulation_id=r.id) OR EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN RETURN false; END IF;
 f:=lv.proposal_freshness(p);RETURN f->>'status'='FRESH' AND f->'pinned_seq'=f->'observed_seq';
END $$;
REVOKE ALL ON FUNCTION lv.execution_available(uuid,uuid) FROM PUBLIC;GRANT EXECUTE ON FUNCTION lv.execution_available(uuid,uuid) TO lv_app;
