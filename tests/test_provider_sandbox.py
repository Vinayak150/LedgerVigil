"""QuickBooks Online SANDBOX read-only connection (0035; PROPOSED PROV-1..PROV-8). Offline: Intuit is replaced by the
documented-shape mock in provider_support (RECORDED_MOCK provenance), never contacted. Real PostgreSQL, real API and
worker database roles; disposable database per test. All data SYNTHETIC."""
import hashlib
import json
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest

from tools.dev_db import connection, migrate
from tools.identity_admin import create_user, grant, owner_transaction
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from ledgervigil.provider import envelope, intuit, runtime
from ledgervigil.provider.settings import ConfigurationError, WorkerSettings, api_settings, worker_settings
from test_proposal_drafts import world, seed, signed_in, fingerprint, counters  # noqa: F401
from detection_support import database  # noqa: F401
import provider_support as ps

BASE = '/api/v1'
PROVIDER_WRITES = {'lv.provider_connection', 'lv.provider_oauth_state', 'lv.provider_credential', 'lv.provider_job', 'lv.provider_page',
                   'lv.provider_object', 'lv.api_idempotency', 'lv_identity.audit_event', 'lv_identity.audit_chain_head'}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = ps.write_config(tmp_path)
    monkeypatch.setenv('LV_PROVIDER_API_CONFIG', c['LV_PROVIDER_API_CONFIG'])
    monkeypatch.delenv('LV_PROVIDER_WORKER_CONFIG', raising=False)   # the API process never sees the worker file
    return c


@pytest.fixture
def papi(database):
    application = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database), provider=True)
    yield application
    application.state.pool.close()


@pytest.fixture
def qbo():
    return ps.FakeIntuit()


def work(database, cfg, qbo, **kw):
    return runtime.run_due(connection_factory=lambda: connection('lv_worker', database), transport=qbo, settings=ps.settings(cfg), **kw)


def post(c, path, key=None, match=None, body=None):
    return c.post(BASE + path, json={} if body is None else body, headers={'Idempotency-Key': key or uuid4().hex, **({'If-Match': match} if match else {})})


def connect(c, qbo, realm=None):
    r = post(c, '/provider-connections')
    assert r.status_code == 201, r.text
    q = parse_qs(urlsplit(r.json()['authorization_url']).query)
    cb = post(c, '/provider-connections/quickbooks/callback', body={'state': q['state'][0], 'code': qbo.issue_code(), 'realm_id': realm or qbo.realm})
    return r, cb


def active(database, cfg, qbo, c):
    _, cb = connect(c, qbo)
    assert cb.status_code == 202, cb.text
    assert [x['outcome'] for x in work(database, cfg, qbo)] == ['CONNECTED']
    conn = c.get(f'{BASE}/provider-connections/{cb.json()["id"]}').json()
    assert conn['state'] == 'ACTIVE'
    return conn


def sync(database, cfg, qbo, c, conn):
    r = post(c, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"')
    assert r.status_code == 202, r.text
    return r.json(), work(database, cfg, qbo)


def all_text(database):
    """Every text/bytea value in every lv/lv_identity table (superuser view), for secret scans."""
    with connection(database=database) as db:
        cols = db.execute("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema IN ('lv','lv_identity') "
                          "AND data_type IN ('text','bytea','jsonb','character','character varying')").fetchall()
        out = []
        for s, t, col in cols:
            out += [str(v) for (v,) in db.execute(psycopg.sql.SQL('SELECT {}::text FROM {}.{}').format(psycopg.sql.Identifier(col), psycopg.sql.Identifier(s),
                                                                                                     psycopg.sql.Identifier(t))).fetchall() if v is not None]
        return '\n'.join(out)


# ------------------------------------------------------------------------------------------------ boundary units (no DB)
def test_envelope_binding_and_public_key_cannot_open(tmp_path):
    private, public = envelope.generate_kek()
    pub, priv = envelope.load_public(public), envelope.load_private(private)
    sealed = envelope.seal(pub, b'secret-token', envelope.aad('tokens', 't', 'c', '1', '1'))
    assert b'secret-token' not in sealed and envelope.open_(priv, sealed, envelope.aad('tokens', 't', 'c', '1', '1')) == b'secret-token'
    for wrong in (envelope.aad('tokens', 't', 'c', '1', '2'), envelope.aad('tokens', 't', 'other', '1', '1'), envelope.aad('authorization-code', '1')):
        with pytest.raises(envelope.EnvelopeError):
            envelope.open_(priv, sealed, wrong)
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    with pytest.raises(envelope.EnvelopeError):
        envelope.open_(priv, tampered, envelope.aad('tokens', 't', 'c', '1', '1'))
    other = envelope.load_private(envelope.generate_kek()[0])
    with pytest.raises(envelope.EnvelopeError):
        envelope.open_(other, sealed, envelope.aad('tokens', 't', 'c', '1', '1'))
    assert not hasattr(pub, 'decrypt')


def test_guard_allows_only_sandbox_reads_and_oauth_endpoints():
    calls = []
    g = intuit.Guard(lambda *a, **k: calls.append(a) or intuit.Response(200, {}, b'{}'))
    for method, url in (('GET', 'https://quickbooks.api.intuit.com/v3/company/1/query'),           # production
                        ('POST', 'https://sandbox-quickbooks.api.intuit.com/v3/company/1/payment'),  # a write
                        ('GET', 'https://sandbox-quickbooks.api.intuit.com/connect/x'),
                        ('GET', 'https://example.com/'), ('GET', 'http://sandbox-quickbooks.api.intuit.com/v3/company/1/query'),
                        ('POST', 'https://oauth.platform.intuit.com/other')):
        with pytest.raises(intuit.TransportRefused):
            g.request(method, url)
    assert calls == []
    g.request('GET', 'https://sandbox-quickbooks.api.intuit.com/v3/company/1/query?query=x')
    redirecting = intuit.Guard(lambda *a, **k: intuit.Response(302, {'location': 'https://evil.example'}, b''))
    with pytest.raises(intuit.TransportRefused):
        redirecting.request('POST', intuit.TOKEN_ENDPOINT)
    assert intuit.query_text('Payment', 101, 100) == 'select * from Payment ORDERBY Id STARTPOSITION 101 MAXRESULTS 100'
    with pytest.raises(ValueError):
        intuit.query_text('Bill', 1, 1)   # outside the accepted read scope


def test_configuration_separates_api_and_worker_secrets(tmp_path):
    c = ps.write_config(tmp_path)
    s = api_settings({'LV_PROVIDER_API_CONFIG': c['LV_PROVIDER_API_CONFIG']})
    assert s.kek_id == ps.settings(c).kek_id and 'secret' not in repr(ps.settings(c)).replace('client_secret=<redacted>', '')
    leaky = tmp_path / 'leaky.env'
    leaky.write_text(open(c['LV_PROVIDER_WORKER_CONFIG']).read())
    with pytest.raises(ConfigurationError) as e:
        api_settings({'LV_PROVIDER_API_CONFIG': str(leaky)})
    assert e.value.code == 'PROVIDER_CONFIG_UNSAFE'
    assert worker_settings({'LV_PROVIDER_WORKER_CONFIG': c['LV_PROVIDER_WORKER_CONFIG']}).kek_id == s.kek_id
    assert 'worker' not in open(c['LV_PROVIDER_API_CONFIG']).read().split('LV_PROVIDER_KEK_PUBLIC=')[0]   # api.env names nothing in worker/
    (c['dir'] / 'worker.env').chmod(0o644)
    with pytest.raises(ConfigurationError) as e:
        worker_settings({'LV_PROVIDER_WORKER_CONFIG': c['LV_PROVIDER_WORKER_CONFIG']})
    assert e.value.code == 'PROVIDER_CONFIG_UNSAFE'
    (c['dir'] / 'worker.env').chmod(0o600)
    # L4: a symlinked secret file and a directory others can write are refused (read through one O_NOFOLLOW descriptor).
    link = c['dir'] / 'linked.env'
    link.symlink_to(c['dir'] / 'worker.env')
    with pytest.raises(ConfigurationError) as e:
        worker_settings({'LV_PROVIDER_WORKER_CONFIG': str(link)})
    assert e.value.code == 'PROVIDER_CONFIG_UNSAFE'
    c['dir'].chmod(0o777)
    try:
        with pytest.raises(ConfigurationError) as e:
            worker_settings({'LV_PROVIDER_WORKER_CONFIG': c['LV_PROVIDER_WORKER_CONFIG']})
        assert e.value.code == 'PROVIDER_CONFIG_UNSAFE'
    finally:
        c['dir'].chmod(0o700)
    with pytest.raises(ConfigurationError) as e:
        api_settings({})
    assert e.value.code == 'PROVIDER_NOT_CONFIGURED'


# ------------------------------------------------------------------------------------------------------- API + worker
def test_gate_off_by_default_and_not_configured_state(database, world, monkeypatch):
    monkeypatch.delenv('LV_PROVIDER_SANDBOX_ENABLED', raising=False)
    off = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database))
    try:
        assert not any(p.startswith('/api/v1/provider') for p in off.openapi()['paths'])
        assert signed_in(off, world, 'admin').get(f'{BASE}/provider-connections').status_code == 404
    finally:
        off.state.pool.close()
    monkeypatch.delenv('LV_PROVIDER_API_CONFIG', raising=False)
    on = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database), provider=True)
    try:
        admin = signed_in(on, world, 'admin')
        listing = admin.get(f'{BASE}/provider-connections').json()
        assert listing['configuration']['configured'] is False and listing['capabilities']['writes']['status'] == 'DISABLED'
        r = post(admin, '/provider-connections')
        assert (r.status_code, r.json()['code']) == (503, 'PROVIDER_NOT_CONFIGURED')
    finally:
        on.state.pool.close()


