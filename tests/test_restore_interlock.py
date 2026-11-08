"""Restore interlock (0037): a database that is not the bound one (a logical restore always is) refuses financial writes by the
runtime logins until recovery verification is recorded; reads, diagnostics and migrations keep working. SYNTHETIC data."""
import hashlib
import json

import psycopg
import pytest

from tools.dev_db import connection
from ledgervigil.ledger.seed import seed_manual_journal
from test_proposal_drafts import world, seed, signed_in, client, api  # noqa: F401
from detection_support import database  # noqa: F401

BASE = '/api/v1'


def unbind(database):
    """Superuser test setup: make the binding name another database, exactly what a restored copy carries."""
    with connection(database=database) as db:
        db.execute('SET session_replication_role = replica')
        db.execute('UPDATE lv.database_binding SET database_oid=0'); db.commit()


def bind(database, report=b'{"status": "PASS"}'):
    with connection('lv_migrator', database) as m:
        m.execute('SET ROLE lv_owner')
        state = m.execute('SELECT lv.recovery_bind(%s)', (hashlib.sha256(report).hexdigest(),)).fetchone()[0]
        m.commit()
    return state


def test_restored_database_refuses_financial_writes_until_verified(database, world, api):
    with connection('lv_app', database) as a:
        assert a.execute('SELECT lv.recovery_state()').fetchone()[0] == 'BOUND'
    unbind(database)
    with connection('lv_app', database) as a:
        assert a.execute('SELECT lv.recovery_state()').fetchone()[0] == 'RESTORED_UNVERIFIED'
    journal = {'source_ref': 'interlock-1', 'effective_date': '2026-10-06', 'description': 'SYNTHETIC journal after restore',
               'lines': [{'account_id': world['l1']['chart']['BANK_OPERATING'], 'side': 'D', 'amount': '1.00'},
                         {'account_id': world['l1']['chart']['OPENING_BALANCE_EQUITY'], 'side': 'C', 'amount': '1.00'}]}
    with connection('lv_seed', database) as s:
        with pytest.raises(psycopg.Error) as refused:
            seed_manual_journal(s, world['A'], world['l1']['ledger'], journal)
        assert refused.value.sqlstate == 'LV503'
    # Reads and diagnostics keep working through the API while writes are paused.
    analyst = signed_in(api, world, 'analyst')
    assert analyst.get(f'{BASE}/ledgers').status_code == 200
    assert bind(database) == 'BOUND'
    with connection('lv_seed', database) as s:
        seed_manual_journal(s, world['A'], world['l1']['ledger'], journal)
        s.commit()
    with connection(database=database) as db:
        hist = db.execute("SELECT reason, report_sha256 IS NOT NULL, previous_oid FROM lv.database_binding_history ORDER BY bound_at").fetchall()
        assert [h[0] for h in hist] == ['INSTALLED', 'RECOVERY_VERIFIED'] and hist[1][1] and hist[1][2] == 0


def test_binding_needs_the_owner_and_a_report_digest(database):
    with connection('lv_app', database) as a:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            a.execute("SELECT lv.recovery_bind(repeat('a',64))")
    with connection('lv_migrator', database) as m:
        m.execute('SET ROLE lv_owner')
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            m.execute("SELECT lv.recovery_bind('not-a-digest')")
        m.rollback()
        m.execute('SET ROLE lv_owner')
        assert m.execute("SELECT lv.recovery_bind(repeat('a',64))").fetchone()[0] == 'ALREADY_BOUND'


