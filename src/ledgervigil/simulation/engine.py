"""Pure, deterministic evaluation of one captured input bundle (Stage 2A §10.1 steps 2–5 and 8; §10.2).

`evaluate(bundle)` reads nothing but the bundle and the installed code: no clock, no randomness, no database, no network.
It returns the canonical outcome:

* REJECTED with a typed problem (Stage 2A §9.1 validation order: schema -> profile -> preconditions -> capability), or
* COMPLETED with predicted postings, account deltas and document effects computed by the existing adapter and the vendored
  kernel (`kernel_adapter.calculation.calculate_settlement`, reused unchanged), plus the kernel's own diagnostics.

Verification (this milestone): for bundles pinned to simulator 0.2.0, a COMPLETED outcome is verified by the SP-1 policy
registry (`policies.verify`, `sp1-policies/1.1`) on the frozen bundle and the simulated effects, producing verdicts and the
ADR-014 aggregate. Bundles pinned to the legacy 0.1.0 profile are evaluated exactly as before (no verdicts, NOT_EVALUATED).
REJECTED outcomes never carry verdicts (Stage 2A §9.1). Nothing here certifies accounting (UNREVIEWED) or makes a proposal
eligible for execution.

Inconsistent captured facts (projection vs postings, a payment without its posting, ...) are not outcomes: they raise
`IntegrityFailure`, and nothing is recorded.
"""
from decimal import Decimal, localcontext

from ledger_kernel.money import CONTEXT, Currency, DomainError, Money

from ledgervigil.kernel_adapter.calculation import calculate_settlement
from ledgervigil.kernel_adapter.types import AdapterError, DuplicateSettlementRefused

from . import policies, versions
from .bundle import ledger_snapshot
from .canonical import canonical, digest

SYNC_ITEM_BUDGET = 200     # Stage 2B: more than 200 documents -> 202 + SIMULATION job (not built here)
CONTRACTED_REASONS = {'DUPLICATE_SETTLEMENT', 'ITEM_ALREADY_SETTLED', 'PERIOD_CLOSED'}      # Stage 2B §4.2 PRECONDITION_FAILED reasons
KERNEL_PRECONDITIONS = {'ITEM_VOIDED', 'PROCESSOR_MISMATCH', 'SETTLEMENT_TOTALS', 'ITEM_TOTAL_MISMATCH', 'INACTIVE_ACCOUNT',
                        'ACCOUNT_ROLE_TYPE_MISMATCH'}                                        # kernel preconditions, reason not named (SM-G2)
ADAPTER_PRECONDITIONS = {'NO_PERIOD_FOR_DATE', 'INCOMPLETE_CONFIGURATION', 'ITEM_NOT_FOUND'}  # adapter preconditions, reason not named (SM-G2)
SCHEMA_CODES = {'REQUEST_FIELDS', 'UNSUPPORTED_INPUT_TYPE', 'ITEMS_INVALID', 'ITEM_FIELDS', 'DUPLICATE_ITEM', 'DATE_INVALID',
                'MONEY_WIRE_FORMAT', 'MONEY_WIRE_SCALE', 'DUPLICATE_ITEM', 'ITEMS_INVALID', 'TOKEN_INVALID', 'DATE_INVALID',
                'SUB_MINOR_UNIT', 'MONEY_RANGE'}
LABEL_DIAGNOSTICS = ('KERNEL DIAGNOSTICS from ledger_kernel settlement slice (check version recorded in versions.kernel_checks). '
                     'These are calculation diagnostics, NOT verdicts of an installed policy set, NOT an accounting certification '
                     'and NOT execution readiness.')
NOT_EVALUATED = ('No policy set is installed (Stage 2A §6.2 / ADR-014 registry absent): no verdict is produced and nothing is '
                 'verified. Verification is a later milestone.')


class IntegrityFailure(Exception):
    """Captured facts are internally inconsistent; the run must not be recorded (500)."""


class VersionUnavailable(Exception):
    def __init__(self, differences):
        super().__init__('versions differ')
        self.differences = differences


def _rejected(bundle, code, detail, reason=None, errors=None, contracted=True, evidence=None) -> dict:
    status = {'PRECONDITION_FAILED': 422, 'UNSUPPORTED_BY_PROFILE': 422, 'CAPABILITY_UNAVAILABLE': 422, 'SCHEMA_INVALID': 422,
              'CURRENCY_MISMATCH': 422}[code]
    problem = {'status': status, 'code': code, 'detail': detail}
    if reason is not None:
        problem['reason'] = reason
        problem['reason_named_by_contract'] = contracted
    if errors:
        problem['errors'] = errors
    if evidence is not None:
        problem['evidence'] = evidence
    outcome = {'format': bundle['versions']['outcome_format'], 'status': 'REJECTED', 'input_hash': digest(canonical(bundle)),
               'simulator_version': bundle['versions']['simulator'], 'rejection': problem, 'verdicts': [], 'aggregate': None,
               'accounting_status': 'UNREVIEWED', 'eligible_for_execution': False}
    if bundle['versions']['simulator'] != versions.LEGACY_SIMULATOR:
        # Stage 2A §9.1: a typed rejection precedes verification and carries no verdicts.
        outcome['verification'] = {'status': 'NOT_REACHED', 'policy_set_version': bundle['versions']['policy_set'],
                                   'reason': 'typed rejection precedes policy verification (Stage 2A §9.1); no verdict is produced'}
    return outcome


