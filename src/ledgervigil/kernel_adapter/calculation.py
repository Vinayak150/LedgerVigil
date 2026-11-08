"""Pure translation from a loaded native ledger snapshot into the existing accounting kernel.

No database, network or framework access happens here. Every accounting rule comes from the vendored,
unchanged `ledger_kernel` package (docs/KERNEL_PROVENANCE.json). This module only:

* validates the internal request shape;
* maps native role bindings, currency and posted journal entries onto kernel types;
* resolves requested payment items, either as typed processor Payment documents (`_typed_items`, authoritative identity)
  or, for older manual-journal fixtures only, through one documented interim mapping (`_legacy_items`);
* reports the kernel's own result, unchanged, with source, snapshot and data labels attached.

Accounting semantics remain UNREVIEWED. Nothing here is an approval or an execution.
"""
from datetime import date
from decimal import Decimal
from uuid import UUID

import ledger_kernel
from ledger_kernel.model import Account, Entry, Item, Line, Settlement, Snapshot
from ledger_kernel.money import Currency, DomainError, Money
from ledger_kernel.settlement import CHECK_VERSION, RULE_VERSION, inspect_settlement_entry, simulate_settlement

from . import evidence as evidence_checks
from .types import CONTRACT, AdapterError, DuplicateSettlementRefused, LedgerSnapshot

# Stage 2A full role names -> kernel short roles (kernel docs/TRACEABILITY.md).
ROLE_TO_KERNEL = {'BANK_OPERATING': 'BANK', 'AR_CONTROL': 'AR', 'PROCESSOR_CLEARING': 'CLR',
                  'OPENING_BALANCE_EQUITY': 'OBE', 'REVENUE_DEFAULT': 'REV', 'SALES_RETURNS': 'RET',
                  'PROCESSOR_FEE_EXPENSE': 'FEE'}
KERNEL_TO_ROLE = {v: k for k, v in ROLE_TO_KERNEL.items()}
REQUEST_KEYS = {'processor', 'payout', 'date', 'currency', 'gross', 'fee', 'net', 'items'}
LEGACY_ITEM_KEYS = {'source_ref', 'kind'}     # interim: a posted MANUAL_JOURNAL fixture named by source_ref
TYPED_ITEM_KEYS = {'document_id', 'kind'}     # Stage 2B RecordSettlementOp item shape (amount omitted: loaded, never asserted)
ITEM_KEYS = LEGACY_ITEM_KEYS                  # kept for older callers
TYPED_RULE = 'sp1-posting/1.0'

# Checks whose kernel verdict cannot rest on persisted native facts in this schema revision.
CHECK_BASIS = {
    'P-DUP-01': 'NOT_ESTABLISHED: the native schema has no settlement-inclusion (I-08) or execution-record persistence; '
                'the kernel evaluated an empty inclusion set and unsettled items',
    **{id: 'Kernel evaluated without a statement. The kernel decides statement checks only for a SOURCE-provenance statement '
           '(settlement.py: anything else is INSUFFICIENT_EVIDENCE), and Stage 2A P-STL-01 compares against a posted statement '
           'document (path B, not implemented). The staged statement is SYNTHETIC, so it is evaluated by the adapter evidence '
           'checks (E-*) as an observation instead.' for id in ('P-CLR-01', 'P-EVD-01', 'P-FEE-01', 'P-STL-01')},
}
TYPED_DUP_BASIS = ('Evaluated by the kernel on authoritative state loaded in this snapshot: used_payouts from '
                   'lv.processor_settlement_identity for the requested payout identity, and Item.settled from the SETTLED payment '
                   'version with its active inclusion (lv.settlement_inclusion_current, I-08). Preview scope only; see E-DUP-01.')
DUPLICATE_CODES = {'DUPLICATE_SETTLEMENT', 'ITEM_ALREADY_SETTLED'}
BLOCKING = {'FAIL', 'INSUFFICIENT_EVIDENCE', 'UNRESOLVED', 'NOT_ESTABLISHED', 'NOT_AVAILABLE'}
EXECUTION_GAP = ('No approval, operation, settlement-inclusion or execution-record model exists. This result is a prediction '
                 'and cannot be executed; BOOKS and provider writes are unavailable.')


