"""Authenticated native financial read API (Stage 2B Family 4 read subset) — through the HTTP application and real PostgreSQL.

Expected behaviour comes from the owning contracts, not from the code under test:
- Stage 2B Family 4 rows `GET /ledgers` · `GET /ledgers/{id}` ("purpose, state (EMPTY, ACTIVE), current seq"),
  `GET /ledgers/{id}/accounts`, `GET /ledgers/{id}/periods`, `GET /ledgers/{id}/trial-balance` (`?as_of_seq&as_of_date` ->
  `{as_of_seq, lines, total_debit, total_credit}`), permission `read`;
- Stage 2B Part 2 (Money strings at the currency exponent; signed values only in `balance`; cursor pagination, limit 1-200
  default 50, signed cursors bound to filter and order, CURSOR_MISMATCH; unknown query parameters 400; snapshot reads state
  `as_of_seq`; other-tenant ids 404), Part 4 codes, Part 10 freshness (ETag / If-None-Match / 304);
- Stage 2A §2.2 (account types and normal sides), §3 (exact money), §4.2 (signed_amount), §7.1 ("balances are signed
  debit-positive; TB = trial-balance total on each side"), §7.2/§7.3 oracle rows, §8.3 (balances at seq <= N), I-04,
  §19.2 (EMPTY = sequence 0, no documents).

EXPECTED TRIAL BALANCES. The expected account balances and TB totals below are copied as literal strings from the Stage 2A
oracle inventory (§7.2 OC-04 start, OC-01 start and end; §7.3 OC-10 start and end), written by the Stage 2A author (Claude,
2 Nov 2026), arithmetic computed by hand and by a second throwaway implementation. Their accounting semantics are
UNREVIEWED (ADR-013; Stage 2A §7.4): no credentialed accountant has validated them. The journal lines that reach the OC-10
starting state are this milestone's construction (Claude, 3 Nov 2026), checked by hand to sum to the 2A state; they are
labelled as such. Passing these tests shows the API reports what is posted; it is not an accounting-accuracy measure.

All identities, tenants and ledgers are SYNTHETIC and created per test.
"""
import json
import secrets
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tools.dev_db import connection
from tools.identity_admin import create_user, grant, owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.reads import ledgers as ledger_reads
from period_close_support import admin_token, close_period
from test_period_close import everything
from test_posting import provision, register, post

COOKIE = '__Host-lv_session'
BASE = '/api/v1'
SANDBOX_LABEL = 'SANDBOX — not accounting records'


# ------------------------------------------------------------------------------------------- world
def _user(owner, w, name, tenant, roles):
    email = f'{name}-{uuid4().hex[:8]}@test.ledgervigil.invalid'
    password = secrets.token_urlsafe(18)
    uid = create_user(owner, email, f'Synthetic {name}', password)
    grant(owner, tenant, uid, roles)
    w['users'][name] = {'email': email, 'id': str(uid), 'password': password, 'tenant': tenant}


@pytest.fixture
def world(database, app):
    """Tenant A: an INR SANDBOX ledger (Oct 2026 period), a USD SANDBOX ledger and a BOOKS ledger; users of several roles.
    Tenant B: its own SANDBOX ledger and analyst."""
    from test_processor_payments import new_tenant
    w = {'A': new_tenant(database), 'B': new_tenant(database), 'users': {}}
    w['inr'] = provision(app, w['A'])
    w['usd'] = provision(app, w['A'], currency='USD')
    w['books'] = provision(app, w['A'], purpose='BOOKS')
    w['b'] = provision(app, w['B'])
    w['actorA'] = register(database, w['A'])
    w['actorB'] = register(database, w['B'])
    with owner_transaction(database) as owner:
        for name, roles, tenant in (('analyst', ['ANALYST'], 'A'), ('controller', ['CONTROLLER'], 'A'), ('admin', ['ADMINISTRATOR'], 'A'),
                                    ('reader', ['ANALYST'], 'A'), ('analystB', ['ANALYST'], 'B')):
            _user(owner, w, name, w[tenant], roles)
    return w


@pytest.fixture
def api(database):
    application = create_app(AuthSettings(), connect=lambda: connection('lv_app', database))
    yield application
    application.state.pool.close()


@pytest.fixture
def seed(database):
    with connection('lv_seed', database, autocommit=True) as c:
        yield c


def client(application):
    return TestClient(application, base_url='https://testserver', raise_server_exceptions=False,
                      client=(f'10.9.{secrets.randbelow(250)}.{secrets.randbelow(250)}', 40000))


def signed_in(application, w, name):
    c = client(application)
    u = w['users'][name]
    r = c.post(f'{BASE}/auth/login', json={'email': u['email'], 'password': u['password']})
    assert r.status_code == 200, r.text
    return c


def routes(ledger):
    return [f'{BASE}/ledgers', f'{BASE}/ledgers/{ledger}', f'{BASE}/ledgers/{ledger}/accounts', f'{BASE}/ledgers/{ledger}/periods',
            f'{BASE}/ledgers/{ledger}/trial-balance']


def journal(cfg, ref, *lines, date='2026-10-14'):
    """A SYNTHETIC manual journal through the existing developer seed path (lv.seed_manual_journal)."""
    body = []
    for role, side, amount in lines:
        item = {'account_id': cfg['chart'][role], 'side': side, 'amount': amount}
        if role == 'AR_CONTROL':
            item['party_id'] = cfg['party']
        body.append(item)
    return {'source_ref': ref, 'effective_date': date, 'description': 'SYNTHETIC — ledger read API fixture', 'lines': body}


def tb_map(body):
    """{code: (debit, credit, balance)} — strings exactly as returned."""
    return {l['code']: (l['debit']['amount'], l['credit']['amount'], l['balance']['amount']) for l in body['lines']}


def problem(r, status, code):
    assert r.status_code == status, (r.status_code, r.text)
    assert r.headers['content-type'].startswith('application/problem+json')
    body = r.json()
    assert body['code'] == code, body
    return body


def walk(c, path, limit, **params):
    """Follow next_cursor to the end; returns (items, pages)."""
    items, pages, cursor = [], 0, None
    while True:
        q = {'limit': str(limit), **params}
        if cursor:
            q['cursor'] = cursor
        r = c.get(path, params=q)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body['items']) <= limit
        items += body['items']; pages += 1
        cursor = body['next_cursor']
        if cursor is None:
            return items, pages
        assert pages < 100