def check_versions(bundle):
    pinned = bundle.get('versions')
    simulator = pinned.get('simulator') if isinstance(pinned, dict) else None
    installed = versions.installed(simulator) if simulator in versions.PROFILES else {}
    if not isinstance(pinned, dict) or pinned != installed:
        keys = sorted(set(installed) | set(pinned or {}))
        raise VersionUnavailable([{'name': k, 'pinned': (pinned or {}).get(k), 'installed': installed.get(k)} for k in keys
                                  if (pinned or {}).get(k) != installed.get(k)])


def evaluate(bundle: dict) -> dict:
    """Pin precision, rounding and traps for the whole adapter/policy calculation (2A §3, §10.2).

    Money guards its own operations, but adapter projections and policy sums also use Decimal directly.
    A caller's context must change neither replay bytes nor errors; localcontext restores it even on refusal.
    """
    with localcontext(CONTEXT):
        return _evaluate(bundle)


def _evaluate(bundle: dict) -> dict:
    check_versions(bundle)
    ledger, proposal = bundle['ledger'], bundle['proposal']
    ops = proposal['operations']
    # Capability first: nothing else can be evaluated for an operation this simulator does not implement.
    if (ledger['purpose'], ledger['source']) != ('SANDBOX', 'NATIVE'):
        return _rejected(bundle, 'CAPABILITY_UNAVAILABLE', 'simulation is available for native SANDBOX ledgers only')
    if len(ops) != 1:
        return _rejected(bundle, 'CAPABILITY_UNAVAILABLE', 'multi-operation simulation (a sequential overlay, Stage 2A §10.1 step 3) is not '
                         'implemented; this foundation simulates exactly one RecordSettlement', reason='MULTI_OPERATION_SIMULATION',
                         contracted=False)
    op = ops[0]
    if not isinstance(op, dict) or op.get('type') != 'RecordSettlement' or op.get('schema_version') != 1:
        return _rejected(bundle, 'CAPABILITY_UNAVAILABLE', 'only RecordSettlement (schema_version 1) is simulated')
    if bundle['facts'] is None:
        raise IntegrityFailure('supported operation captured without ledger facts')
    if len(op['items']) > SYNC_ITEM_BUDGET:
        return _rejected(bundle, 'CAPABILITY_UNAVAILABLE', f'more than {SYNC_ITEM_BUDGET} items needs an asynchronous SIMULATION job, '
                         'which is not built', reason='ASYNC_SIMULATION_REQUIRED', contracted=False)
    try:
        currency = Currency(ledger['currency'], ledger['currency_exponent'])
    except DomainError as error:
        raise IntegrityFailure(error.code) from error
    snap = ledger_snapshot(bundle['facts'])
    # Profile: every amount in the functional currency; each item settles at its full (current) amount (2A §19.3).
    for name in ('gross', 'fee', 'net'):
        if op[name]['currency'] != currency.code:
            return _rejected(bundle, 'CURRENCY_MISMATCH', f'{name} is not in the ledger functional currency',
                             errors=[{'pointer': f'/operations/0/{name}/currency', 'code': 'currency'}])
    payments = {p.document_id: p for p in snap.payments}
    partial = []
    for n, item in enumerate(op['items']):
        pay = payments.get(item['document_id'])
        if item['amount']['currency'] != currency.code:
            return _rejected(bundle, 'CURRENCY_MISMATCH', 'item amount is not in the ledger functional currency',
                             errors=[{'pointer': f'/operations/0/items/{n}/amount/currency', 'code': 'currency'}])
        if pay is not None and Decimal(item['amount']['amount']) != pay.amount:
            partial.append({'pointer': f'/operations/0/items/{n}/amount', 'code': 'PARTIAL_ITEM_SETTLEMENT'})
    if partial:
        return _rejected(bundle, 'UNSUPPORTED_BY_PROFILE', 'SP-1 settles each item at its full amount', reason='PARTIAL_ITEM_SETTLEMENT',
                         errors=partial)
    request = {'processor': op['processor_ref'], 'payout': op['payout_id'], 'date': op['payout_date'], 'currency': currency.code,
               'gross': op['gross']['amount'], 'fee': op['fee']['amount'], 'net': op['net']['amount'],
               'items': [{'document_id': i['document_id'], 'kind': i['kind']} for i in op['items']]}
    try:
        report = calculate_settlement(snap, request)
    except DuplicateSettlementRefused as error:
        return _rejected(bundle, 'PRECONDITION_FAILED', error.evidence['reason'], reason=error.code, evidence=error.evidence)
    except AdapterError as error:
        return _map_refusal(bundle, error)
    return _completed(bundle, op, snap, currency, report)