def _kernel(fn, *args):
    try:
        return fn(*args)
    except DomainError as error:
        raise AdapterError(error.code, 'kernel') from error


def validate_request(request) -> dict:
    """Internal request: strings only (amounts as decimal strings), no JSON numbers, floats or booleans."""
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise AdapterError('REQUEST_FIELDS')
    for key in REQUEST_KEYS - {'items'}:
        if type(request[key]) is not str:
            raise AdapterError('UNSUPPORTED_INPUT_TYPE', detail=key)
    items = request['items']
    if type(items) is not list or not 1 <= len(items) <= 1000:
        raise AdapterError('ITEMS_INVALID')
    shapes = set()
    for item in items:
        if type(item) is not dict or set(item) not in (LEGACY_ITEM_KEYS, TYPED_ITEM_KEYS) or any(type(v) is not str for v in item.values()):
            raise AdapterError('ITEM_FIELDS')
        shapes.add(frozenset(item))
        if item['kind'] != 'PAYMENT':
            # Refund items need a native refund document and inclusion model that do not exist yet.
            raise AdapterError('UNSUPPORTED_ITEM_KIND', detail=item['kind'])
        if 'document_id' in item:
            try:
                canonical = str(UUID(item['document_id']))
            except ValueError:
                canonical = None
            if canonical != item['document_id']:
                raise AdapterError('ITEM_FIELDS', detail='document_id must be a canonical lower-case UUID')
    if len(shapes) != 1:
        raise AdapterError('ITEM_FIELDS', detail='typed document_id items and legacy source_ref items cannot be mixed')
    if 'document_id' in items[0] and len({i['document_id'] for i in items}) != len(items):
        raise AdapterError('DUPLICATE_ITEM')
    return request


def item_mode(request: dict) -> str:
    """TYPED: items are processor Payment documents. LEGACY: interim manual-journal mapping (older fixtures only)."""
    return 'TYPED' if 'document_id' in request['items'][0] else 'LEGACY'


def _money(value: Decimal, currency: Currency) -> Money:
    """Stored amounts must already be exact at the ledger exponent; anything else is refused."""
    return _kernel(Money, value, currency)


def _require_configuration(snap: LedgerSnapshot, currency: Currency) -> dict:
    if snap.purpose != 'SANDBOX' or snap.source_kind != 'NATIVE':
        raise AdapterError('UNSUPPORTED_LEDGER', detail=f'{snap.purpose}/{snap.source_kind}')
    roles = {b.role: b for b in snap.bindings}
    if set(roles) != set(ROLE_TO_KERNEL) or len(snap.bindings) != len(ROLE_TO_KERNEL):
        raise AdapterError('INCOMPLETE_CONFIGURATION', detail='missing roles: ' + ','.join(sorted(set(ROLE_TO_KERNEL) - set(roles))))
    for b in snap.bindings:
        if (b.currency, b.exponent) != (currency.code, currency.exponent):
            raise AdapterError('CURRENCY_MISMATCH', detail=f'account {b.code}')
    # Internal consistency of the snapshot itself (one transaction; these must agree).
    if snap.max_batch_seq != snap.seq or snap.last_entry_no != len(snap.entries):
        raise AdapterError('SNAPSHOT_INCONSISTENT', detail='counter, batches and entries disagree')
    if any(e.seq > snap.seq for e in snap.entries):
        raise AdapterError('SNAPSHOT_INCONSISTENT', detail='entry beyond snapshot sequence')
    return roles


def _entries(snap: LedgerSnapshot, currency: Currency, bound: set) -> tuple:
    result = []
    for e in snap.entries:
        lines = []
        for line in e.lines:
            if (line.currency, line.exponent) != (currency.code, currency.exponent):
                raise AdapterError('CURRENCY_MISMATCH', detail=f'entry {e.entry_no}')
            if line.account_id not in bound:
                raise AdapterError('UNSUPPORTED_UNBOUND_ACCOUNT_ACTIVITY', detail=f'entry {e.entry_no}')
            lines.append(_kernel(Line, line.account_id, line.side, _money(line.amount, currency), line.party_id))
        result.append(_kernel(Entry, tuple(lines)))
    return tuple(result)


