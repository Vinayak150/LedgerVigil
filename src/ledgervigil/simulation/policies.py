"""SP-1 policy registry and evaluator for native RecordSettlement (Stage 2A §6.2, §19.3 P-STL-01; ADR-014; Stage 2B Verdict).

Policy set `sp1-policies/1.1` = the fourteen `sp1-policies/1.0` controls of Stage 2A §6.2 plus P-STL-01 (§19.3, §19.6).

Rules this module enforces mechanically:
* Every contracted policy is registered. Each declares its owning section, the facts it needs, its applicability and the
  outcomes the contract PERMITS for it (the §6.2 columns: an outcome marked "Never" is not permitted). An evaluator that
  returns an outcome outside that set raises `PolicyContractViolation`; nothing is recorded.
* Policies run only on frozen inputs (the captured bundle) and the simulated outcome (Stage 2A §10.1 step 6). No database,
  clock or network is consulted, so verification replays exactly.
* A fact the contract requires but the bundle lacks is never turned into PASS or NOT_APPLICABLE: where the contract permits
  INSUFFICIENT_EVIDENCE for that condition it is used; otherwise `MissingFact` is raised and nothing is recorded.
* P-AUT-01 is "evaluated at approval and execution, not in simulation" (§6.2): it is registered with stage APPROVAL and
  produces no verdict here; the verification block lists it as deferred.
* Aggregation is ADR-014: any FAIL -> BLOCKED_FAIL; else any INSUFFICIENT_EVIDENCE -> BLOCKED_INSUFFICIENT (a controller
  override would be required; overrides are not built); else CLEAR. NOT_APPLICABLE never counts toward a pass: CLEAR needs at
  least one PASS, and counts report NOT_APPLICABLE separately.
* Kernel diagnostics (ledger_kernel settlement slice) are NOT used as verdicts.
* Explanation codes not named by the contract are proposed names (docs/POLICY_VERIFICATION.md PV-N1).

Accounting semantics remain UNREVIEWED: a verdict is a computed check under profile SP-1, never a certification.
"""
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ledger_kernel.model import ROLE_TYPES
from ledger_kernel.money import Currency, DomainError, Money

from ledgervigil.kernel_adapter.calculation import ROLE_TO_KERNEL

POLICY_SET = 'sp1-policies/1.1'
PROFILE_NAME = 'SP-1'
PASS, FAIL, INSUFFICIENT, NA = 'PASS', 'FAIL', 'INSUFFICIENT_EVIDENCE', 'NOT_APPLICABLE'
OUTCOMES = (PASS, FAIL, INSUFFICIENT, NA)
# ADR-014: "Policies are typed Python rules in a versioned registry (semantic version plus content hash)". The content hash is
# the SHA-256 of this module's source, so any change to any rule changes the pinned identifier and old runs refuse to replay
# under it (Stage 2B Family 9: 409 when a pinned version is not installed).
REGISTRY_ID = f'{POLICY_SET}+sha256:{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}'


class PolicyContractViolation(Exception):
    """An evaluator produced an outcome its contract row does not permit."""


class MissingFact(Exception):
    """A fact the contract requires is absent from the frozen inputs, and the contract permits no INSUFFICIENT outcome."""


@dataclass(frozen=True)
class Policy:
    id: str
    version: str
    title: str
    section: str
    stage: str                       # SIMULATION or APPROVAL
    permitted: frozenset
    inputs: tuple
    applicability: str
    implemented: str
    evaluate: object = field(default=None, compare=False, repr=False)


