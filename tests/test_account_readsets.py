"""Native PostgreSQL proofs for account dependencies; no submission or approval route is claimed."""
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import psycopg
from psycopg.types.json import Jsonb
import pytest
from uuid import UUID
from tools.dev_db import connection, migrate
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ledger.seed import seed_manual_journal
from ledgervigil.simulation import bundle as capture_module, versions
from test_proposal_drafts import world, seed, signed_in, fingerprint  # noqa: F401
from test_simulation import api, new_draft, simulate, get_run, _bundle  # noqa: F401
from conftest import ledger, account, binding
from configuration_support import configuration_connection


def version(c, aid):
    return c.execute('SELECT version FROM lv.account WHERE id=%s', (aid,)).fetchone()[0]


def all_versions(db, lid):
    with connection(database=db) as c:
        return dict(c.execute('SELECT id::text,version FROM lv.account WHERE ledger_id=%s ORDER BY id', (lid,)))


def account_refs(run):
    return {e['entity_id']:e['version'] for e in run['read_set'] if e['entity_type']=='ACCOUNT'}


def journal_body(cfg, ref):
    return {
        'source_ref':ref,'effective_date':'2026-10-14','description':'SYNTHETIC dependency change',
        'lines':[{'account_id':cfg['chart']['BANK_OPERATING'],'side':'D','amount':'0.01'},
                 {'account_id':cfg['chart']['OPENING_BALANCE_EQUITY'],'side':'C','amount':'0.01'}]}


def journal(seed, cfg, ref):
    return seed_manual_journal(seed, cfg['tenant'], cfg['ledger'], journal_body(cfg,ref))


def test_account_versions_are_database_owned_and_noop_writes_invalidate(configuration, tenants):
    with tenant_transaction(configuration, tenants[0]):
        lid=ledger(configuration,tenants[0]);aid=account(configuration,tenants[0],lid)
        assert version(configuration,aid)==1
        for n in (2,0,-1):
            with pytest.raises(psycopg.errors.CheckViolation):
                with configuration.transaction():configuration.execute('UPDATE lv.account SET version=%s WHERE id=%s',(n,aid))
        configuration.execute("UPDATE lv.account SET name='renamed' WHERE id=%s",(aid,));assert version(configuration,aid)==2
        configuration.execute('UPDATE lv.account SET name=name WHERE id=%s',(aid,));assert version(configuration,aid)==3
        assert configuration.execute('UPDATE lv.account SET name=name WHERE id=%s AND version=2 RETURNING id',(aid,)).fetchall()==[]
        assert version(configuration,aid)==3
        with pytest.raises(RuntimeError):
            with configuration.transaction():
                configuration.execute("UPDATE lv.account SET name='rolled back' WHERE id=%s",(aid,));assert version(configuration,aid)==4
                raise RuntimeError('abort')
        assert version(configuration,aid)==3


def test_binding_changes_invalidate_old_and_new_accounts(configuration, tenants):
    with tenant_transaction(configuration,tenants[0]):
        lid=ledger(configuration,tenants[0]);a=account(configuration,tenants[0],lid,'1000');b=account(configuration,tenants[0],lid,'1001')
        binding(configuration,tenants[0],lid,a)
        assert (version(configuration,a),version(configuration,b))==(2,1)
        configuration.execute('UPDATE lv.account_role_binding SET account_id=%s WHERE ledger_id=%s',(b,lid))
        assert (version(configuration,a),version(configuration,b))==(3,2)
        with pytest.raises(RuntimeError):
            with configuration.transaction():
                configuration.execute('UPDATE lv.account_role_binding SET account_id=%s WHERE ledger_id=%s',(a,lid))
                raise RuntimeError('abort binding')
        assert (version(configuration,a),version(configuration,b))==(3,2)


def test_account_versions_are_tenant_scoped(configuration, tenants):
    a,b=tenants
    with tenant_transaction(configuration,a):lid=ledger(configuration,a);aid=account(configuration,a,lid)
    with tenant_transaction(configuration,b):
        assert configuration.execute('SELECT version FROM lv.account WHERE id=%s',(aid,)).fetchall()==[]
        assert configuration.execute("UPDATE lv.account SET name='foreign' WHERE id=%s RETURNING version",(aid,)).fetchall()==[]
    with tenant_transaction(configuration,a):assert version(configuration,aid)==1