def _typed_items(snap: LedgerSnapshot, request: dict, currency: Currency, roles: dict) -> tuple:
    """Requested typed processor Payments -> kernel items, from persisted facts only.

    Each requested document must be a PAYMENT of this tenant and ledger with exactly one current version, one authoritative
    processor identity, a RECEIVED (or SETTLED, with a matching active inclusion) projection in the ledger currency, and
    exactly one DOCUMENT_POSTING entry under the supported rule whose lines are Dr PROCESSOR_CLEARING / Cr AR_CONTROL (the
    payment's customer) for the current version's amount (I-15; a SETTLED version re-uses the unchanged original posting).
    A SETTLED payment becomes a kernel item with settled=True, so the kernel itself refuses it (ITEM_ALREADY_SETTLED).
    The kernel item carries the persisted processor_ref (never the request's processor), the persisted processor item id
    and the version number; amounts come from the payment projection, cross-checked against the posted lines.
    """
    clearing, receivable = roles['PROCESSOR_CLEARING'].account_id, roles['AR_CONTROL'].account_id
    by_id = {}
    for payment in snap.payments:
        by_id.setdefault(payment.document_id, []).append(payment)
    entries = {e.entry_id: e for e in snap.entries}
    active = {i.item_document_id: i for i in snap.active_inclusions}
    items, seen_ids = [], set()
    for requested in request['items']:
        found = by_id.get(requested['document_id'], [])
        if not found:
            raise AdapterError('ITEM_NOT_FOUND', detail=requested['document_id'])
        if len(found) != 1:
            raise AdapterError('PAYMENT_POSTING_INCONSISTENT', detail=f"{requested['document_id']}: more than one posting entry")
        pay = found[0]
        if (pay.currency, pay.exponent) != (currency.code, currency.exponent):
            raise AdapterError('CURRENCY_MISMATCH', detail=f'payment {pay.document_id}')
        inclusion = active.get(pay.document_id)
        consistent = (pay.status == 'RECEIVED' and pay.settlement_document_id is None and inclusion is None) or \
            (pay.status == 'SETTLED' and inclusion is not None and inclusion.settlement_document_id == pay.settlement_document_id)
        if not consistent or pay.applied != 0 or pay.unapplied != pay.amount:
            raise AdapterError('UNSUPPORTED_PAYMENT_STATE', detail=f'{pay.document_id}: {pay.status}')
        entry = entries.get(pay.entry_id)
        expected = [(clearing, 'D', pay.amount, None), (receivable, 'C', pay.amount, pay.party_id)]
        if entry is None or pay.entry_kind != 'DOCUMENT_POSTING' or pay.posting_rule != TYPED_RULE or pay.entry_version > pay.version or \
                [(l.account_id, l.side, l.amount, l.party_id) for l in entry.lines] != expected:
            raise AdapterError('PAYMENT_POSTING_INCONSISTENT', detail=pay.document_id)
        if pay.processor_item_id in seen_ids:
            raise AdapterError('AMBIGUOUS_ITEM_IDENTITY', detail=pay.processor_item_id)
        seen_ids.add(pay.processor_item_id)
        items.append(_kernel(Item, pay.processor_item_id, 'PAYMENT', pay.processor_ref, _money(pay.amount, currency), str(pay.version),
                             pay.status == 'SETTLED'))
    return tuple(items)