# ------------------------------------------------------------------------------------------- evaluation context
class Context:
    """Frozen bundle + simulated outcome, with typed views. Built once per evaluation; never touches the database."""

    def __init__(self, bundle, outcome, snap, op):
        self.bundle, self.outcome, self.snap, self.op = bundle, outcome, snap, op
        ledger = bundle['ledger']
        self.currency = Currency(ledger['currency'], ledger['currency_exponent'])
        self.ledger_source = ledger['source']
        self.reconciliation = bundle['snapshot']['reconciliation']
        self.bindings = {b.account_id: b for b in snap.bindings}
        self.roles = {b.role: b for b in snap.bindings}
        self.payments = {p.document_id: p for p in snap.payments}
        self.entries = {e.entry_id: e for e in snap.entries}
        self.lines = outcome['predicted_postings']
        self.statement = (bundle.get('pinned_sources') or {}).get('statement')
        self.after = {d['account_id']: d['after'] for d in outcome['account_deltas']}

    def money(self, wire: dict) -> Money:
        if wire['currency'] != self.currency.code:
            raise DomainError('CURRENCY_MISMATCH')
        return Money.parse(wire['amount'], self.currency)

    def signed(self, line) -> Money:
        amount = self.money(line['amount'])
        return amount if line['side'] == 'D' else -amount

    def role_of(self, account_id):
        b = self.bindings.get(account_id)
        return None if b is None else b.role

    def touched_roles(self):
        return {self.role_of(l['account_id']) for l in self.lines}

    def touched_types(self):
        return {self.bindings[l['account_id']].type for l in self.lines if l['account_id'] in self.bindings}

    def statement_evidence(self):
        s = self.statement
        if s is None:
            return []
        return [{'kind': 'STAGED_DOCUMENT', 'id': s['id'], 'provenance': s['provenance'], 'sha256': None},
                {'kind': 'SOURCE_RECORD', 'id': s['source_record_id'], 'provenance': None, 'sha256': s['source_record_sha256']}]

    def payment_evidence(self):
        return [{'kind': 'DOCUMENT_VERSION', 'id': self.payments[i['document_id']].version_id, 'provenance': None, 'sha256': None}
                for i in sorted(self.op['items'], key=lambda i: i['document_id']) if i['document_id'] in self.payments]

    def statement_money(self, name):
        s = self.statement
        if (s['currency'], s['currency_exponent']) != (self.currency.code, self.currency.exponent):
            raise DomainError('CURRENCY_MISMATCH')
        return Money.parse(s[name], self.currency)


def _v(outcome, code, facts=None, evidence=None):
    return outcome, code, facts or {}, evidence or []


# ------------------------------------------------------------------------------------------- the rules
def p_bal_01(c: Context):
    debit = sum((c.money(l['amount']).value for l in c.lines if l['side'] == 'D'), Decimal(0))
    credit = sum((c.money(l['amount']).value for l in c.lines if l['side'] == 'C'), Decimal(0))
    facts = {'debits': Money(debit, c.currency).wire(), 'credits': Money(credit, c.currency).wire()}
    if debit == credit and debit > 0:
        return _v(PASS, 'ENTRY_BALANCED', facts)
    return _v(FAIL, 'ENTRY_UNBALANCED' if debit != credit else 'ENTRY_TOTAL_NOT_POSITIVE', facts)


def p_acc_01(c: Context):
    for l in c.lines:
        b = c.bindings.get(l['account_id'])
        facts = {'account_id': l['account_id']}
        if b is None:
            return _v(FAIL, 'ACCOUNT_UNBOUND', facts)
        if not b.active:
            return _v(FAIL, 'ACCOUNT_INACTIVE', {**facts, 'role': b.role})
        if b.role not in ROLE_TO_KERNEL:
            return _v(FAIL, 'ACCOUNT_ROLE_NOT_ALLOWED', {**facts, 'role': b.role})
        if b.type != ROLE_TYPES[ROLE_TO_KERNEL[b.role]]:
            return _v(FAIL, 'ACCOUNT_TYPE_NOT_ALLOWED_FOR_ROLE', {**facts, 'role': b.role, 'account_type': b.type})
        if b.requires_party and l['party_id'] is None:
            return _v(FAIL, 'CONTROL_ACCOUNT_LINE_WITHOUT_CUSTOMER', {**facts, 'role': b.role})
    return _v(PASS, 'ACCOUNTS_VALID')


