"""Integration worker for QuickBooks SANDBOX jobs (EXCHANGE, SYNC, REVOKE). ADR-010: PostgreSQL owns every transition; the
broker carries ids only; each job is claimed with a fenced lease, so a duplicate delivery finds nothing to claim.

No network call happens inside a database transaction (Stage 2C §7.4): claim (commit) -> provider call -> record (commit).
Secrets exist only in this process's memory between opening an envelope and the call that needs them. Nothing is logged
except ids, outcome classes and counts.
"""
import json
import logging
import time
from uuid import UUID, uuid4

import psycopg

from . import envelope, intuit
from .settings import ConfigurationError, worker_settings

log = logging.getLogger(__name__)
REFRESH_MARGIN_SECONDS = 300
_after_page = None      # test seams
_before_refresh_finish = None
_on_database_error = None


def connect():
    from ledgervigil.maintenance.runtime import connect as worker_connect
    return worker_connect()


def _token_aad(tenant, connection, realm, version):
    return envelope.aad('tokens', str(tenant), str(connection), realm, str(version))


def _code_aad(tenant, state_hash, realm):
    # bound to the tenant and the single authorization attempt (state hash), not only the realm (review finding L3)
    return envelope.aad('authorization-code', str(tenant), state_hash, realm)


def _key_for(settings, sealed: bytes):
    """The private key an envelope was sealed for: the current KEK or a retained previous one (rotation)."""
    key = settings.keyring.get(envelope.envelope_kek_id(sealed))
    if key is None:
        raise envelope.EnvelopeError('sealed under a key-encryption key this worker does not hold')
    return key


def _seal_tokens(settings, tenant, connection, realm, version, tokens):
    plain = json.dumps({'access_token': tokens['access_token'], 'refresh_token': tokens['refresh_token']}).encode()
    return envelope.seal(settings.private_key.public_key(), plain, _token_aad(tenant, connection, realm, version))


def _open_tokens(settings, tenant, connection, realm, cred):
    sealed = bytes.fromhex(cred['envelope_hex'])
    data = envelope.open_(_key_for(settings, sealed), sealed, _token_aad(tenant, connection, realm, cred['token_version']))
    return json.loads(data)


def run_job(tenant, job, *, connection_factory=connect, transport=None, settings=None, worker=None, sleep=time.sleep):
    t, j = UUID(str(tenant)), UUID(str(job))
    worker = worker or 'provider-' + uuid4().hex
    try:
        settings = settings or worker_settings()
    except ConfigurationError as e:
        return {'status': 'NOT_RUN', 'error_class': e.code}
    guard = intuit.Guard(transport or intuit.httpx_transport())
    with connection_factory() as c:
        claim = c.execute('SELECT lv.provider_claim(%s,%s,%s)', (t, j, worker)).fetchone()[0]
        c.commit()
    if claim is None:
        return {'status': 'NOT_CLAIMED'}
    fence = claim['fence']

    def finish(outcome, code=None, result=None):
        with connection_factory() as c:
            ok = c.execute('SELECT lv.provider_job_finish(%s,%s,%s,%s,%s,%s,%s)', (t, j, worker, fence, outcome, code,
                           json.dumps(result) if result is not None else None)).fetchone()[0]
            c.commit()
        return {'status': 'FINISHED' if ok else 'LOST', 'outcome': outcome, 'error_class': code}

    try:
        if claim['kind'] == 'EXCHANGE':
            return _exchange(t, j, worker, fence, claim, settings, guard, connection_factory, finish)
        if claim['kind'] == 'SYNC':
            return _sync(t, j, worker, fence, claim, settings, guard, connection_factory, finish, sleep)
        return _revoke(t, j, worker, fence, claim, settings, guard, connection_factory, finish)
    except intuit.ProviderError as e:
        return finish(e.outcome, e.code, {'http_status': e.status, 'intuit_tid': e.intuit_tid, 'fault_codes': list(e.fault_codes)})
    except intuit.TransportRefused:
        return finish('TERMINAL', 'TRANSPORT_REFUSED')
    except envelope.EnvelopeError:
        return finish('TERMINAL', 'ENVELOPE_INVALID')
    except psycopg.errors.InsufficientPrivilege:
        return finish('REVOKED', 'WORKER_PRIVILEGE_REVOKED')
    except psycopg.Error as e:
        if _on_database_error:
            _on_database_error(e)
        code = {'55000': 'CONNECTION_NOT_ACTIVE', '22023': 'PROVIDER_RESPONSE_INVALID', '54000': 'PROVIDER_PAGE_TOO_LARGE'}.get(e.sqlstate)
        if e.sqlstate == '40001':
            return {'status': 'LOST', 'error_class': 'LEASE_LOST'}
        return finish('TERMINAL' if code else 'RETRYABLE', code or 'DATABASE_RETRYABLE')
    except Exception as e:   # anything unforeseen ends the job visibly instead of leaving it CLAIMED until the lease expires
        log.error('provider job failed job=%s class=%s', j, type(e).__name__)
        try:
            return finish('TERMINAL', 'WORKER_ERROR')
        except Exception:
            return {'status': 'LOST', 'error_class': 'WORKER_ERROR'}