def _legacy_items(snap: LedgerSnapshot, request: dict, currency: Currency, clearing_account: str) -> tuple:
    """Interim mapping, defined by this adapter (not a kernel or Stage 2A rule), kept only for older manual-journal fixtures.

    Those fixtures are not Payment documents and are not relabelled as such. A requested item
    therefore names a posted MANUAL_JOURNAL document by source_ref, and qualifies as a processor payment
    only if its single entry debits the PROCESSOR_CLEARING-bound account on exactly one line and has
    no clearing credit. The item amount is that persisted debit, never a caller-supplied amount. The
    processor identity is asserted by the request (there is no processor persistence).
    """
    docs = {d.source_ref: d for d in snap.documents}
    items = []
    for requested in request['items']:
        ref = requested['source_ref']
        if ref not in docs:
            raise AdapterError('ITEM_NOT_FOUND', detail=ref)
        entries = [e for e in snap.entries if e.source_document_id == docs[ref].document_id]
        clearing = [l for e in entries for l in e.lines if l.account_id == clearing_account]
        if len(entries) != 1 or len(clearing) != 1 or clearing[0].side != 'D':
            raise AdapterError('ITEM_NOT_A_CLEARING_RECEIPT', detail=ref)
        items.append(_kernel(Item, ref, 'PAYMENT', request['processor'], _money(clearing[0].amount, currency)))
    return tuple(items)


def _closed_months_and_period(snap: LedgerSnapshot, settlement_date: str) -> tuple:
    try:
        when = date.fromisoformat(settlement_date)
    except ValueError as error:
        raise AdapterError('DATE_INVALID', 'kernel') from error
    covering = [p for p in snap.periods if date.fromisoformat(p.start_date) <= when < date.fromisoformat(p.end_date)]
    if not covering:
        raise AdapterError('NO_PERIOD_FOR_DATE', detail=settlement_date)
    return tuple(sorted(p.start_date[:7] for p in snap.periods if p.status == 'CLOSED'))


def _check_projection(snap: LedgerSnapshot, snapshot: Snapshot, currency: Currency):
    """The current-state projection must equal balances rebuilt from the same snapshot's postings."""
    projected = {}
    for row in snap.projection:
        projected[row.account_id] = projected.get(row.account_id, Decimal(0)) + row.debit_total - row.credit_total
    for account_id, balance in snapshot.balances().items():
        if projected.get(account_id, Decimal(0)) != balance.value:
            raise AdapterError('SNAPSHOT_INCONSISTENT', detail='projection differs from postings')
    if set(projected) - set(snapshot.balances()):
        raise AdapterError('UNSUPPORTED_UNBOUND_ACCOUNT_ACTIVITY', detail='projection')


def translate(snap: LedgerSnapshot, request: dict):
    """Native snapshot + internal request -> kernel (Snapshot, Settlement) plus the role table."""
    validate_request(request)
    currency = _kernel(Currency, snap.currency, snap.exponent)
    if request['currency'] != currency.code:
        raise AdapterError('CURRENCY_MISMATCH', detail=f"request {request['currency']} vs ledger {currency.code}")
    roles = _require_configuration(snap, currency)
    accounts = tuple(_kernel(Account, b.account_id, ROLE_TO_KERNEL[b.role], b.active, b.type) for b in snap.bindings)
    entries = _entries(snap, currency, {b.account_id for b in snap.bindings})
    typed = item_mode(request) == 'TYPED'
    items = _typed_items(snap, request, currency, roles) if typed else \
        _legacy_items(snap, request, currency, roles['PROCESSOR_CLEARING'].account_id)
    closed = _closed_months_and_period(snap, request['date'])
    # Typed requests: the kernel's used_payouts come from the authoritative payout identity in this snapshot. Legacy
    # manual-journal requests keep the earlier behaviour (empty), and their duplicate check stays NOT_ESTABLISHED.
    used = ((snap.payout_settlement.processor_ref, snap.payout_settlement.payout_id),) \
        if item_mode(request) == 'TYPED' and snap.payout_settlement is not None else ()
    snapshot = _kernel(Snapshot, snap.tenant_id, snap.ledger_id, str(snap.seq), currency, accounts, entries, items, used, closed)
    _check_projection(snap, snapshot, currency)
    parse = lambda raw: _kernel(Money.parse, raw, currency)  # noqa: E731 - kernel wire-format and scale rules
    command = _kernel(Settlement, snap.tenant_id, snap.ledger_id, request['processor'], request['payout'], request['date'],
                      tuple(i.id for i in items), parse(request['gross']), parse(request['fee']),
                      parse(request['net']))
    return snapshot, command, roles