def p_cur_01(c: Context):
    amounts = [('/predicted_postings/%d/amount' % n, l['amount']) for n, l in enumerate(c.lines)]
    amounts += [(f'/operations/0/{k}', c.op[k]) for k in ('gross', 'fee', 'net')]
    amounts += [(f'/operations/0/items/{n}/amount', i['amount']) for n, i in enumerate(c.op['items'])]
    for pointer, wire in amounts:
        if wire['currency'] != c.currency.code:
            return _v(FAIL, 'AMOUNT_NOT_IN_FUNCTIONAL_CURRENCY', {'pointer': pointer, 'currency': wire['currency']})
        try:
            Money.parse(wire['amount'], c.currency)
        except DomainError:
            return _v(FAIL, 'AMOUNT_OVER_PRECISE', {'pointer': pointer, 'amount': wire['amount']})
    return _v(PASS, 'AMOUNTS_IN_FUNCTIONAL_CURRENCY_AND_SCALE')


def p_rev_01(c: Context):
    revenue = [l for l in c.lines if l['account_id'] in c.bindings and c.bindings[l['account_id']].type == 'REVENUE']
    if not revenue:
        return _v(NA, 'NO_REVENUE_ACCOUNT_TOUCHED')
    # A RecordSettlement entry is a settlement entry: touching revenue is a FAIL whatever the side (§6.2).
    return _v(FAIL, 'SETTLEMENT_ENTRY_TOUCHES_REVENUE',
              {'revenue_lines': [{'account_id': l['account_id'], 'side': l['side'], 'amount': l['amount']} for l in revenue]},
              c.statement_evidence())


def p_clr_01(c: Context):
    clearing = c.roles.get('PROCESSOR_CLEARING')
    if clearing is None:
        raise MissingFact('PROCESSOR_CLEARING binding')
    if clearing.account_id not in {l['account_id'] for l in c.lines}:
        return _v(NA, 'NO_CLEARING_ACCOUNT_TOUCHED')
    if c.statement is None:
        return _v(INSUFFICIENT, 'SETTLEMENT_STATEMENT_MISSING')
    items_movement = Decimal(0)
    for item in c.op['items']:
        pay = c.payments.get(item['document_id'])
        entry = c.entries.get(pay.entry_id) if pay else None
        if entry is None:
            raise MissingFact(f"posting entry of item {item['document_id']}")
        items_movement += sum((l.amount if l.side == 'D' else -l.amount for l in entry.lines if l.account_id == clearing.account_id), Decimal(0))
    settlement_movement = sum((c.signed(l).value for l in c.lines if l['account_id'] == clearing.account_id), Decimal(0))
    residual = Money(items_movement + settlement_movement, c.currency)
    facts = {'items_clearing_movement': Money(items_movement, c.currency).wire(),
             'settlement_clearing_movement': Money(settlement_movement, c.currency).wire(),
             'statement_items_residual': residual.wire(), 'expected_residual': Money(Decimal(0), c.currency).wire()}
    if residual.value == 0:
        return _v(PASS, 'CLEARING_RELIEVED_FOR_SETTLED_ITEMS', facts, c.payment_evidence())
    return _v(FAIL, 'CLEARING_NOT_RELIEVED_FOR_SETTLED_ITEMS', facts, c.payment_evidence())


def p_clr_02(c: Context):
    clearing = c.roles.get('PROCESSOR_CLEARING')
    if clearing is None:
        raise MissingFact('PROCESSOR_CLEARING binding')
    if clearing.account_id not in {l['account_id'] for l in c.lines}:
        return _v(NA, 'NO_CLEARING_ACCOUNT_TOUCHED')
    if clearing.account_id not in c.after:
        raise MissingFact('post-state clearing balance')
    after = c.money(c.after[clearing.account_id])     # debit-positive signed balance (kernel convention)
    facts = {'clearing_balance_after': after.wire()}
    return _v(FAIL, 'CLEARING_ENDS_IN_CREDIT', facts) if after.value < 0 else _v(PASS, 'CLEARING_NOT_IN_CREDIT', facts)


def p_app_01(c: Context):
    changed = [e for e in c.outcome['document_effects'] if e['open_amount_before'] != e['open_amount_after']]
    if not changed:
        return _v(NA, 'NO_APPLICATIONS_CHANGED')
    # No application facts (invoice open amounts) are captured for any operation this simulator supports.
    raise MissingFact('application bounds facts (no supported operation changes applications)')


