"""Shared helpers for the detection-slice tests (0032). All data is SYNTHETIC; databases are disposable."""
from uuid import uuid4

import pytest

from tools.dev_db import connection
from ledgervigil.app import create_app
from ledgervigil.auth.settings import AuthSettings
from test_proposal_drafts import fingerprint, counters  # noqa: F401

BASE = '/api/v1'
DETECTION_TABLES = {'lv.detection_run', 'lv.detection_finding', 'lv.detection_run_finding', 'lv.detection_review', 'lv.detection_disposition'}
# Everything detection may write: its own tables, the idempotency cache and the audit chain. Every other lv/lv_identity table
# (ledger, documents, versions, journal, balances, counters, payments, settlements, ingestion, proposals, ...) must not change.
DETECTION_WRITES = DETECTION_TABLES | {'lv.api_idempotency', 'lv_identity.audit_event', 'lv_identity.audit_chain_head'}


@pytest.fixture
def database():
    """A fresh disposable database per detection test. Detection rows make 0032 refuse a downgrade (by design), and other
    modules downgrade the shared session database (e.g. test_workflow_lock_order), so detection tests never write to it."""
    from conftest import database as base
    generator = base.__wrapped__()
    try:
        yield next(generator)
    finally:
        generator.close()


@pytest.fixture
def dapi(database):
    application = create_app(AuthSettings(), connect=lambda: connection('lv_app', database), issuer_connect=lambda: connection('lv_authn', database), detection=True)
    yield application
    application.state.pool.close()


def run(c, ledger_id, body=None, key=None, **headers):
    return c.post(f'{BASE}/ledgers/{ledger_id}/detection-runs', json={} if body is None else body,
                  headers={'Idempotency-Key': key or uuid4().hex, **headers})


def finding(c, finding_id):
    return c.get(f'{BASE}/detection-findings/{finding_id}')


def dispose(c, finding_id, action, reason, match, key=None, note=None):
    body = {'action': action, 'reason_code': reason, **({'note': note} if note is not None else {})}
    headers = {'Idempotency-Key': key or uuid4().hex, **({'If-Match': match} if match is not None else {})}
    return c.post(f'{BASE}/detection-findings/{finding_id}/dispositions', json=body, headers=headers)


def rows(database, table, tenant=None):
    with connection(database=database) as db:
        if tenant is None:
            return db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
        return db.execute(f'SELECT count(*) FROM {table} WHERE tenant_id = %s', (tenant,)).fetchone()[0]


def audits(database, event, tenant):
    with connection(database=database) as db:
        return db.execute('SELECT count(*) FROM lv_identity.audit_event WHERE event_type = %s AND tenant_id = %s', (event, tenant)).fetchone()[0]


def protected(database):
    """Fingerprint of every table detection must never write, plus the ledger counters."""
    return fingerprint(database, DETECTION_WRITES), counters(database)
