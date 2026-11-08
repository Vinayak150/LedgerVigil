"""SP-1 policy verification of native RecordSettlement from frozen simulation inputs (sp1-policies/1.1).

Owning contracts: Stage 2A §6.2 (the fourteen sp1-policies/1.0 controls and their permitted outcomes), §19.3/§19.6 (P-STL-01,
sp1-policies/1.1), §9.1 (typed rejections carry no verdicts), §10.1 step 6 (policies run on the simulated post-state), §10.2
(determinism); ADR-014 (versioned registry, aggregation, NOT_APPLICABLE never counts toward a pass); Stage 2B Verdict schema,
aggregate enum and Family 9 (409 when a pinned version is not installed).

Two kinds of test, kept apart and named:
* ISOLATED: `policies.verify` on a really captured bundle and its real simulated outcome, with one frozen fact altered in memory.
  Several of these FAIL conditions are unreachable end to end because an earlier typed precondition refuses the same bundle
  first; each such test also asserts that masking (the engine's REJECTED outcome) so the report cannot overstate reachability.
* END-TO-END: drafts created through the real API, simulated through the verified-session boundary (test-only route, as in
  test_simulation), stored in lv.simulation_run and read back.
Nothing relabels stored evidence: the one CLEAR result is an isolated, in-memory copy marked as such. Data is SYNTHETIC;
accounting is UNREVIEWED.
"""
import copy
import dataclasses
import json
from decimal import Decimal

import pytest

from tools.dev_db import connection
from ledgervigil.ledger.seed import seed_manual_journal, seed_processor_payment
from ledgervigil.simulation import engine, policies, versions
from ledgervigil.simulation.bundle import ledger_snapshot
from ledgervigil.simulation.canonical import canonical, digest
from test_proposal_drafts import (BASE, PROPOSAL_TABLES, counters, create, draft, fingerprint, problem, seed, settlement,  # noqa: F401
                                  signed_in, world, _payment, _statement)
from test_simulation import api, get_run, m, new_draft, settle, simulate, tb, _bundle  # noqa: F401

PASS, FAIL, INS, NA = 'PASS', 'FAIL', 'INSUFFICIENT_EVIDENCE', 'NOT_APPLICABLE'
# Stage 2A §6.2 columns ("Never" = not permitted) and §19.3 for P-STL-01. Written out independently of the registry.
CONTRACT_PERMITTED = {
    'P-BAL-01': {PASS, FAIL}, 'P-ACC-01': {PASS, FAIL}, 'P-CUR-01': {PASS, FAIL}, 'P-REV-01': {PASS, FAIL, INS, NA},
    'P-CLR-01': {PASS, FAIL, INS, NA}, 'P-CLR-02': {PASS, FAIL, NA}, 'P-APP-01': {PASS, FAIL, NA}, 'P-DUP-01': {PASS, FAIL, INS, NA},
    'P-FEE-01': {PASS, FAIL, INS, NA}, 'P-PER-01': {PASS, FAIL}, 'P-EVD-01': {PASS, INS, NA}, 'P-SUB-01': {PASS, FAIL, NA},
    'P-SNP-01': {PASS, INS, NA}, 'P-AUT-01': {PASS, FAIL}, 'P-STL-01': {PASS, FAIL, INS}}
BASELINE = {'P-ACC-01': PASS, 'P-APP-01': NA, 'P-BAL-01': PASS, 'P-CLR-01': PASS, 'P-CLR-02': PASS, 'P-CUR-01': PASS, 'P-DUP-01': PASS,
            'P-EVD-01': INS, 'P-FEE-01': PASS, 'P-PER-01': PASS, 'P-REV-01': NA, 'P-SNP-01': NA, 'P-STL-01': PASS, 'P-SUB-01': NA}
FINANCIAL_EXCLUDED = PROPOSAL_TABLES | {'lv.simulation_run'}


def outcomes(out):
    return {v['policy_id']: v['outcome'] for v in out['verdicts']}


def verdict(out, policy_id):
    [v] = [v for v in out['verdicts'] if v['policy_id'] == policy_id]
    return v


def frozen(database, api, world):
    """A really captured bundle and its stored, really simulated outcome (the baseline draft)."""
    a = signed_in(api, world, 'analyst')
    run = get_run(a, simulate(a, new_draft(a, world)).json()['id'])
    bundle = _bundle(database, run['id'])
    assert canonical(engine.evaluate(copy.deepcopy(bundle))) == canonical(run['outcome'])
    return bundle, run['outcome']