# INR fixture chart (Stage 2A §2.2): code, role, type, is_contra, normal side.
CHART = {'1000': ('BANK_OPERATING', 'ASSET', False, 'DEBIT'), '1100': ('AR_CONTROL', 'ASSET', False, 'DEBIT'),
         '1150': ('PROCESSOR_CLEARING', 'ASSET', False, 'DEBIT'), '3000': ('OPENING_BALANCE_EQUITY', 'EQUITY', False, 'CREDIT'),
         '4000': ('REVENUE_DEFAULT', 'REVENUE', False, 'CREDIT'), '4900': ('SALES_RETURNS', 'REVENUE', True, 'DEBIT'),
         '6100': ('PROCESSOR_FEE_EXPENSE', 'EXPENSE', False, 'DEBIT')}


# ------------------------------------------------------------------------------------------- A. authentication and isolation
def test_every_read_route_requires_a_valid_session_and_a_304_is_never_given_without_one(database, api, world):
    good = signed_in(api, world, 'analyst')
    etags = {}
    for path in routes(world['inr']['ledger']):
        r = good.get(path)
        assert r.status_code == 200, (path, r.text)
        etags[path] = r.headers['ETag']
    anonymous = client(api)
    for path in routes(world['inr']['ledger']):
        problem(anonymous.get(path), 401, 'UNAUTHENTICATED')
        problem(anonymous.get(path, headers={'If-None-Match': etags[path]}), 401, 'UNAUTHENTICATED')
        forged = client(api); forged.cookies.set(COOKIE, secrets.token_urlsafe(32))
        problem(forged.get(path, headers={'If-None-Match': etags[path]}), 401, 'UNAUTHENTICATED')
        # Validation never runs before authentication: a bad query from an anonymous caller is still 401.
        problem(anonymous.get(path, params={'limit': 'abc', 'bogus': '1'}), 401, 'UNAUTHENTICATED')
    expired = signed_in(api, world, 'reader')
    from ledgervigil.auth.crypto import token_hash
    with connection(database=database) as admin:     # TEST ONLY: simulate elapsed time (guard triggers bypassed), as in the identity suite
        admin.execute('SET LOCAL session_replication_role = replica')
        admin.execute("UPDATE lv_identity.session SET created_at = created_at - interval '13 hours', last_seen_at = last_seen_at - interval "
                      "'13 hours', absolute_expires_at = absolute_expires_at - interval '13 hours' WHERE token_hash = %s",
                      (token_hash(expired.cookies.get(COOKIE)),))
    for path in routes(world['inr']['ledger']):
        problem(expired.get(path, headers={'If-None-Match': etags[path]}), 401, 'UNAUTHENTICATED')


def test_disabled_user_revoked_membership_and_removed_roles_lose_access_on_the_next_read(database, api, world):
    analyst, controller, reader = (signed_in(api, world, n) for n in ('analyst', 'controller', 'reader'))
    path = f"{BASE}/ledgers/{world['inr']['ledger']}/trial-balance"
    for c in (analyst, controller, reader):
        assert c.get(path).status_code == 200
    with owner_transaction(database) as conn:      # direct row changes; sessions deliberately NOT revoked
        conn.execute('UPDATE lv_identity.app_user SET disabled_at = now() WHERE id = %s', (world['users']['analyst']['id'],))
        conn.execute("UPDATE lv_identity.membership SET status = 'REVOKED', revoked_at = now() WHERE tenant_id = %s AND user_id = %s",
                     (world['A'], world['users']['controller']['id']))
        conn.execute('DELETE FROM lv_identity.membership_role WHERE tenant_id = %s AND user_id = %s', (world['A'], world['users']['reader']['id']))
    for p in routes(world['inr']['ledger']):
        problem(analyst.get(p), 401, 'UNAUTHENTICATED')
        problem(controller.get(p), 401, 'UNAUTHENTICATED')
        body = problem(reader.get(p), 403, 'PERMISSION_DENIED')     # valid session, membership active, no role -> no `read`
        assert body['detail'] == 'requires read'


def test_other_tenant_ledger_ids_are_404_indistinguishable_from_absent_ids(api, world):
    a = signed_in(api, world, 'analyst')
    foreign, absent = world['b']['ledger'], uuid4()
    for path_foreign, path_absent in zip(routes(foreign)[1:], routes(absent)[1:]):
        rf, ra = a.get(path_foreign), a.get(path_absent)
        bf, ba = problem(rf, 404, 'NOT_FOUND'), problem(ra, 404, 'NOT_FOUND')
        bf.pop('request_id'); ba.pop('request_id')
        assert bf == ba and 'ETag' not in rf.headers
        # Even with parameters that would otherwise be meaningful.
        params = {'as_of_seq': '0'} if path_foreign.endswith('balance') else {} if path_foreign.endswith(str(foreign)) else {'limit': '1'}
        problem(a.get(path_foreign, params=params), 404, 'NOT_FOUND')
    listed = {l['id'] for l in walk(a, f'{BASE}/ledgers', 200)[0]}
    assert listed == {str(world[k]['ledger']) for k in ('inr', 'usd', 'books')}
    b = signed_in(api, world, 'analystB')
    assert {l['id'] for l in walk(b, f'{BASE}/ledgers', 200)[0]} == {str(foreign)}


# ------------------------------------------------------------------------------------------- B. financial correctness
def test_empty_ledger_is_EMPTY_at_sequence_zero_with_a_zero_trial_balance(api, world):
    a = signed_in(api, world, 'analyst')
    led = a.get(f"{BASE}/ledgers/{world['inr']['ledger']}").json()
    assert led == {'id': str(world['inr']['ledger']), 'name': 'Synthetic posting tests', 'purpose': 'SANDBOX', 'source': 'NATIVE',
                   'functional_currency': 'INR', 'currency_exponent': 2, 'profile_version': 'sp1/1.0', 'state': 'EMPTY',
                   'current_seq': 0, 'created_at': led['created_at'], 'data_label': SANDBOX_LABEL}
    assert led['created_at'].endswith('Z')
    tb = a.get(f"{BASE}/ledgers/{world['inr']['ledger']}/trial-balance").json()
    assert tb == {'ledger_id': str(world['inr']['ledger']), 'purpose': 'SANDBOX', 'data_label': SANDBOX_LABEL,
                  'accounting_status': 'UNREVIEWED', 'as_of_seq': 0, 'currency': 'INR', 'lines': [],
                  'total_debit': {'amount': '0.00', 'currency': 'INR'}, 'total_credit': {'amount': '0.00', 'currency': 'INR'}}
    assert a.get(f"{BASE}/ledgers/{world['inr']['ledger']}/trial-balance", params={'as_of_seq': '0'}).json() == tb
    body = problem(a.get(f"{BASE}/ledgers/{world['inr']['ledger']}/trial-balance", params={'as_of_seq': '1'}), 422, 'SCHEMA_INVALID')
    assert body['errors'] == [{'pointer': '/query/as_of_seq', 'code': 'beyond_committed_sequence'}]


