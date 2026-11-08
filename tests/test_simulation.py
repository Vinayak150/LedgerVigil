"""Deterministic native RecordSettlement simulation from pinned inputs (migration 0011) — real PostgreSQL, real sessions.

Owning contracts:
- Stage 2A §10.1 (load at the snapshot, validate in §9.1 order, apply, derive with production posting rules, compute deltas,
  hash), §10.2 (no clock, fixed Decimal context, canonical hashing, same inputs + versions -> same hash), §8.2 snapshot_ref,
  §9.1 read set computed by the server, §19.3 RecordSettlement preconditions and reasons, §5.1 posting rule;
- Stage 2B Part 3 SimulationRun / SnapshotRef / ReadSetEntry / AccountDelta / DocumentEffect, §4.2 PRECONDITION_FAILED
  reasons, §7.3 (synchronous runs are COMPLETED or REJECTED, immutable), Family 9 (simulation writes only simulation rows;
  replay recomputes with the same inputs and versions and compares hashes, 409 when a pinned version is not installed),
  CT-10 (authoritative-table checksums and sequence unchanged), CT-14 (unsupported operations write nothing else).
Simulation is invoked through `simulation.service.simulate` behind the real verified-session boundary, mounted on a test-only
route (no public creation route exists in this milestone). Data is SYNTHETIC; accounting is UNREVIEWED.
"""
import copy
import json
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import APIRouter, Request
from psycopg.types.json import Jsonb

from tools.dev_db import connection
from tools.identity_admin import owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from ledgervigil.ledger.seed import seed_processor_payment, seed_processor_settlement
from ledgervigil.simulation import bundle as capture_module
from ledgervigil.simulation import engine, service, versions
from ledgervigil.simulation.canonical import canonical, digest
from period_close_support import admin_token, close_period
from test_proposal_drafts import (BASE, COOKIE, PROPOSAL_TABLES, client, counters, create, draft, fingerprint, money, problem,  # noqa: F401
                                  seed, settlement, signed_in, world, _statement)

SIM_TABLES = PROPOSAL_TABLES | {'lv.simulation_run'}


@pytest.fixture
def api(database):
    """The real application plus ONE test-only route that calls the simulation service behind the verified boundary."""
    application = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database))
    router = APIRouter()

    @router.post('/test/proposals/{proposal_id}/simulate')
    def run(proposal_id: UUID, request: Request, revision: int | None = None):
        return service.simulate(request, proposal_id, revision)
    application.include_router(router)
    yield application
    application.state.pool.close()


def simulate(c, pid, **params):
    return c.post(f'/test/proposals/{pid}/simulate', params=params)


def get_run(c, run_id):
    r = c.get(f'{BASE}/simulations/{run_id}')
    assert r.status_code == 200, r.text
    return r.json()


def new_draft(c, w, **overrides):
    r = create(c, draft(w, **overrides))
    assert r.status_code == 201, r.text
    return r.json()['id']


def runs(database):
    with connection(database=database) as admin:
        return admin.execute('SELECT count(*) FROM lv.simulation_run').fetchone()[0]


def settle(seed, cfg, payout, docs, gross, fee, net):
    return seed_processor_settlement(seed, cfg['tenant'], cfg['ledger'], {
        'processor_ref': 'processor-1', 'payout_id': payout, 'payout_date': '2026-10-14', 'currency': 'INR', 'gross': gross, 'fee': fee,
        'net': net, 'items': [{'document_id': str(d), 'kind': 'PAYMENT', 'amount': a} for d, a in docs],
        'description': 'SYNTHETIC typed processor settlement fixture'})


def m(a):
    return {'amount': a, 'currency': 'INR'}


def tb(c, w, key='l1'):
    return c.get(f"{BASE}/ledgers/{w[key]['ledger']}/trial-balance").content