def check(bundle, outcome, snap=None, op=None):
    """ISOLATED: the registry on frozen facts (optionally altered); never touches the database."""
    snap = snap or ledger_snapshot(bundle['facts'])
    op = op or bundle['proposal']['operations'][0]
    out = copy.deepcopy(outcome)
    out.update(policies.verify(bundle, out, snap, op))
    return out


def replace_binding(snap, role, **changes):
    return dataclasses.replace(snap, bindings=tuple(dataclasses.replace(b, **changes) if b.role == role else b for b in snap.bindings))


# ------------------------------------------------------------------------------------------- A. registry and coverage
def test_registry_covers_every_contracted_policy_with_the_contracts_permitted_outcomes():
    assert [p.id for p in policies.REGISTRY] == list(policies.CONTRACTED_IDS) and len(policies.REGISTRY) == 15
    assert {p.id: set(p.permitted) for p in policies.REGISTRY} == CONTRACT_PERMITTED
    assert all(p.version == '1.0' for p in policies.REGISTRY) and policies.POLICY_SET == 'sp1-policies/1.1'
    deferred = [p for p in policies.REGISTRY if p.stage != 'SIMULATION']
    assert [(p.id, p.stage, p.evaluate) for p in deferred] == [('P-AUT-01', 'APPROVAL', None)]
    assert all(callable(p.evaluate) for p in policies.REGISTRY if p.stage == 'SIMULATION')
    rows = policies.coverage()
    assert all(r['inputs'] and r['applicability'] and r['implemented'] and r['section'] for r in rows)
    assert policies.REGISTRY_ID.startswith('sp1-policies/1.1+sha256:') and len(policies.REGISTRY_ID.split(':')[1]) == 64
    assert versions.installed()['policy_registry'] == policies.REGISTRY_ID


def test_the_coverage_document_is_generated_from_the_registry():
    from pathlib import Path
    from tools.policy_coverage import render
    assert (Path(__file__).parents[1] / 'docs' / 'POLICY_COVERAGE.md').read_text() == render()


def test_an_outcome_outside_the_contract_is_refused_and_nothing_is_recorded(database, api, world, monkeypatch):
    bundle, outcome = frozen(database, api, world)
    evd = next(p for p in policies.REGISTRY if p.id == 'P-EVD-01')
    patched = tuple(dataclasses.replace(p, evaluate=lambda c: ('FAIL', 'X', {}, [])) if p is evd else p for p in policies.REGISTRY)
    monkeypatch.setattr(policies, 'REGISTRY', patched)
    with pytest.raises(policies.PolicyContractViolation):
        check(bundle, outcome)
    with pytest.raises(engine.IntegrityFailure):
        engine.evaluate(copy.deepcopy(bundle))
    a = signed_in(api, world, 'analyst')
    with connection(database=database) as admin:
        before = admin.execute('SELECT count(*) FROM lv.simulation_run').fetchone()[0]
    assert 'nothing was recorded' in problem(simulate(a, new_draft(a, world)), 500, 'INTERNAL')['detail']
    with connection(database=database) as admin:
        assert admin.execute('SELECT count(*) FROM lv.simulation_run').fetchone()[0] == before


# ------------------------------------------------------------------------------------------- B. end-to-end verdicts
def test_end_to_end_baseline_is_BLOCKED_INSUFFICIENT_with_structured_traceable_verdicts(database, api, world):
    a = signed_in(api, world, 'analyst')
    run = get_run(a, simulate(a, new_draft(a, world)).json()['id'])
    out = run['outcome']
    assert run['status'] == 'COMPLETED' and out['format'] == 'lv-simulation-outcome/2'
    assert outcomes(out) == BASELINE and 'P-AUT-01' not in outcomes(out)
    for v in out['verdicts']:
        assert set(v) == {'policy_id', 'policy_version', 'outcome', 'explanation_code', 'facts', 'evidence'} and v['policy_version'] == '1.0'
    assert out['aggregate'] == 'BLOCKED_INSUFFICIENT'
    ver = out['verification']
    assert ver['status'] == 'EVALUATED' and ver['policy_set_version'] == 'sp1-policies/1.1' and ver['policy_registry'] == policies.REGISTRY_ID
    assert ver['counts'] == {PASS: 9, FAIL: 0, INS: 1, NA: 4}                  # NOT_APPLICABLE is reported apart, never as a pass
    assert ver['not_evaluable'] == []
    assert ver['deferred'] == [{'policy_id': 'P-AUT-01', 'policy_version': '1.0', 'stage': 'APPROVAL'}]
    assert ver['result_statement'] == 'blocked under profile SP-1, policy set sp1-policies/1.1' and 'compliant' not in json.dumps(out)
    assert ver['override'].startswith('NOT_AVAILABLE') and out['accounting_status'] == 'UNREVIEWED' and out['eligible_for_execution'] is False
    evd = verdict(out, 'P-EVD-01')
    assert (evd['explanation_code'], evd['facts']['provenance']) == ('REQUIRED_EVIDENCE_NOT_SOURCE', 'SYNTHETIC')
    stl = verdict(out, 'P-STL-01')
    statement = run['pinned_sources']['statement']
    refs = {(e['kind'], e['id']) for e in stl['evidence']}
    assert ('STAGED_DOCUMENT', statement['id']) in refs and ('SOURCE_RECORD', statement['source_record_id']) in refs
    assert {e['sha256'] for e in stl['evidence'] if e['kind'] == 'SOURCE_RECORD'} == {statement['source_record_sha256']}
    pay_versions = {e['id'] for e in stl['evidence'] if e['kind'] == 'DOCUMENT_VERSION'}
    assert pay_versions == {p['version_id'] for p in _bundle(database, run['id'])['facts']['payments']
                            if p['document_id'] in {str(world['p1']), str(world['p2'])}}
    assert stl['facts']['differs'] == [] and stl['facts']['identity_matches'] is True
    assert verdict(out, 'P-BAL-01')['facts'] == {'debits': m('52500.50'), 'credits': m('52500.50')}
    assert verdict(out, 'P-CLR-01')['facts']['statement_items_residual'] == m('0.00')
    assert run['snapshot']['policy_set_version'] == 'sp1-policies/1.1' and run['versions']['policy_registry'] == policies.REGISTRY_ID