def _map_refusal(bundle, error: AdapterError) -> dict:
    code = error.code
    if code in CONTRACTED_REASONS:
        return _rejected(bundle, 'PRECONDITION_FAILED', f'{code} at snapshot seq {bundle["snapshot"]["seq"]}', reason=code)
    if code in KERNEL_PRECONDITIONS or code in ADAPTER_PRECONDITIONS:
        return _rejected(bundle, 'PRECONDITION_FAILED', f'{error.source} precondition {code}' + (f' ({error.detail})' if error.detail else ''),
                         reason=code, contracted=False)
    if code == 'UNSUPPORTED_ITEM_KIND':
        return _rejected(bundle, 'CAPABILITY_UNAVAILABLE', 'only PAYMENT items can be simulated (no native refund documents)')
    if code == 'UNSUPPORTED_LEDGER':
        return _rejected(bundle, 'CAPABILITY_UNAVAILABLE', 'simulation is available for native SANDBOX ledgers only')
    if code == 'CURRENCY_MISMATCH':
        return _rejected(bundle, 'CURRENCY_MISMATCH', error.detail or 'currency differs from the ledger functional currency')
    if code in SCHEMA_CODES:
        return _rejected(bundle, 'SCHEMA_INVALID', f'{error.source} refused the operation shape ({code})', reason=code, contracted=False)
    # SNAPSHOT_INCONSISTENT, PAYMENT_POSTING_INCONSISTENT, UNSUPPORTED_PAYMENT_STATE, AMBIGUOUS_ITEM_IDENTITY,
    # UNSUPPORTED_UNBOUND_ACCOUNT_ACTIVITY and anything unknown: the captured facts cannot be trusted.
    raise IntegrityFailure(f'{error.source}:{code}')


def _signed(text, currency):
    return Money(Decimal(text), currency)


def _completed(bundle, op, snap, currency, report) -> dict:
    payments = {p.document_id: p for p in snap.payments}
    postings = [{'line_no': n, 'account_id': p['account_id'], 'role': p['role'], 'account_code': p['code'], 'side': p['side'],
                 'amount': {'amount': p['amount'], 'currency': p['currency']}, 'party_id': p['party_id'], 'document_id': None}
                for n, p in enumerate(report['predicted_postings'], start=1)]
    deltas = []
    for b in report['balances']:
        before, after = _signed(b['before'], currency), _signed(b['after'], currency)
        deltas.append({'account_id': b['account_id'], 'role': b['role'], 'account_code': b['code'], 'party_id': None,
                       'before': before.wire(), 'after': after.wire(), 'delta': (after - before).wire()})
    effects = [{'document_id': None, 'document_type': 'SETTLEMENT', 'effect': 'CREATE', 'version_before': None, 'version_after': 1,
                'open_amount_before': None, 'open_amount_after': None, 'status_before': None, 'status_after': 'RECORDED',
                'note': 'new settlement document; its id is allocated only at execution'}]
    for item in sorted(op['items'], key=lambda i: i['document_id']):
        pay = payments[item['document_id']]
        open_amount = Money(pay.unapplied, currency).wire()
        effects.append({'document_id': pay.document_id, 'document_type': 'PAYMENT', 'effect': 'UPDATE_IN_PLACE', 'version_before': pay.version,
                        'version_after': pay.version + 1, 'open_amount_before': open_amount, 'open_amount_after': open_amount,
                        'status_before': pay.status, 'status_after': 'SETTLED'})
    kernel = report['kernel']
    outcome = {'format': bundle['versions']['outcome_format'], 'status': 'COMPLETED', 'input_hash': digest(canonical(bundle)),
            'simulator_version': bundle['versions']['simulator'], 'snapshot_seq': bundle['snapshot']['seq'],
            'predicted_postings': postings, 'account_deltas': deltas, 'document_effects': effects,
            'settlement_clearing_residual': {'amount': report['settlement_clearing_residual'], 'currency': currency.code},
            'calculation': {'label': LABEL_DIAGNOSTICS,
                            'kernel': {k: kernel[k] for k in ('package', 'version', 'rule_version', 'check_version', 'outcome',
                                                               'input_hash', 'snapshot_hash', 'envelope_sha256')},
                            'kernel_diagnostics': report['checks'], 'evidence_observations': report['evidence_checks'],
                            'statement_observation': report['statement_observation'],
                            'cited_statement_id': op['statement_document_id'],
                            'payout_identity_statement_id': snap.statement.staged_document_id if snap.statement else None},
            'verdicts': [], 'aggregate': None,
            'verification': {'status': 'NOT_EVALUATED', 'policy_set_version': None, 'reason': NOT_EVALUATED},
            'accounting_status': 'UNREVIEWED', 'eligible_for_execution': False}
    if bundle['versions']['simulator'] == versions.LEGACY_SIMULATOR:
        return outcome      # legacy profile: byte-identical to the simulation foundation; never retrofitted with verdicts
    # Stage 2A §10.1 step 6: policies run on the frozen inputs and the simulated effects only (no database, clock or network).
    try:
        outcome.update(policies.verify(bundle, outcome, snap, op))
    except (policies.MissingFact, policies.PolicyContractViolation) as error:
        raise IntegrityFailure(f'policy verification: {type(error).__name__}: {error}') from error
    return outcome