def _exchange(t, j, worker, fence, claim, settings, guard, connection_factory, finish):
    realm = claim['realm_id']
    if not claim.get('payload_hex') or not claim.get('state_hash'):
        return finish('TERMINAL', 'AUTHORIZATION_CODE_MISSING')
    sealed = bytes.fromhex(claim['payload_hex'])
    code = envelope.open_(_key_for(settings, sealed), sealed, _code_aad(t, claim['state_hash'], realm)).decode()
    tokens = intuit.exchange_code(guard, settings, code)
    del code
    # From here on a live grant exists at Intuit. Every path that does not record it revokes it, exceptions included.
    try:
        try:   # realm binding: the new token must read exactly one CompanyInfo of this realm (read-only call)
            _, n, _ = intuit.query(guard, realm, tokens['access_token'], 'CompanyInfo', 1, 1)
            if n != 1:
                raise intuit.ProviderError('TERMINAL', 'REALM_VERIFICATION_FAILED', status=200)
        except intuit.ProviderError as e:   # the code is single-use, so nothing here is retried: revoke and ask for a new connection
            revoked = _safe_revoke(guard, settings, tokens['refresh_token'])
            code = 'REALM_VERIFICATION_UNAVAILABLE' if e.outcome in ('RETRYABLE', 'THROTTLED') else 'REALM_VERIFICATION_FAILED'
            return finish('TERMINAL', code, {'http_status': e.status, 'intuit_tid': e.intuit_tid, 'grant_revocation': revoked})
        env = _seal_tokens(settings, t, claim['connection_id'], realm, 1, tokens)
        with connection_factory() as c:
            state = c.execute('SELECT lv.provider_exchange_finish(%s,%s,%s,%s,%s,%s,%s,%s)', (t, j, worker, fence, settings.kek_id, env,
                              tokens['access_expires_at'], tokens['refresh_expires_at'])).fetchone()[0]
            c.commit()
    except BaseException:
        _safe_revoke(guard, settings, tokens['refresh_token'])
        raise
    if state == 'REALM_ALREADY_CONNECTED':
        # The verified realm is already bound to another organization. Intuit keeps one connection per app and company, so
        # these tokens belong to the connection the other organization uses: revoking them could disconnect it (review N4).
        # They are discarded unstored instead; the attempt is FAILED and audited by the database.
        del tokens
        return {'status': 'FINISHED', 'outcome': 'REALM_ALREADY_CONNECTED'}
    if state != 'ACTIVE':   # lease lost or attempt cancelled while exchanging: never leave live tokens at Intuit unrecorded
        _safe_revoke(guard, settings, tokens['refresh_token'])
        return {'status': 'LOST', 'error_class': 'EXCHANGE_NOT_RECORDED'}
    return {'status': 'FINISHED', 'outcome': 'CONNECTED'}