def test_chart_of_accounts_types_and_normal_sides_match_stage_2a(api, world):
    a = signed_in(api, world, 'analyst')
    items, _ = walk(a, f"{BASE}/ledgers/{world['inr']['ledger']}/accounts", 50)
    assert [i['code'] for i in items] == sorted(CHART)
    for i in items:
        role, kind, contra, normal = CHART[i['code']]
        assert (i['name'], i['type'], i['is_contra'], i['normal_side']) == (role, kind, contra, normal), i
        assert i['requires_party'] == (role == 'AR_CONTROL') and i['currency'] == 'INR' and i['active'] is True
        assert i['ledger_id'] == str(world['inr']['ledger']) and i['subtype'] is None and i['parent_id'] is None


def test_inr_trial_balance_matches_the_stage_2a_oc01_oracle_at_every_sequence(api, world, seed):
    """Postings: 2A §5.3 / OC-01 chain (INV-1001, PMT-2001 applied, STL-3001). Expected states: §7.2 OC-04 start (seq 1),
    OC-01 start (seq 2) and OC-01 end (seq 3), literally."""
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'INV-1001', ('AR_CONTROL', 'D', '50000.00'), ('REVENUE_DEFAULT', 'C', '50000.00')))
    post(seed, cfg, journal(cfg, 'PMT-2001', ('PROCESSOR_CLEARING', 'D', '50000.00'), ('AR_CONTROL', 'C', '50000.00')))
    post(seed, cfg, journal(cfg, 'STL-3001', ('BANK_OPERATING', 'D', '49000.00'), ('PROCESSOR_FEE_EXPENSE', 'D', '1000.00'),
                            ('PROCESSOR_CLEARING', 'C', '50000.00')))
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    expected = {
        1: ({'1100': ('50000.00', '0.00', '50000.00'), '4000': ('0.00', '50000.00', '-50000.00')}, '50000.00'),
        2: ({'1100': ('0.00', '0.00', '0.00'), '1150': ('50000.00', '0.00', '50000.00'), '4000': ('0.00', '50000.00', '-50000.00')}, '50000.00'),
        3: ({'1000': ('49000.00', '0.00', '49000.00'), '1100': ('0.00', '0.00', '0.00'), '1150': ('0.00', '0.00', '0.00'),
             '4000': ('0.00', '50000.00', '-50000.00'), '6100': ('1000.00', '0.00', '1000.00')}, '50000.00'),
    }
    for seq, (lines, total) in expected.items():
        body = a.get(path, params={'as_of_seq': str(seq)}).json()
        assert body['as_of_seq'] == seq and tb_map(body) == lines, (seq, body)
        assert body['total_debit'] == body['total_credit'] == {'amount': total, 'currency': 'INR'}
        assert [l['code'] for l in body['lines']] == sorted(lines)
        for l in body['lines']:
            assert (l['type'], l['is_contra'], l['normal_side']) == CHART[l['code']][1:]
            assert l['debit']['currency'] == l['credit']['currency'] == l['balance']['currency'] == 'INR'
    latest = a.get(path).json()
    assert latest == a.get(path, params={'as_of_seq': '3'}).json() and latest['as_of_seq'] == 3
    led = a.get(f"{BASE}/ledgers/{cfg['ledger']}").json()
    assert (led['state'], led['current_seq']) == ('ACTIVE', 3)


def test_usd_trial_balance_with_a_contra_account_matches_the_oc10_oracle(api, world, seed):
    """Expected: Stage 2A §7.3 OC-10 start (AR 0.00; REV -500.00; RET 200.00; CLR 300.00; TB 500.00) and end (BANK 285.20;
    FEE 14.80; CLR 0.00; RET 200.00; REV -500.00; AR 0.00; TB 500.00). The four journals reaching the start state are this
    milestone's construction (invoice, payment, credit note, processor refund), hand-checked against the 2A state."""
    cfg = {**world['usd'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'U3-INV', ('AR_CONTROL', 'D', '500.00'), ('REVENUE_DEFAULT', 'C', '500.00')))
    post(seed, cfg, journal(cfg, 'U3-PMT', ('PROCESSOR_CLEARING', 'D', '500.00'), ('AR_CONTROL', 'C', '500.00')))
    post(seed, cfg, journal(cfg, 'U3-CN', ('SALES_RETURNS', 'D', '200.00'), ('AR_CONTROL', 'C', '200.00')))
    post(seed, cfg, journal(cfg, 'U3-RFD', ('AR_CONTROL', 'D', '200.00'), ('PROCESSOR_CLEARING', 'C', '200.00')))
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    start = a.get(path, params={'as_of_seq': '4'}).json()
    assert tb_map(start) == {'1100': ('0.00', '0.00', '0.00'), '1150': ('300.00', '0.00', '300.00'),
                             '4000': ('0.00', '500.00', '-500.00'), '4900': ('200.00', '0.00', '200.00')}
    assert start['total_debit'] == start['total_credit'] == {'amount': '500.00', 'currency': 'USD'}
    post(seed, cfg, journal(cfg, 'U3-STL', ('BANK_OPERATING', 'D', '285.20'), ('PROCESSOR_FEE_EXPENSE', 'D', '14.80'),
                            ('PROCESSOR_CLEARING', 'C', '300.00')))
    end = a.get(path).json()
    assert end['as_of_seq'] == 5 and end['currency'] == 'USD'
    assert tb_map(end) == {'1000': ('285.20', '0.00', '285.20'), '1100': ('0.00', '0.00', '0.00'), '1150': ('0.00', '0.00', '0.00'),
                           '4000': ('0.00', '500.00', '-500.00'), '4900': ('200.00', '0.00', '200.00'), '6100': ('14.80', '0.00', '14.80')}
    assert end['total_debit'] == end['total_credit'] == {'amount': '500.00', 'currency': 'USD'}
    contra = next(l for l in end['lines'] if l['code'] == '4900')
    assert (contra['type'], contra['is_contra'], contra['normal_side']) == ('REVENUE', True, 'DEBIT')


