"""Independent review scenarios (2026-11-03), ported by intent from a separate cross-check build.

Expected outcomes come from accepted requirements (Stage 2A §§3-4, 8.1, 8.4, 11.4, 12.1, 19.2;
ADR-006/015/024/025), not from agreement between implementations. All data is SYNTHETIC; the
accounting oracle status remains UNREVIEWED. Capabilities that do not exist yet (reversals, typed
document operations, EXECUTION commits, PERIOD_CLOSE commits) are not exercised here.
"""
from copy import deepcopy
import threading
import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from tools.dev_db import connection
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ledger.seed import seed_manual_journal
from ledgervigil.ledger.replay import replay_events
from test_posting import (FINANCIAL, HISTORY, provision, register, request, line, post, counts, events,
                          wait_for_block)

DEFINER_FUNCTIONS = {'recovery_state','guard_restored_writes',   # 0037 restore interlock
                     'provider_api','provider_connection_json','provider_due','provider_dispatch','provider_claim','provider_exchange_finish',   # 0035: provider API writer, job summary view and worker entry points
                     'provider_refresh_claim','provider_refresh_finish','provider_credential_current','provider_sync_page','provider_job_finish','provider_revoke_finish','provider_kek_inventory','provider_rewrap',
                     'detection_begin','detection_record','detection_dispose','validate_detection_run','validate_detection_finding','validate_detection_disposition',   # 0032: detection writers and commit-time validators
                     'semantic_begin','semantic_finish','validate_semantic_recheck','semantic_needed','pack_status','pack_anchor_preflight','auto_proof_pack','pack_api','pack_due','pack_dispatch','pack_claim','pack_snapshot','pack_seal','pack_anchor','validate_proof_pack','pack_request','pack_export_request','pack_chain_check','pack_fail','native_export_api','native_export_due','native_export_dispatch','native_export_claim','native_export_finish','native_export_snapshot','validate_native_export_job','authorize_proof_export','edit_account','validate_configuration_change','validate_account_configuration','execution_available','ingestion_ledger','fence_ingestion','executor_blocker','post_native_settlement','execute_native','validate_native_execution','validate_execution_check','guard_execution_batch','record_execution_incident','reject_native','review_blocker','approval_available','approve_native','validate_approval_check','validate_proposal_decision','override_evidence','validate_evidence_override','add_proposal_comment','validate_proposal_comment','register_source_plan','validate_source_registration','workflow_resimulate_begin','workflow_revalidate','validate_freshness_check','maintenance_due','maintenance_dispatched','maintenance_claim','maintenance_step','maintenance_fail','maintenance_sweep','maintenance_health','seed_manual_journal', 'seed_processor_payment', 'seed_processor_settlement', 'validate_entry_balance', 'validate_seed_batch',
                     'close_period', 'validate_period_close_batch',   # 0009: the period-close entry point and its commit-time validator
                     'proposal_create', 'proposal_edit', 'validate_proposal_revision',   # 0010: draft entry points and their validator
                     'simulation_record','workflow_request','workflow_begin','workflow_finish','workflow_withdraw','workflow_refused','validate_submission','validate_reservation'}   # 0011: the only write path for immutable simulation runs
RUNTIME_EXECUTABLE = {'recovery_state',   # 0037 (diagnostic only)
                      'provider_api','provider_connection_json','provider_job_json','provider_object_json',   # 0035 (API writer and JSON views)
                      'detection_inputs_v2','detection_current_fingerprint','detection_expected_v1',   # 0033 (invoker readers)
                      'detection_inputs','detection_fingerprint','detection_config','detection_expected','detection_facts','detection_run_json','detection_finding_json','detection_begin','detection_record','detection_dispose',   # 0032 (invoker readers + definer writers)
                      'semantic_begin','semantic_finish','semantic_projection','semantic_needed','pack_status','pack_request','pack_export_request','pack_chain_check','native_export_api','edit_account','execution_available','execute_native','native_execution_json','execution_posting_proof','record_execution_incident','reject_native','approval_available','approve_native','decision_json','override_evidence','override_json','add_proposal_comment','comment_json','register_source_plan','workflow_resimulate_begin','workflow_revalidate','close_period', 'proposal_create', 'proposal_edit', 'proposal_json',   # proposal_json is SECURITY INVOKER (RLS applies)
                      'simulation_record','workflow_request','workflow_begin','workflow_finish','workflow_withdraw','workflow_refused'}