# ------------------------------------------------------------------------------------------- A. the calculation
def test_a_draft_simulates_to_exact_postings_deltas_effects_and_read_set(database, api, world):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    r = simulate(a, pid)
    assert r.status_code == 200, r.text
    created = r.json()
    run = get_run(a, created['id'])
    assert run['status'] == 'COMPLETED' and run['proposal_id'] == pid and run['revision'] == 1
    assert (run['input_hash'], run['result_hash']) == (created['input_hash'], created['result_hash'])
    out = run['outcome']
    chart = world['l1']['chart']
    assert [(p['account_id'], p['role'], p['side'], p['amount']) for p in out['predicted_postings']] == [
        (chart['BANK_OPERATING'], 'BANK_OPERATING', 'D', m('51500.50')), (chart['PROCESSOR_FEE_EXPENSE'], 'PROCESSOR_FEE_EXPENSE', 'D', m('1000.00')),
        (chart['PROCESSOR_CLEARING'], 'PROCESSOR_CLEARING', 'C', m('52500.50'))]
    deltas = {d['role']: (d['before'], d['after'], d['delta']) for d in out['account_deltas']}
    assert deltas['BANK_OPERATING'] == (m('10.00'), m('51510.50'), m('51500.50'))      # the world's opening journal put 10.00 in the bank
    assert deltas['PROCESSOR_CLEARING'] == (m('52500.50'), m('0.00'), m('-52500.50'))
    assert deltas['PROCESSOR_FEE_EXPENSE'] == (m('0.00'), m('1000.00'), m('1000.00'))
    assert deltas['AR_CONTROL'] == (m('-52500.50'), m('-52500.50'), m('0.00'))
    effects = {e['document_id']: e for e in out['document_effects']}
    assert effects[None]['effect'] == 'CREATE' and effects[None]['status_after'] == 'RECORDED'
    for pay, amount in ((world['p1'], '50000.00'), (world['p2'], '2500.50')):
        e = effects[str(pay)]
        assert (e['effect'], e['version_before'], e['version_after'], e['status_before'], e['status_after']) == ('UPDATE_IN_PLACE', 1, 2, 'RECEIVED', 'SETTLED')
        assert e['open_amount_before'] == e['open_amount_after'] == m(amount)
    assert out['settlement_clearing_residual'] == m('0.00')
    # Calculation is not certification. Since sp1-policies/1.1 (tests/test_policies.py), new runs carry verdicts; the SYNTHETIC
    # statement keeps P-EVD-01 INSUFFICIENT, so the aggregate is BLOCKED_INSUFFICIENT, never CLEAR. Accounting stays UNREVIEWED.
    assert out['aggregate'] == 'BLOCKED_INSUFFICIENT' and out['verification']['status'] == 'EVALUATED'
    assert out['accounting_status'] == 'UNREVIEWED' and out['eligible_for_execution'] is False and run['accounting_status'] == 'UNREVIEWED'
    assert out['calculation']['label'].startswith('KERNEL DIAGNOSTICS') and out['calculation']['kernel_diagnostics']
    # Pinned inputs: snapshot_ref, server-computed read set, versions and sources.
    snap = run['snapshot']
    assert snap == {'ledger_id': str(world['l1']['ledger']), 'seq': snap['seq'], 'profile_version': 'sp1/1.0',
                    'posting_rule_version': 'sp1-posting/1.0', 'policy_set_version': 'sp1-policies/1.1', 'provider_watermark': None,
                    'reconciliation': 'NOT_REQUIRED'}
    with connection(database=database) as admin:
        assert snap['seq'] == admin.execute('SELECT last_seq FROM lv.ledger_counter WHERE ledger_id = %s', (world['l1']['ledger'],)).fetchone()[0]
        period = admin.execute('SELECT id::text, version FROM lv.fiscal_period WHERE ledger_id = %s', (world['l1']['ledger'],)).fetchone()
        accounts = admin.execute('SELECT a.id::text, a.version FROM lv.account a JOIN lv.account_role_binding b ON (b.tenant_id,b.ledger_id,b.account_id)=(a.tenant_id,a.ledger_id,a.id) WHERE a.ledger_id=%s', (world['l1']['ledger'],)).fetchall()
    assert run['read_set'] == sorted([{'entity_type': 'DOCUMENT', 'entity_id': str(world['p1']), 'version': 1},
                                      {'entity_type': 'DOCUMENT', 'entity_id': str(world['p2']), 'version': 1},
                                      {'entity_type': 'PERIOD', 'entity_id': period[0], 'version': period[1]}] + [{'entity_type':'ACCOUNT','entity_id':aid,'version':v} for aid,v in accounts],
                                     key=lambda e: (e['entity_type'], e['entity_id']))
    assert run['versions'] == versions.installed() and run['versions']['policy_set'] == 'sp1-policies/1.1'
    statement = run['pinned_sources']['statement']
    assert statement['id'] == str(world['s1']) and statement['gross'] == '52500.50' and statement['source_record_id'] == str(world['r1'])
    assert {(e['kind'], e['present']) for e in run['pinned_sources']['evidence']} == {('STAGED_DOCUMENT', True), ('SOURCE_RECORD', True)}


