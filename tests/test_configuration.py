"""Failure-oriented tests against the actual migration with actual lv_app login."""
from uuid import uuid7
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import psycopg
from psycopg import sql
import pytest
from tools.dev_db import connection,migrate
from ledgervigil.db.tenant import tenant_transaction
from conftest import ledger,account,binding,period
from configuration_support import configuration_connection

TABLES=('tenant','ledger','account','account_role_binding','party','fiscal_period')

def reject(c,exception,query,params=()):
    with pytest.raises(exception):
        with c.transaction():
            c.execute(query,params)

def test_role_and_schema_authority(app,database):
    assert app.execute('SELECT current_user,session_user').fetchone()==('lv_app','lv_app')
    assert app.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()==(False,False,False,False)
    reject(app,psycopg.errors.InsufficientPrivilege,'SET ROLE lv_owner')
    reject(app,psycopg.errors.InsufficientPrivilege,'CREATE TABLE public.forbidden(id int)')
    reject(app,psycopg.errors.InsufficientPrivilege,'CREATE TABLE lv.forbidden(id int)')
    reject(app,psycopg.errors.InsufficientPrivilege,'ALTER TABLE lv.ledger DISABLE ROW LEVEL SECURITY')
    reject(app,psycopg.errors.InsufficientPrivilege,'SELECT * FROM lv_meta.alembic_version')
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner')
        assert owner.execute('SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()==('lv_owner',False,False)
        assert owner.execute('SELECT version_num FROM lv_meta.alembic_version').fetchone()==('0040_mfa_reset',)
        rows=owner.execute("SELECT relname,relrowsecurity,relforcerowsecurity,pg_get_userbyid(relowner) FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname='lv' AND relkind='r'").fetchall()
        assert len(rows)==72   # 0037 adds database_binding and database_binding_history (restore interlock); 0035 adds six provider_* tables (connection, oauth_state, credential, job, page, object); 0010 adds proposal, proposal_revision, proposal_reference; 0011 adds simulation_run; 0032 adds five detection_* tables (owner-held, FORCE RLS)
        assert all(o=='lv_owner' for _,_,_,o in rows)
        assert all(enabled and forced for name,enabled,forced,_ in rows if name!='currency')
        assert owner.execute('SELECT count(*) FROM lv.tenant').fetchone()[0]==0

@pytest.mark.parametrize('table',TABLES)
def test_missing_context_fails_closed(app,populated,table):
    assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone()[0]==0

@pytest.mark.parametrize('table',TABLES)
def test_application_cannot_delete_or_truncate(app,tenants,table):
    with tenant_transaction(app,tenants[0]):
        reject(app,psycopg.errors.InsufficientPrivilege,sql.SQL('DELETE FROM lv.{}').format(sql.Identifier(table)))
        reject(app,psycopg.errors.InsufficientPrivilege,sql.SQL('TRUNCATE lv.{}').format(sql.Identifier(table)))

def test_context_scopes_rows_and_resets_after_commit_and_failure(app,configuration,tenants):
    a,b=tenants
    with tenant_transaction(app,a):
        with tenant_transaction(configuration,a):la=ledger(configuration,a)
    with tenant_transaction(app,b):
        with tenant_transaction(configuration,b):lb=ledger(configuration,b)
        assert app.execute('SELECT id FROM lv.ledger').fetchall()==[(lb,)]
        reject(app,psycopg.errors.InsufficientPrivilege,"UPDATE lv.ledger SET name='forbidden' WHERE id=%s RETURNING id",(la,))
        reject(app,psycopg.errors.InsufficientPrivilege,"INSERT INTO lv.ledger(tenant_id,name,purpose,source_kind,currency,currency_exponent) VALUES(%s,'forbidden','SANDBOX','NATIVE','INR',2)",(a,))
    assert app.execute("SELECT nullif(current_setting('app.tenant_id',true),'')").fetchone()==(None,)
    assert app.execute('SELECT count(*) FROM lv.ledger').fetchone()==(0,)
    with pytest.raises(RuntimeError,match='synthetic failure'):
        with tenant_transaction(app,a):
            app.execute('SELECT 1')
            raise RuntimeError('synthetic failure')
    assert app.execute('SELECT count(*) FROM lv.ledger').fetchone()==(0,)
    with tenant_transaction(app,a):
        assert app.execute('SELECT id FROM lv.ledger').fetchall()==[(la,)]

def test_context_cannot_switch_inside_transaction(app,tenants):
    with tenant_transaction(app,tenants[0]):
        with pytest.raises(RuntimeError,match='fresh transaction'):
            with tenant_transaction(app,tenants[1]): pass
    with pytest.raises(TypeError):
        with tenant_transaction(app,str(tenants[0])): pass
    with app.transaction():
        app.execute("SELECT set_config('app.tenant_id','invalid',true)")
        reject(app,psycopg.errors.InvalidTextRepresentation,'SELECT * FROM lv.tenant')

def test_missing_context_write_rejected(app,tenants):
    with pytest.raises(psycopg.errors.InsufficientPrivilege): ledger(app,tenants[0])

def test_owner_is_subject_to_force_rls(database,tenants):
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner')
        assert owner.execute('SELECT count(*) FROM lv.tenant').fetchone()==(0,)
        owner.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenants[0]),))
        assert owner.execute('SELECT tenant_id FROM lv.tenant').fetchall()==[(tenants[0],)]
        reject(owner,psycopg.errors.InsufficientPrivilege,'INSERT INTO lv.tenant(tenant_id,name) VALUES(%s,\'forbidden\')',(tenants[1],))