def test_end_to_end_fee_one_minor_unit_off_the_statement_fails_P_STL_01_and_P_FEE_01_and_FAIL_outranks_INSUFFICIENT(database, api, world):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world, operations=[settlement(world, fee='1000.01', net='51500.49')])
    out = get_run(a, simulate(a, pid).json()['id'])['outcome']
    assert outcomes(out) == {**BASELINE, 'P-STL-01': FAIL, 'P-FEE-01': FAIL}
    stl, fee = verdict(out, 'P-STL-01'), verdict(out, 'P-FEE-01')
    assert (stl['explanation_code'], stl['facts']['differs']) == ('STATEMENT_DISAGREES', ['fee', 'net'])
    assert (stl['facts']['statement_fee'], stl['facts']['proposed_fee']) == (m('1000.00'), m('1000.01'))
    assert (fee['explanation_code'], fee['facts']) == ('FEE_DIFFERS_FROM_STATEMENT', {'recorded_fee': m('1000.01'), 'statement_gross_minus_net': m('1000.00')})
    assert out['aggregate'] == 'BLOCKED_FAIL' and out['verification']['counts'][INS] == 1     # INSUFFICIENT present, FAIL wins
    assert out['verification']['override'] is None


def test_end_to_end_item_list_disagreement_with_equal_totals_fails_P_STL_01_only(database, api, world, seed):
    cfg = world['l1']
    p3 = _payment(seed, cfg, 'PMT-2003', '2500.50')
    st = _statement(database, cfg, 'STL-3005', [('PMT-2001', '50000.00'), ('PMT-2003', '2500.50')], '52500.50', '1000.00', '51500.50')
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world, operations=[settlement(world, statement=st['staged_document_id'], payout='STL-3005')], evidence_refs=[])
    out = get_run(a, simulate(a, pid).json()['id'])['outcome']
    assert out['status'] == 'COMPLETED', out
    assert outcomes(out) == {**BASELINE, 'P-STL-01': FAIL}
    stl = verdict(out, 'P-STL-01')
    assert stl['facts']['differs'] == ['items'] and stl['facts']['identity_matches'] is True
    assert ['PMT-2003', 'PAYMENT', '2500.50', '1'] in stl['facts']['statement_items'] and ['PMT-2002', 'PAYMENT', '2500.50', '1'] in stl['facts']['proposed_items']
    assert out['aggregate'] == 'BLOCKED_FAIL'
    assert p3  # PMT-2003 exists but is not in the draft


def test_end_to_end_clearing_that_would_end_in_credit_fails_P_CLR_02(database, api, world, seed):
    cfg = world['l1']
    seed_manual_journal(seed, world['A'], cfg['ledger'], {
        'source_ref': 'clearing-credit-1', 'effective_date': '2026-10-06', 'description': 'SYNTHETIC journal crediting clearing',
        'lines': [{'account_id': cfg['chart']['BANK_OPERATING'], 'side': 'D', 'amount': '0.01'},
                  {'account_id': cfg['chart']['PROCESSOR_CLEARING'], 'side': 'C', 'amount': '0.01'}]})
    a = signed_in(api, world, 'analyst')
    run = get_run(a, simulate(a, new_draft(a, world)).json()['id'])
    out = run['outcome']
    if out['status'] == 'REJECTED':     # reported honestly if an earlier layer masks it
        pytest.fail(f"P-CLR-02 end-to-end masked by {out['rejection']}")
    assert verdict(out, 'P-CLR-02')['outcome'] == FAIL and verdict(out, 'P-CLR-02')['facts'] == {'clearing_balance_after': m('-0.01')}
    assert verdict(out, 'P-CLR-01')['outcome'] == PASS          # the settled items' own clearing movements still net to zero
    assert out['aggregate'] == 'BLOCKED_FAIL'