def p_dup_01(c: Context):
    creates = [e for e in c.outcome['document_effects'] if e['effect'] == 'CREATE']
    if not creates:
        return _v(NA, 'NO_NEW_DOCUMENTS_OR_SETTLEMENTS')
    facts = {'processor_ref': c.op['processor_ref'], 'payout_id': c.op['payout_id']}
    if c.snap.payout_settlement is not None:
        return _v(FAIL, 'SETTLEMENT_SOURCE_REFERENCE_ALREADY_ACTIVE', {**facts, 'existing_settlement_document_id': c.snap.payout_settlement.document_id})
    ids = [i['document_id'] for i in c.op['items']]
    if len(set(ids)) != len(ids):
        return _v(FAIL, 'ITEM_REPEATED_IN_SETTLEMENT', facts)
    included = {i.item_document_id for i in c.snap.active_inclusions}
    settled = sorted(d for d in ids if d in included or (d in c.payments and c.payments[d].status != 'RECEIVED'))
    if settled:
        return _v(FAIL, 'ITEM_IN_ANOTHER_SETTLEMENT', {**facts, 'items': settled}, c.payment_evidence())
    missing = [d for d in ids if d not in c.payments]
    if missing:
        raise MissingFact(f'payment facts for {missing}')
    return _v(PASS, 'NO_DUPLICATE_SETTLEMENT_OR_ITEM', facts)


def p_fee_01(c: Context):
    fee_role = c.roles.get('PROCESSOR_FEE_EXPENSE')
    fee_lines = [l for l in c.lines if fee_role and l['account_id'] == fee_role.account_id]
    recorded = sum((c.signed(l).value for l in fee_lines), Decimal(0))
    if recorded == 0 and c.money(c.op['fee']).value == 0:
        return _v(NA, 'NO_FEE_RECORDED')
    if c.statement is None:
        return _v(INSUFFICIENT, 'SETTLEMENT_STATEMENT_MISSING')
    try:
        expected = c.statement_money('gross') - c.statement_money('net')
    except DomainError:
        return _v(FAIL, 'STATEMENT_NOT_COMPARABLE', {'statement_currency': c.statement['currency']}, c.statement_evidence())
    facts = {'recorded_fee': Money(recorded, c.currency).wire(), 'statement_gross_minus_net': expected.wire()}
    if Money(recorded, c.currency) == expected:
        return _v(PASS, 'FEE_EQUALS_STATEMENT_GROSS_MINUS_NET', facts, c.statement_evidence())
    return _v(FAIL, 'FEE_DIFFERS_FROM_STATEMENT', facts, c.statement_evidence())


def p_per_01(c: Context):
    when = c.op['payout_date']
    covering = [p for p in c.snap.periods if p.start_date <= when < p.end_date]
    if len(covering) != 1:
        raise MissingFact(f'period covering {when}')
    facts = {'effective_date': when, 'period_start': covering[0].start_date, 'period_status': covering[0].status}
    return _v(FAIL, 'LINE_IN_CLOSED_PERIOD', facts) if covering[0].status == 'CLOSED' else _v(PASS, 'LINES_IN_OPEN_PERIOD', facts)


def p_evd_01(c: Context):
    # §9.2: RecordSettlement requires the settlement statement as evidence; §6.2: INSUFFICIENT when missing or not SOURCE.
    s = c.statement
    if s is None:
        return _v(INSUFFICIENT, 'REQUIRED_EVIDENCE_MISSING', {'required': 'settlement statement'})
    if s['provenance'] != 'SOURCE':
        return _v(INSUFFICIENT, 'REQUIRED_EVIDENCE_NOT_SOURCE', {'required': 'settlement statement', 'provenance': s['provenance']},
                  c.statement_evidence())
    return _v(PASS, 'REQUIRED_EVIDENCE_PRESENT_WITH_SOURCE_PROVENANCE', {'provenance': s['provenance']}, c.statement_evidence())


def p_sub_01(c: Context):
    ar = c.roles.get('AR_CONTROL')
    if ar is None or ar.account_id not in {l['account_id'] for l in c.lines}:
        return _v(NA, 'AR_NOT_TOUCHED')
    raise MissingFact('customer subledger post-state (no supported operation touches AR)')