def test_identical_state_gives_identical_input_and_result_hashes(database, api, world):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    first, second = simulate(a, pid).json(), simulate(a, pid).json()
    assert first['id'] != second['id']
    assert (first['input_hash'], first['result_hash']) == (second['input_hash'], second['result_hash'])
    run = get_run(a, first['id'])
    with connection(database=database) as admin:
        text = admin.execute('SELECT input_canonical FROM lv.simulation_run WHERE id = %s', (first['id'],)).fetchone()[0]
    bundle = json.loads(text)
    assert canonical(bundle) == text and digest(text) == run['input_hash']
    outcome_a, outcome_b = engine.evaluate(copy.deepcopy(bundle)), engine.evaluate(copy.deepcopy(bundle))
    assert canonical(outcome_a) == canonical(outcome_b) and digest(canonical(outcome_a)) == run['result_hash']
    assert 'EXCLUDED_FROM_BUNDLE' == bundle['facts']['transaction_timestamp']


@pytest.mark.parametrize('amounts', [('0.01', '2500.50', '0.01'), ('9999999999.99', '0.01', '1234.56'),
                                     ('4999999999999999.99', '0.01', '0.01')])   # 18 significant digits: beyond binary64
def test_exact_decimals_survive_capture_calculation_and_hashing(database, api, world, seed, amounts):
    cfg = world['l2']
    x, y, fee = amounts
    d1 = seed_processor_payment(seed, cfg['tenant'], cfg['ledger'], {'processor_ref': 'processor-1', 'processor_item_id': 'PMT-D1',
        'party_id': cfg['party'], 'currency': 'INR', 'amount': x, 'received_date': '2026-10-10', 'description': 'SYNTHETIC'})['document_id']
    d2 = seed_processor_payment(seed, cfg['tenant'], cfg['ledger'], {'processor_ref': 'processor-1', 'processor_item_id': 'PMT-D2',
        'party_id': cfg['party'], 'currency': 'INR', 'amount': y, 'received_date': '2026-10-10', 'description': 'SYNTHETIC'})['document_id']
    from decimal import Decimal
    gross = format(Decimal(x) + Decimal(y), 'f')
    net = format(Decimal(gross) - Decimal(fee), 'f')
    st = _statement(database, cfg, 'STL-DEC', [('PMT-D1', x), ('PMT-D2', y)], gross, fee, net)
    a = signed_in(api, world, 'analyst')
    body = draft(world, ledger=cfg['ledger'], operations=[settlement(world, (d1, x), (d2, y), statement=st['staged_document_id'],
                                                                    gross=gross, fee=fee, net=net, payout='STL-DEC')], evidence_refs=[])
    r = create(a, body)
    assert r.status_code == 201, r.text
    out = get_run(a, simulate(a, r.json()['id']).json()['id'])['outcome']
    assert out['status'] == 'COMPLETED'
    assert [p['amount']['amount'] for p in out['predicted_postings']] == [net, fee, gross]
    deltas = {d['role']: d for d in out['account_deltas']}
    assert deltas['PROCESSOR_CLEARING']['delta'] == m('-' + gross)
    assert (deltas['BANK_OPERATING']['before'], deltas['BANK_OPERATING']['after'], deltas['BANK_OPERATING']['delta']) == (m('0.00'), m(net), m(net))
    assert deltas['PROCESSOR_FEE_EXPENSE']['delta'] == m(fee)