@pytest.mark.parametrize('amounts', [('0.01', '0.01', '0.01'), ('4999999999999999.99', '0.01', '0.01')])
def test_end_to_end_exact_decimal_boundaries(database, api, world, seed, amounts):
    """Equality at 1 minor unit and at 18 significant digits passes; one minor unit away fails (no float, no rounding)."""
    cfg = world['l2']
    x, y, fee = amounts
    ids = [seed_processor_payment(seed, cfg['tenant'], cfg['ledger'], {'processor_ref': 'processor-1', 'processor_item_id': ref,
           'party_id': cfg['party'], 'currency': 'INR', 'amount': amt, 'received_date': '2026-10-10', 'description': 'SYNTHETIC'})['document_id']
           for ref, amt in (('PMT-B1', x), ('PMT-B2', y))]
    gross = format(Decimal(x) + Decimal(y), 'f')
    net = format(Decimal(gross) - Decimal(fee), 'f')
    st = _statement(database, cfg, 'STL-BND', [('PMT-B1', x), ('PMT-B2', y)], gross, fee, net)
    a = signed_in(api, world, 'analyst')

    def run(fee_value, net_value):
        body = draft(world, ledger=cfg['ledger'], operations=[settlement(world, (ids[0], x), (ids[1], y), statement=st['staged_document_id'],
                                                                        gross=gross, fee=fee_value, net=net_value, payout='STL-BND')], evidence_refs=[])
        r = create(a, body)
        assert r.status_code == 201, r.text
        return get_run(a, simulate(a, r.json()['id']).json()['id'])['outcome']
    exact = run(fee, net)
    assert outcomes(exact)['P-STL-01'] == PASS and outcomes(exact)['P-FEE-01'] == PASS and outcomes(exact)['P-CLR-01'] == PASS
    assert verdict(exact, 'P-STL-01')['facts']['statement_gross'] == m(gross)
    off = run(format(Decimal(fee) - Decimal('0.01'), 'f'), format(Decimal(net) + Decimal('0.01'), 'f'))
    if Decimal(fee) - Decimal('0.01') == 0:
        # fee 0.00 and no fee line: P-FEE-01 is NOT_APPLICABLE (no fee recorded); P-STL-01 still catches the disagreement.
        assert outcomes(off)['P-FEE-01'] == NA and outcomes(off)['P-STL-01'] == FAIL
    else:
        assert outcomes(off)['P-FEE-01'] == FAIL and outcomes(off)['P-STL-01'] == FAIL
    assert off['aggregate'] == 'BLOCKED_FAIL'


def test_end_to_end_typed_rejections_carry_no_verdicts(database, api, world, seed):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    settle(seed, world['l1'], 'STL-3001', [(world['p1'], '50000.00'), (world['p2'], '2500.50')], '52500.50', '1000.00', '51500.50')
    out = get_run(a, simulate(a, pid).json()['id'])['outcome']
    assert out['status'] == 'REJECTED' and out['rejection']['reason'] == 'DUPLICATE_SETTLEMENT'
    assert out['verdicts'] == [] and out['aggregate'] is None and out['verification']['status'] == 'NOT_REACHED'
    assert out['format'] == 'lv-simulation-outcome/2'


# ------------------------------------------------------------------------------------------- C. isolated policy tests
def test_isolated_revenue_misuse_fails_P_REV_01(database, api, world):
    bundle, outcome = frozen(database, api, world)
    assert outcomes(check(bundle, outcome))['P-REV-01'] == NA
    rev = world['l1']['chart']['REVENUE_DEFAULT']
    for side in ('C', 'D'):
        altered = copy.deepcopy(outcome)
        altered['predicted_postings'].append({'line_no': 4, 'account_id': rev, 'role': 'REVENUE_DEFAULT', 'account_code': '4000', 'side': side,
                                              'amount': m('5.00'), 'party_id': None, 'document_id': None})
        out = check(bundle, altered)
        assert verdict(out, 'P-REV-01')['outcome'] == FAIL and verdict(out, 'P-REV-01')['explanation_code'] == 'SETTLEMENT_ENTRY_TOUCHES_REVENUE'
        assert verdict(out, 'P-BAL-01')['outcome'] == FAIL and out['aggregate'] == 'BLOCKED_FAIL'
    # Reachability: no RecordSettlement input reaches a revenue account; the production posting rule fixes the three roles.