def _safe_revoke(guard, settings, token):
    """Best effort; the outcome is returned (and recorded in the job result where one is written). A grant whose revocation
    could not be confirmed is logged by class only so an operator can disconnect the app at Intuit."""
    try:
        return intuit.revoke(guard, settings, token)
    except Exception as exc:
        log.warning('provider grant revocation unconfirmed class=%s', type(exc).__name__)
        return 'PROVIDER_UNCONFIRMED'


def _credential(t, j, worker, fence, connection_factory):
    with connection_factory() as c:
        cred = c.execute('SELECT lv.provider_credential_current(%s,%s,%s,%s)', (t, j, worker, fence)).fetchone()[0]
        c.commit()
    return cred


def _access_token(t, j, worker, fence, claim, settings, guard, connection_factory, sleep, *, rejected_version=None):
    """(access token, token version). Refreshes under the per-connection lease when the token expires within 5 minutes, or
    when rejected_version (the version a 401 was received with) is still current. The new pair is committed before it is
    used (ADR-016). A concurrent refresher is waited for, never raced; a pair refreshed by someone else is simply used."""
    def usable(cred):
        if rejected_version is not None:
            return cred['token_version'] != rejected_version
        return cred['access_expires_at'] - time.time() > REFRESH_MARGIN_SECONDS

    cred = _credential(t, j, worker, fence, connection_factory)
    if cred is None:
        raise intuit.ProviderError('AUTH_INVALID', 'NO_CREDENTIAL')
    if usable(cred) and not cred['refresh_in_progress']:
        return _open_tokens(settings, t, claim['connection_id'], claim['realm_id'], cred)['access_token'], cred['token_version']
    for _ in range(20):
        with connection_factory() as c:
            lease = c.execute('SELECT lv.provider_refresh_claim(%s,%s,%s,%s)', (t, j, worker, fence)).fetchone()[0]
            c.commit()
        if lease['granted']:
            break
        if lease.get('reason') == 'NO_CREDENTIAL':
            raise intuit.ProviderError('AUTH_INVALID', 'NO_CREDENTIAL')
        if lease.get('reason') == 'CONNECTION_NOT_ACTIVE':
            raise intuit.ProviderError('TERMINAL', 'CONNECTION_NOT_ACTIVE')
        sleep(0.5)
        cred = _credential(t, j, worker, fence, connection_factory)
        if cred and usable(cred) and not cred['refresh_in_progress']:
            return _open_tokens(settings, t, claim['connection_id'], claim['realm_id'], cred)['access_token'], cred['token_version']
    else:
        raise intuit.ProviderError('RETRYABLE', 'REFRESH_IN_PROGRESS')

    def release(code):
        with connection_factory() as c:
            c.execute('SELECT lv.provider_refresh_finish(%s,%s,%s,%s,%s,NULL,NULL,NULL,NULL,%s)', (t, j, worker, lease['lease_fence'], lease['token_version'], code))
            c.commit()
    current = _open_tokens(settings, t, claim['connection_id'], claim['realm_id'], lease)
    if usable(lease):   # someone else refreshed between our read and our lease: use their pair, call Intuit once only
        release('RELEASE')
        return current['access_token'], lease['token_version']
    try:
        tokens = intuit.refresh(guard, settings, current['refresh_token'])
    except intuit.ProviderError as e:
        release('AUTH_INVALID' if e.outcome == 'AUTH_INVALID' else 'RELEASE')
        raise
    # The rotated pair exists only in memory until committed; if it is not committed it is revoked, never dropped.
    try:
        env = _seal_tokens(settings, t, claim['connection_id'], claim['realm_id'], lease['token_version'] + 1, tokens)
        if _before_refresh_finish:
            _before_refresh_finish()
        with connection_factory() as c:
            done = c.execute('SELECT lv.provider_refresh_finish(%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)', (t, j, worker, lease['lease_fence'], lease['token_version'],
                             settings.kek_id, env, tokens['access_expires_at'], tokens['refresh_expires_at'])).fetchone()[0]
            c.commit()
    except BaseException:
        _safe_revoke(guard, settings, tokens['refresh_token'])
        raise
    if not done['accepted']:
        # Another refresher may have committed: if Intuit handed both of us the same refresh token (it can keep the value),
        # revoking it would kill the stored, healthy grant (review N4). Revoke only a token that is not the stored one.
        try:
            stored = _credential(t, j, worker, fence, connection_factory)
            same = stored is not None and _open_tokens(settings, t, claim['connection_id'], claim['realm_id'], stored)['refresh_token'] == tokens['refresh_token']
        except Exception:
            same = False
        if not same:
            _safe_revoke(guard, settings, tokens['refresh_token'])
        if done.get('reason') == 'CONNECTION_NOT_ACTIVE':
            raise intuit.ProviderError('TERMINAL', 'CONNECTION_NOT_ACTIVE')
        raise intuit.ProviderError('RETRYABLE', 'REFRESH_NOT_RECORDED')
    return tokens['access_token'], done['token_version']