# ------------------------------------------------------------------------------------------- B. preconditions (current, consistent inputs)
def test_duplicate_payout_identity_is_a_precondition_failure_and_the_draft_is_unchanged(database, api, world, seed):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world, ledger_id=str(world['l2']['ledger']), operations=[settlement(world, (world['q1'], '50000.00'),
                    statement=world['s2'], gross='50000.00', fee='1000.00', net='49000.00', payout='STL-9001')], evidence_refs=[])
    before = a.get(f'{BASE}/proposals/{pid}').json()
    settle(seed, world['l2'], 'STL-9001', [(world['q1'], '50000.00')], '50000.00', '1000.00', '49000.00')
    run = get_run(a, simulate(a, pid).json()['id'])
    assert run['status'] == 'REJECTED'
    rej = run['outcome']['rejection']
    assert (rej['status'], rej['code'], rej['reason'], rej['reason_named_by_contract']) == (422, 'PRECONDITION_FAILED', 'DUPLICATE_SETTLEMENT', True)
    assert run['outcome']['verdicts'] == [] and 'predicted_postings' not in run['outcome']
    after = a.get(f'{BASE}/proposals/{pid}').json()
    assert after == before and after['state'] == 'DRAFT'           # draft acceptance and state unchanged


def test_an_already_settled_item_is_ITEM_ALREADY_SETTLED_resolving_SG7_at_simulation(database, api, world, seed):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world, ledger_id=str(world['l2']['ledger']), operations=[settlement(world, (world['q1'], '50000.00'),
                    statement=world['s2'], gross='50000.00', fee='1000.00', net='49000.00', payout='STL-9001')], evidence_refs=[])
    settle(seed, world['l2'], 'STL-OTHER', [(world['q1'], '50000.00')], '50000.00', '0.00', '50000.00')
    run = get_run(a, simulate(a, pid).json()['id'])
    rej = run['outcome']['rejection']
    assert (run['status'], rej['code'], rej['reason']) == ('REJECTED', 'PRECONDITION_FAILED', 'ITEM_ALREADY_SETTLED')
    assert rej['evidence']['id'] == 'E-DUP-01' and rej['evidence']['verdict'] == 'FAIL'
    assert {'entity_type': 'DOCUMENT', 'entity_id': str(world['q1']), 'version': 2} in run['read_set']
    # The draft API still accepts a SETTLED payment (SG-7 unchanged at drafting).
    r = create(a, draft(world, ledger_id=str(world['l2']['ledger']), operations=[settlement(world, (world['q1'], '50000.00'),
               statement=world['s2'], gross='50000.00', fee='1000.00', net='49000.00', payout='STL-9001')], evidence_refs=[]))
    assert r.status_code == 201


def test_a_closed_period_is_PERIOD_CLOSED(database, api, world):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    with connection(database=database) as admin:
        period = str(admin.execute('SELECT id FROM lv.fiscal_period WHERE ledger_id = %s', (world['l1']['ledger'],)).fetchone()[0])
    assert close_period(database, admin_token(database, world['A']), period)['status'] == 200
    run = get_run(a, simulate(a, pid).json()['id'])
    rej = run['outcome']['rejection']
    assert (run['status'], rej['code'], rej['reason'], rej['reason_named_by_contract']) == ('REJECTED', 'PRECONDITION_FAILED', 'PERIOD_CLOSED', True)
    assert {'entity_type': 'PERIOD', 'entity_id': period, 'version': 2} in run['read_set']


def test_unsupported_operation_sets_are_rejected_explicitly(database, api, world):
    a = signed_in(api, world, 'analyst')
    two = [settlement(world), settlement(world, (world['p1'], '50000.00'), gross='50000.00', fee='0.00', net='50000.00', payout='STL-X')]
    r = create(a, draft(world, operations=two))
    assert r.status_code == 201, r.text
    run = get_run(a, simulate(a, r.json()['id']).json()['id'])
    rej = run['outcome']['rejection']
    assert (run['status'], rej['code'], rej['reason'], rej['reason_named_by_contract']) == (
        'REJECTED', 'CAPABILITY_UNAVAILABLE', 'MULTI_OPERATION_SIMULATION', False)


def _bundle(database, run_id):
    with connection(database=database) as admin:
        return json.loads(admin.execute('SELECT input_canonical FROM lv.simulation_run WHERE id = %s', (run_id,)).fetchone()[0])