def p_snp_01(c: Context):
    if c.ledger_source == 'NATIVE':
        return _v(NA, 'NATIVE_LEDGER')
    if c.reconciliation != 'RECONCILED':
        return _v(INSUFFICIENT, 'PROVIDER_SNAPSHOT_NOT_RECONCILED', {'reconciliation': c.reconciliation})
    return _v(PASS, 'PROVIDER_SNAPSHOT_RECONCILED')


def p_stl_01(c: Context):
    s = c.statement
    if s is None:
        return _v(INSUFFICIENT, 'SETTLEMENT_STATEMENT_MISSING')
    try:
        stated = {k: c.statement_money(k) for k in ('gross', 'fee', 'net')}
        stated_items = Counter((ref, kind, Money.parse(amount, c.currency).text()) for _, ref, kind, amount in s['items'])
    except DomainError:
        return _v(FAIL, 'STATEMENT_NOT_COMPARABLE', {'statement_currency': s['currency']}, c.statement_evidence())
    proposed = {k: c.money(c.op[k]) for k in ('gross', 'fee', 'net')}
    proposed_items = Counter()
    for item in c.op['items']:
        pay = c.payments.get(item['document_id'])
        if pay is None:
            raise MissingFact(f"payment identity of {item['document_id']}")
        proposed_items[(pay.processor_item_id, item['kind'], c.money(item['amount']).text())] += 1
    differs = [k for k in ('gross', 'fee', 'net') if stated[k] != proposed[k]]
    if stated_items != proposed_items:
        differs.append('items')
    facts = {**{f'statement_{k}': stated[k].wire() for k in stated}, **{f'proposed_{k}': proposed[k].wire() for k in proposed},
             'differs': differs,
             'identity_matches': [s['processor_ref'], s['payout_id'], s['payout_date']] == [c.op['processor_ref'], c.op['payout_id'], c.op['payout_date']]}
    if differs:
        facts['statement_items'] = [list(k) + [str(n)] for k, n in sorted(stated_items.items())]
        facts['proposed_items'] = [list(k) + [str(n)] for k, n in sorted(proposed_items.items())]
        return _v(FAIL, 'STATEMENT_DISAGREES', facts, c.statement_evidence() + c.payment_evidence())
    return _v(PASS, 'STATEMENT_AGREES', facts, c.statement_evidence() + c.payment_evidence())