def test_isolated_account_validity_fails_P_ACC_01_and_is_masked_end_to_end_by_kernel_preconditions(database, api, world):
    bundle, outcome = frozen(database, api, world)
    snap = ledger_snapshot(bundle['facts'])
    for changed, code in ((replace_binding(snap, 'PROCESSOR_FEE_EXPENSE', active=False), 'ACCOUNT_INACTIVE'),
                          (replace_binding(snap, 'BANK_OPERATING', type='LIABILITY'), 'ACCOUNT_TYPE_NOT_ALLOWED_FOR_ROLE')):
        assert verdict(check(bundle, outcome, snap=changed), 'P-ACC-01')['explanation_code'] == code
    unbound = copy.deepcopy(outcome); unbound['predicted_postings'][0]['account_id'] = '00000000-0000-0000-0000-000000000000'
    assert verdict(check(bundle, unbound), 'P-ACC-01')['explanation_code'] == 'ACCOUNT_UNBOUND'
    # Masking: the same altered facts as a captured bundle are refused by the kernel precondition before verification.
    inactive = copy.deepcopy(bundle)
    for b in inactive['facts']['bindings']:
        if b['role'] == 'PROCESSOR_FEE_EXPENSE':
            b['active'] = False
    rejected = engine.evaluate(inactive)
    assert rejected['status'] == 'REJECTED' and rejected['rejection']['reason'] == 'INACTIVE_ACCOUNT' and rejected['verdicts'] == []


def test_isolated_control_line_without_customer_fails_P_ACC_01(database, api, world):
    bundle, outcome = frozen(database, api, world)
    snap = replace_binding(ledger_snapshot(bundle['facts']), 'BANK_OPERATING', requires_party=True)
    out = check(bundle, outcome, snap=snap)
    assert verdict(out, 'P-ACC-01')['explanation_code'] == 'CONTROL_ACCOUNT_LINE_WITHOUT_CUSTOMER'


def test_isolated_currency_and_scale_fail_P_CUR_01(database, api, world):
    bundle, outcome = frozen(database, api, world)
    usd = copy.deepcopy(outcome); usd['predicted_postings'][1]['amount'] = {'amount': '1000.00', 'currency': 'USD'}
    out = check(bundle, usd)
    assert verdict(out, 'P-CUR-01')['explanation_code'] == 'AMOUNT_NOT_IN_FUNCTIONAL_CURRENCY' and out['aggregate'] == 'BLOCKED_FAIL'
    # Amount-dependent policies are listed as not evaluable, never PASS or NOT_APPLICABLE.
    skipped = {n['policy_id'] for n in out['verification']['not_evaluable']}
    assert 'P-BAL-01' in skipped and not skipped & set(outcomes(out))
    precise = copy.deepcopy(bundle); precise['proposal']['operations'][0]['fee'] = {'amount': '1000.001', 'currency': 'INR'}
    out = check(precise, outcome, op=precise['proposal']['operations'][0])
    assert verdict(out, 'P-CUR-01')['explanation_code'] == 'AMOUNT_OVER_PRECISE'
    # Masking: the engine refuses a foreign-currency operation amount as CURRENCY_MISMATCH before verification.
    foreign = copy.deepcopy(bundle); foreign['proposal']['operations'][0]['fee']['currency'] = 'USD'
    assert engine.evaluate(foreign)['rejection']['code'] == 'CURRENCY_MISMATCH'


def test_isolated_closed_period_fails_P_PER_01_and_a_missing_period_fails_closed(database, api, world):
    bundle, outcome = frozen(database, api, world)
    snap = ledger_snapshot(bundle['facts'])
    closed = dataclasses.replace(snap, periods=tuple(dataclasses.replace(p, status='CLOSED') for p in snap.periods))
    v = verdict(check(bundle, outcome, snap=closed), 'P-PER-01')
    assert (v['outcome'], v['explanation_code'], v['facts']['period_status']) == (FAIL, 'LINE_IN_CLOSED_PERIOD', 'CLOSED')
    with pytest.raises(policies.MissingFact):
        check(bundle, outcome, snap=dataclasses.replace(snap, periods=()))
    masked = copy.deepcopy(bundle)
    for p in masked['facts']['periods']:
        p['status'] = 'CLOSED'
    assert engine.evaluate(masked)['rejection']['reason'] == 'PERIOD_CLOSED'


