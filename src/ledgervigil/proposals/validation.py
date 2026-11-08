"""Draft validation, in the order Stage 2A §9.1 prescribes for the checks a DRAFT can make: schema -> profile -> capability.

Pure: no database access. Reference ownership (ledger, payments, statement, evidence) and the SP-1 full-item rule need data and
are resolved by the database function in the same transaction as the write (migration 0010, lv.proposal_resolve).

Reused rather than re-implemented:
* ledger_kernel.money.Money.parse / Currency  - exact decimal strings, scale <= exponent, 16 integer digits, no float;
* ledger_kernel.model.Settlement / token / iso_date - identifier and date rules the kernel will apply at simulation;
* ledgervigil.kernel_adapter.calculation.validate_request - the adapter's own item rules (canonical UUIDs, PAYMENT only, no
  duplicates), applied to the internal request the operation will become at submission.
No "validation registry" exists in the source; these are the existing validators.
"""
import hashlib
import json
import re
from uuid import UUID

from ledger_kernel import money as km
from ledger_kernel.model import Settlement, iso_date, token
from ledgervigil.kernel_adapter.calculation import validate_request
from ledgervigil.kernel_adapter.types import AdapterError

MAX_BODY_BYTES = 1048576          # Stage 2B Part 2 "Limits": request body 1 MiB
MAX_OPERATIONS, MAX_ITEMS_TOTAL, MAX_EVIDENCE, MAX_RATIONALE = 10, 1000, 100, 4000
CATALOG = frozenset({'ApplyPayment', 'ReallocatePayment', 'RecordSettlement', 'RecordProcessorFee', 'IssueCreditNote', 'RecordRefund',
                     'ReverseJournalEntry', 'VoidDocument', 'ProposeAdjustment', 'PostStagedDocuments', 'BootstrapLedger'})
SETTLEMENT_KEYS = frozenset({'type', 'schema_version', 'processor_ref', 'payout_id', 'payout_date', 'items', 'gross', 'fee', 'net',
                             'statement_document_id'})
ITEM_KEYS = frozenset({'document_id', 'kind', 'amount'})
EVIDENCE_KINDS = frozenset({'SOURCE_RECORD', 'DOCUMENT_VERSION', 'STAGED_DOCUMENT', 'ATTACHMENT'})
CREATE_REQUIRED = frozenset({'ledger_id', 'target_adapter', 'operations'})
CREATE_KEYS = CREATE_REQUIRED | {'evidence_refs', 'rationale'}
PATCH_KEYS = frozenset({'operations', 'evidence_refs', 'rationale'})
MONEY_CODES = frozenset({'MONEY_MUST_BE_STRING', 'MONEY_SCALE_EXCEEDED', 'CURRENCY_MISMATCH'})   # Stage 2B Part 4
IDENT = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')
CURRENCY = re.compile(r'[A-Z]{3}\Z')
UNSIGNED = re.compile(r'[0-9]{1,16}(?:\.[0-9]{1,4})?\Z')
BOOTSTRAP_DETAIL = ('BootstrapLedger is not available: its EMPTY-ledger eligibility conflicts with sequence-advancing period-close '
                    'commits (gap RG-4) and needs an owner decision')


class Invalid(Exception):
    def __init__(self, problem: dict):
        super().__init__(problem['code'])
        self.problem = problem


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate key')
    return dict(pairs)


def _bad_constant(name):
    raise ValueError(f'non-finite number {name}')


def parse_body(raw: bytes | None) -> object:
    """Strict JSON: UTF-8, <= 1 MiB, no duplicate keys, no NaN/Infinity."""
    if raw is None or len(raw) > MAX_BODY_BYTES:
        raise Invalid(problem(422, 'SCHEMA_INVALID', 'request body must be a JSON object of at most 1 MiB',
                              [{'pointer': '', 'code': 'size'}]))
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates, parse_constant=_bad_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Invalid(problem(422, 'SCHEMA_INVALID', 'request body is not valid JSON', [{'pointer': '', 'code': 'json'}])) from None