def test_competing_account_writes_serialize_without_lost_versions(database, configuration, tenants):
    tenant=tenants[0]
    with tenant_transaction(configuration,tenant):lid=ledger(configuration,tenant);aid=account(configuration,tenant,lid)
    barrier=threading.Barrier(8)
    def write(n):
        with configuration_connection(database) as c:
            barrier.wait(timeout=10)
            with tenant_transaction(c,tenant):
                c.execute('SET LOCAL lock_timeout=\'5s\'')
                return c.execute('UPDATE lv.account SET name=%s WHERE id=%s RETURNING version',(f'writer {n}',aid)).fetchone()[0]
    with ThreadPoolExecutor(max_workers=8) as pool:values=list(pool.map(write,range(8)))
    assert sorted(values)==list(range(2,10))
    with tenant_transaction(configuration,tenant):assert version(configuration,aid)==9


def test_capture_pins_all_bound_account_versions_in_the_same_snapshot(database, api, world):
    c=signed_in(api,world,'analyst')
    run=get_run(c,simulate(c,new_draft(c,world)).json()['id'])
    actual=all_versions(database,world['l1']['ledger'])
    assert len(account_refs(run))==7 and account_refs(run)==actual
    assert run['versions']['account_read_set']=='lv-account-dependencies/1'


def test_account_edit_changes_new_capture_but_frozen_run_replays_identically(database, configuration, api, world):
    c=signed_in(api,world,'analyst');pid=new_draft(c,world)
    first=get_run(c,simulate(c,pid).json()['id']);aid=world['l1']['chart']['BANK_OPERATING']
    old=account_refs(first)[str(aid)]
    with tenant_transaction(configuration,world['A']):configuration.execute("UPDATE lv.account SET name='SYNTHETIC renamed bank' WHERE id=%s",(aid,))
    second=get_run(c,simulate(c,pid).json()['id'])
    assert account_refs(second)[str(aid)]==old+1
    assert second['snapshot']['seq']==first['snapshot']['seq']
    assert first['input_hash']!=second['input_hash']
    assert c.post(f"/api/v1/simulations/{first['id']}/replay").json()['identical'] is True
    assert get_run(c,first['id'])==first


def test_posting_invalidates_balance_dependencies_and_idempotent_replay_does_not(database, api, world, seed):
    c=signed_in(api,world,'analyst');pid=new_draft(c,world)
    first=get_run(c,simulate(c,pid).json()['id']);old=account_refs(first)
    journal(seed,world['l1'],'account-version-balance')
    after=all_versions(database,world['l1']['ledger'])
    touched={str(world['l1']['chart'][r]) for r in ('BANK_OPERATING','OPENING_BALANCE_EQUITY')}
    assert {a for a in old if after[a]>old[a]}==touched
    replay=journal(seed,world['l1'],'account-version-balance');assert replay['replayed'] is True
    assert all_versions(database,world['l1']['ledger'])==after
    second=get_run(c,simulate(c,pid).json()['id']);assert account_refs(second)==after
    assert second['snapshot']['seq']==first['snapshot']['seq']+1
    assert c.post(f"/api/v1/simulations/{first['id']}/replay").json()['identical'] is True


def test_failed_posting_rolls_back_account_versions_with_financial_records(database, api, world, seed):
    cfg=world['l1'];before=all_versions(database,cfg['ledger'])
    with pytest.raises(RuntimeError, match='abort whole commit'):
        with tenant_transaction(seed,world['A']):
            seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'],Jsonb(journal_body(cfg,'rolled-back-version'))))
            assert version(seed,cfg['chart']['BANK_OPERATING'])==before[str(cfg['chart']['BANK_OPERATING'])]+1
            raise RuntimeError('abort whole commit')
    assert all_versions(database,cfg['ledger'])==before


def test_concurrent_posting_cannot_mix_old_facts_with_new_account_versions(database, api, world, seed, monkeypatch):
    c=signed_in(api,world,'analyst');pid=new_draft(c,world)
    old=all_versions(database,world['l1']['ledger']);called=[]
    def change():
        if not called:called.append(journal(seed,world['l1'],'during-account-capture'))
    monkeypatch.setattr(capture_module,'_after_capture_counter',change)
    run=get_run(c,simulate(c,pid).json()['id'])
    assert called and account_refs(run)==old
    assert run['snapshot']['seq']==int(called[0]['seq'])-1
    assert all_versions(database,world['l1']['ledger'])!=old
    assert c.post(f"/api/v1/simulations/{run['id']}/replay").json()['identical'] is True


def test_simulation_only_does_not_increment_any_account_version(database, api, world):
    c=signed_in(api,world,'analyst');pid=new_draft(c,world);before=all_versions(database,world['l1']['ledger'])
    run=get_run(c,simulate(c,pid).json()['id'])
    assert c.post(f"/api/v1/simulations/{run['id']}/replay").json()['identical'] is True
    assert all_versions(database,world['l1']['ledger'])==before