def test_isolated_duplicates_fail_P_DUP_01(database, api, world, seed):
    bundle, outcome = frozen(database, api, world)
    snap = ledger_snapshot(bundle['facts'])
    pay = snap.payments[0]
    from ledgervigil.kernel_adapter.types import ActiveInclusion, SettlementRef
    existing = SettlementRef(document_id='s-1', version=1, processor_ref='processor-1', payout_id='STL-3001', payout_date='2026-10-14',
                             gross=Decimal('1.00'), fee=Decimal('0.00'), net=Decimal('1.00'), status='RECORDED', seq=1)
    cases = [(dataclasses.replace(snap, payout_settlement=existing), 'SETTLEMENT_SOURCE_REFERENCE_ALREADY_ACTIVE'),
             (dataclasses.replace(snap, active_inclusions=(ActiveInclusion(item_document_id=pay.document_id, settlement_document_id='s-2',
                                  settlement_version=1, processor_ref='processor-1', payout_id='OTHER', last_seq=1),)), 'ITEM_IN_ANOTHER_SETTLEMENT'),
             (dataclasses.replace(snap, payments=tuple(dataclasses.replace(p, status='SETTLED') if p is pay else p for p in snap.payments)),
              'ITEM_IN_ANOTHER_SETTLEMENT')]
    for changed, code in cases:
        v = verdict(check(bundle, outcome, snap=changed), 'P-DUP-01')
        assert (v['outcome'], v['explanation_code']) == (FAIL, code)
    repeated = copy.deepcopy(bundle['proposal']['operations'][0]); repeated['items'].append(repeated['items'][0])
    assert verdict(check(bundle, outcome, op=repeated), 'P-DUP-01')['explanation_code'] == 'ITEM_REPEATED_IN_SETTLEMENT'
    no_create = copy.deepcopy(outcome); no_create['document_effects'] = [e for e in no_create['document_effects'] if e['effect'] != 'CREATE']
    assert verdict(check(bundle, no_create), 'P-DUP-01')['outcome'] == NA


def test_isolated_evidence_provenance_and_missing_statement(database, api, world):
    bundle, outcome = frozen(database, api, world)
    missing = copy.deepcopy(bundle); missing['pinned_sources']['statement'] = None
    out = check(missing, outcome)
    for pid, code in (('P-EVD-01', 'REQUIRED_EVIDENCE_MISSING'), ('P-STL-01', 'SETTLEMENT_STATEMENT_MISSING'),
                      ('P-FEE-01', 'SETTLEMENT_STATEMENT_MISSING'), ('P-CLR-01', 'SETTLEMENT_STATEMENT_MISSING')):
        assert (verdict(out, pid)['outcome'], verdict(out, pid)['explanation_code']) == (INS, code)
    assert out['aggregate'] == 'BLOCKED_INSUFFICIENT'
    for provenance in ('SYNTHETIC', 'DERIVED', 'MANUAL'):
        other = copy.deepcopy(bundle); other['pinned_sources']['statement']['provenance'] = provenance
        assert verdict(check(other, outcome), 'P-EVD-01')['outcome'] == INS
    # ISOLATED, in memory only: the rule's PASS branch. Stored evidence is never relabeled (asserted below).
    source = copy.deepcopy(bundle); source['pinned_sources']['statement']['provenance'] = 'SOURCE'
    out = check(source, outcome)
    assert verdict(out, 'P-EVD-01')['outcome'] == PASS and out['aggregate'] == 'CLEAR'
    assert out['verification']['result_statement'] == 'verified under profile SP-1, policy set sp1-policies/1.1'
    assert out['verification']['counts'] == {PASS: 10, FAIL: 0, INS: 0, NA: 4} and out['accounting_status'] == 'UNREVIEWED'
    with connection(database=database) as admin:
        assert admin.execute('SELECT provenance::text FROM lv.staged_document WHERE id = %s', (world['s1'],)).fetchone()[0] == 'SYNTHETIC'


def test_isolated_clearing_not_relieved_fails_P_CLR_01(database, api, world):
    bundle, outcome = frozen(database, api, world)
    altered = copy.deepcopy(outcome)
    line = next(l for l in altered['predicted_postings'] if l['role'] == 'PROCESSOR_CLEARING')
    line['amount'] = m('52500.49')
    v = verdict(check(bundle, altered), 'P-CLR-01')
    assert (v['outcome'], v['explanation_code'], v['facts']['statement_items_residual']) == (FAIL, 'CLEARING_NOT_RELIEVED_FOR_SETTLED_ITEMS', m('0.01'))
    reversed_sign = copy.deepcopy(outcome)
    next(l for l in reversed_sign['predicted_postings'] if l['role'] == 'PROCESSOR_CLEARING')['side'] = 'D'
    assert verdict(check(bundle, reversed_sign), 'P-CLR-01')['outcome'] == FAIL
    untouched = copy.deepcopy(outcome)
    untouched['predicted_postings'] = [l for l in untouched['predicted_postings'] if l['role'] != 'PROCESSOR_CLEARING']
    out = check(bundle, untouched)
    assert outcomes(out)['P-CLR-01'] == NA and outcomes(out)['P-CLR-02'] == NA


