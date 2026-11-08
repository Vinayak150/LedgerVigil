"""Real migrated PostgreSQL test databases, always separate from development data."""
from pathlib import Path
import sys
from uuid import uuid4,uuid7
import pytest
from psycopg import sql
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.dev_db import connection,prepare_database,migrate
from ledgervigil.db.tenant import tenant_transaction

@pytest.fixture(scope='session')
def database():
    name='lv_migration_test_'+uuid4().hex
    with connection(autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        prepare_database(name)
        migrate(name)
        # Deliberate faults are confined to this disposable test DB, never dev.
        import os
        mutation=os.environ.get('LV_GUARD_MUTATION')
        if mutation:
            faults={
                'posting_balance': 'DROP TRIGGER entry_balanced ON lv.journal_entry; DROP TRIGGER lines_balanced ON lv.journal_line',
                'posting_precision': "CREATE OR REPLACE FUNCTION lv.parse_line_amount(value text, exponent smallint) RETURNS numeric LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS 'SELECT value::numeric'",
                'posting_immutable': 'DROP TRIGGER immutable ON lv.journal_line',
                'posting_projection': 'DROP TRIGGER seed_commit_complete ON lv.commit_batch',
                'owner_rls': 'ALTER TABLE lv.tenant NO FORCE ROW LEVEL SECURITY',
                'purpose': 'DROP TRIGGER ledger_purpose_immutable ON lv.ledger',
                'period_overlap': 'ALTER TABLE lv.fiscal_period DROP CONSTRAINT no_overlapping_periods',
                'definer_search_path': 'ALTER FUNCTION lv.seed_manual_journal(uuid,jsonb) SET search_path = pg_catalog',
            }
            with connection(database=name) as admin:
                if mutation=='binding_fk':
                    names=admin.execute("SELECT conname FROM pg_constraint WHERE conrelid='lv.account_role_binding'::regclass AND contype='f'").fetchall()
                    assert len(names)==1
                    admin.execute(sql.SQL('ALTER TABLE lv.account_role_binding DROP CONSTRAINT {}').format(sql.Identifier(names[0][0])))
                else:
                    admin.execute(faults[mutation])
        yield name
    finally:
        with connection(autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

@pytest.fixture
def tenants(database):
    a,b=uuid7(),uuid7()
    for tenant in (a,b):
        with connection('lv_migrator',database) as c:
            c.execute('SET LOCAL ROLE lv_owner')
            c.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenant),))
            c.execute('INSERT INTO lv.tenant(tenant_id,name) VALUES(%s,%s)',(tenant,'Synthetic test tenant'))
    return a,b

@pytest.fixture
def app(database):
    with connection('lv_app',database,autocommit=True) as c:
        yield c


def ledger(c,tenant,purpose='SANDBOX',currency='INR'):
    return c.execute("INSERT INTO lv.ledger(tenant_id,name,purpose,source_kind,currency,currency_exponent) VALUES(%s,'Synthetic ledger',%s,'NATIVE',%s,2) RETURNING id",(tenant,purpose,currency)).fetchone()[0]

def account(c,tenant,lid,code='1000',kind='ASSET',contra=False,party=False,active=True,parent=None):
    return c.execute('INSERT INTO lv.account(tenant_id,ledger_id,code,name,type,is_contra,requires_party,currency,currency_exponent,active,parent_id) VALUES(%s,%s,%s,%s,%s,%s,%s,\'INR\',2,%s,%s) RETURNING id',(tenant,lid,code,'Synthetic account',kind,contra,party,active,parent)).fetchone()[0]

def binding(c,tenant,lid,aid,role='BANK_OPERATING',kind='ASSET',contra=False,party=False,active=True):
    c.execute('INSERT INTO lv.account_role_binding(tenant_id,ledger_id,role,account_id,account_type,account_is_contra,account_requires_party,account_active) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(tenant,lid,role,aid,kind,contra,party,active))

def period(c,tenant,lid,start='2026-11-01',end='2026-12-01',status='OPEN'):
    return c.execute('INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date,status) VALUES(%s,%s,%s,%s,%s) RETURNING id',(tenant,lid,start,end,status)).fetchone()[0]

@pytest.fixture
def configuration(database):
    from configuration_support import configuration_connection
    with configuration_connection(database) as c:yield c

@pytest.fixture
def populated(configuration,tenants):
    app=configuration  # Explicit private operator fixture; runtime reads still use lv_app.

    for tenant in tenants:
        with tenant_transaction(app,tenant):
            lid=ledger(app,tenant)
            aid=account(app,tenant,lid)
            binding(app,tenant,lid,aid)
            app.execute("INSERT INTO lv.party(tenant_id,ledger_id,name) VALUES(%s,%s,'Synthetic customer')",(tenant,lid))
            period(app,tenant,lid)
    return tenants

@pytest.fixture
def migration_database():
    """Fresh DB for exact function round trips; retained semantic evidence forbids downgrade."""
    yield from database.__wrapped__()
