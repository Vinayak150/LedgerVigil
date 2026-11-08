"""Redacted end-to-end demonstration over real HTTP (uvicorn on 127.0.0.1) and real PostgreSQL, in a disposable database.

    PYTHONPATH=src:.:tests python -m tools.demo_detection OUT.json

Builds the SYNTHETIC test world (users of every role, ledger L1 with payments PMT-2001/PMT-2002 RECEIVED and statement STL-3001,
no settlement), serves the API with the detection gate on, and records each request/response with cookies, CSRF tokens,
passwords and e-mail addresses removed. Financial tables and ledger counters are fingerprinted before and after.
"""
import json
import socket
import sys
import threading
import time
from uuid import uuid4

import httpx
import uvicorn
from psycopg import sql

from tools.dev_db import connection, migrate, prepare_database
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def main(out_path):
    import test_proposal_drafts as tp
    from detection_support import protected
    from ledgervigil.ledger.seed import seed_processor_settlement
    name = 'lv_migration_test_' + uuid4().hex
    with connection(autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    transcript, server = [], None
    try:
        prepare_database(name); migrate(name)
        with connection('lv_app', name, autocommit=True) as app_conn, connection('lv_seed', name, autocommit=True) as seed:
            w = tp.world.__wrapped__(name, app_conn, seed)
        app = create_app(AuthSettings(cookie_mode='local-http', environment='development'), connect=lambda: connection('lv_app', name), detection=True)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.05)
        base = f'http://127.0.0.1:{port}'

        def session(role):
            c = httpx.Client(base_url=base, headers={'Origin': base})
            r = c.post('/api/v1/auth/login', json={'email': w['users'][role]['email'], 'password': w['users'][role]['password']})
            assert r.status_code == 200, r.text
            c.headers['X-CSRF-Token'] = r.json()['csrf_token']
            transcript.append({'step': f'login as synthetic {role}', 'request': 'POST /api/v1/auth/login {email: <redacted>, password: <redacted>}',
                               'status': r.status_code, 'response': '<session cookie and CSRF token redacted>'})
            return c

        def call(step, c, method, path, keep, **kw):
            started = time.perf_counter(); r = c.request(method, path, **kw); ms = round((time.perf_counter() - started) * 1000, 1)
            body = r.json() if r.headers.get('content-type', '').startswith(('application/json', 'application/problem+json')) else None
            shown = {k: body[k] for k in keep if body and k in body} if keep else body
            headers = {k: v for k, v in kw.get('headers', {}).items() if k in ('Idempotency-Key', 'If-Match')}
            transcript.append({'step': step, 'request': f'{method} {path}', 'request_headers': headers, 'request_body': kw.get('json'),
                               'status': r.status_code, 'elapsed_ms': ms,
                               'response_headers': {k: r.headers[k] for k in ('etag', 'idempotent-replayed') if k in r.headers}, 'response': shown})
            return r, body

        before = protected(name)
        analyst, controller, other = session('analyst'), session('controller'), session('analystB')
        key = 'demo-run-1'
        _, run = call('analyst runs detection on L1', analyst, 'POST', f'/api/v1/ledgers/{w["l1"]["ledger"]}/detection-runs',
                      ['id', 'status', 'scope', 'findings', 'abstentions', 'snapshot_seq', 'basis', 'model', 'accounting_review_status'],
                      json={}, headers={'Idempotency-Key': key})
        call('retry with the same key replays the same run', analyst, 'POST', f'/api/v1/ledgers/{w["l1"]["ledger"]}/detection-runs',
             ['id', 'finding_count'], json={}, headers={'Idempotency-Key': key})
        fid = run['findings'][0]['id']
        call('analyst inspects the finding', analyst, 'GET', f'/api/v1/detection-findings/{fid}', None)
        call('another tenant cannot see it', other, 'GET', f'/api/v1/detection-findings/{fid}', ['code', 'status'])
        call('controller confirms after checking the cited evidence', controller, 'POST', f'/api/v1/detection-findings/{fid}/dispositions',
             ['review', 'dispositions'], json={'action': 'CONFIRMED', 'reason_code': 'VERIFIED_AGAINST_EVIDENCE'},
             headers={'Idempotency-Key': 'demo-confirm-1', 'If-Match': '"v1"'})
        after_review = protected(name)
        with connection('lv_seed', name, autocommit=True) as seed:
            seed_processor_settlement(seed, w['A'], w['l1']['ledger'], {
                'processor_ref': 'processor-1', 'payout_id': 'STL-3001', 'payout_date': '2026-10-14', 'currency': 'INR', 'gross': '52500.50',
                'fee': '1000.00', 'net': '51500.50', 'description': 'SYNTHETIC settlement recorded after detection (demo)',
                'items': [{'document_id': str(w['p1']), 'kind': 'PAYMENT', 'amount': '50000.00'}, {'document_id': str(w['p2']), 'kind': 'PAYMENT', 'amount': '2500.50'}]})
        transcript.append({'step': 'developer seed path records the STL-3001 settlement (outside the detection API)'})
        call('the confirmed finding now shows STALE inputs', analyst, 'GET', f'/api/v1/detection-findings/{fid}', ['review', 'freshness'])
        call('a new run finds nothing: the ledger now matches the statement', analyst, 'POST', f'/api/v1/ledgers/{w["l1"]["ledger"]}/detection-runs',
             ['id', 'status', 'findings', 'finding_count'], json={}, headers={'Idempotency-Key': 'demo-run-2'})
        summary = {'financial_tables_and_counters_unchanged_by_detection_and_review': before == after_review,
                   'environment': 'Claude Linux workspace; uvicorn on 127.0.0.1 (local-http, development); PostgreSQL 18.6 disposable database',
                   'data': 'SYNTHETIC', 'redactions': 'session cookies, CSRF tokens, passwords and e-mail addresses are not recorded'}
        with open(out_path, 'w') as f:
            json.dump({'summary': summary, 'transcript': transcript}, f, indent=1); f.write('\n')
        print(json.dumps(summary))
    finally:
        if server:
            server.should_exit = True; time.sleep(0.3)
        with connection(autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__ == '__main__':
    main(sys.argv[1])