def test_engine_refusals_on_altered_bundles_are_typed_and_pure(database, api, world):
    """Pure engine: profile and capability refusals, and integrity failures, from a captured bundle with one field changed."""
    a = signed_in(api, world, 'analyst')
    base = _bundle(database, simulate(a, new_draft(a, world)).json()['id'])
    cases = []
    partial = copy.deepcopy(base); partial['proposal']['operations'][0]['items'][0]['amount']['amount'] = '1.00'
    cases.append((partial, 'UNSUPPORTED_BY_PROFILE', 'PARTIAL_ITEM_SETTLEMENT'))
    currency = copy.deepcopy(base); currency['proposal']['operations'][0]['fee']['currency'] = 'USD'
    cases.append((currency, 'CURRENCY_MISMATCH', None))
    books = copy.deepcopy(base); books['ledger']['purpose'] = 'BOOKS'
    cases.append((books, 'CAPABILITY_UNAVAILABLE', None))
    big = copy.deepcopy(base); big['proposal']['operations'][0]['items'] = big['proposal']['operations'][0]['items'] * 101
    cases.append((big, 'CAPABILITY_UNAVAILABLE', 'ASYNC_SIMULATION_REQUIRED'))
    other = copy.deepcopy(base); other['proposal']['operations'][0]['type'] = 'BootstrapLedger'
    cases.append((other, 'CAPABILITY_UNAVAILABLE', None))
    arithmetic = copy.deepcopy(base); arithmetic['proposal']['operations'][0]['net']['amount'] = '51500.49'
    cases.append((arithmetic, 'PRECONDITION_FAILED', 'SETTLEMENT_TOTALS'))
    for changed, code, reason in cases:
        out = engine.evaluate(changed)
        assert out['status'] == 'REJECTED' and out['rejection']['code'] == code and out['rejection'].get('reason') == reason, out
        assert canonical(engine.evaluate(copy.deepcopy(changed))) == canonical(out)
    corrupt = copy.deepcopy(base); corrupt['facts']['projection'][0]['debit_total'] = '999.0000'
    with pytest.raises(engine.IntegrityFailure):
        engine.evaluate(corrupt)
    lost = copy.deepcopy(base); lost['facts'] = None
    with pytest.raises(engine.IntegrityFailure):
        engine.evaluate(lost)


# ------------------------------------------------------------------------------------------- C. pinned inputs and replay
def test_replay_uses_captured_inputs_after_live_state_changes(database, api, world, seed):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    original = simulate(a, pid).json()
    first = get_run(a, original['id'])
    settle(seed, world['l1'], 'STL-3001', [(world['p1'], '50000.00'), (world['p2'], '2500.50')], '52500.50', '1000.00', '51500.50')
    replay = a.post(f"{BASE}/simulations/{original['id']}/replay")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body['identical'] is True and body['original_hash'] == body['replay_hash'] == original['result_hash']
    assert body['reloaded_current_state'] is False and body['input_hash'] == original['input_hash']
    assert get_run(a, original['id']) == first                       # the stored run is immutable
    now = get_run(a, simulate(a, pid).json()['id'])                   # a NEW capture sees the live state
    assert now['status'] == 'REJECTED' and now['outcome']['rejection']['reason'] == 'DUPLICATE_SETTLEMENT'
    assert now['snapshot']['seq'] == first['snapshot']['seq'] + 1


def test_replay_refuses_when_a_pinned_version_is_not_installed(database, api, world, monkeypatch):
    a = signed_in(api, world, 'analyst')
    run_id = simulate(a, new_draft(a, world)).json()['id']
    pinned = versions.SIMULATOR
    for name, value in (('simulator', 'lv-native-recordsettlement/9.9.9'), ('profile', 'sp1/2.0')):
        with monkeypatch.context() as patch:
            patch.setitem(versions.PROFILES, pinned, {**versions.PROFILES[pinned], name: value})
            body = problem(a.post(f'{BASE}/simulations/{run_id}/replay'), 409, 'SIMULATION_VERSION_UNAVAILABLE')
            assert 'pinned' in body['detail']
    assert a.post(f'{BASE}/simulations/{run_id}/replay').json()['identical'] is True
    original = versions.PROFILES[pinned]
    for changed in ({**original, 'kernel_rule': 'sp1-posting/9.9:settlement-slice'}, {k: v for k, v in original.items() if k != 'adapter'}):
        with monkeypatch.context() as patch:
            patch.setitem(versions.PROFILES, pinned, changed)
            problem(a.post(f'{BASE}/simulations/{run_id}/replay'), 409, 'SIMULATION_VERSION_UNAVAILABLE')