@pytest.fixture
def fixture_ledger(app, tenants, database):
    config = provision(app, tenants[0]); config['actor'] = register(database, tenants[0]); return config


@pytest.fixture
def seed(database):
    with connection('lv_seed', database, autocommit=True) as c:
        yield c


def owner_session(database, tenant=None):
    c = connection('lv_migrator', database)
    c.execute('SET ROLE lv_owner')
    if tenant is not None:
        c.execute("SELECT set_config('app.tenant_id',%s,false)", (str(tenant),))
    return c


# ------------------------------------------------------------------------------------------- SECURITY DEFINER
def test_definer_functions_are_owned_pinned_and_not_public(database):
    """ADR-015 / 2A §12.1: privileged code must not depend on objects a caller controls."""
    with connection(database=database) as admin:
        rows = admin.execute(
            "SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig, "
            "has_function_privilege('lv_app', p.oid, 'EXECUTE'), has_function_privilege('lv_seed', p.oid, 'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'lv'").fetchall()
    assert {r[0] for r in rows if r[2]} == DEFINER_FUNCTIONS
    for name, owner, definer, config, app_exec, seed_exec in rows:
        assert owner == 'lv_owner', name
        # pg_temp must be listed, and last: otherwise PostgreSQL searches it FIRST for type names.
        assert config is not None and 'search_path=pg_catalog, pg_temp' in config, (name, config)
        assert app_exec == (name in RUNTIME_EXECUTABLE), name   # 0009/0010: the only runtime-executable lv functions
        assert seed_exec == (name in ('seed_manual_journal', 'seed_processor_payment', 'seed_processor_settlement')), name


def test_caller_temporary_types_cannot_hijack_the_definer(database, app, fixture_ledger):
    """R-01. A caller holding TEMPORARY (PostgreSQL's default PUBLIC grant on a new database) creates
    a temporary domain named `uuid` whose CHECK calls the caller's temporary function. The posting
    function must not resolve its types through the caller's temporary schema, so the probe must never
    run as lv_owner and the seed must behave normally."""
    cfg = fixture_ledger
    with connection(database=database, autocommit=True) as admin:
        admin.execute(sql.SQL('GRANT TEMPORARY ON DATABASE {} TO lv_seed').format(sql.Identifier(database)))
    try:
        with connection('lv_seed', database, autocommit=True) as c:
            c.execute("""
              CREATE FUNCTION pg_temp.shadow_probe(v text) RETURNS boolean LANGUAGE plpgsql AS $$
              BEGIN RAISE EXCEPTION 'SHADOW EXECUTED AS %', current_user; END $$;
              CREATE DOMAIN pg_temp.uuid AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.date AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
              CREATE DOMAIN pg_temp.jsonb AS pg_catalog.text CHECK (pg_temp.shadow_probe(VALUE));
            """)
            result = post(c, cfg)
        assert result['seq'] == '1' and not result['replayed']
    finally:
        with connection(database=database, autocommit=True) as admin:
            admin.execute(sql.SQL('REVOKE TEMPORARY ON DATABASE {} FROM lv_seed').format(sql.Identifier(database)))
    assert counts(app, cfg)['commit_batch'] == 1


def test_restricted_logins_have_no_object_creation_path(database):
    with connection(database=database) as admin:
        for role in ('lv_app', 'lv_seed'):
            assert admin.execute("SELECT has_database_privilege(%s, current_database(), 'TEMPORARY'), "
                                 "has_database_privilege(%s, current_database(), 'CREATE'), "
                                 "has_schema_privilege(%s, 'public', 'CREATE'), has_schema_privilege(%s, 'lv', 'CREATE')",
                                 (role, role, role, role)).fetchone() == (False, False, False, False), role


