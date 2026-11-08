"""Synthetic processor-statement fixture format, version 1 (pure; no database or network access).

This is a LedgerVigil-defined, clearly synthetic fixture layout. Stage 2A leaves processor file layouts
undefined. It claims no compatibility with any real processor's statement, file or API.

File = UTF-8 JSON object, at most 262,144 bytes, no JSON numbers, no duplicate keys:

    {
      "format": "ledgervigil.synthetic-processor-statement",
      "version": "1",
      "data_classification": "SYNTHETIC",
      "statement": {                       # the accounting kernel's lab statement fields, unchanged
        "tenant": "<tenant uuid>", "ledger": "<ledger uuid>",
        "processor": "processor-1", "payout": "STL-3001", "date": "2026-10-14", "currency": "INR",
        "items": [{"id": "PMT-2001", "kind": "PAYMENT", "amount": "50000.00"}],
        "gross": "50000.00", "fee": "1000.00", "net": "49000.00",
        "memo": "untrusted free text"
      }
    }

Field validation reuses the vendored kernel unchanged: `strict_load`, `parse_statement` and `Money.parse`.
That means exact decimal strings, scale at most the ledger's registered exponent, never floats, and
gross = fee + net. This module adds the envelope and version, the size bound, scope binding to the target
tenant and ledger, and the reconciliation of item totals to gross (2A §19.3: gross = Σ payments − Σ refunds).
The memo is untrusted data: it is kept, but never interpreted.
"""
from dataclasses import dataclass
import hashlib
import json

from ledger_kernel.canonical import digest, strict_load
from ledger_kernel.evidence import parse_statement
from ledger_kernel.money import Currency, DomainError, total

FORMAT = 'ledgervigil.synthetic-processor-statement'
VERSION = '1'
SOURCE_SYSTEM = f'{FORMAT}/{VERSION}'
PARSER = 'ledgervigil.ingestion.statement_format'
PARSER_VERSION = '1'
MAX_FILE_BYTES = 262144
ENVELOPE_KEYS = {'format', 'version', 'data_classification', 'statement'}


@dataclass(frozen=True)
class Rejection:
    code: str
    detail: str = ''


@dataclass(frozen=True)
class ParsedStatement:
    record_content: str            # canonical JSON of the "statement" object (the raw source row)
    record_hash: str
    source_key: str                # processor:payout
    processor: str
    payout: str
    date: str
    currency: str
    exponent: int
    gross: str
    fee: str
    net: str
    items: tuple                   # ((line_no, item_ref, kind, amount_text), ...) in file order
    memo: str
    financial_fingerprint: str


@dataclass(frozen=True)
class ParseOutcome:
    sha256: str
    byte_size: int
    record_content: str | None     # present once a statement object was extracted, even if later rejected
    source_key: str | None
    parsed: ParsedStatement | None
    rejection: Rejection | None


def canonical_record(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _key(obj, sha):
    p, q = obj.get('processor'), obj.get('payout')
    if isinstance(p, str) and isinstance(q, str) and 0 < len(p) <= 100 and 0 < len(q) <= 100:
        return f'{p}:{q}'
    return f'unidentified:{sha[:16]}'


def parse(raw: bytes, *, tenant_id: str, ledger_id: str, currency: Currency,
          declared_format: str, declared_version: str) -> ParseOutcome:
    if not isinstance(raw, bytes):
        raise TypeError('raw file bytes required')
    sha, size = hashlib.sha256(raw).hexdigest(), len(raw)
    reject = lambda code, detail='', content=None, key=None: ParseOutcome(sha, size, content, key, None, Rejection(code, detail))  # noqa: E731
    if size > MAX_FILE_BYTES:
        return reject('FILE_TOO_LARGE', f'{size} bytes > {MAX_FILE_BYTES}')
    if (declared_format, declared_version) != (FORMAT, VERSION):
        return reject('UNSUPPORTED_DECLARED_FORMAT', 'only ' + SOURCE_SYSTEM + ' is supported')
    try:
        envelope = strict_load(raw)
    except DomainError as error:
        return reject(error.code)
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_KEYS:
        return reject('ENVELOPE_FIELDS')
    if (envelope['format'], envelope['version']) != (declared_format, declared_version):
        return reject('FORMAT_VERSION_MISMATCH', 'file envelope differs from the declared format/version')
    if envelope['data_classification'] != 'SYNTHETIC':
        return reject('NOT_SYNTHETIC', 'this milestone ingests synthetic fixtures only')
    statement = envelope['statement']
    if not isinstance(statement, dict):
        return reject('STATEMENT_FIELDS')
    content = canonical_record(statement)
    key = _key(statement, sha)
    rejected = lambda code, detail='': reject(code, detail, content, key)  # noqa: E731
    if len(content.encode()) > MAX_FILE_BYTES:
        return rejected('RECORD_TOO_LARGE')
    if (statement.get('tenant'), statement.get('ledger')) != (tenant_id, ledger_id):
        return rejected('SCOPE_MISMATCH', 'statement tenant/ledger differ from the ingestion target')
    try:
        parsed = parse_statement(content.encode(), currency, provenance='SOURCE')
    except DomainError as error:
        return rejected(error.code)
    payments = [m for _, k, m in parsed.items if k == 'PAYMENT']
    refunds = [m for _, k, m in parsed.items if k == 'REFUND']
    if total(payments, currency) - total(refunds, currency) != parsed.gross:
        return rejected('ITEM_TOTAL_MISMATCH', 'payments minus refunds must equal gross')
    in_file_order = tuple((n, i['id'], i['kind'], i['amount']) for n, i in enumerate(statement['items'], start=1))
    financial = {'processor': parsed.processor, 'payout': parsed.payout, 'date': parsed.date, 'currency': currency.code,
                 'exponent': str(currency.exponent), 'gross': parsed.gross.text(), 'fee': parsed.fee.text(), 'net': parsed.net.text(),
                 'items': [{'id': i, 'kind': k, 'amount': m.text()} for i, k, m in parsed.items]}
    return ParseOutcome(sha, size, content, key, ParsedStatement(
        content, hashlib.sha256(content.encode()).hexdigest(), key, parsed.processor, parsed.payout, parsed.date,
        currency.code, currency.exponent, statement['gross'], statement['fee'], statement['net'], in_file_order,
        statement['memo'], digest(financial)), None)