def test_legacy_capture_profiles_keep_their_original_read_set_shape(database, api, world, monkeypatch):
    c=signed_in(api,world,'analyst');pid=new_draft(c,world)
    for profile in (versions.LEGACY_SIMULATOR,versions.PREVIOUS_SIMULATOR,versions.DECIMAL_SIMULATOR):
        with monkeypatch.context() as patch:
            patch.setattr(versions,'SIMULATOR',profile)
            run=get_run(c,simulate(c,pid).json()['id'])
            assert account_refs(run)=={} and 'account_read_set' not in run['versions']
            assert c.post(f"/api/v1/simulations/{run['id']}/replay").json()['identical'] is True


@pytest.mark.parametrize('forged', [0, 2, 2147483647])
def test_new_accounts_cannot_claim_an_arbitrary_initial_version(configuration, tenants, forged):
    with tenant_transaction(configuration, tenants[0]):
        lid=ledger(configuration,tenants[0])
        with pytest.raises(psycopg.errors.CheckViolation):
            with configuration.transaction():
                configuration.execute("INSERT INTO lv.account(tenant_id,ledger_id,code,name,type,currency,currency_exponent,version) VALUES (%s,%s,'9999','forged version','ASSET','INR',2,%s)", (tenants[0],lid,forged))


def test_concurrent_postings_advance_each_touched_account_without_lost_updates(database, api, world, seed):
    cfg=world['l1'];before=all_versions(database,cfg['ledger']);barrier=threading.Barrier(6)
    def post(n):
        with connection('lv_seed',database,autocommit=True) as writer:
            barrier.wait(timeout=10)
            return journal(writer,cfg,f'concurrent-account-{n}')
    with ThreadPoolExecutor(max_workers=6) as pool:receipts=list(pool.map(post,range(6)))
    assert len({r['seq'] for r in receipts})==6 and all(not r['replayed'] for r in receipts)
    after=all_versions(database,cfg['ledger'])
    touched={str(cfg['chart'][r]) for r in ('BANK_OPERATING','OPENING_BALANCE_EQUITY')}
    assert after=={a:v+(6 if a in touched else 0) for a,v in before.items()}


def test_0012_upgrade_preserves_a_stored_old_profile_and_backfills_existing_accounts(monkeypatch):
    from test_processor_payments import disposable
    from test_proposal_drafts import world as setup_world
    from test_simulation import api as setup_api
    from test_period_close import columns_hash
    with disposable('0011_simulation_runs') as db:
        with connection('lv_app',db,autocommit=True) as app, connection('lv_seed',db,autocommit=True) as seed_:
            w=setup_world.__wrapped__(db,app,seed_)
        fixture=setup_api.__wrapped__(db);application=next(fixture)
        from legacy_issuer_support import use_historical_issuer
        use_historical_issuer(application, db, '0011_simulation_runs')
        c=signed_in(application,w,'analyst')
        try:
            with monkeypatch.context() as patch:
                patch.setattr(versions,'SIMULATOR',versions.DECIMAL_SIMULATOR)
                old=get_run(c,simulate(c,new_draft(c,w)).json()['id'])
            assert account_refs(old)=={}
            with connection(database=db) as admin:
                tables=[r[0] for r in admin.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='lv' AND c.relkind='r' ORDER BY 1")]
                cols={t:[r[0] for r in admin.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum",('lv.'+t,))] for t in tables}
            before=columns_hash(db,cols)
            migrate(db,'0012_account_versions')
            assert columns_hash(db,cols)==before
            assert set(all_versions(db,w['l1']['ledger']).values())=={1}
            assert get_run(c,old['id'])==old
            assert c.post(f"/api/v1/simulations/{old['id']}/replay").json()['identical'] is True
            new=get_run(c,simulate(c,new_draft(c,w)).json()['id'])
            assert len(account_refs(new))==7
            assert new['versions']['simulator']==versions.SIMULATOR
        finally:
            c.close();fixture.close()


def test_projection_write_without_dependency_authority_is_refused_and_rolled_back(database, api, world, seed):
    cfg=world['l1'];before=fingerprint(database,set())
    with connection(database=database) as admin:
        admin.execute('GRANT UPDATE ON lv.account_balance_current TO lv_seed')
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege,match='account'):
            with tenant_transaction(seed,world['A']):
                seed.execute('SELECT lv.seed_manual_journal(%s,%s)', (cfg['ledger'],Jsonb(journal_body(cfg,'unauthorized-projection-change'))))
                seed.execute('UPDATE lv.account_balance_current SET debit_total=debit_total+1 WHERE ledger_id=%s',(cfg['ledger'],))
        assert fingerprint(database,set())==before
    finally:
        with connection(database=database) as admin:
            admin.execute('REVOKE UPDATE ON lv.account_balance_current FROM lv_seed')