def _labelled(snap: LedgerSnapshot, request: dict, roles: dict, report, mode: str, kernel_snapshot=None) -> dict:
    by_account = {b.account_id: b for b in roles.values()}
    envelope = report.wire()
    payload = envelope['payload']
    describe = lambda account_id: {'role': by_account[account_id].role, 'code': by_account[account_id].code, 'account_id': account_id}  # noqa: E731
    observation, evidence = evidence_checks.evaluate(snap, request, kernel_snapshot, payload)
    typed = item_mode(request) == 'TYPED'
    basis = {**CHECK_BASIS, 'P-DUP-01': TYPED_DUP_BASIS} if typed else CHECK_BASIS
    checks = [{**c, 'basis': basis.get(c['id'], 'Evaluated by the kernel on loaded native facts')} for c in payload['checks']]
    execution = {'id': 'X-EXEC-01', 'verdict': 'NOT_AVAILABLE', 'reason': EXECUTION_GAP}
    blocking = [f"{c['id']}: {c['verdict']}" for c in checks + evidence + [execution] if c['verdict'] in BLOCKING]
    if typed:
        requested = {i['document_id'] for i in request['items']}
        source_items = [{'document_id': p.document_id, 'document_type': 'PAYMENT', 'version': str(p.version),
                         'document_version_id': p.version_id, 'financial_fingerprint': p.financial_fingerprint,
                         'processor_ref': p.processor_ref, 'processor_item_id': p.processor_item_id, 'party_id': p.party_id,
                         'amount': f'{p.amount:.{p.exponent}f}', 'currency': p.currency, 'status': p.status, 'identity_seq': str(p.identity_seq),
                         'posting_entry_id': p.entry_id, 'posting_entry_document_version': str(p.entry_version),
                         'posting_rule': p.posting_rule, 'settlement_document_id': p.settlement_document_id}
                        for p in sorted(snap.payments, key=lambda p: p.processor_item_id) if p.document_id in requested]
        item_labels = {'item_kind': 'TYPED_PROCESSOR_PAYMENT',
                       'item_processor': 'AUTHORITATIVE_PAYMENT_IDENTITY (lv.processor_payment_identity; the request processor is not used for items)',
                       'item_amounts': 'LOADED_FROM_TYPED_PAYMENT_VERSIONS (lv.payment_current, cross-checked with the DOCUMENT_POSTING lines)',
                       'duplicate_check_scope': 'PREVIEW_SNAPSHOT: authoritative payout identity and active inclusions at snapshot seq '
                                                f'{snap.seq}; not a reservation and not an execution-time revalidation'}
    else:
        source_items = [{'source_ref': d.source_ref, 'document_id': d.document_id} for d in snap.documents
                        if d.source_ref in {i['source_ref'] for i in request['items']}]
        item_labels = {'item_kind': 'LEGACY_MANUAL_JOURNAL_FIXTURE (interim mapping; not a Payment document)',
                       'item_processor': 'REQUEST_ASSERTED', 'item_amounts': 'LOADED_FROM_POSTED_CLEARING_DEBITS'}
    return {
        'contract': CONTRACT,
        'mode': mode,
        'labels': {'dataset_provenance': 'SYNTHETIC', 'ledger_purpose': snap.purpose, 'source_kind': snap.source_kind,
                   'accounting_semantics': 'UNREVIEWED', 'eligible_for_execution': False,
                   'statement': 'STAGED_SYNTHETIC_OBSERVATION' if snap.statement else 'NO_STAGED_STATEMENT_FOR_PAYOUT',
                   'statement_source': 'Loaded from lv.staged_document in the same snapshot; no caller-supplied statement or verified flag is accepted',
                   **item_labels},
        'source': {'tenant_id': snap.tenant_id, 'ledger_id': snap.ledger_id, 'currency': snap.currency,
                   'exponent': str(snap.exponent),
                   'items': source_items,
                   'role_bindings': {b.role: {'account_id': b.account_id, 'code': b.code} for b in sorted(roles.values(), key=lambda b: b.role)}},
        'snapshot': {'seq': str(snap.seq), 'batch_head': snap.batch_head, 'event_head': snap.event_head,
                     'entries': str(len(snap.entries)), 'isolation': snap.isolation, 'read_only': snap.read_only,
                     'read_role': snap.read_role, 'write_xid_assigned': snap.write_xid_assigned,
                     'transaction_timestamp': snap.transaction_timestamp},
        'kernel': {'package': 'ledger_kernel', 'version': ledger_kernel.__version__, 'rule_version': RULE_VERSION,
                   'check_version': CHECK_VERSION, 'provenance': 'docs/KERNEL_PROVENANCE.json',
                   'outcome': payload['outcome'], 'envelope_sha256': envelope['sha256'],
                   'input_hash': payload['input_hash'], 'snapshot_hash': payload['snapshot_hash']},
        'request': request,
        'predicted_postings': [{**describe(l['account']), 'side': l['side'], 'amount': l['amount']['amount'],
                                'currency': l['amount']['currency'], 'party_id': l['party']} for l in payload['entry']],
        'balances': [{**describe(account_id), 'before': payload['before'][account_id], 'after': payload['after'][account_id]}
                     for account_id in sorted(payload['before'], key=lambda a: by_account[a].code)],
        'settlement_clearing_residual': payload['settlement_clearing_residual'],
        'checks': checks,
        'statement_observation': observation,
        'evidence_checks': evidence,
        'execution_readiness': execution,
        'outcome': 'BLOCKED' if blocking else 'NO_BLOCKING_REASON_FOUND',
        'blocking_reasons': blocking,
        'adapter_assessment': ('PREDICTION_ONLY: statement values are observations, not ledger facts. Statement item identity is '
                               'resolved against typed Payment identities (see E-ITEM-01); duplicate settlement is assessed from '
                               'authoritative payout and inclusion state in this snapshot only (see E-DUP-01). ' if typed else
                               'PREDICTION_ONLY: statement values are observations, not ledger facts; item identity is not established; '
                               'duplicate-settlement facts are not established. ')
                              + 'Not an approval and not executable.',
        'kernel_envelope': envelope,
    }