def _sync(t, j, worker, fence, claim, settings, guard, connection_factory, finish, sleep):
    progress = claim['progress'] or {}
    done = list(progress.get('entities_done', []))
    truncated = list(progress.get('truncated', []))
    entity, start = progress.get('entity'), progress.get('next_start', 1)
    token, version = _access_token(t, j, worker, fence, claim, settings, guard, connection_factory, sleep)
    realm = claim['realm_id']
    for name in intuit.ENTITIES:
        if name in done:
            continue
        if entity != name:
            entity, start = name, 1
        pages = progress.get('counts', {}).get(name, {}).get('pages', 0)
        while True:
            size = 1 if name in intuit.SINGLETONS else intuit.PAGE_SIZE
            try:
                body, n, tid = intuit.query(guard, realm, token, name, start, size)
            except intuit.ProviderError as e:
                if e.outcome != 'AUTH_INVALID':
                    raise
                token, version = _access_token(t, j, worker, fence, claim, settings, guard, connection_factory, sleep, rejected_version=version)
                body, n, tid = intuit.query(guard, realm, token, name, start, size)   # a second 401 is AUTH_INVALID
            pages += 1
            more = name in intuit.LISTS and n == size
            if more and pages >= intuit.MAX_PAGES_PER_ENTITY:
                truncated.append(name)
                more = False
            nxt = {'entities_done': done + ([] if more else [name]), 'truncated': truncated, 'entity': name if more else None,
                   'next_start': start + size if more else 1}
            with connection_factory() as c:
                c.execute('SELECT lv.provider_sync_page(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (t, j, worker, fence, name, start, size, body, tid,
                          intuit.MINOR_VERSION, getattr(guard.transport, 'provenance', 'PROVIDER_SANDBOX'), json.dumps(nxt)))
                c.commit()
            if _after_page:
                _after_page(name, start)
            if not more:
                done.append(name)
                break
            start += size
    status = 'PARTIAL' if truncated else 'COMPLETED'
    return finish('DONE', None, {'status': status, 'truncated_entities': truncated, 'minor_version': intuit.MINOR_VERSION,
                                 'bound': {'page_size': intuit.PAGE_SIZE, 'max_pages_per_entity': intuit.MAX_PAGES_PER_ENTITY},
                                 'provenance': getattr(guard.transport, 'provenance', 'PROVIDER_SANDBOX')})


