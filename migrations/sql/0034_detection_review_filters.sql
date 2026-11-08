-- 0034_detection_review_filters: indexes for the bounded, server-side filters of GET /ledgers/{id}/detection-findings
-- (kind, review status, staged statement, detector version; PROPOSED DET-11). No table, function, policy or grant changes.
CREATE INDEX detection_finding_ledger_order ON lv.detection_finding (tenant_id, ledger_id, id);
CREATE INDEX detection_finding_ledger_kind ON lv.detection_finding (tenant_id, ledger_id, kind, id);
CREATE INDEX detection_finding_ledger_statement ON lv.detection_finding (tenant_id, ledger_id, staged_document_id, id);
CREATE INDEX detection_review_status ON lv.detection_review (tenant_id, status, finding_id);