def test_isolated_snapshot_eligibility_for_provider_ledgers(database, api, world):
    bundle, outcome = frozen(database, api, world)
    qbo = copy.deepcopy(bundle); qbo['ledger']['source'] = 'QBO'; qbo['snapshot']['reconciliation'] = 'NOT_RECONCILED'
    assert verdict(check(qbo, outcome), 'P-SNP-01')['outcome'] == INS
    qbo['snapshot']['reconciliation'] = 'RECONCILED'
    assert verdict(check(qbo, outcome), 'P-SNP-01')['outcome'] == PASS
    qbo['ledger']['purpose'] = 'SANDBOX'
    assert engine.evaluate(qbo)['rejection']['code'] == 'CAPABILITY_UNAVAILABLE'      # masked: no provider simulation


def test_isolated_policies_without_facts_fail_closed_rather_than_pass_or_not_applicable(database, api, world):
    bundle, outcome = frozen(database, api, world)
    ar = copy.deepcopy(outcome)
    ar['predicted_postings'].append({'line_no': 4, 'account_id': world['l1']['chart']['AR_CONTROL'], 'role': 'AR_CONTROL', 'account_code': '1100',
                                     'side': 'D', 'amount': m('0.00'), 'party_id': 'x', 'document_id': None})
    with pytest.raises(policies.MissingFact, match='subledger'):
        check(bundle, ar)
    app = copy.deepcopy(outcome); app['document_effects'][1]['open_amount_after'] = m('0.00')
    with pytest.raises(policies.MissingFact, match='application'):
        check(bundle, app)
    with pytest.raises(policies.PolicyContractViolation):
        policies.aggregate([{'outcome': NA}, {'outcome': NA}])


def test_aggregation_precedence_is_ADR_014():
    v = lambda *o: [{'outcome': x} for x in o]  # noqa: E731
    assert policies.aggregate(v(PASS, NA, INS, FAIL)) == 'BLOCKED_FAIL'
    assert policies.aggregate(v(FAIL, PASS)) == 'BLOCKED_FAIL'
    assert policies.aggregate(v(PASS, NA, INS)) == 'BLOCKED_INSUFFICIENT'
    assert policies.aggregate(v(PASS, NA, NA)) == 'CLEAR'
    assert policies.aggregate(v(PASS)) == 'CLEAR'
    with pytest.raises(policies.PolicyContractViolation):
        policies.aggregate(v(NA))


def test_kernel_diagnostics_are_not_verdicts(database, api, world):
    bundle, outcome = frozen(database, api, world)
    garbled = copy.deepcopy(outcome)
    garbled['calculation']['kernel_diagnostics'] = [{'id': 'E-ANY', 'verdict': 'FAIL'}]
    garbled['calculation']['statement_observation'] = {'verdict': 'FAIL'}
    assert check(bundle, garbled)['verdicts'] == check(bundle, outcome)['verdicts']


# ------------------------------------------------------------------------------------------- D. replay, versions, old runs
def test_verification_is_deterministic_and_replays_from_frozen_inputs_only(database, api, world, seed):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    first, second = simulate(a, pid).json(), simulate(a, pid).json()
    assert (first['input_hash'], first['result_hash']) == (second['input_hash'], second['result_hash'])
    stored = get_run(a, first['id'])
    settle(seed, world['l1'], 'STL-3001', [(world['p1'], '50000.00'), (world['p2'], '2500.50')], '52500.50', '1000.00', '51500.50')
    body = a.post(f"{BASE}/simulations/{first['id']}/replay").json()
    assert body['identical'] is True and body['reloaded_current_state'] is False
    replayed = engine.evaluate(_bundle(database, first['id']))
    assert replayed['verdicts'] == stored['outcome']['verdicts'] and outcomes(replayed)['P-DUP-01'] == PASS    # live duplicate not consulted
    assert get_run(a, first['id']) == stored