# ------------------------------------------------------------------------------------------- exact money
def test_exponent_four_asymmetric_rounding_into_balance_refused(database, app, seed, tenants):
    """2A §3.3: never round. With exponent 4, 1.00004 would round to 1.0000 in NUMERIC(20,4) and balance."""
    with connection('lv_migrator', database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner'); owner.execute("INSERT INTO lv.currency VALUES('CLF',4) ON CONFLICT DO NOTHING")
    cfg = provision(app, tenants[0], currency='CLF', exponent=4); register(database, tenants[0])
    body = request(cfg, amount='1.0000'); body['lines'][0]['amount'] = '1.00004'
    with pytest.raises(psycopg.errors.CheckViolation):
        post(seed, cfg, body)
    assert counts(app, cfg)['commit_batch'] == 0
    post(seed, cfg, request(cfg, ref='max', amount='9999999999999999.9999'))
    with tenant_transaction(app, cfg['tenant']):
        assert str(app.execute('SELECT DISTINCT amount FROM lv.journal_line WHERE ledger_id=%s', (cfg['ledger'],)).fetchone()[0]) == '9999999999999999.9999'


@pytest.mark.parametrize('role', ['lv_app', 'lv_seed'])
def test_direct_line_insert_cannot_bypass_the_write_boundary(database, app, seed, fixture_ledger, role):
    """The NUMERIC(20,4) column rounds before CHECKs run, so exactness relies on no restricted login
    being able to write amounts except through the validating function."""
    cfg = fixture_ledger; post(seed, cfg)
    with connection(role, database) as c:
        with tenant_transaction(c, cfg['tenant']):
            entry = c.execute('SELECT id, seq FROM lv.journal_entry WHERE ledger_id=%s', (cfg['ledger'],)).fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_transaction(c, cfg['tenant']):
                c.execute("INSERT INTO lv.journal_line(tenant_id,ledger_id,entry_id,line_no,seq,account_id,requires_party,side,amount,currency,currency_exponent) "
                          "VALUES(%s,%s,%s,9,%s,%s,false,'D','10.00001','INR',2)",
                          (cfg['tenant'], cfg['ledger'], entry[0], entry[1], cfg['chart']['BANK_OPERATING']))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_transaction(c, cfg['tenant']):
                c.execute("SELECT lv.parse_line_amount('1.00', 2::smallint)")


# ------------------------------------------------------------------------------------------- atomicity
def test_tenant_switch_before_commit_fails_closed_without_fragments(app, seed, fixture_ledger, tenants):
    """2A §12.1: the deferred checks run as the definer under FORCE RLS with the context present at
    COMMIT. Switching the context after the function returns must reject the commit, not skip checks."""
    cfg = fixture_ledger; before = counts(app, cfg)
    with pytest.raises(psycopg.errors.CheckViolation):
        with tenant_transaction(seed, cfg['tenant']):
            seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'], Jsonb(request(cfg))))
            seed.execute("SELECT set_config('app.tenant_id',%s,true)", (str(tenants[1]),))
    assert counts(app, cfg) == before
    retry = post(seed, cfg)                      # no idempotency fragment survives: a fresh, non-replayed commit
    assert retry['seq'] == '1' and not retry['replayed']


def test_two_seeds_in_one_transaction_with_savepoint_rollback(app, seed, fixture_ledger):
    cfg = fixture_ledger
    with tenant_transaction(seed, cfg['tenant']):
        seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'], Jsonb(request(cfg, ref='a'))))
        with pytest.raises(psycopg.errors.CheckViolation):
            with seed.transaction():            # savepoint
                bad = request(cfg, ref='b'); bad['lines'][0]['amount'] = '1.001'
                seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'], Jsonb(bad)))
        seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'], Jsonb(request(cfg, ref='c'))))
    c = counts(app, cfg)
    assert c['commit_batch'] == 2 and c['counter'][0][:2] == (2, 2)
    assert replay_events(events(app, cfg))['entries'] == 2


# ------------------------------------------------------------------------------------------- isolation
def test_connection_reuse_does_not_carry_tenant_context(app, seed, fixture_ledger):
    cfg = fixture_ledger; post(seed, cfg)
    with tenant_transaction(app, cfg['tenant']):
        assert app.execute('SELECT count(*) FROM lv.journal_entry').fetchone() == (1,)
    assert app.execute("SELECT coalesce(current_setting('app.tenant_id',true),'')").fetchone() == ('',)
    for table in FINANCIAL:
        assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,), table
    with pytest.raises(RuntimeError):
        with app.transaction():
            with tenant_transaction(app, cfg['tenant']):
                pass


