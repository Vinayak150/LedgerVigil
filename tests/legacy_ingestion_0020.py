"""Frozen pre-0021 ingestion fixture; used only while migration tests run old schema versions."""
"""Record an ingestion attempt (Stage 2A §19.2 path A) through the restricted `lv_ingest` login.

The login can only INSERT into source_file, import_batch, source_record, staged_document and
staged_statement_item. It has no privilege on documents, versions, journal entries, projections, the ledger
counter or commit history, and no EXECUTE on any function. Outcomes are deterministic within a tenant/ledger:

* the same bytes again                -> REPEATED_FILE: the existing batch is returned and nothing new is written;
* same statement content, other bytes -> a DUPLICATE batch that points at the existing staged statement;
* same payout identity, other content -> a REJECTED batch, PAYOUT_IDENTITY_CONFLICT, pointing at the existing one;
* any validation failure              -> a REJECTED batch with a specific failure code; the raw bytes are kept.
"""
from uuid import UUID

import psycopg

from ledger_kernel.money import Currency
from ledgervigil.db.tenant import tenant_transaction

from ledgervigil.ingestion import statement_format as fmt


class IngestionRefused(PermissionError):
    """The attempt could not be scoped (wrong login, unknown ledger); nothing was recorded."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _result(row, repeated=False):
    keys = ('source_file_id', 'sha256', 'byte_size', 'import_batch_id', 'status', 'failure_code', 'failure_detail',
            'related_staged_document_id', 'staged_document_id', 'source_record_id')
    out = {k: (None if v is None else str(v)) for k, v in zip(keys, row)}
    out['repeated_file'] = repeated
    out['data_classification'] = 'SYNTHETIC'
    return out


SUMMARY = ('SELECT f.id, f.sha256, f.byte_size, b.id, b.status::text, b.failure_code, b.failure_detail, b.related_staged_document_id, '
           's.id, r.id FROM lv.source_file f JOIN lv.import_batch b ON (b.tenant_id, b.ledger_id, b.source_file_id) = (f.tenant_id, f.ledger_id, f.id) '
           'LEFT JOIN lv.source_record r ON (r.tenant_id, r.ledger_id, r.import_batch_id) = (b.tenant_id, b.ledger_id, b.id) '
           'LEFT JOIN lv.staged_document s ON (s.tenant_id, s.ledger_id, s.import_batch_id) = (b.tenant_id, b.ledger_id, b.id) '
           'WHERE f.tenant_id = %s AND f.ledger_id = %s AND f.sha256 = %s')


def ingest_statement(connection, tenant_id: UUID, ledger_id: UUID, raw: bytes, *,
                     declared_format: str, declared_version: str) -> dict:
    if not isinstance(ledger_id, UUID):
        raise TypeError('ledger_id must be a UUID')
    for attempt in range(2):          # one retry if a concurrent identical/conflicting ingestion wins the race
        try:
            with tenant_transaction(connection, tenant_id):
                return _ingest(connection, tenant_id, ledger_id, raw, declared_format, declared_version)
        except psycopg.errors.UniqueViolation:
            if attempt:
                raise
    raise AssertionError('unreachable')


def _ingest(c, tenant_id, ledger_id, raw, declared_format, declared_version):
    role = c.execute('SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user').fetchone()
    if role != ('lv_ingest', False, False):
        raise IngestionRefused('WRONG_DATABASE_ROLE')
    ledger = c.execute('SELECT currency, currency_exponent FROM lv.ledger WHERE tenant_id = %s AND id = %s',
                       (tenant_id, ledger_id)).fetchone()
    if ledger is None:
        raise IngestionRefused('LEDGER_NOT_FOUND')
    outcome = fmt.parse(raw, tenant_id=str(tenant_id), ledger_id=str(ledger_id), currency=Currency(ledger[0].strip(), ledger[1]),
                        declared_format=declared_format, declared_version=declared_version)
    existing = c.execute(SUMMARY, (tenant_id, ledger_id, outcome.sha256)).fetchone()
    if existing:
        return _result(existing, repeated=True)

    status, code, detail, related, parsed = 'STAGED', None, None, None, outcome.parsed
    if outcome.rejection:
        status, code, detail = 'REJECTED', outcome.rejection.code, outcome.rejection.detail or None
    else:
        same_record = c.execute('SELECT s.id FROM lv.source_record r JOIN lv.staged_document s ON (s.tenant_id, s.ledger_id, s.source_record_id) = '
                                '(r.tenant_id, r.ledger_id, r.id) WHERE r.tenant_id = %s AND r.ledger_id = %s AND r.source_system = %s '
                                'AND r.source_key = %s AND r.content_hash = %s', (tenant_id, ledger_id, fmt.SOURCE_SYSTEM, parsed.source_key, parsed.record_hash)).fetchone()
        prior = c.execute('SELECT id, financial_fingerprint FROM lv.staged_document WHERE tenant_id = %s AND ledger_id = %s '
                          'AND processor_ref = %s AND payout_id = %s', (tenant_id, ledger_id, parsed.processor, parsed.payout)).fetchone()
        if same_record or (prior and prior[1] == parsed.financial_fingerprint):
            status, code, related = 'DUPLICATE', 'DUPLICATE_STATEMENT', (same_record or prior)[0]
        elif prior:
            status, code, related = 'REJECTED', 'PAYOUT_IDENTITY_CONFLICT', prior[0]
            detail = 'a different staged statement already holds this processor/payout identity'

    file_id = c.execute('INSERT INTO lv.source_file(tenant_id, ledger_id, sha256, byte_size, content, media_type, declared_format, '
                        "declared_version, data_classification) VALUES (%s, %s, %s, %s, %s, 'application/json', %s, %s, 'SYNTHETIC') RETURNING id",
                        (tenant_id, ledger_id, outcome.sha256, outcome.byte_size, raw if outcome.byte_size <= fmt.MAX_FILE_BYTES else None,
                         declared_format[:80], declared_version[:16])).fetchone()[0]
    batch_id = c.execute('INSERT INTO lv.import_batch(tenant_id, ledger_id, source_file_id, parser, parser_version, status, failure_code, '
                         'failure_detail, related_staged_document_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                         (tenant_id, ledger_id, file_id, fmt.PARSER, fmt.PARSER_VERSION, status, code, detail, related)).fetchone()[0]
    record_id = None
    if outcome.record_content is not None and status != 'DUPLICATE':
        record_id = c.execute('INSERT INTO lv.source_record(tenant_id, ledger_id, import_batch_id, record_index, source_system, source_key, '
                              'content, content_hash) VALUES (%s, %s, %s, 0, %s, %s, %s, encode(sha256(convert_to(%s, %s)), %s)) '
                              'ON CONFLICT (tenant_id, ledger_id, source_system, source_key, content_hash) DO NOTHING RETURNING id',
                              (tenant_id, ledger_id, batch_id, fmt.SOURCE_SYSTEM, outcome.source_key, outcome.record_content,
                               outcome.record_content, 'UTF8', 'hex')).fetchone()
        record_id = record_id[0] if record_id else None
    if status == 'STAGED':
        p = parsed
        staged = c.execute('INSERT INTO lv.staged_document(tenant_id, ledger_id, import_batch_id, source_record_id, doc_type, validation_status, '
                           "provenance, processor_ref, payout_id, payout_date, currency, currency_exponent, gross, fee, net, item_count, "
                           "financial_fingerprint, description_untrusted) VALUES (%s, %s, %s, %s, 'SETTLEMENT_STATEMENT', 'VALID', 'SYNTHETIC', "
                           '%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                           (tenant_id, ledger_id, batch_id, record_id, p.processor, p.payout, p.date, p.currency, p.exponent,
                            p.gross, p.fee, p.net, len(p.items), p.financial_fingerprint, p.memo)).fetchone()[0]
        with c.cursor() as cur:
            cur.executemany('INSERT INTO lv.staged_statement_item(tenant_id, ledger_id, staged_document_id, line_no, item_ref, kind, amount, '
                            'currency_exponent) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                            [(tenant_id, ledger_id, staged, n, ref, kind, amount, p.exponent) for n, ref, kind, amount in p.items])
    return _result(c.execute(SUMMARY, (tenant_id, ledger_id, outcome.sha256)).fetchone())