def test_global_currency_read_only(app):
    assert app.execute("SELECT code,exponent FROM lv.currency WHERE code IN ('INR','USD','GBP') ORDER BY code").fetchall()==[('GBP',2),('INR',2),('USD',2)]
    for query in ["INSERT INTO lv.currency VALUES('XXX',4)","UPDATE lv.currency SET exponent=3 WHERE code='INR'","DELETE FROM lv.currency WHERE code='INR'"]:
        reject(app,psycopg.errors.InsufficientPrivilege,query)

@pytest.mark.parametrize('purpose',['BOOKS','SANDBOX'])
def test_ledger_purpose_immutable_and_uuid7_default(configuration,tenants,purpose):
    with tenant_transaction(configuration,tenants[0]):
        lid=ledger(configuration,tenants[0],purpose)
        assert lid.version==7
        reject(configuration,psycopg.errors.CheckViolation,"UPDATE lv.ledger SET purpose=%s WHERE id=%s",('BOOKS' if purpose=='SANDBOX' else 'SANDBOX',lid))
        configuration.execute("UPDATE lv.ledger SET name='Renamed' WHERE id=%s",(lid,))

def test_registry_exponent_cannot_be_forged(configuration,tenants):
    with tenant_transaction(configuration,tenants[0]):
        reject(configuration,psycopg.errors.ForeignKeyViolation,"INSERT INTO lv.ledger(tenant_id,name,purpose,source_kind,currency,currency_exponent) VALUES(%s,'x','SANDBOX','NATIVE','INR',4)",(tenants[0],))

def test_all_configuration_foreign_keys_respect_scope(configuration,tenants):
    a,b=tenants
    with tenant_transaction(configuration,b): lb=ledger(configuration,b)
    with tenant_transaction(configuration,a):
        la=ledger(configuration,a); la2=ledger(configuration,a)
        aa=account(configuration,a,la)
        for tbl,columns,values in [('party','name',"'x'"),('fiscal_period','start_date,end_date',"'2026-11-01','2026-12-01'")]:
            reject(configuration,psycopg.errors.ForeignKeyViolation,f'INSERT INTO lv.{tbl}(tenant_id,ledger_id,{columns}) VALUES(%s,%s,{values})',(a,lb))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with configuration.transaction(): account(configuration,a,lb)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with configuration.transaction(): account(configuration,a,la2,parent=aa)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with configuration.transaction(): binding(configuration,a,la2,aa)
        reject(configuration,psycopg.errors.ForeignKeyViolation,"UPDATE lv.account SET currency='USD' WHERE id=%s",(aa,))
        reject(configuration,psycopg.errors.CheckViolation,'UPDATE lv.account SET ledger_id=%s WHERE id=%s',(la2,aa))
        reject(configuration,psycopg.errors.CheckViolation,'UPDATE lv.ledger SET tenant_id=%s WHERE id=%s',(b,la))