def request_hash(raw: bytes | None, parsed: object = None, parsed_ok: bool = False) -> str:
    """Stage 2B Part 8.1: SHA-256 of the canonical body (or of the raw bytes when the body is not JSON)."""
    if parsed_ok:
        data = json.dumps(parsed, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    else:
        data = raw or b''
    return hashlib.sha256(data).hexdigest()


def problem(status: int, code: str, detail: str, errors: list | None = None, **extra) -> dict:
    p = {'status': status, 'code': code, 'detail': detail}
    if errors is not None:
        p['errors'] = errors
    p.update(extra)
    return p


def _uuid(value) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


class _Check:
    def __init__(self, currency: km.Currency | None):
        self.currency, self.errors, self.capability = currency, [], []

    def err(self, pointer: str, code: str):
        self.errors.append({'pointer': pointer, 'code': code})

    def money(self, ptr: str, m) -> dict | None:
        if type(m) is not dict or set(m) != {'amount', 'currency'}:
            self.err(ptr, 'money_object')
            return None
        amount, currency = m['amount'], m['currency']
        if type(amount) in (int, float):
            self.err(ptr + '/amount', 'MONEY_MUST_BE_STRING')
            return None
        if type(amount) is not str or not UNSIGNED.match(amount):
            self.err(ptr + '/amount', 'pattern')
            return None
        if type(currency) is not str or not CURRENCY.match(currency):
            self.err(ptr + '/currency', 'pattern')
            return None
        if self.currency is None:
            return m
        if currency != self.currency.code:
            self.err(ptr + '/currency', 'CURRENCY_MISMATCH')
            return None
        try:
            return km.Money.parse(amount, self.currency).wire()       # normalised to the exponent (response rule)
        except km.DomainError as e:
            self.err(ptr + '/amount', 'MONEY_SCALE_EXCEEDED' if e.code == 'MONEY_WIRE_SCALE' else 'range')
            return None

    def operation(self, ptr: str, op) -> dict | None:
        if type(op) is not dict:
            self.err(ptr, 'type')
            return None
        kind = op.get('type')
        if type(kind) is not str or kind not in CATALOG:
            self.err(ptr + '/type', 'unknown_operation_type')
            return None
        if kind == 'BootstrapLedger':
            self.capability.append(BOOTSTRAP_DETAIL)
            return None
        if kind != 'RecordSettlement':
            self.capability.append(f'{kind} drafts are not available yet; only RecordSettlement is')
            return None
        before = len(self.errors)
        for k in sorted(SETTLEMENT_KEYS - set(op)):
            self.err(f'{ptr}/{k}', 'required')
        for k in sorted(set(op) - SETTLEMENT_KEYS):
            self.err(f'{ptr}/{k[:64]}', 'unknown_field')
        if len(self.errors) > before:
            return None
        if type(op['schema_version']) is not int or op['schema_version'] != 1:
            self.err(ptr + '/schema_version', 'unsupported_schema_version')
        for k in ('processor_ref', 'payout_id'):
            if type(op[k]) is not str or not IDENT.match(op[k]):
                self.err(f'{ptr}/{k}', 'pattern')
        try:
            iso_date(op['payout_date'])
        except km.DomainError:
            self.err(ptr + '/payout_date', 'date')
        amounts = {k: self.money(f'{ptr}/{k}', op[k]) for k in ('gross', 'fee', 'net')}
        if not _uuid(op['statement_document_id']):
            self.err(ptr + '/statement_document_id', 'uuid')
        items = op['items']
        out_items = []
        if type(items) is not list or not 1 <= len(items) <= MAX_ITEMS_TOTAL:
            self.err(ptr + '/items', 'size')
        else:
            seen = set()
            for i, item in enumerate(items):
                ip = f'{ptr}/items/{i}'
                if type(item) is not dict or set(item) != ITEM_KEYS:
                    self.err(ip, 'item_fields')
                    continue
                if not _uuid(item['document_id']):
                    self.err(ip + '/document_id', 'uuid')
                elif item['document_id'] in seen:
                    self.err(ip + '/document_id', 'duplicate')
                if _uuid(item['document_id']):
                    seen.add(item['document_id'])
                if item['kind'] not in ('PAYMENT', 'REFUND'):
                    self.err(ip + '/kind', 'enum')
                elif item['kind'] == 'REFUND':
                    self.capability.append('native refund documents do not exist; only PAYMENT items are available')
                out_items.append({'document_id': item['document_id'], 'kind': item['kind'], 'amount': self.money(ip + '/amount', item['amount'])})
        if len(self.errors) > before or self.capability or self.currency is None:
            return None
        normalised = {'type': 'RecordSettlement', 'schema_version': 1, 'processor_ref': op['processor_ref'], 'payout_id': op['payout_id'],
                      'payout_date': op['payout_date'], 'items': out_items, **amounts, 'statement_document_id': op['statement_document_id']}
        self._reuse_existing_validators(ptr, normalised)
        return normalised

    def _reuse_existing_validators(self, ptr: str, op: dict):
        internal = {'processor': op['processor_ref'], 'payout': op['payout_id'], 'date': op['payout_date'], 'currency': self.currency.code,
                    'gross': op['gross']['amount'], 'fee': op['fee']['amount'], 'net': op['net']['amount'],
                    'items': [{'document_id': i['document_id'], 'kind': i['kind']} for i in op['items']]}
        try:
            validate_request(internal)
        except AdapterError as e:
            self.err(ptr, f'adapter:{e.code}')
            return
        try:
            token(op['processor_ref']); token(op['payout_id'])
            Settlement('draft', 'draft', op['processor_ref'], op['payout_id'], op['payout_date'],
                       tuple(i['document_id'] for i in op['items']),
                       *(km.Money.parse(op[k]['amount'], self.currency) for k in ('gross', 'fee', 'net')))
        except km.DomainError as e:
            self.err(ptr, f'kernel:{e.code}')

    def evidence(self, ptr: str, refs) -> list | None:
        if type(refs) is not list or len(refs) > MAX_EVIDENCE:
            self.err(ptr, 'size')
            return None
        out, seen = [], set()
        for i, ref in enumerate(refs):
            rp = f'{ptr}/{i}'
            if type(ref) is not dict or set(ref) - {'kind', 'id'} or set(ref) != {'kind', 'id'}:
                self.err(rp, 'read_only' if type(ref) is dict and set(ref) & {'provenance', 'sha256'} else 'evidence_fields')
                continue
            if type(ref['kind']) is not str or ref['kind'] not in EVIDENCE_KINDS:
                self.err(rp + '/kind', 'enum')
                continue
            if ref['kind'] == 'ATTACHMENT':
                self.capability.append('ATTACHMENT evidence cannot be referenced yet: no attachment store exists')
            if not _uuid(ref['id']):
                self.err(rp + '/id', 'uuid')
                continue
            if (ref['kind'], ref['id']) in seen:
                self.err(rp, 'duplicate')
            seen.add((ref['kind'], ref['id']))
            out.append({'kind': ref['kind'], 'id': ref['id']})
        return out

    def rationale(self, ptr: str, value):
        if value is not None and (type(value) is not str or len(value) > MAX_RATIONALE or '\x00' in value):
            self.err(ptr, 'rationale')
        return value

    def operations(self, ptr: str, ops) -> list | None:
        if type(ops) is not list or not 1 <= len(ops) <= MAX_OPERATIONS:
            self.err(ptr, 'size')
            return None
        result = [self.operation(f'{ptr}/{i}', op) for i, op in enumerate(ops)]
        if sum(len(o['items']) for o in result if o) > MAX_ITEMS_TOTAL:
            self.err(ptr, 'too_many_items')
        return result

    def outcome(self):
        if self.errors:
            first = self.errors[0]['code']
            code = first if first in MONEY_CODES else 'SCHEMA_INVALID'
            raise Invalid(problem(422, code, 'the request does not match the operation schema', self.errors[:50]))
        if self.capability:
            raise Invalid(problem(422, 'CAPABILITY_UNAVAILABLE', self.capability[0]))


def check_create_shape(body) -> str:
    """Top-level shape and ledger id; returns the ledger id. Runs before the ledger is read."""
    c = _Check(None)
    if type(body) is not dict:
        c.err('', 'type')
        c.outcome()
    for k in sorted(CREATE_REQUIRED - set(body)):
        c.err('/' + k, 'required')
    for k in sorted(set(body) - CREATE_KEYS):
        c.err('/' + k[:64], 'read_only' if k in ('id', 'state', 'revision', 'version', 'created_by', 'origin') else 'unknown_field')
    if 'ledger_id' in body and not _uuid(body['ledger_id']):
        c.err('/ledger_id', 'uuid')
    target = body.get('target_adapter')
    if 'target_adapter' in body and target not in ('NATIVE', 'QUICKBOOKS'):
        c.err('/target_adapter', 'enum')
    c.outcome()
    if target == 'QUICKBOOKS':
        raise Invalid(problem(422, 'CAPABILITY_UNAVAILABLE', 'the QuickBooks adapter is not available; drafts target NATIVE only'))
    return body['ledger_id']


def check_ledger(row) -> km.Currency:
    """row = (purpose, source_kind, currency, exponent) read under RLS, or None."""
    if row is None:
        raise Invalid(problem(404, 'NOT_FOUND', 'ledger not found', [{'pointer': '/ledger_id', 'code': 'not_found'}]))
    purpose, source, currency, exponent = row
    if (purpose, source) != ('SANDBOX', 'NATIVE'):
        raise Invalid(problem(422, 'CAPABILITY_UNAVAILABLE', 'proposal drafts are available for native SANDBOX ledgers only'))
    return km.Currency(currency.strip(), exponent)


def check_create(body: dict, currency: km.Currency) -> dict:
    c = _Check(currency)
    ops = c.operations('/operations', body['operations'])
    evidence = c.evidence('/evidence_refs', body.get('evidence_refs', []))
    rationale = c.rationale('/rationale', body.get('rationale'))
    c.outcome()
    return {'ledger_id': body['ledger_id'], 'target_adapter': 'NATIVE', 'operations': ops, 'evidence_refs': evidence, 'rationale': rationale}


def check_patch(body, currency: km.Currency) -> dict:
    c = _Check(currency)
    if type(body) is not dict or not body:
        c.err('', 'empty_patch' if type(body) is dict else 'type')
        c.outcome()
    for k in sorted(set(body) - PATCH_KEYS):
        c.err('/' + k[:64], 'immutable' if k in ('ledger_id', 'target_adapter', 'id', 'state', 'revision', 'version', 'created_by', 'origin')
              else 'unknown_field')
    out = {}
    if 'operations' in body:
        out['operations'] = c.operations('/operations', body['operations'])
    if 'evidence_refs' in body:
        out['evidence_refs'] = c.evidence('/evidence_refs', body['evidence_refs'])
    if 'rationale' in body:
        out['rationale'] = c.rationale('/rationale', body['rationale'])
    c.outcome()
    return out