def test_replay_refuses_unavailable_policy_versions(database, api, world, monkeypatch):
    a = signed_in(api, world, 'analyst')
    run_id = simulate(a, new_draft(a, world)).json()['id']
    pinned = versions.SIMULATOR
    for key, value in (('policy_set', 'sp1-policies/1.2'), ('policy_registry', 'sp1-policies/1.1+sha256:' + '0' * 64)):
        with monkeypatch.context() as patch:
            patch.setitem(versions.PROFILES, pinned, {**versions.PROFILES[pinned], key: value})
            detail = problem(a.post(f'{BASE}/simulations/{run_id}/replay'), 409, 'SIMULATION_VERSION_UNAVAILABLE')['detail']
            assert key in detail
    bundle = _bundle(database, run_id)
    del bundle['versions']['policy_registry']
    with pytest.raises(engine.VersionUnavailable):
        engine.evaluate(bundle)
    unknown = _bundle(database, run_id); unknown['versions']['simulator'] = 'lv-native-recordsettlement/99.0.0'
    with pytest.raises(engine.VersionUnavailable):
        engine.evaluate(unknown)
    assert a.post(f'{BASE}/simulations/{run_id}/replay').json()['identical'] is True


def test_runs_pinned_to_the_legacy_profile_stay_NOT_EVALUATED_and_unchanged(database, api, world, monkeypatch):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    with monkeypatch.context() as patch:
        patch.setattr(versions, 'SIMULATOR', versions.LEGACY_SIMULATOR)    # capture pins the 0.1.0 profile, as the foundation did
        legacy_id = simulate(a, pid).json()['id']
    with connection(database=database) as admin:
        row = admin.execute('SELECT input_canonical, input_hash, outcome_canonical, result_hash, simulator_version FROM lv.simulation_run '
                            'WHERE id = %s', (legacy_id,)).fetchone()
    legacy = json.loads(row[2])
    assert row[4] == 'lv-native-recordsettlement/0.1.0' and legacy['format'] == 'lv-simulation-outcome/1'
    assert legacy['verdicts'] == [] and legacy['aggregate'] is None and legacy['verification']['status'] == 'NOT_EVALUATED'
    assert json.loads(row[0])['snapshot']['policy_set_version'] is None and 'policy_registry' not in json.loads(row[0])['versions']
    new_id = simulate(a, pid).json()['id']                                 # a new run on the same draft is verified
    assert get_run(a, new_id)['outcome']['verification']['status'] == 'EVALUATED'
    body = a.post(f'{BASE}/simulations/{legacy_id}/replay').json()
    assert body['identical'] is True and body['replay_hash'] == row[3]       # legacy replay: same bytes, never retrofitted
    assert engine.evaluate(json.loads(row[0]))['verdicts'] == []
    with connection(database=database) as admin:
        assert admin.execute('SELECT input_canonical, input_hash, outcome_canonical, result_hash, simulator_version FROM lv.simulation_run '
                             'WHERE id = %s', (legacy_id,)).fetchone() == row
    assert digest(row[2]) == row[3]


# ------------------------------------------------------------------------------------------- E. financial preservation
def test_verification_changes_no_financial_table_proposal_counter_or_event(database, api, world, seed):
    a = signed_in(api, world, 'analyst')
    ok = new_draft(a, world)
    failing = new_draft(a, world, operations=[settlement(world, fee='1000.01', net='51500.49')])
    with connection(database=database) as admin:
        proposals = admin.execute('SELECT id, state::text, revision, version FROM lv.proposal ORDER BY id').fetchall()
        revisions = admin.execute('SELECT proposal_id, revision, content_hash FROM lv.proposal_revision ORDER BY 1, 2').fetchall()
    before, ctrs, tb1 = fingerprint(database, FINANCIAL_EXCLUDED), counters(database), tb(a, world)
    ids = [simulate(a, pid).json()['id'] for pid in (ok, failing, ok)]
    for run_id in ids:
        a.get(f'{BASE}/simulations/{run_id}')
        assert a.post(f'{BASE}/simulations/{run_id}/replay').json()['identical'] is True
    assert {get_run(a, i)['outcome']['aggregate'] for i in ids} == {'BLOCKED_INSUFFICIENT', 'BLOCKED_FAIL'}
    assert fingerprint(database, FINANCIAL_EXCLUDED) == before and counters(database) == ctrs and tb(a, world) == tb1
    with connection(database=database) as admin:
        assert admin.execute('SELECT id, state::text, revision, version FROM lv.proposal ORDER BY id').fetchall() == proposals
        assert admin.execute('SELECT proposal_id, revision, content_hash FROM lv.proposal_revision ORDER BY 1, 2').fetchall() == revisions
        assert {r[0] for r in admin.execute('SELECT state::text FROM lv.proposal WHERE id IN (%s, %s)', (ok, failing)).fetchall()} == {'DRAFT'}
