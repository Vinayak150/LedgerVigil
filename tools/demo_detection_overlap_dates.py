"""Redacted end-to-end demonstration of detector 1.1.0 (DET-9 overlap, DET-10 payout date) over real HTTP and PostgreSQL.

    PYTHONPATH=src:.:tests python -m tools.demo_detection_overlap_dates OUT.json

Disposable database at head; SYNTHETIC test world (ledger L1: payments PMT-2001/PMT-2002 RECEIVED, statement STL-3001; tenant B
has its own PMT-2001 and STL-3001). A second L1 statement STL-3002 also lists PMT-2001. uvicorn on 127.0.0.1 serves the API with
the detection gate ON in this process only. Cookies, CSRF tokens, passwords and e-mail addresses are never recorded. Protected
financial/workflow tables and ledger counters are fingerprinted around every detection and review step.
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

FINDING_KEYS = ['id', 'kind', 'class', 'staged_document_id', 'detector_version', 'input_fingerprint', 'summary', 'computed', 'evidence',
                'freshness', 'review', 'dispositions', 'basis', 'accounting_review_status']


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
    transcript, server, checks = [], None, {}
    try:
        prepare_database(name); migrate(name)
        with connection(database=name) as db:
            head = db.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()[0]
        with connection('lv_app', name, autocommit=True) as app_conn, connection('lv_seed', name, autocommit=True) as seed:
            w = tp.world.__wrapped__(name, app_conn, seed)
        l1 = w['l1']['ledger']
        stl3002 = tp._statement(name, w['l1'], 'STL-3002', [('PMT-2001', '50000.00')], '50000.00', '500.00', '49500.00')
        transcript.append({'step': 'ingestion writer stages a second L1 statement, payout STL-3002, that also lists PMT-2001 (outside the detection API)',
                           'staged_document_id': str(stl3002['staged_document_id']), 'status': stl3002['status']})
        app = create_app(AuthSettings(cookie_mode='local-http', environment='development'), connect=lambda: connection('lv_app', name), issuer_connect=lambda: connection('lv_authn', name), detection=True)
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

        def pick(run, kind, sid):
            return next(f['id'] for f in run['findings'] if f['kind'] == kind and f['staged_document_id'] == str(sid))

        analyst, controller, other = session('analyst'), session('controller'), session('analystB')
        before = protected(name)
        run_keys = ['id', 'status', 'detector_version', 'scope', 'findings', 'abstentions', 'snapshot_seq', 'basis', 'model', 'accounting_review_status']
        _, run1 = call('analyst runs detection on L1 (both statements)', analyst, 'POST', f'/api/v1/ledgers/{l1}/detection-runs', run_keys,
                       json={}, headers={'Idempotency-Key': 'demo-overlap-run-1'})
        _, replay = call('retry with the same key replays the same run', analyst, 'POST', f'/api/v1/ledgers/{l1}/detection-runs',
                         ['id', 'finding_count'], json={}, headers={'Idempotency-Key': 'demo-overlap-run-1'})
        checks['retry_replays_same_run'] = replay['id'] == run1['id']
        ovl = pick(run1, 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', w['s1'])
        ovl_other = pick(run1, 'PAYMENT_CLAIMED_BY_OTHER_PAYOUT', stl3002['staged_document_id'])
        _, f1 = call('analyst inspects the STL-3001 overlap finding (cites STL-3002)', analyst, 'GET', f'/api/v1/detection-findings/{ovl}', FINDING_KEYS)
        _, f2 = call('analyst inspects the STL-3002 overlap finding (cites STL-3001)', analyst, 'GET', f'/api/v1/detection-findings/{ovl_other}', ['kind', 'computed'])
        checks['mutual_citation'] = (f1['computed']['claims'][0]['other_claims'][0]['payout'][1] == 'STL-3002'
                                     and f2['computed']['claims'][0]['other_claims'][0]['payout'][1] == 'STL-3001')
        checks['tenant_B_identical_ids_not_cited'] = all(o['payout'][1] != 'STL-3001' for c in f1['computed']['claims'] for o in c['other_claims'])
        r, _ = call('another tenant cannot see it', other, 'GET', f'/api/v1/detection-findings/{ovl}', ['code', 'status'])
        checks['cross_tenant_404'] = r.status_code == 404
        call('controller confirms after checking both cited statements', controller, 'POST', f'/api/v1/detection-findings/{ovl}/dispositions',
             ['review', 'dispositions'], json={'action': 'CONFIRMED', 'reason_code': 'VERIFIED_AGAINST_EVIDENCE'},
             headers={'Idempotency-Key': 'demo-overlap-confirm-1', 'If-Match': '"v1"'})
        after_review = protected(name)
        checks['protected_tables_and_counters_unchanged_by_detection_and_review'] = before == after_review
        with connection('lv_seed', name, autocommit=True) as seed:
            seed_processor_settlement(seed, w['A'], l1, {
                'processor_ref': 'processor-1', 'payout_id': 'STL-3001', 'payout_date': '2026-10-16', 'currency': 'INR', 'gross': '52500.50',
                'fee': '1000.00', 'net': '51500.50', 'description': 'SYNTHETIC settlement recorded after detection (demo), later value date',
                'items': [{'document_id': str(w['p1']), 'kind': 'PAYMENT', 'amount': '50000.00'}, {'document_id': str(w['p2']), 'kind': 'PAYMENT', 'amount': '2500.50'}]})
        transcript.append({'step': 'developer seed path records the STL-3001 settlement with payout_date 2026-10-16 (statement says 2026-10-14), outside the detection API'})
        _, stale = call('the confirmed overlap finding now shows STALE inputs; its review history is kept', analyst, 'GET',
                        f'/api/v1/detection-findings/{ovl}', ['review', 'freshness', 'dispositions'])
        checks['confirmed_finding_stale_history_kept'] = (stale['freshness']['status'], stale['review']['status'], len(stale['dispositions'])) == ('STALE', 'CONFIRMED', 1)
        before2 = protected(name)
        _, run2 = call('a new run: STL-3001 now differs only by payout date; STL-3002 lists a payment settled elsewhere', analyst, 'POST',
                       f'/api/v1/ledgers/{l1}/detection-runs', run_keys, json={}, headers={'Idempotency-Key': 'demo-overlap-run-2'})
        date_f = pick(run2, 'PAYOUT_DATE_DIFFERS', w['s1'])
        _, fd = call('analyst inspects the payout-date finding', analyst, 'GET', f'/api/v1/detection-findings/{date_f}', FINDING_KEYS)
        checks['date_difference_days'] = fd['computed']['calendar_days_statement_minus_ledger'] == -2
        checks['rerun_kinds'] = sorted((f['staged_document_id'] == str(w['s1']), f['kind']) for f in run2['findings']) == sorted([
            (True, 'PAYOUT_DATE_DIFFERS'), (False, 'SETTLEMENT_NOT_RECORDED'), (False, 'STATEMENT_ITEM_SETTLED_ELSEWHERE')])
        checks['protected_tables_and_counters_unchanged_by_second_run_and_reads'] = before2 == protected(name)
        summary = {'migration_head': head, 'detector_version': run1['detector_version'], 'checks': checks, 'all_checks_passed': all(checks.values()),
                   'environment': "Claude's Linux workspace (not the Mac); uvicorn on 127.0.0.1 (local-http, development); disposable PostgreSQL 18.6",
                   'detection_gate': 'ON in this demo process only', 'data': 'SYNTHETIC',
                   'redactions': 'session cookies, CSRF tokens, passwords and e-mail addresses are not recorded'}
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