@pytest.mark.parametrize('kind,contra,side',[(k,c, ('D' if (k in ('ASSET','EXPENSE')) != c else 'C')) for k in ['ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE'] for c in (False,True)])
def test_normal_side_is_derived(configuration,tenants,kind,contra,side):
    with tenant_transaction(configuration,tenants[0]):
        lid=ledger(configuration,tenants[0]);aid=account(configuration,tenants[0],lid,kind=kind,contra=contra)
        assert configuration.execute('SELECT normal_side FROM lv.account WHERE id=%s',(aid,)).fetchone()==(side,)
        reject(configuration,psycopg.errors.GeneratedAlways,'UPDATE lv.account SET normal_side=\'C\' WHERE id=%s',(aid,))

@pytest.mark.parametrize('role,kind,contra,party',[('AR_CONTROL','ASSET',False,True),('REVENUE_DEFAULT','REVENUE',False,False),('SALES_RETURNS','REVENUE',True,False),('PROCESSOR_CLEARING','ASSET',False,False),('BANK_OPERATING','ASSET',False,False),('PROCESSOR_FEE_EXPENSE','EXPENSE',False,False),('OPENING_BALANCE_EQUITY','EQUITY',False,False)])
def test_role_binding_enforces_account_properties_and_stays_valid(configuration,tenants,role,kind,contra,party):
    with tenant_transaction(configuration,tenants[0]):
        lid=ledger(configuration,tenants[0]);aid=account(configuration,tenants[0],lid,kind=kind,contra=contra,party=party)
        binding(configuration,tenants[0],lid,aid,role,kind,contra,party)
        reject(configuration,psycopg.errors.ForeignKeyViolation,'UPDATE lv.account SET active=false WHERE id=%s',(aid,))
        reject(configuration,psycopg.errors.ForeignKeyViolation,'UPDATE lv.account SET is_contra=NOT is_contra WHERE id=%s',(aid,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            with configuration.transaction(): binding(configuration,tenants[0],lid,aid,role,kind,contra,party)

@pytest.mark.parametrize('case',['wrong_type','inactive','ar_without_party','forged_copy'])
def test_invalid_role_binding_rejected(configuration,tenants,case):
    with tenant_transaction(configuration,tenants[0]):
        lid=ledger(configuration,tenants[0])
        kind='EXPENSE' if case=='wrong_type' else 'ASSET'
        active=case!='inactive'
        aid=account(configuration,tenants[0],lid,kind=kind,active=active)
        error=psycopg.errors.ForeignKeyViolation if case=='forged_copy' else psycopg.errors.CheckViolation
        with pytest.raises(error):
            with configuration.transaction():
                binding(configuration,tenants[0],lid,aid,role='AR_CONTROL' if case=='ar_without_party' else ('PROCESSOR_FEE_EXPENSE' if case=='forged_copy' else 'BANK_OPERATING'),kind='EXPENSE' if case in ('wrong_type','forged_copy') else 'ASSET',active=active)

def test_uniqueness_is_scoped_to_ledger(configuration,tenants):
    with tenant_transaction(configuration,tenants[0]):
        one,two=ledger(configuration,tenants[0]),ledger(configuration,tenants[0])
        account(configuration,tenants[0],one);account(configuration,tenants[0],two)
        with pytest.raises(psycopg.errors.UniqueViolation):
            with configuration.transaction(): account(configuration,tenants[0],one)
        for lid in (one,two):
            configuration.execute("INSERT INTO lv.party(tenant_id,ledger_id,name,external_ref) VALUES(%s,%s,'Customer','source-1')",(tenants[0],lid))
        reject(configuration,psycopg.errors.UniqueViolation,"INSERT INTO lv.party(tenant_id,ledger_id,name,external_ref) VALUES(%s,%s,'Customer','source-1')",(tenants[0],one))

@pytest.mark.parametrize('start,end',[('2026-11-02','2026-12-01'),('2026-11-01','2026-11-30'),('2026-11-01','2027-01-01'),('-infinity','2026-12-01'),('2026-11-01','infinity')])
def test_period_must_be_finite_calendar_month(configuration,tenants,start,end):
    with tenant_transaction(configuration,tenants[0]):
        lid=ledger(configuration,tenants[0])
        with pytest.raises(psycopg.errors.CheckViolation):
            with configuration.transaction(): period(configuration,tenants[0],lid,start,end)

def test_period_adjacency_overlap_and_no_reopen(database,configuration,app,tenants):
    from period_close_support import admin_token,close_period
    with tenant_transaction(configuration,tenants[0]):
        lid=ledger(configuration,tenants[0]);pid=period(configuration,tenants[0],lid)
        period(configuration,tenants[0],lid,'2026-12-01','2027-01-01')
        with pytest.raises(psycopg.errors.ExclusionViolation):
            with configuration.transaction(): period(configuration,tenants[0],lid)
        # 0009 (R-02): the runtime has no UPDATE on periods at all, and cannot create one already CLOSED.
        reject(app,psycopg.errors.InsufficientPrivilege,"UPDATE lv.fiscal_period SET start_date='2026-10-01',end_date='2026-11-01' WHERE id=%s",(pid,))
        reject(app,psycopg.errors.InsufficientPrivilege,"UPDATE lv.fiscal_period SET status='CLOSED' WHERE id=%s",(pid,))
        with pytest.raises(psycopg.errors.CheckViolation):
            with configuration.transaction(): period(configuration,tenants[0],lid,'2027-01-01','2027-02-01','CLOSED')
    assert close_period(database,admin_token(database,tenants[0]),pid)['status']==200
    with tenant_transaction(configuration,tenants[0]):
        with pytest.raises(psycopg.errors.CheckViolation):   # nothing may be created before a closed period
            with configuration.transaction(): period(configuration,tenants[0],lid,'2026-10-01','2026-11-01')
    with connection('lv_migrator',database) as owner:   # even the schema owner cannot change dates or reopen
        owner.execute('SET LOCAL ROLE lv_owner');owner.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenants[0]),))
        for statement in ("UPDATE lv.fiscal_period SET status='OPEN' WHERE id=%s","UPDATE lv.fiscal_period SET start_date='2026-10-01',end_date='2026-11-01' WHERE id=%s",
                          "UPDATE lv.fiscal_period SET closed_by=NULL WHERE id=%s"):
            with pytest.raises(psycopg.errors.CheckViolation):
                with owner.transaction(): owner.execute(statement,(pid,))