def test_seed_principal_is_per_tenant_and_actor_comes_from_session(database, app, seed, fixture_ledger, tenants):
    cfg = fixture_ledger
    other = provision(app, tenants[1])          # tenant B has a complete ledger but no seed principal
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        post(seed, other)
    body = request(cfg); body['created_by'] = str(cfg['actor'])
    with pytest.raises(psycopg.errors.CheckViolation):
        post(seed, cfg, body)                   # no caller-supplied actor field
    post(seed, cfg)
    with tenant_transaction(app, cfg['tenant']):
        assert {r[0] for r in app.execute('SELECT created_by FROM lv.commit_batch WHERE ledger_id=%s', (cfg['ledger'],))} == {cfg['actor']}
    assert counts(app, other)['commit_batch'] == 0


def test_owner_without_context_sees_and_changes_nothing(database, seed, fixture_ledger):
    cfg = fixture_ledger; post(seed, cfg)
    with owner_session(database) as c:
        for table in FINANCIAL + ('ledger', 'account', 'service_principal'):
            assert c.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone() == (0,), table


# ------------------------------------------------------------------------------------------- concurrency
def _blocked_call(database, role, fn):
    ready = threading.Event(); pid = []; out = {}
    def run():
        with connection(role, database, autocommit=True) as c:
            
            if role=='lv_migrator':c.execute('SET ROLE lv_owner')
            pid.append(c.info.backend_pid); c.execute("SET lock_timeout='6s'"); ready.set()
            try:
                out['result'] = fn(c)
            except Exception as error:  # noqa: BLE001
                out['error'] = error
    thread = threading.Thread(target=run); thread.start(); assert ready.wait(3)
    return thread, pid, out


# These two tests exercise low-level operator/seed locking, not the runtime edit API.
def test_account_deactivation_waits_for_pending_posting(database, configuration, seed, fixture_ledger):
    cfg = fixture_ledger
    with tenant_transaction(configuration, cfg['tenant']):
        aid = configuration.execute("INSERT INTO lv.account(tenant_id,ledger_id,code,name,type,currency,currency_exponent) VALUES(%s,%s,'1010','Synthetic unbound','ASSET','INR',2) RETURNING id", (cfg['tenant'], cfg['ledger'])).fetchone()[0]
    body = request(cfg); body['lines'][0]['account_id'] = str(aid)
    with tenant_transaction(seed, cfg['tenant']):
        seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'], Jsonb(body)))
        def deactivate(c):
            with tenant_transaction(c, cfg['tenant']):
                c.execute('UPDATE lv.account SET active=false WHERE id=%s', (aid,))
            return True
        thread, pid, out = _blocked_call(database, 'lv_migrator', deactivate)
        wait_for_block(database, pid[0], seed.info.backend_pid)
    thread.join(7)
    assert out.get('result') is True, out
    assert counts(configuration, cfg)['commit_batch'] == 1     # posted while active; deactivated afterwards


def test_posting_waits_for_pending_deactivation_then_refuses(database, configuration, fixture_ledger):
    cfg = fixture_ledger
    with tenant_transaction(configuration, cfg['tenant']):
        aid = configuration.execute("INSERT INTO lv.account(tenant_id,ledger_id,code,name,type,currency,currency_exponent) VALUES(%s,%s,'1011','Synthetic unbound','ASSET','INR',2) RETURNING id", (cfg['tenant'], cfg['ledger'])).fetchone()[0]
    body = request(cfg); body['lines'][0]['account_id'] = str(aid)
    with tenant_transaction(configuration, cfg['tenant']):
        configuration.execute('UPDATE lv.account SET active=false WHERE id=%s', (aid,))
        thread, pid, out = _blocked_call(database, 'lv_seed', lambda c: post(c, cfg, body))
        wait_for_block(database, pid[0], configuration.info.backend_pid)
    thread.join(7)
    assert isinstance(out.get('error'), psycopg.errors.CheckViolation), out
    assert counts(configuration, cfg)['commit_batch'] == 0


def test_principal_revocation_waits_for_pending_posting_then_takes_effect(database, app, seed, fixture_ledger):
    cfg = fixture_ledger
    with tenant_transaction(seed, cfg['tenant']):
        seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'], Jsonb(request(cfg))))
        done = {}
        def revoke():
            with owner_session(database, cfg['tenant']) as c:
                c.execute("SET lock_timeout='6s'")
                c.execute('UPDATE lv.service_principal SET active=false WHERE id=%s', (cfg['actor'],)); c.commit()
                done['ok'] = True
        thread = threading.Thread(target=revoke); thread.start()
        thread.join(0.7)
        assert thread.is_alive(), 'revocation must wait for the in-flight posting that holds the principal'
    thread.join(7)
    assert done.get('ok') and counts(app, cfg)['commit_batch'] == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        post(seed, cfg, request(cfg, ref='after-revoke'))