def test_amounts_are_exact_decimal_strings_beyond_binary_floating_point(api, world, seed):
    """Values with 18 significant digits cannot survive a float64 round trip; every amount must equal the posted string."""
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'BIG-1', ('BANK_OPERATING', 'D', '1234567890123456.78'), ('OPENING_BALANCE_EQUITY', 'C', '1234567890123456.78')))
    post(seed, cfg, journal(cfg, 'SMALL-1', ('PROCESSOR_FEE_EXPENSE', 'D', '0.01'), ('BANK_OPERATING', 'C', '0.01')))
    assert '%.2f' % float('1234567890123456.77') != '1234567890123456.77'      # float64 cannot carry this value
    body = signed_in(api, world, 'analyst').get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance").json()
    assert tb_map(body) == {'1000': ('1234567890123456.77', '0.00', '1234567890123456.77'),
                            '3000': ('0.00', '1234567890123456.78', '-1234567890123456.78'), '6100': ('0.01', '0.00', '0.01')}
    assert body['total_debit'] == body['total_credit'] == {'amount': '1234567890123456.78', 'currency': 'INR'}


def test_a_total_outside_the_money_range_is_refused_not_emitted(api, world, seed):
    cfg = {**world['inr'], 'actor': world['actorA']}
    for n in (1, 2):
        post(seed, cfg, journal(cfg, f'HUGE-{n}', ('BANK_OPERATING', 'D', '6000000000000000.00'), ('OPENING_BALANCE_EQUITY', 'C', '6000000000000000.00')))
    a = signed_in(api, world, 'analyst')
    body = problem(a.get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"), 422, 'CAPABILITY_UNAVAILABLE')
    assert 'MONEY_RANGE' in body['detail']
    assert a.get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance", params={'as_of_seq': '1'}).json()['total_debit']['amount'] == '6000000000000000.00'


def test_historical_sequences_never_return_current_figures(api, world, seed):
    cfg = {**world['inr'], 'actor': world['actorA']}
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    captured = {}
    for n in range(1, 4):
        post(seed, cfg, journal(cfg, f'H-{n}', ('BANK_OPERATING', 'D', f'{n}0.00'), ('PROCESSOR_CLEARING', 'C', f'{n}0.00')))
        r = a.get(path)
        captured[n] = (r.content, r.headers['ETag'])
        assert r.json()['as_of_seq'] == n
    for n in range(1, 4):
        r = a.get(path, params={'as_of_seq': str(n)})
        assert (r.content, r.headers['ETag']) == captured[n], n        # byte-identical to what was current at seq n
    assert tb_map(a.get(path, params={'as_of_seq': '1'}).json())['1000'] == ('10.00', '0.00', '10.00')
    assert tb_map(a.get(path).json())['1000'] == ('60.00', '0.00', '60.00')


def test_as_of_date_is_refused_explicitly_and_never_silently_ignored(api, world, seed):
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'D-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    for params in ({'as_of_date': '2026-10-14'}, {'as_of_date': '2026-10-31'}, {'as_of_date': '2026-10-13'},
                   {'as_of_date': '2026-10-14', 'as_of_seq': '1'}):
        body = problem(a.get(path, params=params), 422, 'CAPABILITY_UNAVAILABLE')
        assert 'as_of_date' in body['detail'] and 'lines' not in body
    for bad in ('2026-13-01', '2026-02-30', '14/10/2026', '2026-10-14T00:00:00Z', ''):
        problem(a.get(path, params={'as_of_date': bad}), 422, 'SCHEMA_INVALID')


@pytest.mark.parametrize('value', ['-1', 'abc', '01', '1.0', '', ' 1', '1e1', '+1', '9' * 19])
def test_malformed_as_of_seq_is_422(api, world, value):
    a = signed_in(api, world, 'analyst')
    problem(a.get(f"{BASE}/ledgers/{world['inr']['ledger']}/trial-balance", params={'as_of_seq': value}), 422, 'SCHEMA_INVALID')


def test_trial_balance_is_refused_for_books_ledgers_but_metadata_reads_work(api, world):
    a = signed_in(api, world, 'analyst')
    led = a.get(f"{BASE}/ledgers/{world['books']['ledger']}").json()
    assert (led['purpose'], led['data_label'], led['state']) == ('BOOKS', None, 'EMPTY')
    assert a.get(f"{BASE}/ledgers/{world['books']['ledger']}/accounts").status_code == 200
    body = problem(a.get(f"{BASE}/ledgers/{world['books']['ledger']}/trial-balance"), 422, 'CAPABILITY_UNAVAILABLE')
    assert 'SANDBOX' in body['detail']


def test_period_close_advances_the_sequence_but_not_the_balances(database, api, world, seed):
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'PC-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
    a = signed_in(api, world, 'analyst')
    before = a.get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance").json()
    assert close_period(database, admin_token(database, world['A']), cfg['period'])['status'] == 200
    after = a.get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance").json()
    assert after['as_of_seq'] == 2 and after['lines'] == before['lines'] and after['total_debit'] == before['total_debit']
    periods = a.get(f"{BASE}/ledgers/{cfg['ledger']}/periods").json()['items']
    assert [(p['start_date'], p['end_date'], p['status'], p['close_seq']) for p in periods] == [('2026-10-01', '2026-11-01', 'CLOSED', '2')]


