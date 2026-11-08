from uuid import uuid4
from psycopg import sql
import pytest
from tools.dev_db import connection,prepare_database,migrate

def test_fresh_database_upgrade_downgrade_upgrade():
    name='lv_migration_test_'+uuid4().hex
    with connection(autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        prepare_database(name);migrate(name)
        with connection('lv_app',name) as c:
            assert c.execute('SELECT count(*) FROM lv.currency').fetchone()==(3,)
        migrate(name,'base',downgrade=True,disposable=True)
        with connection(database=name) as c:
            assert c.execute("SELECT to_regnamespace('lv')").fetchone()==(None,)
            assert c.execute('SELECT count(*) FROM lv_meta.alembic_version').fetchone()==(0,)
        migrate(name)
        with connection('lv_app',name) as c:
            assert c.execute('SELECT count(*) FROM lv.currency').fetchone()==(3,)
            assert c.execute('SELECT count(*) FROM lv.ledger').fetchone()==(0,)
    finally:
        with connection(autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

def test_development_downgrade_refused():
    with pytest.raises(ValueError,match='disposable'):
        migrate('ledgervigil_dev','base',downgrade=True,disposable=True)

def test_upgrade_from_0002_preserves_synthetic_records():
    """0003 changes function settings only: synthetic records written under 0002 survive unchanged,
    keep replaying to the same chain head, and posting continues under the new revision."""
    import sys
    from pathlib import Path
    from uuid import uuid7
    sys.path.insert(0, str(Path(__file__).parent))
    from test_posting import provision, register, request, post, events, counts
    from ledgervigil.ledger.replay import replay_events
    name='lv_migration_test_'+uuid4().hex
    with connection(autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        prepare_database(name);migrate(name,'0002_posting')
        tenant=uuid7()
        with connection('lv_migrator',name) as c:
            c.execute('SET LOCAL ROLE lv_owner');c.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenant),))
            c.execute("INSERT INTO lv.tenant(tenant_id,name) VALUES(%s,'Synthetic upgrade tenant')",(tenant,))
        with connection('lv_app',name,autocommit=True) as app:
            cfg=provision(app,tenant)
        register(name,tenant)
        with connection('lv_seed',name,autocommit=True) as s:
            for i in range(3): post(s,cfg,request(cfg,ref=f'pre-upgrade-{i}',amount=f'{i+1}.25'))
        def snapshot():
            with connection('lv_app',name,autocommit=True) as app:
                return counts(app,cfg),events(app,cfg)
        before=snapshot()
        migrate(name)
        with connection(database=name) as c:
            assert c.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0033_detection_overlap_dates',)
        after=snapshot()
        assert after==before
        assert replay_events(after[1])['head']==after[0]['counter'][0][3]
        with connection('lv_seed',name,autocommit=True) as s:
            assert post(s,cfg,request(cfg,ref='post-upgrade'))['seq']=='4'
        migrate(name,'0002_posting',downgrade=True,disposable=True)
        with connection(database=name) as c:
            assert c.execute("SELECT proconfig FROM pg_proc WHERE oid='lv.seed_manual_journal(uuid,jsonb)'::regprocedure").fetchone()==(['search_path=pg_catalog'],)
        migrate(name)
    finally:
        with connection(autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
