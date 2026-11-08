ALTER TABLE lv.source_file ADD COLUMN ingestion_xid xid8;
ALTER TABLE lv.import_batch ADD COLUMN ingestion_xid xid8;
CREATE FUNCTION lv.ingestion_ledger(p_ledger uuid) RETURNS TABLE(currency text,currency_exponent smallint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE t uuid:=nullif(current_setting('app.tenant_id',true),'')::uuid;
BEGIN
 IF session_user<>'lv_ingest' OR t IS NULL THEN RAISE EXCEPTION 'ingestion context required' USING ERRCODE='42501'; END IF;
 RETURN QUERY SELECT l.currency::text,l.currency_exponent FROM lv.ledger l WHERE l.tenant_id=t AND l.id=p_ledger FOR SHARE;
END $$;
REVOKE ALL ON FUNCTION lv.ingestion_ledger(uuid) FROM PUBLIC,lv_app,lv_seed,lv_worker;GRANT EXECUTE ON FUNCTION lv.ingestion_ledger(uuid) TO lv_ingest;
CREATE FUNCTION lv.fence_ingestion() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF NEW.tenant_id IS DISTINCT FROM nullif(current_setting('app.tenant_id',true),'')::uuid THEN RAISE EXCEPTION 'tenant context required' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM lv.ledger WHERE tenant_id=NEW.tenant_id AND id=NEW.ledger_id FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'ingestion ledger unavailable' USING ERRCODE='42501'; END IF;
 IF TG_TABLE_NAME='source_file' THEN NEW.ingestion_xid:=pg_current_xact_id();
 ELSIF TG_TABLE_NAME='import_batch' THEN
 IF NOT EXISTS(SELECT 1 FROM lv.source_file WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND id=NEW.source_file_id AND ingestion_xid=pg_current_xact_id()) THEN RAISE EXCEPTION 'source batch must share its file transaction' USING ERRCODE='23514'; END IF;
 NEW.ingestion_xid:=pg_current_xact_id();
 ELSIF TG_TABLE_NAME IN ('source_record','staged_document') THEN
 IF NOT EXISTS(SELECT 1 FROM lv.import_batch WHERE tenant_id=NEW.tenant_id AND ledger_id=NEW.ledger_id AND id=NEW.import_batch_id AND ingestion_xid=pg_current_xact_id()) THEN RAISE EXCEPTION 'source children cannot append to a committed batch' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='staged_statement_item' THEN
 IF NOT EXISTS(SELECT 1 FROM lv.staged_document d JOIN lv.import_batch b ON(b.tenant_id,b.ledger_id,b.id)=(d.tenant_id,d.ledger_id,d.import_batch_id) WHERE d.tenant_id=NEW.tenant_id AND d.ledger_id=NEW.ledger_id AND d.id=NEW.staged_document_id AND b.ingestion_xid=pg_current_xact_id()) THEN RAISE EXCEPTION 'statement items cannot append to a committed batch' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION lv.fence_ingestion() FROM PUBLIC;
CREATE TRIGGER execution_fence BEFORE INSERT ON lv.source_file FOR EACH ROW EXECUTE FUNCTION lv.fence_ingestion();
CREATE TRIGGER execution_fence BEFORE INSERT ON lv.import_batch FOR EACH ROW EXECUTE FUNCTION lv.fence_ingestion();
CREATE TRIGGER execution_fence BEFORE INSERT ON lv.source_record FOR EACH ROW EXECUTE FUNCTION lv.fence_ingestion();
CREATE TRIGGER execution_fence BEFORE INSERT ON lv.staged_document FOR EACH ROW EXECUTE FUNCTION lv.fence_ingestion();
CREATE TRIGGER execution_fence BEFORE INSERT ON lv.staged_statement_item FOR EACH ROW EXECUTE FUNCTION lv.fence_ingestion();