def test_replay_reports_a_different_result_when_the_calculation_changes_under_the_same_labels(database, api, world, monkeypatch):
    """Replay compares hashes; it does not trust the stored result. A changed calculation with unchanged version labels (a defect
    that a version bump should have declared) is reported as not identical, never silently accepted."""
    a = signed_in(api, world, 'analyst')
    run_id = simulate(a, new_draft(a, world)).json()['id']
    original = engine._completed

    def altered(*args):
        out = original(*args)
        out['predicted_postings'][0]['amount'] = m('1.00')
        return out
    monkeypatch.setattr(engine, '_completed', altered)
    body = a.post(f'{BASE}/simulations/{run_id}/replay').json()
    assert body['identical'] is False and body['original_hash'] != body['replay_hash']


def test_capture_is_one_snapshot_even_when_a_settlement_commits_during_it(database, api, world, seed, monkeypatch):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    fired = []

    def commit_now():
        if not fired:
            fired.append(settle(seed, world['l1'], 'STL-3001', [(world['p1'], '50000.00'), (world['p2'], '2500.50')],
                                '52500.50', '1000.00', '51500.50'))
    monkeypatch.setattr(capture_module, '_after_capture_counter', commit_now)
    run = get_run(a, simulate(a, pid).json()['id'])
    assert fired, 'the concurrent settlement did not run'
    # Everything describes the sequence before the concurrent commit: payments RECEIVED at version 1, simulation COMPLETED.
    assert run['status'] == 'COMPLETED' and run['snapshot']['seq'] == int(fired[0]['seq']) - 1
    assert {e['entity_id']: e['version'] for e in run['read_set'] if e['entity_type'] == 'DOCUMENT'} == {str(world['p1']): 1, str(world['p2']): 1}
    facts = _bundle(database, run['id'])['facts']
    assert facts['payout_settlement'] is None and facts['active_inclusions'] == [] and {p['status'] for p in facts['payments']} == {'RECEIVED'}
    monkeypatch.setattr(capture_module, '_after_capture_counter', None)
    later = get_run(a, simulate(a, pid).json()['id'])
    assert later['status'] == 'REJECTED' and later['snapshot']['seq'] == int(fired[0]['seq'])


def test_a_draft_edit_during_simulation_cannot_mix_revisions(database, api, world, monkeypatch):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    edited = []

    def edit_now():
        if not edited:
            b = signed_in(api, world, 'automation')
            edited.append(b.patch(f'{BASE}/proposals/{pid}', json={'rationale': 'edited during simulation'}, headers={'If-Match': '"v1"'}).status_code)
    monkeypatch.setattr(service, '_before_record', edit_now)
    run = get_run(a, simulate(a, pid).json()['id'])
    assert edited == [200]
    bundle = _bundle(database, run['id'])
    with connection(database=database) as admin:
        content_hash = admin.execute('SELECT content_hash FROM lv.proposal_revision WHERE proposal_id = %s AND revision = 1', (pid,)).fetchone()[0]
    assert run['revision'] == 1 and bundle['proposal']['content_hash'] == content_hash and bundle['proposal']['revision'] == '1'