def calculate_settlement(snap: LedgerSnapshot, request: dict) -> dict:
    """Typed settlement simulation by the kernel (derive_settlement + diagnostics)."""
    snapshot, command, roles = translate(snap, request)
    try:
        report = _kernel(simulate_settlement, snapshot, command, None)
    except AdapterError as error:
        # The kernel's own preconditions refused a duplicate. Attach the authoritative evidence for that refusal.
        if error.source == 'kernel' and error.code in DUPLICATE_CODES and item_mode(request) == 'TYPED':
            raise DuplicateSettlementRefused(error.code, evidence_checks.duplicate_check(snap, request), _snapshot_identity(snap)) from error
        raise
    return _labelled(snap, request, roles, report, 'SIMULATE_SETTLEMENT', snapshot)


def _snapshot_identity(snap: LedgerSnapshot) -> dict:
    return {'tenant_id': snap.tenant_id, 'ledger_id': snap.ledger_id, 'seq': str(snap.seq), 'batch_head': snap.batch_head,
            'event_head': snap.event_head, 'isolation': snap.isolation, 'read_only': snap.read_only,
            'write_xid_assigned': snap.write_xid_assigned, 'transaction_timestamp': snap.transaction_timestamp}


def inspect_settlement_candidate(snap: LedgerSnapshot, request: dict, candidate: list) -> dict:
    """Kernel diagnostics for a proposed set of settlement lines given by role, e.g. a balanced wrong sale."""
    snapshot, command, roles = translate(snap, request)
    if type(candidate) is not list or not candidate:
        raise AdapterError('CANDIDATE_INVALID')
    lines = []
    for line in candidate:
        if type(line) is not dict or set(line) != {'role', 'side', 'amount'} or any(type(v) is not str for v in line.values()) \
                or line['role'] not in roles:
            raise AdapterError('CANDIDATE_INVALID')
        lines.append(_kernel(Line, roles[line['role']].account_id, line['side'], _kernel(Money.parse, line['amount'], snapshot.currency)))
    entry = _kernel(Entry, tuple(lines))
    return _labelled(snap, request, roles, _kernel(inspect_settlement_entry, snapshot, command, entry, None), 'INSPECT_CANDIDATE', snapshot)
