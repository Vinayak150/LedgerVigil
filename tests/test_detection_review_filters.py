"""0034 / PROPOSED DET-11: bounded server-side filters on GET /ledgers/{id}/detection-findings for the review UI.
Disposable database per test; SYNTHETIC data."""
from uuid import uuid4

from tools.dev_db import connection
from test_proposal_drafts import world, seed, signed_in, _statement, fingerprint, counters  # noqa: F401
from detection_support import BASE, DETECTION_WRITES, database, dapi, run, dispose  # noqa: F401


def listing(c, ledger, **q):
    return c.get(f'{BASE}/ledgers/{ledger}/detection-findings', params=q)


def test_filters_are_exact_server_side_and_cursor_bound(database, dapi, world):
    _statement(database, world['l1'], 'STL-3002', [('PMT-2001', '50000.00')], '50000.00', '500.00', '49500.00')
    a = signed_in(dapi, world, 'analyst')
    ctl = signed_in(dapi, world, 'controller')
    ledger = world['l1']['ledger']
    assert run(a, ledger).status_code == 201
    before = (fingerprint(database, DETECTION_WRITES), counters(database))
    everything = listing(a, ledger).json()['items']
    kinds = sorted(f['kind'] for f in everything)
    assert kinds == ['PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', 'SETTLEMENT_NOT_RECORDED', 'SETTLEMENT_NOT_RECORDED']
    overlap = listing(a, ledger, kind='PAYMENT_CLAIMED_BY_OTHER_PAYOUT').json()
    assert [f['kind'] for f in overlap['items']] == ['PAYMENT_CLAIMED_BY_OTHER_PAYOUT'] * 2 and overlap['filters']['kind'] == 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT'
    mine = listing(a, ledger, staged_document_id=str(world['s1'])).json()['items']
    assert {f['staged_document_id'] for f in mine} == {str(world['s1'])} and len(mine) == 2
    assert listing(a, ledger, detector_version='1.0.0').json()['items'] == []
    assert len(listing(a, ledger, detector_version='1.1.0').json()['items']) == 4
    # Review status is the current one, read from the review row.
    target = overlap['items'][0]
    assert dispose(ctl, target['id'], 'DISMISSED', 'EXPECTED_TIMING', '"v1"').status_code == 200
    assert [f['id'] for f in listing(a, ledger, review_status='DISMISSED').json()['items']] == [target['id']]
    assert len(listing(a, ledger, review_status='OPEN').json()['items']) == 3
    assert listing(a, ledger, review_status='CONFIRMED', kind='SETTLEMENT_NOT_RECORDED').json()['items'] == []
    # Paging within a filter; a cursor from one filter never verifies under another.
    p1 = listing(a, ledger, kind='SETTLEMENT_NOT_RECORDED', limit='1').json()
    assert len(p1['items']) == 1 and p1['next_cursor']
    p2 = listing(a, ledger, kind='SETTLEMENT_NOT_RECORDED', limit='1', cursor=p1['next_cursor']).json()
    assert len(p2['items']) == 1 and p2['items'][0]['id'] != p1['items'][0]['id'] and p2['next_cursor'] is None
    wrong = listing(a, ledger, kind='PAYMENT_CLAIMED_BY_OTHER_PAYOUT', limit='1', cursor=p1['next_cursor'])
    assert (wrong.status_code, wrong.json()['code']) == (400, 'CURSOR_MISMATCH')
    # Validation and isolation.
    assert listing(a, ledger, kind='FRAUD').status_code == 422
    assert listing(a, ledger, review_status='open').status_code == 422
    assert listing(a, ledger, freshness='STALE').status_code == 400          # not a filter: freshness is computed per item
    assert listing(signed_in(dapi, world, 'analystB'), ledger, kind='SETTLEMENT_NOT_RECORDED').status_code == 404
    # Reads changed nothing outside detection's own tables (the disposition above is detection's own write).
    assert (fingerprint(database, DETECTION_WRITES), counters(database)) == before


def test_configuration_states_deterministic_rules_and_no_model(database, dapi, world):
    a = signed_in(dapi, world, 'analyst')
    r = a.get(f'{BASE}/detection-configuration')
    assert r.status_code == 200
    body = r.json()
    assert (body['basis'], body['model'], body['detector_version'], body['accounting_review_status']) == ('DETERMINISTIC_COMPARISON', 'NONE', '1.1.0', 'UNREVIEWED')
    labels = {x['kind']: x['label'] for x in body['rules']}
    assert labels['PAYMENT_CLAIMED_BY_OTHER_PAYOUT'] == 'Potential overlapping claim' and labels['PAYOUT_DATE_DIFFERS'] == 'Date difference'
    text = r.text.lower()
    assert 'confidence' not in text.replace('no model, score or confidence', '') and 'fraud' not in text.replace('not a fraud determination', '')
    assert a.get(f'{BASE}/detection-configuration?x=1').status_code == 400