ALL = frozenset(OUTCOMES)
REGISTRY = (
    Policy('P-BAL-01', '1.0', 'Double entry', '2A §6.2; I-01', 'SIMULATION', frozenset({PASS, FAIL}),
           ('predicted postings',), 'Always', 'FAIL unless Σ debits = Σ credits > 0 for the simulated entry', p_bal_01),
    Policy('P-ACC-01', '1.0', 'Account validity', '2A §6.2; §2.2', 'SIMULATION', frozenset({PASS, FAIL}),
           ('predicted postings', 'role bindings: active, type, requires_party'), 'Always',
           'FAIL on an unbound or inactive account, a role bound to a disallowed account type, or a party-required line without a customer', p_acc_01),
    Policy('P-CUR-01', '1.0', 'Currency and scale', '2A §6.2; I-02, I-03', 'SIMULATION', frozenset({PASS, FAIL}),
           ('predicted postings', 'operation amounts', 'ledger currency and exponent'), 'Always',
           'FAIL on any amount not in the functional currency or over-precise', p_cur_01),
    Policy('P-REV-01', '1.0', 'Revenue source', '2A §6.2', 'SIMULATION', ALL,
           ('predicted postings', 'account types'), 'Applicable when a REVENUE-type account is touched',
           'RecordSettlement: NOT_APPLICABLE unless a revenue account is touched, then FAIL (a settlement entry touching revenue). '
           'The invoice-source PASS and manual-adjustment INSUFFICIENT branches belong to operations this simulator does not support', p_rev_01),
    Policy('P-CLR-01', '1.0', 'Clearing closure', '2A §6.2; §19.3 postconditions', 'SIMULATION', ALL,
           ('predicted postings', 'item payments\' posting entries', 'cited statement'), 'Applicable when the clearing account is touched',
           'INSUFFICIENT without a statement; FAIL unless the items\' clearing movements plus the settlement\'s clearing movement net to zero', p_clr_01),
    Policy('P-CLR-02', '1.0', 'Clearing sign', '2A §6.2', 'SIMULATION', frozenset({PASS, FAIL, NA}),
           ('simulated post-state clearing balance',), 'Applicable when the clearing account is touched',
           'FAIL when Processor Clearing ends with a credit balance', p_clr_02),
    Policy('P-APP-01', '1.0', 'Application bounds', '2A §6.2; I-06, I-07', 'SIMULATION', frozenset({PASS, FAIL, NA}),
           ('simulated document effects',), 'Applicable when applications change',
           'NOT_APPLICABLE when no open amount changes (RecordSettlement changes none); any change raises MissingFact (no application facts)', p_app_01),
    Policy('P-DUP-01', '1.0', 'Duplicate posting', '2A §6.2; I-08; §19.3', 'SIMULATION', ALL,
           ('payout identity settlement', 'active inclusions', 'payment versions', 'document effects'), 'Applicable when documents or settlements are created',
           'FAIL when the payout source reference is already active, an item repeats, or an item is in another settlement', p_dup_01),
    Policy('P-FEE-01', '1.0', 'Fee consistency', '2A §6.2; DC-18', 'SIMULATION', ALL,
           ('predicted fee posting', 'operation fee', 'cited statement gross and net'), 'Applicable when a fee is recorded',
           'INSUFFICIENT without a statement; FAIL unless the recorded fee equals statement gross − statement net', p_fee_01),
    Policy('P-PER-01', '1.0', 'Closed period', '2A §6.2; I-12', 'SIMULATION', frozenset({PASS, FAIL}),
           ('payout date', 'period statuses'), 'Always', 'FAIL when the simulated lines are dated in a CLOSED period', p_per_01),
    Policy('P-EVD-01', '1.0', 'Evidence completeness', '2A §6.2; §9.2', 'SIMULATION', frozenset({PASS, INSUFFICIENT, NA}),
           ('cited statement and its provenance',), 'Applicable: RecordSettlement requires a settlement statement',
           'INSUFFICIENT when the statement is missing or not SOURCE provenance; PASS only with SOURCE provenance', p_evd_01),
    Policy('P-SUB-01', '1.0', 'Subledger integrity', '2A §6.2; I-05', 'SIMULATION', frozenset({PASS, FAIL, NA}),
           ('predicted postings',), 'Applicable when AR is touched',
           'NOT_APPLICABLE when AR is not touched (RecordSettlement never touches AR); touching AR raises MissingFact (no subledger post-state)', p_sub_01),
    Policy('P-SNP-01', '1.0', 'Snapshot eligibility', '2A §6.2; §1.1', 'SIMULATION', frozenset({PASS, INSUFFICIENT, NA}),
           ('ledger source', 'snapshot reconciliation'), 'Not applicable to NATIVE ledgers',
           'NOT_APPLICABLE for NATIVE; INSUFFICIENT for an unreconciled provider snapshot', p_snp_01),
    Policy('P-AUT-01', '1.0', 'Segregation of duties', '2A §6.2; §12.2', 'APPROVAL', frozenset({PASS, FAIL}),
           ('approver, submitter, editors, executor',), 'Evaluated at approval and execution, not in simulation',
           'Deferred: no verdict in simulation (contract). Maker and editors are recorded on revisions for that stage', None),
    Policy('P-STL-01', '1.0', 'Statement agreement', '2A §19.3, §19.6 (sp1-policies/1.1)', 'SIMULATION', frozenset({PASS, FAIL, INSUFFICIENT}),
           ('operation gross, fee, net and items', 'cited statement gross, fee, net and items', 'item payment identities'),
           'Applicable to every RecordSettlement',
           'INSUFFICIENT without a statement; FAIL when gross, fee, net or the item list (processor item id, kind, amount) differ', p_stl_01),
)
CONTRACTED_IDS = ('P-BAL-01', 'P-ACC-01', 'P-CUR-01', 'P-REV-01', 'P-CLR-01', 'P-CLR-02', 'P-APP-01', 'P-DUP-01', 'P-FEE-01',
                  'P-PER-01', 'P-EVD-01', 'P-SUB-01', 'P-SNP-01', 'P-AUT-01', 'P-STL-01')