def test_concurrent_period_conflict_serializes_in_database(database,configuration,tenants):
    tenant=tenants[0]
    with tenant_transaction(configuration,tenant): lid=ledger(configuration,tenant)
    ready=threading.Event();worker_pid=[]
    def competing_insert():
        with configuration_connection(database) as c:
            worker_pid.append(c.info.backend_pid)
            with pytest.raises(psycopg.errors.ExclusionViolation):
                with tenant_transaction(c,tenant):
                    c.execute("SET LOCAL lock_timeout='5s'")
                    ready.set();period(c,tenant,lid)
        return 'conflict rejected'
    with ThreadPoolExecutor(max_workers=1) as executor:
        with tenant_transaction(configuration,tenant):
            period(configuration,tenant,lid)
            future=executor.submit(competing_insert)
            assert ready.wait(3)
            with connection(database=database,autocommit=True) as inspector:
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    blockers=inspector.execute('SELECT pg_blocking_pids(%s)',(worker_pid[0],)).fetchone()[0]
                    if configuration.info.backend_pid in blockers: break
                    time.sleep(.02)
                else: raise AssertionError('Competing insert never blocked on the first transaction')
        assert future.result(timeout=6)=='conflict rejected'

def test_other_ledger_does_not_wait_on_period_lock(database,configuration,tenants):
    tenant=tenants[0]
    with tenant_transaction(configuration,tenant): one,two=ledger(configuration,tenant),ledger(configuration,tenant)
    with tenant_transaction(configuration,tenant):
        period(configuration,tenant,one)
        with configuration_connection(database) as second:
            with tenant_transaction(second,tenant):
                second.execute("SET LOCAL lock_timeout='500ms'")
                period(second,tenant,two)

def test_no_api_financial_write_surface(app):
    for table in ('journal_entry','journal_line','document_version','commit_batch','ledger_counter','ledger_event','account_balance_current'):
        assert app.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE')",('lv.'+table,)).fetchone()==(False,)
    assert app.execute("SELECT has_function_privilege(current_user,'lv.seed_manual_journal(uuid,jsonb)','EXECUTE')").fetchone()==(False,)


@pytest.mark.parametrize('table',TABLES)
def test_all_scoped_tables_filter_populated_other_tenants(app,populated,table):
    for tenant in populated:
        with tenant_transaction(app,tenant):
            rows=app.execute(sql.SQL('SELECT DISTINCT tenant_id FROM lv.{}').format(sql.Identifier(table))).fetchall()
            assert rows==[(tenant,)]