def _revoke(t, j, worker, fence, claim, settings, guard, connection_factory, finish):
    cred = claim.get('credential')
    if cred is None:
        outcome, version = 'NO_TOKENS', None
    else:
        tokens = _open_tokens(settings, t, claim['connection_id'], claim['realm_id'], cred)
        outcome, version = intuit.revoke(guard, settings, tokens['refresh_token']), cred['token_version']   # RETRYABLE raises
    with connection_factory() as c:
        ok = c.execute('SELECT lv.provider_revoke_finish(%s,%s,%s,%s,%s,%s)', (t, j, worker, fence, outcome, version)).fetchone()[0]
        c.commit()
    if not ok:   # a newer pair was committed after the claim (or the lease was lost): retry, which revokes the newer pair too
        return finish('RETRYABLE', 'NEWER_TOKEN_TO_REVOKE')
    return {'status': 'FINISHED', 'outcome': outcome}


def relay(c, publish):
    """Publish due provider jobs (ids only) before marking them dispatched; a crash republishes and the claim deduplicates."""
    with c.transaction():
        jobs = [r[0] for r in c.execute('SELECT lv.provider_due(25)')]
        for job in jobs:
            publish(job['tenant_id'], job['job_id'])
            c.execute('SELECT lv.provider_dispatch(%s,%s)', (job['tenant_id'], job['job_id']))
    return len(jobs)


def run_due(*, connection_factory=connect, transport=None, settings=None, limit=25):
    """Process due jobs directly (no broker): the same claim protocol, so it is safe beside Celery workers."""
    with connection_factory() as c:
        with c.transaction():
            jobs = [r[0] for r in c.execute('SELECT lv.provider_due(%s)', (limit,))]
    return [dict(run_job(x['tenant_id'], x['job_id'], connection_factory=connection_factory, transport=transport, settings=settings), job_id=x['job_id']) for x in jobs]


def rotate_kek(*, connection_factory=connect, settings=None, batch=200):
    """Rewrap every stored token envelope under the CURRENT key-encryption key (worker only).

    Procedure (docs/QUICKBOOKS_SANDBOX.md, "Key rotation"): generate a new KEK; give the worker the new private key as
    LV_PROVIDER_KEK_PRIVATE and the old one in LV_PROVIDER_KEK_PRIVATE_PREVIOUS; give the API the new public key; run this
    until it reports remaining 0 and pending_code_envelopes 0; then remove the old private key from the worker. Each row is
    rewrapped with a compare-and-set (same token version, same old KEK, no live refresh lease), so it is safe to run while
    jobs run. Returns counts only."""
    settings = settings or worker_settings()
    current = settings.kek_id
    done = skipped = 0
    with connection_factory() as c:
        rows = [r[0] for r in c.execute('SELECT lv.provider_kek_inventory(%s,%s)', (current, batch))]
        c.commit()
    pending = next((r['pending_code_envelopes'] for r in rows if 'pending_code_envelopes' in r), 0)
    for r in rows:
        if 'connection_id' not in r:
            continue
        sealed = bytes.fromhex(r['envelope_hex'])
        old = settings.keyring.get(envelope.envelope_kek_id(sealed))
        if old is None:
            skipped += 1   # sealed under a key this worker does not hold: left untouched, reported
            continue
        aad = _token_aad(r['tenant_id'], r['connection_id'], r['realm_id'], r['token_version'])
        new = envelope.seal(settings.private_key.public_key(), envelope.open_(old, sealed, aad), aad)
        with connection_factory() as c:
            ok = c.execute('SELECT lv.provider_rewrap(%s,%s,%s,%s,%s,%s)', (r['tenant_id'], r['connection_id'], r['token_version'], r['kek_id'], current, new)).fetchone()[0]
            c.commit()
        done += 1 if ok else 0
        skipped += 0 if ok else 1
    with connection_factory() as c:
        remaining = sum(1 for (x,) in c.execute('SELECT lv.provider_kek_inventory(%s,%s)', (current, 500)) if 'connection_id' in x)
        c.commit()
    return {'rewrapped': done, 'skipped': skipped, 'remaining': remaining, 'pending_code_envelopes': pending, 'current_kek_id': current}