def aggregate(verdicts) -> str:
    outcomes = [v['outcome'] for v in verdicts]
    if FAIL in outcomes:
        return 'BLOCKED_FAIL'
    if INSUFFICIENT in outcomes:
        return 'BLOCKED_INSUFFICIENT'
    if PASS in outcomes:
        return 'CLEAR'
    # Only NOT_APPLICABLE: NOT_APPLICABLE never counts toward a pass (ADR-014). Unreachable for RecordSettlement (P-BAL-01 is
    # never NOT_APPLICABLE); refused rather than reported as CLEAR.
    raise PolicyContractViolation('no policy passed; NOT_APPLICABLE cannot make a result CLEAR')


def verify(bundle, outcome, snap, op) -> dict:
    """Evaluate every SIMULATION-stage policy on frozen inputs and the simulated outcome; return the verification block."""
    context = Context(bundle, outcome, snap, op)
    verdicts, not_evaluable = [], []
    # P-CUR-01 first: when amounts are not in the functional currency and scale, policies that compute with amounts cannot
    # produce an honest outcome. They are then listed as not evaluable (never PASS or NOT_APPLICABLE); the P-CUR-01 FAIL
    # already blocks. An amount error without a P-CUR-01 FAIL is an inconsistency and fails closed.
    ordered = sorted((p for p in REGISTRY if p.stage == 'SIMULATION'), key=lambda p: p.id != 'P-CUR-01')
    currency_failed = False
    for policy in ordered:
        try:
            result, code, facts, evidence = policy.evaluate(context)
        except DomainError as error:
            if not currency_failed:
                raise MissingFact(f'{policy.id}: amount not evaluable ({error.code}) without a P-CUR-01 FAIL') from error
            not_evaluable.append({'policy_id': policy.id, 'policy_version': policy.version, 'reason': f'AMOUNT_NOT_EVALUABLE ({error.code}); P-CUR-01 FAIL'})
            continue
        if result not in policy.permitted:
            raise PolicyContractViolation(f'{policy.id} returned {result}; permitted: {sorted(policy.permitted)}')
        currency_failed = currency_failed or (policy.id == 'P-CUR-01' and result == FAIL)
        verdicts.append({'policy_id': policy.id, 'policy_version': policy.version, 'outcome': result, 'explanation_code': code,
                         'facts': facts, 'evidence': evidence})
    verdicts.sort(key=lambda v: v['policy_id'])
    agg = aggregate(verdicts)
    if not_evaluable and agg != 'BLOCKED_FAIL':
        raise PolicyContractViolation('policies not evaluable without a blocking FAIL')
    counts = Counter(v['outcome'] for v in verdicts)
    return {'verdicts': verdicts, 'aggregate': agg,
            'verification': {'status': 'EVALUATED', 'policy_set_version': POLICY_SET, 'policy_registry': REGISTRY_ID, 'profile': PROFILE_NAME,
                             'counts': {o: counts.get(o, 0) for o in OUTCOMES}, 'not_evaluable': not_evaluable,
                             'deferred': [{'policy_id': p.id, 'policy_version': p.version, 'stage': p.stage} for p in REGISTRY if p.stage != 'SIMULATION'],
                             'result_statement': f"{'verified' if agg == 'CLEAR' else 'blocked'} under profile {PROFILE_NAME}, policy set {POLICY_SET}",
                             'override': 'NOT_AVAILABLE (INSUFFICIENT_EVIDENCE overrides are not built)' if agg == 'BLOCKED_INSUFFICIENT' else None,
                             'certification': 'NONE: accounting semantics UNREVIEWED; a verdict is a computed check, not an accounting review'}}


def coverage() -> list:
    """The coverage table (docs/POLICY_VERIFICATION.md is generated from this and tested against it)."""
    return [{'policy_id': p.id, 'policy_version': p.version, 'title': p.title, 'section': p.section, 'stage': p.stage,
             'permitted': sorted(p.permitted), 'inputs': list(p.inputs), 'applicability': p.applicability, 'implemented': p.implemented}
            for p in REGISTRY]