# ------------------------------------------------------------------------------------------- D. boundary
def test_sessions_permissions_and_tenants(database, api, world):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    run_id = simulate(a, pid).json()['id']
    anonymous = client(api)
    problem(anonymous.post(f'/test/proposals/{pid}/simulate'), 401, 'UNAUTHENTICATED')
    problem(anonymous.get(f'{BASE}/simulations/{run_id}'), 401, 'UNAUTHENTICATED')
    problem(anonymous.post(f'{BASE}/simulations/{run_id}/replay'), 401, 'UNAUTHENTICATED')
    no_csrf = signed_in(api, world, 'analyst'); del no_csrf.headers['X-CSRF-Token']
    problem(no_csrf.post(f'/test/proposals/{pid}/simulate'), 403, 'PERMISSION_DENIED')
    for name in ('admin', 'controller'):          # read but no propose (Part 5.1): may read and replay, may not simulate
        c = signed_in(api, world, name)
        assert problem(simulate(c, pid), 403, 'PERMISSION_DENIED')['detail'] == 'requires propose'
        assert c.get(f'{BASE}/simulations/{run_id}').status_code == 200
        assert c.post(f'{BASE}/simulations/{run_id}/replay').json()['identical'] is True
    b = signed_in(api, world, 'analystB')
    for foreign, absent in ((f'/test/proposals/{pid}/simulate', f'/test/proposals/{uuid4()}/simulate'),
                            (f'{BASE}/simulations/{run_id}/replay', f'{BASE}/simulations/{uuid4()}/replay')):
        bf, ba = problem(b.post(foreign), 404, 'NOT_FOUND'), problem(b.post(absent), 404, 'NOT_FOUND')
        bf.pop('request_id'); ba.pop('request_id')
        assert bf == ba
    problem(b.get(f'{BASE}/simulations/{run_id}'), 404, 'NOT_FOUND')
    problem(simulate(a, pid, revision=7), 404, 'NOT_FOUND')
    problem(a.get(f'{BASE}/simulations/{run_id}', params={'x': '1'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    with owner_transaction(database) as conn:
        conn.execute("UPDATE lv_identity.membership SET status = 'REVOKED', revoked_at = now() WHERE tenant_id = %s AND user_id = %s",
                     (world['A'], world['users']['analyst']['id']))
    problem(simulate(a, pid), 401, 'UNAUTHENTICATED')
    problem(a.get(f'{BASE}/simulations/{run_id}'), 401, 'UNAUTHENTICATED')


def test_simulation_rows_are_immutable_and_only_the_definer_writes_them(database, api, world):
    a = signed_in(api, world, 'analyst')
    run_id = simulate(a, new_draft(a, world)).json()['id']
    with connection('lv_app', database) as app:
        app.execute("SELECT set_config('app.tenant_id', %s, true)", (str(world['A']),))
        for sql in ("UPDATE lv.simulation_run SET status = 'REJECTED' WHERE id = %s", 'DELETE FROM lv.simulation_run WHERE id = %s'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with app.transaction():
                    app.execute(sql, (run_id,))
    with connection(database=database) as admin:
        for sql in ("UPDATE lv.simulation_run SET simulator_version = 'x' WHERE id = %s", 'DELETE FROM lv.simulation_run WHERE id = %s'):
            with pytest.raises(psycopg.errors.CheckViolation, match='immutable'):
                with admin.transaction():
                    admin.execute(sql, (run_id,))
        row = admin.execute('SELECT tenant_id, ledger_id, proposal_id, revision, status, simulator_version, snapshot_seq, input_canonical, '
                            'input_hash, outcome_canonical, result_hash, created_by, session_id FROM lv.simulation_run WHERE id = %s', (run_id,)).fetchone()
        tampered = list(row); tampered[9] = row[9].replace('51500.50', '51500.51')
        with pytest.raises(psycopg.errors.CheckViolation):
            with admin.transaction():
                admin.execute('INSERT INTO lv.simulation_run(tenant_id, ledger_id, proposal_id, revision, status, simulator_version, snapshot_seq, '
                              'input_canonical, input_hash, outcome_canonical, result_hash, created_by, session_id) VALUES '
                              '(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)', tampered)


def test_the_recorder_refuses_a_bundle_for_another_revision_or_a_future_sequence(database, api, world):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    run_id = simulate(a, pid).json()['id']
    token = a.cookies.get(COOKIE)
    from ledgervigil.auth.crypto import token_hash
    with connection(database=database) as admin:
        bundle_text, outcome_text = admin.execute('SELECT input_canonical, outcome_canonical FROM lv.simulation_run WHERE id = %s', (run_id,)).fetchone()
    bundle = json.loads(bundle_text)
    for change in (lambda b: b['proposal'].update(content_hash='0' * 64), lambda b: b['snapshot'].update(seq='999'),
                   lambda b: b.update(tenant_id=str(world['B']))):
        forged = copy.deepcopy(bundle); change(forged)
        text = canonical(forged)
        before = runs(database)
        with connection('lv_app', database) as app, pytest.raises(psycopg.errors.InvalidParameterValue):
            app.execute('SELECT lv.simulation_record(%s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                        (token_hash(token), pid, text, digest(text), outcome_text, digest(outcome_text), 'COMPLETED', versions.SIMULATOR,
                         int(forged['snapshot']['seq']), 'test', 'test'))
        assert runs(database) == before


# ------------------------------------------------------------------------------------------- E. failure paths and financial preservation
def test_failures_after_capture_record_nothing(database, api, world, monkeypatch):
    a = signed_in(api, world, 'analyst')
    pid = new_draft(a, world)
    before = runs(database)

    def boom():
        raise RuntimeError('injected failure before the record call')
    monkeypatch.setattr(service, '_before_record', boom)
    problem(simulate(a, pid), 500, 'INTERNAL')
    monkeypatch.setattr(service, '_before_record', None)
    from ledgervigil.kernel_adapter.types import AdapterError

    def inconsistent(*_):
        raise AdapterError('SNAPSHOT_INCONSISTENT', detail='injected')
    monkeypatch.setattr(engine, 'calculate_settlement', inconsistent)
    assert 'nothing was recorded' in problem(simulate(a, pid), 500, 'INTERNAL')['detail']
    assert runs(database) == before


def test_preparation_simulation_reads_and_replay_change_no_authoritative_state(database, api, world, seed):
    """CT-10: after fixture preparation, every lv/lv_identity table except the proposal, idempotency, audit and simulation tables,
    every ledger counter (sequence, batch and event heads) and the trial balances are byte-identical across draft preparation,
    simulation (COMPLETED and REJECTED), GET and replay."""
    a = signed_in(api, world, 'analyst')
    before, ctrs, tb1, tb2 = fingerprint(database, SIM_TABLES), counters(database), tb(a, world), tb(a, world, 'l2')
    pid = new_draft(a, world)
    run_id = simulate(a, pid).json()['id']
    a.get(f'{BASE}/simulations/{run_id}'); a.post(f'{BASE}/simulations/{run_id}/replay')
    bad = new_draft(a, world, operations=[settlement(world), settlement(world)])
    simulate(a, bad)
    assert fingerprint(database, SIM_TABLES) == before and counters(database) == ctrs
    assert (tb(a, world), tb(a, world, 'l2')) == (tb1, tb2)
    with connection(database=database) as admin:
        assert admin.execute('SELECT count(*) FROM lv.simulation_run WHERE proposal_id IN (%s, %s)', (pid, bad)).fetchone()[0] == 2


def test_openapi_documents_the_two_read_routes_and_no_creation_route():
    spec = create_app(AuthSettings()).openapi()
    assert set(p for p in spec['paths'] if 'simulat' in p or 'submit' in p) == {'/api/v1/simulations/{simulation_id}',
                                                                               '/api/v1/simulations/{simulation_id}/replay','/api/v1/proposals/{proposal_id}/submit', '/api/v1/proposals/{proposal_id}/resimulate', '/api/v1/proposals/{proposal_id}/simulations'}
    assert spec['paths']['/api/v1/simulations/{simulation_id}']['get']['x-lv-permission'] == 'read'
    assert spec['paths']['/api/v1/simulations/{simulation_id}/replay']['post']['x-lv-permission'] == 'read'
    assert '409' in spec['paths']['/api/v1/simulations/{simulation_id}/replay']['post']['responses']


def test_0011_upgrade_adds_only_the_simulation_table_and_downgrade_removes_it():
    from test_processor_payments import disposable
    from tools.dev_db import migrate
    with disposable('0010_proposal_drafts') as name:
        before = fingerprint(name, set())
        migrate(name, '0011_simulation_runs')
        with connection(database=name) as admin:
            assert admin.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0011_simulation_runs',)
            assert admin.execute("SELECT relforcerowsecurity FROM pg_class WHERE oid = 'lv.simulation_run'::regclass").fetchone() == (True,)
        after = fingerprint(name, set())
        assert after.pop('lv.simulation_run') == fingerprint(name, set())['lv.simulation_run'] and after == before
        migrate(name, '0010_proposal_drafts', downgrade=True, disposable=True)
        with connection(database=name) as admin:
            assert admin.execute("SELECT to_regclass('lv.simulation_run')").fetchone() == (None,)
            assert admin.execute("SELECT to_regproc('lv.simulation_record')").fetchone() == (None,)
        assert fingerprint(name, set()) == before