def test_projection_disagreement_fails_closed_at_the_head_only(database, api, world, seed):
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'P-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
    post(seed, cfg, journal(cfg, 'P-2', ('BANK_OPERATING', 'D', '5.00'), ('PROCESSOR_CLEARING', 'C', '5.00')))
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    assert a.get(path).status_code == 200
    with connection(database=database) as admin:     # deliberate corruption of derived data, superuser, triggers bypassed
        admin.execute('SET session_replication_role = replica')
        admin.execute('UPDATE lv.account_balance_current SET debit_total = debit_total + 0.01 WHERE ledger_id = %s AND account_id = %s',
                      (cfg['ledger'], cfg['chart']['BANK_OPERATING']))
    try:
        body = problem(a.get(path), 500, 'INTERNAL')
        assert 'lines' not in body and body['retryable'] is False
        problem(a.get(path, params={'as_of_seq': '2'}), 500, 'INTERNAL')
        assert tb_map(a.get(path, params={'as_of_seq': '1'}).json())['1000'] == ('10.00', '0.00', '10.00')   # not the head
    finally:
        with connection(database=database) as admin:
            admin.execute('SET session_replication_role = replica')
            admin.execute('UPDATE lv.account_balance_current SET debit_total = debit_total - 0.01 WHERE ledger_id = %s AND account_id = %s',
                          (cfg['ledger'], cfg['chart']['BANK_OPERATING']))
    assert a.get(path).status_code == 200


def test_an_unbalanced_snapshot_is_refused_not_displayed(database, api, world, seed):
    """I-04 (debits equal credits at every sequence). A historical snapshot is not covered by the projection check, so a
    corrupted journal line there must still be refused rather than shown as an unbalanced trial balance."""
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'UB-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
    post(seed, cfg, journal(cfg, 'UB-2', ('BANK_OPERATING', 'D', '5.00'), ('PROCESSOR_CLEARING', 'C', '5.00')))
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    fix = "UPDATE lv.journal_line SET amount = %s WHERE ledger_id = %s AND seq = 1 AND side = 'D'"
    with connection(database=database) as admin:     # deliberate corruption, superuser, guard triggers bypassed
        admin.execute('SET session_replication_role = replica')
        admin.execute(fix, ('11.00', cfg['ledger']))
    try:
        body = problem(a.get(path, params={'as_of_seq': '1'}), 500, 'INTERNAL')
        assert 'lines' not in body and 'balance' in body['detail']
    finally:
        with connection(database=database) as admin:
            admin.execute('SET session_replication_role = replica')
            admin.execute(fix, ('10.00', cfg['ledger']))
    assert a.get(path, params={'as_of_seq': '1'}).status_code == 200


def test_a_ledger_whose_only_commit_is_a_period_close_is_ACTIVE_with_no_lines(database, api, world):
    """2A §19.2 defines EMPTY as "sequence 0, no documents". A PERIOD_CLOSE commit (2A §8.1) advances the sequence, so the
    literal definition makes such a ledger ACTIVE. Recorded as gap RG-4 (it conflicts with BootstrapLedger eligibility)."""
    cfg = world['inr']
    assert close_period(database, admin_token(database, world['A']), cfg['period'])['status'] == 200
    a = signed_in(api, world, 'analyst')
    led = a.get(f"{BASE}/ledgers/{cfg['ledger']}").json()
    assert (led['state'], led['current_seq']) == ('ACTIVE', 1)
    tb = a.get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance").json()
    assert (tb['as_of_seq'], tb['lines'], tb['total_debit']['amount']) == (1, [], '0.00')