def test_api_reports_paused_writes_clearly(database, world, api):
    account = world['l1']['chart']['BANK_OPERATING']
    with connection(database=database) as db:
        version = db.execute('SELECT version FROM lv.account WHERE id=%s', (account,)).fetchone()[0]
    unbind(database)
    admin = signed_in(api, world, 'admin')
    patch = admin.patch(f'{BASE}/accounts/{account}', json={'name': 'SYNTHETIC renamed'}, headers={'If-Match': f'"v{version}"'})
    assert (patch.status_code, patch.json()['code']) == (503, 'RECOVERY_VERIFICATION_REQUIRED'), patch.text
    with connection(database=database) as db:
        assert db.execute('SELECT version FROM lv.account WHERE id=%s', (account,)).fetchone()[0] == version   # nothing written
    bind(database)
    assert admin.patch(f'{BASE}/accounts/{account}', json={'name': 'SYNTHETIC renamed'}, headers={'If-Match': f'"v{version}"'}).status_code == 200


def test_operator_bind_rejects_bare_pass_marker_before_connecting(tmp_path, monkeypatch):
    from tools import deploy_entry
    report = tmp_path / 'report.json'
    report.write_text('{"status":"PASS"}')
    monkeypatch.setattr(deploy_entry, '_secret', lambda _: pytest.fail('credential must not be read'))
    with pytest.raises(RuntimeError, match='complete logical restore'):
        deploy_entry.bind_restored(report)


def test_operator_bind_checks_configured_anchors_even_before_first_pack(database, tmp_path, monkeypatch):
    from uuid import uuid4

    from ledgervigil.proofpacks.anchors import GitAnchors
    from ledgervigil.proofpacks.integrity import IntegrityError, ZERO
    from tools import deploy_entry
    from tools.verify_recovery import fingerprints, replay, schema_digest

    unbind(database)
    with connection(database=database) as db:
        report = {
            'status': 'PASS', 'snapshot_consistent': True, 'restore_interlock': 'RESTORED_UNVERIFIED',
            'schema_owners_grants_policies_constraints_and_functions_match': True,
            'table_fingerprints': fingerprints(db), 'schema_sha256': schema_digest(db),
            'ledger_replay': replay(db), 'external_anchor': {'status': 'NOT_CONFIGURED'},
        }
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(report))
    monkeypatch.setattr(deploy_entry, '_secret', lambda _: 'postgresql://synthetic.invalid')
    # Use the real test-cluster connection without exporting its credentials.
    with connection(database=database) as db, monkeypatch.context() as m:
        m.setattr(psycopg, 'connect', lambda _: db)
        store = GitAnchors(tmp_path / 'trusted').initialize()
        store.append({'tenant_id': str(uuid4()), 'chain_seq': 1,
                      'previous_chain_head': ZERO, 'chain_hash': '1' * 64})
        m.setenv('LV_ANCHOR_REPOSITORY', str(store.root))
        with pytest.raises(IntegrityError, match='older'):
            deploy_entry.bind_restored(path)
    with connection(database=database) as db:
        assert db.execute('SELECT lv.recovery_state()').fetchone() == ('RESTORED_UNVERIFIED',)


def test_cluster_identifier_mismatch_refuses_same_database_oid(database):
    # Simulate a logical restore whose destination OID coincides with the source.
    with connection(database=database) as c:
        c.execute('UPDATE lv.database_binding SET cluster_system_id=NULL')
    with connection('lv_app', database) as c:
        assert c.execute('SELECT lv.recovery_state()').fetchone() == ('RESTORED_UNVERIFIED',)
    assert bind(database) == 'BOUND'
    with connection('lv_app', database) as c:
        assert c.execute('SELECT lv.recovery_state()').fetchone() == ('BOUND',)


def test_readiness_never_passes_with_only_anchors(tmp_path, monkeypatch):
    from tools.deploy_entry import readiness
    for key in ('LV_DATABASE_URL','LV_WORKER_DATABASE_URL','LV_AUTHN_DATABASE_URL'):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key+'_FILE', raising=False)
    monkeypatch.delenv('LV_MIGRATOR_DATABASE_URL_FILE', raising=False)
    monkeypatch.setenv('LV_ANCHOR_REPOSITORY', str(tmp_path))
    verdict = readiness()
    assert verdict['status'] == 'NOT_READY'
    assert verdict['checks']['lv_authn_login'] == 'MISSING'