def test_connect_exchange_sync_end_to_end_without_secrets_at_rest(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    started, cb = connect(admin, qbo)
    url = urlsplit(started.json()['authorization_url'])
    q = parse_qs(url.query)
    assert (url.scheme, url.netloc, url.path) == ('https', 'appcenter.intuit.com', '/connect/oauth2')
    assert q['scope'] == ['com.intuit.quickbooks.accounting'] and q['response_type'] == ['code'] and q['client_id'] == ['SYNTHETIC-client-id']
    assert q['redirect_uri'] == ['http://127.0.0.1:3000/connections/quickbooks/callback'] and len(q['state'][0]) == 43
    assert cb.status_code == 202 and cb.json()['state'] == 'EXCHANGING' and cb.json()['realm_id'] == qbo.realm
    before = (fingerprint(database, PROVIDER_WRITES), counters(database))
    assert [x['outcome'] for x in work(database, cfg, qbo)] == ['CONNECTED']
    conn = admin.get(f'{BASE}/provider-connections/{cb.json()["id"]}').json()
    job, ran = sync(database, cfg, qbo, admin, conn)
    assert ran[0]['outcome'] == 'DONE'
    after = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    done = next(j for j in after['jobs'] if j['id'] == job['id'])
    assert done['state'] == 'DONE' and done['result']['status'] == 'COMPLETED' and done['result']['provenance'] == 'RECORDED_MOCK'
    assert after['replica'] == {'Preferences': 1, 'CompanyInfo': 1, 'Account': 3, 'Customer': 1, 'Invoice': 1, 'Payment': 5, 'Deposit': 1}
    assert after['data_label'].startswith('PROVIDER SANDBOX DATA') and after['writes'] == 'DISABLED'
    # Only GETs to the sandbox host, plus the two token calls; never a write, never production.
    assert {(m, h) for m, h, p, _ in qbo.calls} == {('POST', 'oauth.platform.intuit.com'), ('GET', 'sandbox-quickbooks.api.intuit.com')}
    # Exact decimals end to end: the object text keeps the provider's digits.
    page = admin.get(f'{BASE}/provider-connections/{conn["id"]}/objects?entity=Payment').json()
    assert [o['provider_id'] for o in page['items']] == ['200', '201', '202', '203', '204'] and all(o['provenance'] == 'RECORDED_MOCK' for o in page['items'])
    texts = [admin.get(f'{BASE}/provider-objects/{o["id"]}').json()['object_text'] for o in page['items']]
    assert '"TotalAmt": 50000.00' in texts[0] and '"TotalAmt": 2500.50' in texts[1] and '"TotalAmt": 0.10' in texts[2]
    assert all(hashlib.sha256(t.encode()).hexdigest() == o['object_sha256'] for t, o in zip(texts, page['items']))
    # No financial or workflow table or ledger counter changed; no secret of any kind is stored in clear.
    assert (fingerprint(database, PROVIDER_WRITES), counters(database)) == before
    stored = all_text(database)
    state = q['state'][0]
    worker_env = open(cfg['LV_PROVIDER_WORKER_CONFIG']).read()
    secret = next(line.split('=', 1)[1] for line in worker_env.splitlines() if line.startswith('LV_QBO_CLIENT_SECRET='))
    for leaked in [state, secret, *qbo.access, *qbo.refresh, *qbo.revoked]:
        assert leaked not in stored
    # The runtime role cannot read credentials, OAuth states or the sealed exchange payload.
    with connection('lv_app', database) as app:
        for statement in ('SELECT envelope FROM lv.provider_credential', 'SELECT state_hash FROM lv.provider_oauth_state', 'SELECT payload FROM lv.provider_job'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(statement)
            app.rollback()


def test_callback_state_is_session_bound_single_use_and_expires(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    r = post(admin, '/provider-connections')
    state = parse_qs(urlsplit(r.json()['authorization_url']).query)['state'][0]
    body = {'state': state, 'code': qbo.issue_code(), 'realm_id': qbo.realm}
    other_session = signed_in(papi, world, 'admin')   # same user, different browser session
    r2 = post(other_session, '/provider-connections/quickbooks/callback', body=body)
    assert (r2.status_code, r2.json()['code']) == (400, 'OAUTH_STATE_INVALID')
    forged = post(admin, '/provider-connections/quickbooks/callback', body={**body, 'state': 'A' * 43})
    assert (forged.status_code, forged.json()['code']) == (400, 'OAUTH_STATE_INVALID')
    key = uuid4().hex
    ok = post(admin, '/provider-connections/quickbooks/callback', key=key, body=body)
    assert ok.status_code == 202
    replay = post(admin, '/provider-connections/quickbooks/callback', key=key, body=body)
    assert replay.status_code == 202 and replay.headers['Idempotent-Replayed'] == 'true'
    again = post(admin, '/provider-connections/quickbooks/callback', body=body)
    assert (again.status_code, again.json()['code']) == (409, 'OAUTH_STATE_USED')
    assert work(database, cfg, qbo)[0]['outcome'] == 'CONNECTED'
    # Expiry: a fresh attempt whose state is older than 10 minutes is refused and the attempt fails.
    conn = admin.get(f'{BASE}/provider-connections').json()['items'][0]
    post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{conn["etag"]}"'); work(database, cfg, qbo)
    r = post(admin, '/provider-connections')
    state = parse_qs(urlsplit(r.json()['authorization_url']).query)['state'][0]
    with connection(database=database) as db:
        db.execute('SET session_replication_role = replica')   # superuser test setup only: age the state row
        db.execute("UPDATE lv.provider_oauth_state SET created_at=created_at-interval '11 minutes', expires_at=expires_at-interval '11 minutes' WHERE consumed_at IS NULL")
        db.commit()
    late = post(admin, '/provider-connections/quickbooks/callback', body={'state': state, 'code': qbo.issue_code(), 'realm_id': qbo.realm})
    assert (late.status_code, late.json()['code']) == (400, 'OAUTH_STATE_EXPIRED')
    assert admin.get(f'{BASE}/provider-connections/{r.json()["connection"]["id"]}').json()['failure_code'] == 'AUTHORIZATION_EXPIRED'


def test_provider_error_denied_and_cross_tenant_realm_binding(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    r = post(admin, '/provider-connections')
    state = parse_qs(urlsplit(r.json()['authorization_url']).query)['state'][0]
    denied = post(admin, '/provider-connections/quickbooks/callback', body={'state': state, 'error': 'access_denied'})
    assert denied.status_code == 200 and (denied.json()['state'], denied.json()['failure_code']) == ('FAILED', 'AUTHORIZATION_DENIED')
    active(database, cfg, qbo, admin)
    email, password = f'adminB-{uuid4().hex[:8]}@test.ledgervigil.invalid', 'pw-' + uuid4().hex
    with owner_transaction(database) as owner:
        uid = create_user(owner, email, 'Synthetic adminB', password)
        grant(owner, world['B'], uid, ['ADMINISTRATOR'])
        owner.execute("UPDATE lv_identity.membership_role SET granted_at=statement_timestamp()-interval '48 hours' WHERE tenant_id=%s AND user_id=%s", (world['B'], uid))
    b = signed_in_with(papi, email, password)
    _, cb = connect(b, qbo)   # tenant B tries to bind the realm tenant A already holds
    # The callback reveals nothing about other organizations (no existence oracle): it is accepted like any other.
    assert cb.status_code == 202 and cb.json()['state'] == 'EXCHANGING', cb.text
    revoked_before = len(qbo.revoked)
    assert work(database, cfg, qbo)[0]['outcome'] == 'REALM_ALREADY_CONNECTED'
    failed = b.get(f'{BASE}/provider-connections/{cb.json()["id"]}').json()
    assert (failed['state'], failed['failure_code']) == ('FAILED', 'REALM_ALREADY_CONNECTED')
    # Not revoked: Intuit has one connection per app and company, so revoking could disconnect A (review N4). Never stored.
    assert len(qbo.revoked) == revoked_before
    assert [x['state'] for x in admin.get(f'{BASE}/provider-connections').json()['items'] if x['state'] != 'FAILED'] == ['ACTIVE']
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv.provider_credential WHERE tenant_id=%s', (world['B'],)).fetchone()[0] == 0
    # Tenant isolation: B sees no A connection and A's ids answer 404 to B.
    a_conn = admin.get(f'{BASE}/provider-connections').json()['items'][0]
    assert all(x['id'] != a_conn['id'] for x in b.get(f'{BASE}/provider-connections').json()['items'])
    assert b.get(f'{BASE}/provider-connections/{a_conn["id"]}').status_code == 404
    assert post(b, f'/provider-connections/{a_conn["id"]}/syncs', match=f'"{a_conn["etag"]}"').status_code == 404


def signed_in_with(application, email, password):
    from test_proposal_drafts import client
    c = client(application)
    r = c.post(f'{BASE}/auth/login', json={'email': email, 'password': password})
    assert r.status_code == 200, r.text
    c.headers['X-CSRF-Token'] = r.json()['csrf_token']
    return c


def test_permissions_if_match_and_one_read_at_a_time(database, world, papi, cfg, qbo):
    analyst = signed_in(papi, world, 'analyst')
    assert analyst.get(f'{BASE}/provider-connections').status_code == 200          # read: state is visible
    assert post(analyst, '/provider-connections').status_code == 403                # connect: administer only
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    assert post(analyst, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"').status_code == 403
    assert post(admin, f'/provider-connections/{conn["id"]}/syncs').status_code == 428
    assert post(admin, f'/provider-connections/{conn["id"]}/syncs', match='"v999"').status_code == 412
    assert post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"').status_code == 202
    second = post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"')
    assert (second.status_code, second.json()['code']) == (409, 'SYNC_IN_PROGRESS')
    assert post(admin, '/provider-connections').json()['code'] == 'CONNECTION_EXISTS'


def test_throttle_resumes_from_the_last_committed_page_and_bound_marks_partial(database, world, papi, cfg, qbo, monkeypatch):
    monkeypatch.setattr(intuit, 'PAGE_SIZE', 2)
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    qbo.faults.append((lambda m, p, q: 'Payment' in q.get('query', [''])[0] and 'STARTPOSITION 3 ' in q['query'][0], 429, b'{"Fault":{"Error":[{"code":"003001"}],"type":"ThrottleExceeded"}}'))
    job, ran = sync(database, cfg, qbo, admin, conn)
    assert ran[0]['outcome'] == 'THROTTLED'
    with connection(database=database) as db:
        state, attempts, throttles, wait = db.execute("SELECT state, attempts, throttles, available_at-clock_timestamp() > interval '55 seconds' FROM lv.provider_job WHERE id=%s", (job['id'],)).fetchone()
        assert (state, attempts, throttles, wait) == ('QUEUED', 0, 1, True)
        db.execute("UPDATE lv.provider_job SET available_at=clock_timestamp() WHERE id=%s", (job['id'],)); db.commit()
    payment_calls = lambda: [c[3] for c in qbo.calls if c[3] and 'from Payment' in c[3]]
    before = len(payment_calls())
    assert work(database, cfg, qbo)[0]['outcome'] == 'DONE'
    resumed = payment_calls()[before:]
    assert resumed[0].endswith('STARTPOSITION 3 MAXRESULTS 2')            # pages 1-2 were committed and not fetched again
    with connection(database=database) as db:
        assert db.execute("SELECT count(*) FROM lv.provider_object WHERE entity='Payment'").fetchone()[0] == 5
    monkeypatch.setattr(intuit, 'MAX_PAGES_PER_ENTITY', 2)
    conn = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    job, ran = sync(database, cfg, qbo, admin, conn)
    result = next(j for j in admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()['jobs'] if j['id'] == job['id'])['result']
    assert result['status'] == 'PARTIAL' and result['truncated_entities'] == ['Payment']   # Account (3 objects) fits in 2 pages of 2
    # Re-reading identical (Id, SyncToken) objects stores nothing new.
    assert all(c['new_versions'] == 0 for c in result['counts'].values())


def test_expiring_token_refreshes_once_and_rotation_is_persisted_before_use(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    with connection(database=database) as db:
        db.execute("UPDATE lv.provider_credential SET access_expires_at=clock_timestamp()+interval '1 minute'"); db.commit()
    job, ran = sync(database, cfg, qbo, admin, conn)
    assert ran[0]['outcome'] == 'DONE' and qbo.refresh_calls == 1
    with connection(database=database) as db:
        assert db.execute('SELECT token_version FROM lv.provider_credential').fetchone()[0] == 2


def test_concurrent_refreshers_call_intuit_once(database, world, papi, cfg, qbo):
    """A stalled worker (old fence) is refreshing when its job is reclaimed by a second worker. The second worker finds the
    per-connection refresh lease held, waits, and uses the rotated pair the first one commits: Intuit sees one refresh."""
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    job = post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"').json()['id']
    with connection(database=database) as db:
        db.execute("UPDATE lv.provider_credential SET access_expires_at=clock_timestamp()+interval '1 minute'"); db.commit()
    factory = lambda: connection('lv_worker', database)
    with factory() as w:
        c1 = w.execute('SELECT lv.provider_claim(%s,%s,%s)', (world['A'], job, 'w1')).fetchone()[0]; w.commit()
    s, g, results, errors = ps.settings(cfg), intuit.Guard(qbo), {}, []
    qbo.refresh_gate = threading.Event()

    def call(name, claim, fence, sleep):
        try:
            results[name] = runtime._access_token(world['A'], job, name, fence, claim, s, g, factory, sleep)
        except Exception as e:   # recorded, asserted below
            errors.append((name, repr(e)))
    t1 = threading.Thread(target=call, args=('w1', c1, c1['fence'], lambda x: None)); t1.start()
    for _ in range(200):
        if qbo.refresh_calls:
            break
        threading.Event().wait(0.02)
    assert qbo.refresh_calls == 1
    with connection(database=database) as db:   # w1 stalls past its job lease; the sweeper requeues and w2 claims
        db.execute("UPDATE lv.provider_job SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (job,)); db.commit()
    with factory() as w:
        list(w.execute('SELECT lv.provider_due(10)')); w.commit()
        c2 = w.execute('SELECT lv.provider_claim(%s,%s,%s)', (world['A'], job, 'w2')).fetchone()[0]; w.commit()
    assert c2['fence'] > c1['fence']
    t2 = threading.Thread(target=call, args=('w2', c2, c2['fence'], lambda x: threading.Event().wait(0.05))); t2.start()
    threading.Event().wait(0.3)
    assert qbo.refresh_calls == 1 and 'w2' not in results   # w2 is waiting on the refresh lease, not calling Intuit
    qbo.refresh_gate.set(); t1.join(10); t2.join(10)
    assert errors == [] and qbo.refresh_calls == 1
    assert results['w1'][1] == results['w2'][1] == 2 and results['w1'][0] == results['w2'][0]
    with connection(database=database) as db:
        assert db.execute('SELECT token_version, lease_owner FROM lv.provider_credential').fetchone() == (2, None)


def test_auth_rejection_degrades_and_invalid_refresh_degrades(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    unauth = b'{"Fault":{"Error":[{"Message":"AuthenticationFailed","code":"3200"}],"type":"AUTHENTICATION"}}'
    for _ in range(2):                       # 401 -> one refresh -> 401 again with the new token: AUTH_INVALID, no third try
        qbo.faults.append((lambda m, p, q: p.startswith('/v3/'), 401, unauth))
    job, ran = sync(database, cfg, qbo, admin, conn)
    assert ran[0]['outcome'] == 'AUTH_INVALID' and qbo.refresh_calls == 1
    after = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (after['state'], after['failure_code']) == ('DEGRADED', 'AUTH_INVALID')
    r = post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{after["etag"]}"')
    assert (r.status_code, r.json()['code']) == (409, 'CONNECTION_DEGRADED')


def test_invalid_refresh_token_degrades_without_retrying(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    qbo.access.clear(); qbo.refresh.clear()  # Intuit revoked the grant (for example the user disconnected the app at Intuit)
    job, ran = sync(database, cfg, qbo, admin, conn)
    assert ran[0]['outcome'] == 'AUTH_INVALID' and qbo.refresh_calls == 1
    after = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (after['state'], after['failure_code']) == ('DEGRADED', 'AUTH_INVALID')
    with connection(database=database) as db:
        assert db.execute('SELECT token_version, lease_owner FROM lv.provider_credential').fetchone() == (1, None)


def test_unavailable_provider_backs_off_then_dead_after_five_attempts(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    for _ in range(5):
        qbo.faults.append((lambda m, p, q: p.startswith('/v3/'), 503, b'unavailable'))
    job, _ = sync(database, cfg, qbo, admin, conn)
    for _ in range(4):
        with connection(database=database) as db:
            db.execute('UPDATE lv.provider_job SET available_at=clock_timestamp() WHERE id=%s', (job['id'],)); db.commit()
        work(database, cfg, qbo)
    with connection(database=database) as db:
        assert db.execute('SELECT state, attempts, last_error FROM lv.provider_job WHERE id=%s', (job['id'],)).fetchone() == ('DEAD', 5, 'PROVIDER_UNAVAILABLE')
    assert admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()['state'] == 'ACTIVE'   # a read failure never degrades auth


def test_disconnect_revokes_at_intuit_and_erases_tokens_even_when_unconfirmed(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    refresh_token = next(iter(qbo.refresh))
    r = post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{conn["etag"]}"')
    assert r.status_code == 202 and r.json()['state'] == 'REVOKING'
    assert work(database, cfg, qbo)[0]['outcome'] == 'PROVIDER_CONFIRMED' and qbo.revoked == [refresh_token]
    done = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (done['state'], done['revoke_outcome']) == ('REVOKED', 'PROVIDER_CONFIRMED')
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv.provider_credential').fetchone()[0] == 0
    conn = active(database, cfg, qbo, admin)
    live = next(iter(qbo.refresh))
    for _ in range(5):
        qbo.faults.append((lambda m, p, q: p == '/v2/oauth2/tokens/revoke', 503, b''))
    post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{conn["etag"]}"')
    for _ in range(5):
        with connection(database=database) as db:
            db.execute("UPDATE lv.provider_job SET available_at=clock_timestamp() WHERE kind='REVOKE' AND state='QUEUED'"); db.commit()
        work(database, cfg, qbo)
    # Intuit unreachable five times: the encrypted tokens are the only way to revoke the grant, so they are KEPT.
    pending = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (pending['state'], pending['failure_code'], pending['revoke_outcome']) == ('REVOKING', 'REVOCATION_PENDING', None)
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv.provider_credential').fetchone()[0] == 1
        assert db.execute("SELECT state FROM lv.provider_job WHERE kind='REVOKE' AND connection_id=%s", (conn['id'],)).fetchone()[0] == 'DEAD'
    assert live in qbo.refresh   # the grant is still live at Intuit; nothing pretends otherwise
    # A pending connection cannot be used: reads are refused.
    r = post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{pending["etag"]}"')
    assert r.status_code == 409
    # No automatic retry within the hour; after it, the scheduler queues another attempt, which now succeeds.
    assert work(database, cfg, qbo) == []
    with connection(database=database) as db:
        db.execute('SET session_replication_role = replica')   # superuser test setup only: age the dead attempt by an hour
        db.execute("UPDATE lv.provider_job SET finished_at=finished_at-interval '61 minutes' WHERE kind='REVOKE' AND connection_id=%s", (conn['id'],)); db.commit()
    assert [x['outcome'] for x in work(database, cfg, qbo)] == ['PROVIDER_CONFIRMED']
    done = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (done['state'], done['revoke_outcome']) == ('REVOKED', 'PROVIDER_CONFIRMED') and live in qbo.revoked and live not in qbo.refresh
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv.provider_credential').fetchone()[0] == 0


def test_pending_revocation_can_be_retried_by_an_administrator(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    for _ in range(5):
        qbo.faults.append((lambda m, p, q: p == '/v2/oauth2/tokens/revoke', 503, b''))
    post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{conn["etag"]}"')
    for _ in range(5):
        with connection(database=database) as db:
            db.execute("UPDATE lv.provider_job SET available_at=clock_timestamp() WHERE kind='REVOKE' AND state='QUEUED'"); db.commit()
        work(database, cfg, qbo)
    pending = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    r = post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{pending["etag"]}"')
    assert r.status_code == 202, r.text
    again = post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{pending["etag"]}"')
    assert (again.status_code, again.json()['code']) == (409, 'REQUEST_IN_PROGRESS')
    assert [x['outcome'] for x in work(database, cfg, qbo)] == ['PROVIDER_CONFIRMED']
    assert admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()['state'] == 'REVOKED'


def test_requester_revoked_before_exchange_denies_and_fails(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    _, cb = connect(admin, qbo)
    with owner_transaction(database) as owner:
        owner.execute("DELETE FROM lv_identity.membership_role WHERE tenant_id=%s AND user_id=%s", (world['A'], world['users']['admin']['id']))
    assert work(database, cfg, qbo)[0]['status'] == 'NOT_CLAIMED'
    with connection(database=database) as db:
        assert db.execute('SELECT state, last_error, payload IS NULL FROM lv.provider_job').fetchone() == ('DENIED', 'REQUESTER_REVOKED', True)
        assert db.execute('SELECT state, failure_code FROM lv.provider_connection').fetchone() == ('FAILED', 'REQUESTER_REVOKED')
    assert qbo.calls == []   # nothing was sent to Intuit


def test_books_stepup_is_required_to_enqueue_but_worker_does_not_impersonate_session(database, world, papi, cfg, qbo):
    unverified = signed_in(papi, world, 'admin', with_mfa=False)
    refused = post(unverified, '/provider-connections')
    assert refused.status_code == 403
    assert qbo.calls == []
    verified = signed_in(papi, world, 'admin')
    connect(verified, qbo)
    # A completed authorization can outlive the interactive step-up. The worker
    # still checks current role/membership (covered by the adjacent revocation test).
    with connection(database=database) as db:
        db.execute("UPDATE lv_identity.session SET mfa_verified_at=statement_timestamp()-interval '16 minutes'")
    assert [x['outcome'] for x in work(database, cfg, qbo)] == ['CONNECTED']


def test_exchange_record_failure_revokes_the_new_grant(database, world, papi, cfg, qbo, monkeypatch):
    """M1: once Intuit has issued tokens, any failure before they are recorded revokes them; nothing is left live and unrecorded."""
    admin = signed_in(papi, world, 'admin')
    _, cb = connect(admin, qbo)
    def broken(*a, **k):
        raise RuntimeError('SYNTHETIC failure after the exchange')
    monkeypatch.setattr(runtime, '_seal_tokens', broken)
    ran = work(database, cfg, qbo)
    assert (ran[0]['outcome'], ran[0]['error_class']) == ('TERMINAL', 'WORKER_ERROR')
    assert len(qbo.revoked) == 1 and qbo.refresh == set()   # the only grant issued is revoked at Intuit
    after = admin.get(f'{BASE}/provider-connections/{cb.json()["id"]}').json()
    assert after['state'] != 'ACTIVE'
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv.provider_credential').fetchone()[0] == 0


def test_realm_verification_requires_exactly_one_company(database, world, papi, cfg, qbo):
    """L1: an empty CompanyInfo answer does not verify the realm; the attempt fails and the grant is revoked."""
    admin = signed_in(papi, world, 'admin')
    qbo.data['CompanyInfo'] = []
    _, cb = connect(admin, qbo)
    ran = work(database, cfg, qbo)
    assert (ran[0]['outcome'], ran[0]['error_class']) == ('TERMINAL', 'REALM_VERIFICATION_FAILED')
    after = admin.get(f'{BASE}/provider-connections/{cb.json()["id"]}').json()
    assert (after['state'], after['failure_code']) == ('FAILED', 'REALM_VERIFICATION_FAILED')
    assert len(qbo.revoked) == 1 and qbo.refresh == set()


def test_refresh_landing_after_disconnect_is_revoked_not_stored(database, world, papi, cfg, qbo, monkeypatch):
    """M4: a refresh in flight when the user disconnects is refused by the database and its new refresh token is revoked."""
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    with connection(database=database) as db:
        db.execute("UPDATE lv.provider_credential SET access_expires_at=clock_timestamp()+interval '1 minute'"); db.commit()
    post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"')

    def disconnect_now():
        current = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
        r = post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{current["etag"]}"')
        assert r.status_code == 202 and r.json()['state'] == 'REVOKING', r.text
    monkeypatch.setattr(runtime, '_before_refresh_finish', disconnect_now)
    ran = work(database, cfg, qbo)
    assert (ran[0]['outcome'], ran[0]['error_class']) == ('TERMINAL', 'CONNECTION_NOT_ACTIVE') and qbo.refresh_calls == 1
    monkeypatch.setattr(runtime, '_before_refresh_finish', None)
    with connection(database=database) as db:
        assert db.execute('SELECT token_version FROM lv.provider_credential').fetchone()[0] == 1   # the new pair was never stored
        db.execute("UPDATE lv.provider_job SET available_at=clock_timestamp() WHERE kind='REVOKE' AND state='QUEUED'"); db.commit()
    for _ in range(2):
        work(database, cfg, qbo)
    done = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert done['state'] == 'REVOKED'
    assert qbo.refresh == set()   # no refresh token, old or new, is still live at Intuit
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv.provider_credential').fetchone()[0] == 0


def test_revoke_retries_when_a_newer_pair_was_committed(database, world, papi, cfg, qbo, monkeypatch):
    """M4 (defence in depth): revoke_finish refuses to erase a credential whose version changed after the claim; the retry
    revokes the newer pair as well."""
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{conn["etag"]}"')
    real, s, bumped = intuit.revoke, ps.settings(cfg), []

    def revoke_and_race(guard, settings, token):
        outcome = real(guard, settings, token)
        if not bumped:   # superuser test setup: a rotated pair is committed between the revoke call and its record
            newer = qbo._tokens()
            with connection(database=database) as db:
                row = db.execute('SELECT tenant_id, connection_id FROM lv.provider_credential').fetchone()
                env = runtime._seal_tokens(s, row[0], row[1], qbo.realm, 2, newer)
                db.execute('SET session_replication_role = replica')
                db.execute('UPDATE lv.provider_credential SET envelope=%s, token_version=2', (env,)); db.commit()
            bumped.append(newer['refresh_token'])
        return outcome
    monkeypatch.setattr(intuit, 'revoke', revoke_and_race)
    ran = work(database, cfg, qbo)
    assert (ran[0]['outcome'], ran[0]['error_class']) == ('RETRYABLE', 'NEWER_TOKEN_TO_REVOKE')
    with connection(database=database) as db:
        assert db.execute('SELECT token_version FROM lv.provider_credential').fetchone()[0] == 2
        db.execute("UPDATE lv.provider_job SET available_at=clock_timestamp() WHERE kind='REVOKE' AND state='QUEUED'"); db.commit()
    work(database, cfg, qbo)
    assert bumped[0] in qbo.revoked and qbo.refresh == set()
    done = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (done['state'], done['revoke_outcome']) == ('REVOKED', 'PROVIDER_CONFIRMED')


def test_unconfigured_and_unauthorized_callers_learn_nothing(database, world, papi, monkeypatch):
    """L5: configuration state is disclosed only after authentication and the administer check."""
    monkeypatch.delenv('LV_PROVIDER_API_CONFIG', raising=False)
    analyst = signed_in(papi, world, 'analyst')
    r = post(analyst, '/provider-connections')
    assert (r.status_code, r.json()['code']) == (403, 'PERMISSION_DENIED') and 'reason' not in r.json()
    cb = post(analyst, '/provider-connections/quickbooks/callback', body={'state': 'A' * 43, 'code': 'SYNTHETICCODE1', 'realm_id': '1'})
    assert (cb.status_code, cb.json()['code']) == (403, 'PERMISSION_DENIED')
    admin = signed_in(papi, world, 'admin')
    r = post(admin, '/provider-connections')
    assert (r.status_code, r.json()['code']) == (503, 'PROVIDER_NOT_CONFIGURED')


def _pending_revocation(database, cfg, qbo, admin):
    conn = active(database, cfg, qbo, admin)
    for _ in range(5):
        qbo.faults.append((lambda m, p, q: p == '/v2/oauth2/tokens/revoke', 503, b''))
    post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{conn["etag"]}"')
    for _ in range(5):
        with connection(database=database) as db:
            db.execute("UPDATE lv.provider_job SET available_at=clock_timestamp() WHERE kind='REVOKE' AND state='QUEUED'"); db.commit()
        work(database, cfg, qbo)
    pending = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (pending['state'], pending['failure_code'], pending['revocation_auto_retry']) == ('REVOKING', 'REVOCATION_PENDING', True)
    return pending


def test_a_pending_revocation_can_be_abandoned_explicitly(database, world, papi, cfg, qbo):
    """Review N1: a revocation that cannot complete has an audited way out; the outcome says the grant may still be live."""
    admin = signed_in(papi, world, 'admin')
    pending = _pending_revocation(database, cfg, qbo, admin)
    r = post(admin, f'/provider-connections/{pending["id"]}/disconnect', match=f'"{pending["etag"]}"', body={'abandon_revocation': True})
    assert r.status_code == 200, r.text
    done = r.json()
    assert (done['state'], done['revoke_outcome'], done['failure_code']) == ('REVOKED', 'ABANDONED', None)
    with connection(database=database) as db:
        assert db.execute('SELECT count(*) FROM lv.provider_credential').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM lv_identity.audit_event WHERE event_type='PROVIDER_REVOCATION_ABANDONED'").fetchone()[0] == 1
    bad = post(admin, f'/provider-connections/{pending["id"]}/disconnect', match=f'"{done["etag"]}"', body={'abandon_revocation': 'yes'})
    assert bad.status_code == 422
    # The organization can connect again.
    assert post(admin, '/provider-connections').status_code == 201


def test_revocation_that_cannot_succeed_is_not_retried_automatically(database, world, papi, cfg, qbo, tmp_path):
    """Review N1: an envelope this worker cannot open never succeeds on retry, so no automatic requeue is made."""
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    post(admin, f'/provider-connections/{conn["id"]}/disconnect', match=f'"{conn["etag"]}"')
    (tmp_path / 'other').mkdir()
    stranger = ps.settings(ps.write_config(tmp_path / 'other'))   # a worker holding a different key-encryption key
    ran = runtime.run_due(connection_factory=lambda: connection('lv_worker', database), transport=qbo, settings=stranger)
    assert (ran[0]['outcome'], ran[0]['error_class']) == ('TERMINAL', 'ENVELOPE_INVALID')
    pending = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    assert (pending['state'], pending['failure_code'], pending['revocation_auto_retry']) == ('REVOKING', 'REVOCATION_PENDING', False)
    with connection(database=database) as db:
        db.execute('SET session_replication_role = replica')
        db.execute("UPDATE lv.provider_job SET finished_at=finished_at-interval '2 hours' WHERE kind='REVOKE'"); db.commit()
    assert work(database, cfg, qbo) == []


def test_envelope_header_must_match_its_recorded_key(database, world, papi, cfg, qbo):
    """Review N3: a credential cannot claim one KEK while its envelope is sealed under another."""
    admin = signed_in(papi, world, 'admin')
    active(database, cfg, qbo, admin)
    with connection(database=database) as db:
        db.execute('SET session_replication_role = replica')
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("UPDATE lv.provider_credential SET kek_id=repeat('a',64)")


def test_stale_refresher_cannot_reseal_under_a_retired_key(database, world, papi, cfg, qbo, monkeypatch):
    """Review N3: a refresh whose lease expired while a rotation ran is refused, so rotation cannot be undone silently."""
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    old = ps.settings(cfg)
    new_private, _ = envelope.generate_kek()
    rotated = WorkerSettings(old.client_id, old.client_secret, old.redirect_uri, envelope.load_private(new_private), (old.private_key,))
    factory = lambda: connection('lv_worker', database)
    with connection(database=database) as db:
        db.execute("UPDATE lv.provider_credential SET access_expires_at=clock_timestamp()+interval '1 minute'"); db.commit()
    post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"')

    def rotation_runs_while_stalled():
        with connection(database=database) as db:   # superuser test setup: the stalled refresher's lease runs out
            db.execute('SET session_replication_role = replica')
            db.execute("UPDATE lv.provider_credential SET lease_expires_at=clock_timestamp()-interval '1 second'"); db.commit()
        assert runtime.rotate_kek(connection_factory=factory, settings=rotated)['rewrapped'] == 1
    monkeypatch.setattr(runtime, '_before_refresh_finish', rotation_runs_while_stalled)
    ran = runtime.run_due(connection_factory=factory, transport=qbo, settings=old)   # the un-restarted worker still holds only the old key
    assert (ran[0]['outcome'], ran[0]['error_class']) == ('RETRYABLE', 'REFRESH_NOT_RECORDED')
    with connection(database=database) as db:
        assert db.execute('SELECT kek_id, token_version FROM lv.provider_credential').fetchone() == (rotated.kek_id, 1)


def test_sealed_code_is_bound_to_tenant_and_attempt(tmp_path):
    """L3: an authorization-code envelope opens only for the tenant and authorization attempt (state hash) it was sealed for."""
    c = ps.write_config(tmp_path)
    s = ps.settings(c)
    sealed = envelope.seal(s.private_key.public_key(), b'SYNTHETICCODE', runtime._code_aad('t-a', 'a' * 64, '1'))
    assert envelope.open_(s.private_key, sealed, runtime._code_aad('t-a', 'a' * 64, '1')) == b'SYNTHETICCODE'
    for aad in (runtime._code_aad('t-b', 'a' * 64, '1'), runtime._code_aad('t-a', 'b' * 64, '1'), runtime._code_aad('t-a', 'a' * 64, '2')):
        with pytest.raises(envelope.EnvelopeError):
            envelope.open_(s.private_key, sealed, aad)


def test_expired_exchange_lease_dead_clears_the_sealed_code(database, world, papi, cfg, qbo):
    """L2: an EXCHANGE that dies by lease expiry keeps no sealed authorization code."""
    admin = signed_in(papi, world, 'admin')
    _, cb = connect(admin, qbo)
    with connection('lv_worker', database) as w:
        job = w.execute("SELECT lv.provider_due(10)").fetchone()[0]['job_id']; w.commit()
        assert w.execute('SELECT lv.provider_claim(%s,%s,%s)', (world['A'], job, 'stalled')).fetchone()[0]['kind'] == 'EXCHANGE'; w.commit()
    with connection(database=database) as db:   # superuser test setup: the stalled worker used its last attempt and its lease ran out
        db.execute('SET session_replication_role = replica')
        db.execute("UPDATE lv.provider_job SET attempts=5, lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (job,)); db.commit()
    with connection('lv_worker', database) as w:
        list(w.execute('SELECT lv.provider_due(10)')); w.commit()
    with connection(database=database) as db:
        assert db.execute('SELECT state, last_error, payload IS NULL FROM lv.provider_job WHERE id=%s', (job,)).fetchone() == ('DEAD', 'LEASE_EXPIRED', True)
    after = admin.get(f'{BASE}/provider-connections/{cb.json()["id"]}').json()
    assert (after['state'], after['failure_code']) == ('FAILED', 'LEASE_EXPIRED')


def test_oauth_state_superseded_cancelled_and_wrong_tenant(database, world, papi, cfg, qbo):
    """State is single-use and bound to session, user, tenant and the one attempt: a superseded or cancelled attempt's state,
    and a state used after switching organization, are refused."""
    email, password = f'admin2-{uuid4().hex[:8]}@test.ledgervigil.invalid', 'pw-' + uuid4().hex
    with owner_transaction(database) as owner:
        uid = create_user(owner, email, 'Synthetic two-tenant admin', password)
        for tenant in (world['A'], world['B']):
            grant(owner, tenant, uid, ['ADMINISTRATOR'])
            owner.execute("UPDATE lv_identity.membership_role SET granted_at=statement_timestamp()-interval '48 hours' WHERE tenant_id=%s AND user_id=%s", (tenant, uid))
    c = signed_in_with(papi, email, password)
    sel = c.post(f'{BASE}/auth/switch-tenant', json={'tenant_id': str(world['A'])})
    assert sel.status_code == 200, sel.text
    c.headers['X-CSRF-Token'] = sel.json()['csrf_token']
    from mfa_support import establish_mfa
    establish_mfa(papi, c, password, sel.json())
    here = sel.json()['tenant']['id']
    first = post(c, '/provider-connections')
    s1 = parse_qs(urlsplit(first.json()['authorization_url']).query)['state'][0]
    second = post(c, '/provider-connections')   # starting again supersedes the unfinished attempt
    s2 = parse_qs(urlsplit(second.json()['authorization_url']).query)['state'][0]
    old = post(c, '/provider-connections/quickbooks/callback', body={'state': s1, 'code': qbo.issue_code(), 'realm_id': qbo.realm})
    assert (old.status_code, old.json()['code']) == (409, 'OAUTH_STATE_USED')
    assert c.get(f'{BASE}/provider-connections/{first.json()["connection"]["id"]}').json()['failure_code'] == 'AUTHORIZATION_SUPERSEDED'
    cancel = post(c, f'/provider-connections/{second.json()["connection"]["id"]}/disconnect', match=f'"{second.json()["connection"]["etag"]}"')
    assert cancel.status_code == 200 and cancel.json()['failure_code'] == 'AUTHORIZATION_CANCELLED'
    late = post(c, '/provider-connections/quickbooks/callback', body={'state': s2, 'code': qbo.issue_code(), 'realm_id': qbo.realm})
    assert (late.status_code, late.json()['code']) == (409, 'OAUTH_STATE_USED')
    third = post(c, '/provider-connections')
    s3 = parse_qs(urlsplit(third.json()['authorization_url']).query)['state'][0]
    other = world['B'] if here == str(world['A']) else world['A']
    sw = c.post(f'{BASE}/auth/switch-tenant', json={'tenant_id': str(other)})
    assert sw.status_code == 200, sw.text
    c.headers['X-CSRF-Token'] = sw.json()['csrf_token']
    wrong = post(c, '/provider-connections/quickbooks/callback', body={'state': s3, 'code': qbo.issue_code(), 'realm_id': qbo.realm})
    assert (wrong.status_code, wrong.json()['code']) == (400, 'OAUTH_STATE_INVALID')
    assert qbo.calls == []   # no attempt above reached Intuit


def test_runtime_role_cannot_reach_worker_material(database, world, papi, cfg, qbo):
    """Deployment isolation at the database: the API's login (lv_app) can neither read stored credentials nor call any
    worker function that returns or rewrites them."""
    admin = signed_in(papi, world, 'admin')
    active(database, cfg, qbo, admin)
    calls = ["SELECT lv.provider_credential_current(%s,%s,'x',1)", "SELECT lv.provider_claim(%s,%s,'x')", "SELECT lv.provider_refresh_claim(%s,%s,'x',1)",
             "SELECT lv.provider_kek_inventory(repeat('a',64),1) WHERE %s IS NOT NULL AND %s IS NOT NULL", "SELECT * FROM lv.provider_credential WHERE %s IS NOT NULL AND %s IS NOT NULL",
             "SELECT lv.provider_rewrap(%s,%s,1,repeat('a',64),repeat('b',64),'\\x00'::bytea)"]
    with connection('lv_app', database) as app:
        for statement in calls:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(statement, (world['A'], uuid4()))
            app.rollback()


def test_key_rotation_rewraps_stored_tokens_without_a_refresh(database, world, papi, cfg, qbo, tmp_path):
    admin = signed_in(papi, world, 'admin')
    conn = active(database, cfg, qbo, admin)
    old = ps.settings(cfg)
    new_private, _ = envelope.generate_kek()
    rotated = WorkerSettings(old.client_id, old.client_secret, old.redirect_uri, envelope.load_private(new_private), (old.private_key,))
    factory = lambda: connection('lv_worker', database)
    assert runtime.rotate_kek(connection_factory=factory, settings=rotated) == {'rewrapped': 1, 'skipped': 0, 'remaining': 0, 'pending_code_envelopes': 0,
                                                                                 'current_kek_id': rotated.kek_id}
    with connection(database=database) as db:
        assert db.execute('SELECT kek_id, token_version FROM lv.provider_credential').fetchone() == (rotated.kek_id, 1)
    assert qbo.refresh_calls == 0
    only_new = type(rotated)(old.client_id, old.client_secret, old.redirect_uri, rotated.private_key, ())
    r = post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"')
    assert r.status_code == 202
    ran = runtime.run_due(connection_factory=factory, transport=qbo, settings=only_new)
    assert ran[0]['outcome'] == 'DONE'   # the old private key is no longer needed
    conn = admin.get(f'{BASE}/provider-connections/{conn["id"]}').json()
    r = post(admin, f'/provider-connections/{conn["id"]}/syncs', match=f'"{conn["etag"]}"')
    ran = runtime.run_due(connection_factory=factory, transport=qbo, settings=old)
    assert (ran[0]['outcome'], ran[0]['error_class']) == ('TERMINAL', 'ENVELOPE_INVALID')   # the retired key opens nothing


def test_downgrade_refused_while_a_connection_exists(database, world, papi, cfg, qbo):
    admin = signed_in(papi, world, 'admin')
    post(admin, '/provider-connections')
    with pytest.raises(Exception) as refused:
        migrate(database, '0034_detection_review_filters', downgrade=True, disposable=True)
    assert 'no_provider_downgrade' in str(refused.value)
    with connection(database=database) as db:
        assert db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone() == ('0040_mfa_reset',)


def test_committed_contracts_match_the_generated_ones(monkeypatch):
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for k in ('LV_DETECTION_ENABLED', 'LV_PROVIDER_SANDBOX_ENABLED'):
        monkeypatch.delenv(k, raising=False)
    default = create_app(AuthSettings()).openapi()
    full = create_app(AuthSettings(), detection=True, provider=True).openapi()
    assert json.loads((root / 'api/openapi.json').read_text()) == default            # the default contract has no gated route
    assert json.loads((root / 'api/openapi.full.json').read_text()) == full
    assert not any(p.startswith(('/api/v1/provider', '/api/v1/detection')) for p in default['paths'])
    writes = [(p, m) for p, ops in full['paths'].items() if p.startswith('/api/v1/provider') for m in ops if m != 'get']
    assert sorted(writes) == sorted([('/api/v1/provider-connections', 'post'), ('/api/v1/provider-connections/quickbooks/callback', 'post'),
                                     ('/api/v1/provider-connections/{connection_id}/syncs', 'post'), ('/api/v1/provider-connections/{connection_id}/disconnect', 'post')])