def test_trial_balance_line_cap_is_explicit(api, world, seed, monkeypatch):
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'CAP-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '5.00'), ('REVENUE_DEFAULT', 'C', '5.00')))
    a = signed_in(api, world, 'analyst')
    monkeypatch.setattr(ledger_reads, 'MAX_TRIAL_BALANCE_LINES', 3)
    assert len(a.get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance").json()['lines']) == 3
    monkeypatch.setattr(ledger_reads, 'MAX_TRIAL_BALANCE_LINES', 2)
    problem(a.get(f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"), 422, 'CAPABILITY_UNAVAILABLE')


# ------------------------------------------------------------------------------------------- C. API conventions
def test_unknown_and_repeated_query_parameters_are_rejected(api, world):
    a = signed_in(api, world, 'analyst')
    for path in routes(world['inr']['ledger']):
        body = problem(a.get(path, params={'sort': 'name'}), 400, 'UNKNOWN_QUERY_PARAMETER')
        assert body['errors'] == [{'pointer': '/query/sort', 'code': 'unknown'}]
    problem(a.get(f"{BASE}/ledgers/{world['inr']['ledger']}", params={'limit': '5'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    problem(a.get(f"{BASE}/ledgers/{world['inr']['ledger']}/trial-balance", params={'limit': '5'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    problem(a.get(f"{BASE}/ledgers/{world['inr']['ledger']}/accounts", params={'as_of_seq': '0'}), 400, 'UNKNOWN_QUERY_PARAMETER')
    problem(a.get(f'{BASE}/ledgers?limit=5&limit=6'), 422, 'SCHEMA_INVALID')
    problem(a.get(f"{BASE}/ledgers/{world['inr']['ledger']}/trial-balance?as_of_seq=0&as_of_seq=0"), 422, 'SCHEMA_INVALID')


@pytest.mark.parametrize('limit', ['0', '201', '-1', 'abc', '', '1.5', '007', '1000'])
def test_invalid_limits_are_422(api, world, limit):
    a = signed_in(api, world, 'analyst')
    for path in (f'{BASE}/ledgers', f"{BASE}/ledgers/{world['inr']['ledger']}/accounts", f"{BASE}/ledgers/{world['inr']['ledger']}/periods"):
        problem(a.get(path, params={'limit': limit}), 422, 'SCHEMA_INVALID')


def _many_ledgers_setup(app, tenant):
    """Seven SANDBOX ledgers in a fresh tenant; names repeat so the id tie-breaker decides order."""
    ids = []
    with tenant_transaction(app, tenant):
        for name in ('Beta', 'alpha', 'Beta', 'Alpha', 'Beta', 'Zed', 'alpha'):
            ids.append((name, str(app.execute("INSERT INTO lv.ledger(tenant_id, name, purpose, source_kind, currency, currency_exponent) "
                                               "VALUES (%s, %s, 'SANDBOX', 'NATIVE', 'INR', 2) RETURNING id", (tenant, name)).fetchone()[0])))
    return ids


def _many_ledgers(app,tenant):
    from configuration_support import configuration_connection
    with configuration_connection(app.info.dbname) as setup:return _many_ledgers_setup(setup,tenant)

def test_ledger_pagination_is_stable_with_equal_sort_values_and_exact_boundaries(database, app, api, world):
    from test_processor_payments import new_tenant
    tenant = new_tenant(database)
    expected = sorted(_many_ledgers(app, tenant), key=lambda x: (x[0].encode(), x[1]))
    with owner_transaction(database) as owner:
        _user(owner, world, 'pager', tenant, ['ANALYST'])
    c = signed_in(api, world, 'pager')
    for limit in (1, 2, 3, 6, 7, 8, 200):
        items, pages = walk(c, f'{BASE}/ledgers', limit)
        assert [(i['name'], i['id']) for i in items] == expected, limit
        assert pages == max(1, -(-7 // limit)), limit
    full = c.get(f'{BASE}/ledgers', params={'limit': '7'}).json()
    assert len(full['items']) == 7 and full['next_cursor'] is None
    assert c.get(f'{BASE}/ledgers').json()['next_cursor'] is None          # default limit 50
    first = c.get(f'{BASE}/ledgers', params={'limit': '3'}).json()
    # The same cursor with a different (valid) limit continues from the same position: limit is not part of the binding.
    tail = c.get(f'{BASE}/ledgers', params={'limit': '200', 'cursor': first['next_cursor']}).json()
    assert [(i['name'], i['id']) for i in first['items'] + tail['items']] == expected and tail['next_cursor'] is None


def test_account_and_period_pagination_cover_every_row_once_in_order(configuration, api, world):
    cfg = world['inr']
    with tenant_transaction(configuration, world['A']):
        for n in range(1, 14):
            configuration.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, "
                        "(date '2026-10-01' + make_interval(months => %s))::date, (date '2026-10-01' + make_interval(months => %s))::date)",
                        (world['A'], cfg['ledger'], n, n + 1))
        # 'a-0' is inserted before 'B-2': bytewise order puts 'B-2' first, case-insensitive order would not.
        for code in ('1000.1', '10', '2', 'A-1', 'a-1', 'a-0', 'B-2'):
            configuration.execute("INSERT INTO lv.account(tenant_id, ledger_id, code, name, type, currency, currency_exponent) "
                        "VALUES (%s, %s, %s, 'Synthetic extra', 'ASSET', 'INR', 2)", (world['A'], cfg['ledger'], code))
    a = signed_in(api, world, 'analyst')
    periods, pages = walk(a, f"{BASE}/ledgers/{cfg['ledger']}/periods", 5)
    assert pages == 3 and len(periods) == 14 and [p['start_date'] for p in periods] == sorted(p['start_date'] for p in periods)
    assert len({p['id'] for p in periods}) == 14
    accounts, pages = walk(a, f"{BASE}/ledgers/{cfg['ledger']}/accounts", 4)
    codes = [x['code'] for x in accounts]
    assert pages == 4 and codes == sorted(codes, key=str.encode) and len(set(codes)) == 14


def _tamper(cursor):
    payload, mac = cursor.split('.')
    flip = lambda s: s[:-1] + ('A' if s[-1] != 'A' else 'B')
    import base64
    forged = base64.urlsafe_b64encode(json.dumps({'v': 1, 'k': ['', '00000000-0000-0000-0000-000000000000']}, separators=(',', ':')).encode()).rstrip(b'=').decode()
    return [flip(payload) + '.' + mac, payload + '.' + flip(mac), payload, payload + '.' + mac + '.x', 'x' * 600, '', '.',
            forged + '.' + mac, payload + '.' + mac[:-4], '%%%.%%%', payload.upper() + '.' + mac]


def test_tampered_cursors_are_400_cursor_mismatch(database, app, api, world):
    from test_processor_payments import new_tenant
    tenant = new_tenant(database)
    _many_ledgers(app, tenant)
    with owner_transaction(database) as owner:
        _user(owner, world, 'pager', tenant, ['ANALYST'])
    c = signed_in(api, world, 'pager')
    cursor = c.get(f'{BASE}/ledgers', params={'limit': '2'}).json()['next_cursor']
    assert cursor and c.get(f'{BASE}/ledgers', params={'limit': '2', 'cursor': cursor}).status_code == 200
    for bad in _tamper(cursor):
        body = problem(c.get(f'{BASE}/ledgers', params={'limit': '2', 'cursor': bad}), 400, 'CURSOR_MISMATCH')
        assert body['errors'] == [{'pointer': '/query/cursor', 'code': 'invalid'}]


def test_cursors_do_not_cross_operations_ledgers_tenants_or_keys(database, configuration, api, world):
    cfg = world['inr']
    with tenant_transaction(configuration, world['A']):
        for n in range(1, 4):
            configuration.execute("INSERT INTO lv.fiscal_period(tenant_id, ledger_id, start_date, end_date) VALUES (%s, %s, "
                        "(date '2026-10-01' + make_interval(months => %s))::date, (date '2026-10-01' + make_interval(months => %s))::date)",
                        (world['A'], cfg['ledger'], n, n + 1))
    a = signed_in(api, world, 'analyst')
    acc_cursor = a.get(f"{BASE}/ledgers/{cfg['ledger']}/accounts", params={'limit': '2'}).json()['next_cursor']
    per_cursor = a.get(f"{BASE}/ledgers/{cfg['ledger']}/periods", params={'limit': '2'}).json()['next_cursor']
    led_cursor = a.get(f'{BASE}/ledgers', params={'limit': '1'}).json()['next_cursor']
    assert acc_cursor and per_cursor and led_cursor
    # Same tenant, other ledger with the same chart codes: the position would be meaningful there, but the binding refuses it.
    problem(a.get(f"{BASE}/ledgers/{world['usd']['ledger']}/accounts", params={'limit': '2', 'cursor': acc_cursor}), 400, 'CURSOR_MISMATCH')
    problem(a.get(f"{BASE}/ledgers/{cfg['ledger']}/periods", params={'limit': '2', 'cursor': acc_cursor}), 400, 'CURSOR_MISMATCH')
    problem(a.get(f"{BASE}/ledgers/{cfg['ledger']}/accounts", params={'limit': '2', 'cursor': per_cursor}), 400, 'CURSOR_MISMATCH')
    problem(a.get(f'{BASE}/ledgers', params={'cursor': acc_cursor}), 400, 'CURSOR_MISMATCH')
    problem(a.get(f"{BASE}/ledgers/{cfg['ledger']}/accounts", params={'cursor': led_cursor}), 400, 'CURSOR_MISMATCH')
    # Another tenant's session cannot use tenant A's ledger-list cursor, even though that route has no ledger id.
    b = signed_in(api, world, 'analystB')
    problem(b.get(f'{BASE}/ledgers', params={'cursor': led_cursor}), 400, 'CURSOR_MISMATCH')
    # A server with another cursor key (e.g. after a restart without LV_CURSOR_KEY) refuses old cursors.
    other = create_app(AuthSettings(), connect=lambda: connection('lv_app', database))
    try:
        a2 = signed_in(other, world, 'analyst')
        problem(a2.get(f"{BASE}/ledgers/{cfg['ledger']}/accounts", params={'limit': '2', 'cursor': acc_cursor}), 400, 'CURSOR_MISMATCH')
        assert a2.get(f"{BASE}/ledgers/{cfg['ledger']}/accounts", params={'limit': '2'}).status_code == 200
    finally:
        other.state.pool.close()


def test_a_configured_cursor_key_is_shared_between_processes(database, world):
    key = secrets.token_bytes(32)
    one = create_app(AuthSettings(cursor_key=key), connect=lambda: connection('lv_app', database))
    two = create_app(AuthSettings(cursor_key=key), connect=lambda: connection('lv_app', database))
    try:
        cursor = signed_in(one, world, 'analyst').get(f"{BASE}/ledgers/{world['inr']['ledger']}/accounts", params={'limit': '3'}).json()['next_cursor']
        r = signed_in(two, world, 'analyst').get(f"{BASE}/ledgers/{world['inr']['ledger']}/accounts", params={'limit': '3', 'cursor': cursor})
        assert r.status_code == 200 and [i['code'] for i in r.json()['items']] == ['3000', '4000', '4900']
    finally:
        one.state.pool.close(); two.state.pool.close()


def test_conditional_requests_return_304_only_for_the_current_representation(api, world, seed):
    cfg = {**world['inr'], 'actor': world['actorA']}
    a = signed_in(api, world, 'analyst')
    for path in routes(cfg['ledger']):
        r = a.get(path)
        etag = r.headers['ETag']
        assert etag.startswith('"r') and r.headers['Cache-Control'] == 'no-store'
        for header in (etag, f'W/{etag}', f'"rnope", {etag}', '*'):
            n = a.get(path, headers={'If-None-Match': header})
            assert n.status_code == 304 and n.content == b'' and n.headers['ETag'] == etag, (path, header)
        assert a.get(path, headers={'If-None-Match': '"rnope"'}).status_code == 200
        assert a.get(path, headers={'If-None-Match': etag.replace('"r', '"v')}).status_code == 200
    ledger_path, tb_path = routes(cfg['ledger'])[1], routes(cfg['ledger'])[4]
    old = {p: a.get(p).headers['ETag'] for p in (ledger_path, tb_path)}
    post(seed, cfg, journal(cfg, 'E-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
    for p in (ledger_path, tb_path):
        r = a.get(p, headers={'If-None-Match': old[p]})
        assert r.status_code == 200 and r.headers['ETag'] != old[p]
    # Another tenant's session presenting the ETag of tenant A's representation learns nothing (404, not 304).
    b = signed_in(api, world, 'analystB')
    problem(b.get(ledger_path, headers={'If-None-Match': old[ledger_path]}), 404, 'NOT_FOUND')
    problem(b.get(ledger_path, headers={'If-None-Match': '*'}), 404, 'NOT_FOUND')


def test_every_new_operation_is_documented_with_read_permission():
    spec = create_app(AuthSettings()).openapi()
    expected = {('/api/v1/ledgers', 'listLedgers'), ('/api/v1/ledgers/{ledger_id}', 'getLedger'),
                ('/api/v1/ledgers/{ledger_id}/accounts', 'listLedgerAccounts'), ('/api/v1/ledgers/{ledger_id}/periods', 'listLedgerPeriods'),
                ('/api/v1/ledgers/{ledger_id}/trial-balance', 'getTrialBalance')}
    for path, op in expected:
        operation = spec['paths'][path]['get']
        assert operation['operationId'] == op and operation['x-lv-permission'] == 'read'
        assert {'401', '403', '400'} <= set(operation['responses'])
    tb = spec['components']['schemas']['TrialBalance']
    assert set(tb['required']) >= {'as_of_seq', 'lines', 'total_debit', 'total_credit'}
    assert spec['components']['schemas']['Money']['properties']['amount']['pattern'] == '^[0-9]{1,16}(\\.[0-9]{1,4})?$'
    assert spec['components']['schemas']['SignedMoney']['properties']['amount']['pattern'] == '^-?[0-9]{1,16}(\\.[0-9]{1,4})?$'


# ------------------------------------------------------------------------------------------- D. read safety
def _session_row(database, token):
    from ledgervigil.auth.crypto import token_hash
    with connection(database=database) as admin:
        cols = [r[0] for r in admin.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'lv_identity' "
                                            "AND table_name = 'session' ORDER BY ordinal_position").fetchall()]
        row = admin.execute('SELECT * FROM lv_identity.session WHERE token_hash = %s', (token_hash(token),)).fetchone()
    return dict(zip(cols, row))


def _counters(database):
    with connection(database=database) as admin:
        return admin.execute('SELECT tenant_id, ledger_id, last_seq, last_entry_no, batch_head, event_head FROM lv.ledger_counter ORDER BY 1, 2').fetchall()


def test_reads_change_no_financial_or_audit_state_and_only_touch_session_bookkeeping(database, api, world, seed):
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'RS-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
    a = signed_in(api, world, 'analyst')
    token = a.cookies.get(COOKIE)
    before, counters, session_before = everything(database), _counters(database), _session_row(database, token)
    time.sleep(0.01)
    for path in routes(cfg['ledger']) + routes(world['books']['ledger']) + routes(world['b']['ledger']) + routes(uuid4()):
        for params in ({}, {'limit': '1'}, {'as_of_seq': '0'}, {'as_of_date': '2026-10-14'}, {'bogus': '1'}, {'cursor': 'x.y'}):
            r = a.get(path, params=params)
            r2 = a.get(path, params=params, headers={'If-None-Match': r.headers.get('ETag', '*')})
            assert r.status_code < 500 and r2.status_code < 500, (path, params, r.text)
    # Financial and audit state: byte-for-byte identical (every lv table, identity audit chain, stored idempotent responses).
    assert everything(database) == before
    assert _counters(database) == counters
    # Session bookkeeping is the only write: last_seen_at moves forward, nothing else in the session row changes.
    session_after = _session_row(database, token)
    assert session_after['last_seen_at'] > session_before['last_seen_at']
    assert {k: v for k, v in session_after.items() if k != 'last_seen_at'} == {k: v for k, v in session_before.items() if k != 'last_seen_at'}


def test_the_read_transaction_refuses_writes_even_if_route_code_attempts_one(database, api, world, monkeypatch):
    original = ledger_reads._read_ledger
    attempted = []

    def writing(ctx, ledger_id):
        attempted.append(True)
        from ledgervigil.auth.crypto import token_hash
        try:ctx.conn.execute('SELECT lv_identity.session_context(%s,true)',(token_hash(ctx.session_token),))
        except Exception as exc:
            assert exc.sqlstate=='25006',type(exc).__name__
            attempted.append('READ_ONLY');raise
        return original(ctx, ledger_id)
    monkeypatch.setattr(ledger_reads, '_read_ledger', writing)
    a = signed_in(api, world, 'analyst')
    before = everything(database)
    body = problem(a.get(f"{BASE}/ledgers/{world['inr']['ledger']}"), 500, 'INTERNAL')
    assert attempted==[True,'READ_ONLY'] and 'tampered' not in json.dumps(body)
    assert everything(database) == before
    with connection(database=database) as admin:
        assert admin.execute('SELECT name FROM lv.ledger WHERE id = %s', (world['inr']['ledger'],)).fetchone()[0] == 'Synthetic posting tests'


# ------------------------------------------------------------------------------------------- concurrency
def test_a_commit_between_snapshot_resolution_and_aggregation_cannot_leak_into_the_reported_sequence(database, api, world, seed, monkeypatch):
    """Deterministic interleaving: the request resolves as_of_seq = 1, then a posting COMMITS seq 2, then the aggregate runs.
    The response must describe seq 1 exactly (lines, totals, as_of_seq), and the next request must describe seq 2."""
    cfg = {**world['inr'], 'actor': world['actorA']}
    post(seed, cfg, journal(cfg, 'C-1', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    at_one = a.get(path).content
    fired = []

    def commit_in_between():
        if not fired:
            fired.append(post(seed, cfg, journal(cfg, 'C-2', ('BANK_OPERATING', 'D', '7.00'), ('PROCESSOR_CLEARING', 'C', '7.00'))))
    monkeypatch.setattr(ledger_reads, '_after_snapshot_resolved', commit_in_between)
    r = a.get(path)
    assert fired and fired[0]['seq'] == '2'
    assert r.status_code == 200 and r.content == at_one and r.json()['as_of_seq'] == 1
    monkeypatch.setattr(ledger_reads, '_after_snapshot_resolved', None)
    now = a.get(path).json()
    assert now['as_of_seq'] == 2 and tb_map(now)['1000'] == ('17.00', '0.00', '17.00')


def test_an_uncommitted_posting_is_invisible_and_does_not_block_reads(database, api, world):
    cfg = {**world['inr'], 'actor': world['actorA']}
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    held = connection('lv_seed', database)          # not autocommit: the posting holds the counter lock, uncommitted
    try:
        from psycopg.types.json import Jsonb
        held.execute("SELECT set_config('app.tenant_id', %s, true)", (str(cfg['tenant']),))
        result = held.execute('SELECT lv.seed_manual_journal(%s, %s)', (cfg['ledger'], Jsonb(journal(cfg, 'U-1', ('BANK_OPERATING', 'D', '10.00'),
                                                                                                  ('PROCESSOR_CLEARING', 'C', '10.00'))))).fetchone()[0]
        assert result['seq'] == '1'
        started = time.monotonic()
        body = a.get(path).json()
        assert time.monotonic() - started < 3
        assert body['as_of_seq'] == 0 and body['lines'] == []
        assert a.get(f"{BASE}/ledgers/{cfg['ledger']}").json()['current_seq'] == 0
        held.commit()
    finally:
        held.close()
    assert a.get(path).json()['as_of_seq'] == 1


def test_reads_racing_a_stream_of_postings_are_always_internally_consistent(database, api, world):
    """15 sequential postings of 10.00 (Dr BANK / Cr CLR) race 40 trial-balance reads. Each response must be exactly the
    state at its own as_of_seq: BANK = 10.00 x seq, CLR = -10.00 x seq, totals equal — never a mix of two sequences."""
    cfg = {**world['inr'], 'actor': world['actorA']}
    a = signed_in(api, world, 'analyst')
    path = f"{BASE}/ledgers/{cfg['ledger']}/trial-balance"
    errors, seen = [], set()

    def poster():
        try:
            with connection('lv_seed', database, autocommit=True) as c:
                for n in range(1, 16):
                    post(c, cfg, journal(cfg, f'S-{n}', ('BANK_OPERATING', 'D', '10.00'), ('PROCESSOR_CLEARING', 'C', '10.00')))
                    time.sleep(0.005)
        except Exception as e:      # pragma: no cover - reported below
            errors.append(e)
    t = threading.Thread(target=poster)
    t.start()
    for _ in range(40):
        body = a.get(path).json()
        n = body['as_of_seq']
        seen.add(n)
        expected = {} if n == 0 else {'1000': (f'{10 * n}.00', '0.00', f'{10 * n}.00'), '1150': ('0.00', f'{10 * n}.00', f'-{10 * n}.00')}
        assert tb_map(body) == expected, body
        assert body['total_debit'] == body['total_credit'] == {'amount': f'{10 * n}.00', 'currency': 'INR'}
    t.join()
    assert not errors and len(seen) > 1
    assert a.get(path).json()['as_of_seq'] == 15


def test_noncanonical_base64_aliases_are_refused_without_breaking_valid_cursors():
    import base64,hashlib
    from ledgervigil.reads.paging import CursorCodec
    from ledgervigil.auth.problems import ProblemError
    alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    context={'operation':'canonical-alias-regression'}
    position=['00000000-0000-0000-0000-000000000001']
    for i in range(256):
        codec=CursorCodec(hashlib.sha256(str(i).encode()).digest());cursor=codec.encode(context,position)
        payload,mac=cursor.split('.')
        alias=mac[:-1]+alphabet[alphabet.index(mac[-1])^1]
        assert base64.urlsafe_b64decode(mac+'=')==base64.urlsafe_b64decode(alias+'=')
        assert codec.decode(context,cursor,1)==position
        with pytest.raises(ProblemError) as e:codec.decode(context,payload+'.'+alias,1)
        assert e.value.code=='CURSOR_MISMATCH'