@pytest.mark.parametrize('workers,per_worker', [(6, 5)])
def test_concurrent_mixed_failures_leave_a_gapless_sequence(database, app, fixture_ledger, workers, per_worker):
    cfg = fixture_ledger
    def worker(w):
        outcomes = []
        with connection('lv_seed', database, autocommit=True) as c:
            c.execute("SET lock_timeout='20s'")
            for i in range(per_worker):
                body = request(cfg, ref=f'w{w}-{i}', amount=f'{w + 1}.{i:02d}')
                if (w + i) % 3 == 0:
                    body['lines'][1]['amount'] = '0.01'            # unbalanced: fails at COMMIT
                try:
                    outcomes.append(int(post(c, cfg, body)['seq']))
                except psycopg.errors.CheckViolation:
                    outcomes.append(None)
        return outcomes
    threads, results = [], []
    lock = threading.Lock()
    def run(w):
        r = worker(w)
        with lock: results.extend(r)
    for w in range(workers):
        t = threading.Thread(target=run, args=(w,)); t.start(); threads.append(t)
    for t in threads: t.join(60)
    ok = sorted(r for r in results if r is not None)
    assert None in results and ok == list(range(1, len(ok) + 1))
    c = counts(app, cfg)
    assert c['counter'][0][:2] == (len(ok), len(ok))
    replay = replay_events(events(app, cfg))
    assert replay['entries'] == len(ok) and replay['head'] == c['counter'][0][3]


# ------------------------------------------------------------------------------------------- replay (2A §8.4, I-14)
def test_replay_rejects_reordered_duplicated_and_spliced_events(app, seed, fixture_ledger, tenants):
    cfg = fixture_ledger
    for i in range(3):
        post(seed, cfg, request(cfg, ref=f'r{i}', amount=f'{i + 1}.00'))
    rows = events(app, cfg)
    assert replay_events(rows)['entries'] == 3
    for broken in ([rows[1], rows[0], rows[2]], rows[:2] + [rows[1]], [rows[0], rows[2]]):
        with pytest.raises(ValueError):
            replay_events(deepcopy(broken))
    other = provision(app, tenants[1]); register_ = register
    register_(seed.info.dbname, tenants[1]); post(seed, other)
    with pytest.raises(ValueError):                       # an event from another ledger cannot be spliced in
        replay_events(deepcopy(rows[:1]) + events(app, other))


def test_replay_alone_cannot_detect_tail_truncation_but_head_comparison_does(app, seed, fixture_ledger):
    """Recorded limitation: a valid prefix replays cleanly. I-14 requires comparing the replayed head
    with the stored chain head; the database keeps that head in ledger_counter.event_head."""
    cfg = fixture_ledger
    for i in range(2):
        post(seed, cfg, request(cfg, ref=f't{i}'))
    rows = events(app, cfg); head = counts(app, cfg)['counter'][0][3]
    assert replay_events(rows[:1])['head'] != head
    assert replay_events(rows)['head'] == head


# ------------------------------------------------------------------------------------------- authority
def test_seed_on_provider_or_books_refused_even_with_complete_chart(database, app, seed, tenants):
    for purpose in ('BOOKS',):
        cfg = provision(app, tenants[1], purpose=purpose); register(database, tenants[1])
        with pytest.raises(psycopg.errors.CheckViolation):
            post(seed, cfg)
        assert counts(app, cfg)['commit_batch'] == 0
    with connection(database=database) as admin:
        kinds = {r[0] for r in admin.execute("SELECT unnest(enum_range(NULL::lv.commit_kind))::text")}
        prov = {r[0] for r in admin.execute("SELECT unnest(enum_range(NULL::lv.provenance))::text")}
    assert kinds == {'SEED', 'PERIOD_CLOSE','EXECUTION'} and prov == {'SYNTHETIC'}   # 0009 adds PERIOD_CLOSE; 0021 adds separately guarded native EXECUTION
