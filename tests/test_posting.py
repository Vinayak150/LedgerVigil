from copy import deepcopy
from decimal import Decimal
from uuid import uuid7
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from tools.dev_db import connection
from period_close_support import admin_token, close_period, close_call, held_close   # 0009: audited close replaces the R-02 UPDATE
from ledgervigil.db.tenant import tenant_transaction
from ledgervigil.ledger.seed import seed_manual_journal
from ledgervigil.ledger.replay import replay_events,canonical

ROLES=[('1000','BANK_OPERATING','ASSET',False,False),('1100','AR_CONTROL','ASSET',False,True),('1150','PROCESSOR_CLEARING','ASSET',False,False),('3000','OPENING_BALANCE_EQUITY','EQUITY',False,False),('4000','REVENUE_DEFAULT','REVENUE',False,False),('4900','SALES_RETURNS','REVENUE',True,False),('6100','PROCESSOR_FEE_EXPENSE','EXPENSE',False,False)]
HISTORY=('commit_batch','document','document_version','journal_entry','journal_line','ledger_event')
FINANCIAL=HISTORY+('document_current','account_balance_current','party_balance_current','ledger_counter')

def _provision(app,tenant,purpose='SANDBOX',currency='INR',exponent=2):
    with tenant_transaction(app,tenant):
        lid=app.execute("INSERT INTO lv.ledger(tenant_id,name,purpose,source_kind,currency,currency_exponent) VALUES(%s,'Synthetic posting tests',%s,'NATIVE',%s,%s) RETURNING id",(tenant,purpose,currency,exponent)).fetchone()[0]
        chart={}
        for code,role,kind,contra,party in ROLES:
            aid=app.execute("INSERT INTO lv.account(tenant_id,ledger_id,code,name,type,is_contra,requires_party,currency,currency_exponent) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",(tenant,lid,code,role,kind,contra,party,currency,exponent)).fetchone()[0]
            chart[role]=str(aid)
            app.execute('INSERT INTO lv.account_role_binding VALUES(%s,%s,%s,%s,%s,%s,%s,true)',(tenant,lid,role,aid,kind,contra,party))
        pid=app.execute("INSERT INTO lv.party(tenant_id,ledger_id,name) VALUES(%s,%s,'Synthetic customer') RETURNING id",(tenant,lid)).fetchone()[0]
        period=app.execute("INSERT INTO lv.fiscal_period(tenant_id,ledger_id,start_date,end_date) VALUES(%s,%s,'2026-10-01','2026-11-01') RETURNING id",(tenant,lid)).fetchone()[0]
    return {'tenant':tenant,'ledger':lid,'chart':chart,'party':str(pid),'period':period}

def provision(app,tenant,purpose='SANDBOX',currency='INR',exponent=2):
    # Fixture allocation uses the existing private migration/operator login.
    # The caller's lv_app login receives no configuration-write privilege.
    from configuration_support import configuration_connection
    with configuration_connection(app.info.dbname) as setup:
        return _provision(setup,tenant,purpose,currency,exponent)

def register(database,tenant,role='lv_seed'):
    with connection('lv_migrator',database) as c:
        c.execute('SET LOCAL ROLE lv_owner');c.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenant),))
        return c.execute("INSERT INTO lv.service_principal(tenant_id,name,purpose,database_role) VALUES(%s,'Synthetic developer','DEVELOPER_SEED',%s) RETURNING id",(tenant,role)).fetchone()[0]

@pytest.fixture
def fixture_ledger(app,tenants,database):
    config=provision(app,tenants[0]);config['actor']=register(database,tenants[0]);return config

@pytest.fixture
def seed(database):
    with connection('lv_seed',database,autocommit=True) as c: yield c

def line(config,role,side,amount,party=None):
    result={'account_id':config['chart'][role],'side':side,'amount':amount}
    if party is not None:result['party_id']=party
    return result

def request(config,ref='fixture-1',amount='10.00'):
    return {'source_ref':ref,'effective_date':'2026-10-14','description':'SYNTHETIC — primitive journal fixture','lines':[line(config,'BANK_OPERATING','D',amount),line(config,'PROCESSOR_CLEARING','C',amount)]}

def post(seed,cfg,body=None):return seed_manual_journal(seed,cfg['tenant'],cfg['ledger'],body or request(cfg))

def counts(app,cfg):
    with tenant_transaction(app,cfg['tenant']):
        values={table:app.execute(sql.SQL('SELECT count(*) FROM lv.{} WHERE ledger_id=%s').format(sql.Identifier(table)),(cfg['ledger'],)).fetchone()[0] for table in FINANCIAL}
        values['counter']=app.execute('SELECT last_seq,last_entry_no,batch_head,event_head FROM lv.ledger_counter WHERE ledger_id=%s',(cfg['ledger'],)).fetchall()
        return values

def events(app,cfg):
    with tenant_transaction(app,cfg['tenant']):
        rows=app.execute('SELECT seq,canonical_content,previous_hash,hash FROM lv.ledger_event WHERE ledger_id=%s ORDER BY seq',(cfg['ledger'],)).fetchall()
    return [dict(zip(('seq','canonical_content','previous_hash','hash'),r)) for r in rows]

def test_atomic_seed_and_replay(app,seed,fixture_ledger):
    cfg=fixture_ledger;r=post(seed,cfg)
    assert r['seq']=='1' and not r['replayed']
    snapshot=counts(app,cfg)
    for name in HISTORY:assert snapshot[name]==(2 if name=='journal_line' else 1)
    assert snapshot['document_current']==1 and snapshot['account_balance_current']==2
    result=replay_events(events(app,cfg));assert result['entries']==1 and result['head']==snapshot['counter'][0][3]
    with tenant_transaction(app,cfg['tenant']):
        stored=app.execute('SELECT account_id::text,party_id::text,debit_total,credit_total FROM lv.account_balance_current WHERE ledger_id=%s',(cfg['ledger'],)).fetchall()
        assert {(a,p):[d,c] for a,p,d,c in stored}==result['balances']
        assert app.execute('SELECT created_by,provenance::text FROM lv.journal_entry WHERE ledger_id=%s',(cfg['ledger'],)).fetchone()==(cfg['actor'],'SYNTHETIC')

@pytest.mark.parametrize('bad',['1.00001','1.001','1.000','0.00','-1.00','+1.00','NaN','Infinity','1e2',' 1.00','1.00 ','10000000000000000.00','',None,1,1.25,True])
def test_invalid_amounts_are_rejected_before_cast(app,seed,fixture_ledger,bad):
    cfg=fixture_ledger;before=counts(app,cfg)
    body=request(cfg);body['lines'][0]['amount']=bad
    with pytest.raises(psycopg.errors.CheckViolation):post(seed,cfg,body)
    assert counts(app,cfg)==before

@pytest.mark.parametrize('amount',['0.01','9999999999999999.99'])
def test_exact_amount_limits_round_trip(app,seed,fixture_ledger,amount):
    cfg=fixture_ledger;post(seed,cfg,request(cfg,amount=amount))
    with tenant_transaction(app,cfg['tenant']):
        assert app.execute('SELECT DISTINCT amount FROM lv.journal_line WHERE ledger_id=%s',(cfg['ledger'],)).fetchall()==[(Decimal(amount),)]

@pytest.mark.parametrize('code,exponent,amount,bad',[('JPY',0,'7','7.0'),('TND',3,'7.001','7.0001'),('CLF',4,'7.0001','7.00001')])
def test_registered_exponents_are_data_driven(database,app,seed,tenants,code,exponent,amount,bad):
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner');owner.execute('INSERT INTO lv.currency VALUES(%s,%s) ON CONFLICT DO NOTHING',(code,exponent))
    cfg=provision(app,tenants[0],currency=code,exponent=exponent);register(database,tenants[0])
    post(seed,cfg,request(cfg,amount=amount))
    with pytest.raises(psycopg.errors.CheckViolation):post(seed,cfg,request(cfg,ref='bad-scale',amount=bad))
    assert counts(app,cfg)['commit_batch']==1

@pytest.mark.parametrize('case',['unbalanced','one_line','bad_side','unknown_key','party_missing','party_elsewhere','account_elsewhere','bad_date','too_long_description'])
def test_invalid_journals_leave_no_partial_state(app,seed,fixture_ledger,case):
    cfg=fixture_ledger;before=counts(app,cfg);body=request(cfg)
    if case=='unbalanced':body['lines'][1]['amount']='9.99'
    elif case=='one_line':body['lines']=body['lines'][:1]
    elif case=='bad_side':body['lines'][0]['side']='X'
    elif case=='unknown_key':body['lines'][0]['currency']='USD'
    elif case=='party_missing':body['lines'][0]['account_id']=cfg['chart']['AR_CONTROL']
    elif case=='party_elsewhere':body['lines'][0]['party_id']=str(uuid7())
    elif case=='account_elsewhere':body['lines'][0]['account_id']=str(uuid7())
    elif case=='bad_date':body['effective_date']='2026-12-01'
    else:body['description']='x'*513
    with pytest.raises((psycopg.errors.CheckViolation,psycopg.errors.ForeignKeyViolation)):post(seed,cfg,body)
    assert counts(app,cfg)==before

def test_balance_is_checked_at_actual_commit(app,seed,fixture_ledger):
    cfg=fixture_ledger;body=request(cfg);body['lines'][1]['amount']='9.00'
    with pytest.raises(psycopg.errors.CheckViolation):
        with tenant_transaction(seed,cfg['tenant']):
            result=seed.execute('SELECT lv.seed_manual_journal(%s,%s)',(cfg['ledger'],Jsonb(body))).fetchone()[0]
            assert result['seq']=='1' # Function returned; deferred database constraint must still reject COMMIT.
    assert counts(app,cfg)['commit_batch']==0
    assert post(seed,cfg)['seq']=='1'

def test_client_rollback_leaves_no_number_gap(app,seed,fixture_ledger):
    cfg=fixture_ledger
    with pytest.raises(RuntimeError):
        with tenant_transaction(seed,cfg['tenant']):
            seed.execute('SELECT lv.seed_manual_journal(%s,%s)',(cfg['ledger'],Jsonb(request(cfg))))
            raise RuntimeError('client fails before commit')
    assert counts(app,cfg)['journal_entry']==0
    assert post(seed,cfg)['seq']=='1'

def test_duplicate_is_idempotent_and_changed_body_refused(app,seed,fixture_ledger):
    cfg=fixture_ledger;r=post(seed,cfg);before=counts(app,cfg);again=post(seed,cfg)
    assert again=={**r,'replayed':True} and counts(app,cfg)==before
    with pytest.raises(psycopg.errors.UniqueViolation):post(seed,cfg,request(cfg,amount='11.00'))
    assert counts(app,cfg)==before
    assert post(seed,cfg,request(cfg,ref='fixture-2'))['seq']=='2'

@pytest.mark.parametrize('mode',['BOOKS','PROVIDER_CONNECTED'])
def test_seed_refuses_non_native_sandbox(database,configuration,seed,tenants,mode):
    cfg=provision(configuration,tenants[0],purpose='BOOKS' if mode=='BOOKS' else 'SANDBOX');register(database,tenants[0])
    if mode=='PROVIDER_CONNECTED':
        with tenant_transaction(configuration,cfg['tenant']):configuration.execute("UPDATE lv.ledger SET source_kind='PROVIDER_CONNECTED' WHERE id=%s",(cfg['ledger'],))
    before=counts(configuration,cfg)
    with pytest.raises(psycopg.errors.CheckViolation):post(seed,cfg)
    assert counts(configuration,cfg)==before

def test_api_role_cannot_post_or_impersonate(seed,app,fixture_ledger):
    cfg=fixture_ledger
    with pytest.raises(psycopg.errors.InsufficientPrivilege):post(app,cfg)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):seed.execute('SET ROLE lv_owner')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):seed.execute('SET ROLE lv_app')
    for c in (seed,app):
        with tenant_transaction(c,cfg['tenant']):
            for table in FINANCIAL:
                for privilege in ('INSERT','UPDATE','DELETE','TRUNCATE'):
                    assert c.execute('SELECT has_table_privilege(current_user,%s,%s)',('lv.'+table,privilege)).fetchone()==(False,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):seed.execute('SELECT lv.seed_manual_journal(%s,%s)',(cfg['ledger'],Jsonb(request(cfg))))

def test_unregistered_or_revoked_principal_refused(database,app,seed,tenants):
    cfg=provision(app,tenants[0])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):post(seed,cfg)
    aid=register(database,tenants[0]);post(seed,cfg)
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner');owner.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenants[0]),));owner.execute('UPDATE lv.service_principal SET active=false WHERE id=%s',(aid,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):post(seed,cfg,request(cfg,ref='second'))
    assert counts(app,cfg)['commit_batch']==1

@pytest.mark.parametrize('table',HISTORY)
def test_history_immutable_even_to_owner(database,app,seed,fixture_ledger,table):
    cfg=fixture_ledger;post(seed,cfg)
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner');owner.execute("SELECT set_config('app.tenant_id',%s,true)",(str(cfg['tenant']),))
        for verb in ('UPDATE','DELETE'):
            with pytest.raises(psycopg.errors.CheckViolation):
                with owner.transaction():
                    command=sql.SQL('UPDATE lv.{} SET seq=seq WHERE ledger_id=%s' if verb=='UPDATE' else 'DELETE FROM lv.{} WHERE ledger_id=%s').format(sql.Identifier(table))
                    owner.execute(command,(cfg['ledger'],))

@pytest.mark.parametrize('table',FINANCIAL)
def test_posted_tables_do_not_leak_tenants(app,seed,fixture_ledger,tenants,table):
    cfg=fixture_ledger;post(seed,cfg)
    with tenant_transaction(app,tenants[1]):
        assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone()==(0,)
    assert app.execute(sql.SQL('SELECT count(*) FROM lv.{}').format(sql.Identifier(table))).fetchone()==(0,)

def test_document_references_are_exact_and_late_append_refused(database,app,seed,fixture_ledger):
    cfg=fixture_ledger;post(seed,cfg)
    with tenant_transaction(app,cfg['tenant']):
        assert app.execute('SELECT count(*) FROM lv.journal_entry e JOIN lv.document_version v ON (e.tenant_id,e.ledger_id,e.source_document_id,e.source_document_version,e.seq)=(v.tenant_id,v.ledger_id,v.document_id,v.version,v.seq) WHERE e.ledger_id=%s',(cfg['ledger'],)).fetchone()==(1,)
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner');owner.execute("SELECT set_config('app.tenant_id',%s,true)",(str(cfg['tenant']),))
        with pytest.raises(psycopg.errors.CheckViolation,match='earlier commit'):
            with owner.transaction():
                owner.execute("INSERT INTO lv.document(tenant_id,ledger_id,doc_type,source_ref,seq) VALUES(%s,%s,'MANUAL_JOURNAL','late',1)",(cfg['tenant'],cfg['ledger']))

def test_posted_interpretation_is_frozen(configuration,seed,fixture_ledger):
    cfg=fixture_ledger;post(seed,cfg)
    with tenant_transaction(configuration,cfg['tenant']):
        for query,params in [("UPDATE lv.ledger SET source_kind='PROVIDER_CONNECTED' WHERE id=%s",(cfg['ledger'],)),("UPDATE lv.account_role_binding SET account_id=%s WHERE ledger_id=%s AND role='BANK_OPERATING'",(cfg['chart']['PROCESSOR_CLEARING'],cfg['ledger']))]:
            with pytest.raises(psycopg.errors.CheckViolation):
                with configuration.transaction():configuration.execute(query,params)

def test_canonical_unicode_and_replay_tampering(app,seed,fixture_ledger):
    cfg=fixture_ledger;body=request(cfg);body['description']='Line\n"quoted" ₹ café 😀 \u2028 slash/';post(seed,cfg,body)
    rows=events(app,cfg);assert canonical(json.loads(rows[0]['canonical_content']))==rows[0]['canonical_content']
    assert replay_events(rows)['entries']==1
    altered=deepcopy(rows);altered[0]['canonical_content']=altered[0]['canonical_content'].replace('10.0000','11.0000')
    with pytest.raises(ValueError):replay_events(altered)
    post(seed,cfg,request(cfg,ref='second'))
    with pytest.raises(ValueError):replay_events(events(app,cfg)[1:])

def wait_for_block(database,blocked_pid,blocker_pid):
    with connection(database=database,autocommit=True) as inspector:
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if blocker_pid in inspector.execute('SELECT pg_blocking_pids(%s)',(blocked_pid,)).fetchone()[0]:return
            time.sleep(.02)
    raise AssertionError('Expected PostgreSQL lock dependency was not observed')

def test_symmetric_excess_scale_cannot_disappear(app,seed,fixture_ledger):
    cfg=fixture_ledger
    with pytest.raises(psycopg.errors.CheckViolation):post(seed,cfg,request(cfg,amount='1.00001'))
    assert counts(app,cfg)['commit_batch']==0

def test_posting_waits_for_period_close_then_refuses(database,app,fixture_ledger):
    cfg=fixture_ledger;ready=threading.Event();pid=[];token=admin_token(database,cfg['tenant'])
    def posting():
        with connection('lv_seed',database,autocommit=True) as c:
            pid.append(c.info.backend_pid);c.execute("SET lock_timeout='6s'");ready.set()
            with pytest.raises(psycopg.errors.CheckViolation):post(c,cfg)
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with held_close(database,token,cfg['period']) as (closer,result):
            assert result['status']==200
            future=pool.submit(posting);assert ready.wait(3)
            wait_for_block(database,pid[0],closer.info.backend_pid)
        assert future.result(timeout=7)
    assert counts(app,cfg)['commit_batch']==1   # only the audited PERIOD_CLOSE commit; the waiting posting wrote nothing

def test_close_waits_for_pending_posting_to_commit(database,seed,fixture_ledger):
    cfg=fixture_ledger;ready=threading.Event();pid=[];token=admin_token(database,cfg['tenant'])
    def closing():
        with connection('lv_app',database,autocommit=True) as c:
            pid.append(c.info.backend_pid)
            with c.transaction():
                c.execute("SET LOCAL lock_timeout='6s'");ready.set()
                assert close_call(c,token,cfg['period'])['status']==200
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_transaction(seed,cfg['tenant']):
            seed.execute('SELECT lv.seed_manual_journal(%s,%s)',(cfg['ledger'],Jsonb(request(cfg))))
            future=pool.submit(closing);assert ready.wait(3)
            wait_for_block(database,pid[0],seed.info.backend_pid)
        assert future.result(timeout=7)
    with connection('lv_app',database,autocommit=True) as reader:
        assert counts(reader,cfg)['commit_batch']==2   # the posting that held the lock, then the PERIOD_CLOSE commit
    with pytest.raises(psycopg.errors.CheckViolation):post(seed,cfg,request(cfg,ref='after-close'))

def test_concurrent_same_ledger_sequences_and_duplicate_replay(database,app,seed,fixture_ledger):
    cfg=fixture_ledger;ready=threading.Event();pid=[]
    def second():
        with connection('lv_seed',database,autocommit=True) as c:
            pid.append(c.info.backend_pid);c.execute("SET lock_timeout='6s'");ready.set()
            return post(c,cfg,request(cfg,ref='second'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_transaction(seed,cfg['tenant']):
            first=seed.execute('SELECT lv.seed_manual_journal(%s,%s)',(cfg['ledger'],Jsonb(request(cfg)))).fetchone()[0]
            future=pool.submit(second);assert ready.wait(3)
            wait_for_block(database,pid[0],seed.info.backend_pid)
        other=future.result(timeout=7)
    assert (first['seq'],other['seq'])==('1','2')
    assert counts(app,cfg)['counter'][0][:2]==(2,2)
    # A simultaneous duplicate source reference must converge to one effect.
    barrier=threading.Barrier(2)
    def duplicate():
        with connection('lv_seed',database,autocommit=True) as c:
            c.execute("SET lock_timeout='6s'");barrier.wait(timeout=3)
            return post(c,cfg,request(cfg,ref='concurrent-duplicate'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(duplicate);b=pool.submit(duplicate);results=[a.result(timeout=7),b.result(timeout=7)]
    assert {r['seq'] for r in results}=={'3'} and sum(r['replayed'] for r in results)==1
    assert len({r['entry_id'] for r in results})==1
    assert counts(app,cfg)['commit_batch']==3

def test_other_ledger_posts_while_first_transaction_is_pending(database,app,seed,fixture_ledger):
    cfg=fixture_ledger;other=provision(app,cfg['tenant'])
    with tenant_transaction(seed,cfg['tenant']):
        seed.execute('SELECT lv.seed_manual_journal(%s,%s)',(cfg['ledger'],Jsonb(request(cfg))))
        with connection('lv_seed',database,autocommit=True) as second:
            second.execute("SET lock_timeout='500ms'")
            assert post(second,other)['seq']=='1'
    assert counts(app,cfg)['commit_batch']==1 and counts(app,other)['commit_batch']==1

def test_missing_role_configuration_refused(database,configuration,seed,tenants):
    from conftest import ledger
    with tenant_transaction(configuration,tenants[0]):lid=ledger(configuration,tenants[0])
    register(database,tenants[0])
    cfg={'tenant':tenants[0],'ledger':lid,'chart':{'BANK_OPERATING':str(uuid7()),'PROCESSOR_CLEARING':str(uuid7())}}
    with pytest.raises(psycopg.errors.CheckViolation,match='account-role'):post(seed,cfg)
    assert counts(configuration,cfg)['commit_batch']==0

def test_three_manual_fixture_journals_reconstruct_expected_balances(app,seed,fixture_ledger):
    cfg=fixture_ledger
    legs=[
      [line(cfg,'AR_CONTROL','D','50000.00',cfg['party']),line(cfg,'REVENUE_DEFAULT','C','50000.00')],
      [line(cfg,'PROCESSOR_CLEARING','D','50000.00'),line(cfg,'AR_CONTROL','C','50000.00',cfg['party'])],
      [line(cfg,'BANK_OPERATING','D','49000.00'),line(cfg,'PROCESSOR_FEE_EXPENSE','D','1000.00'),line(cfg,'PROCESSOR_CLEARING','C','50000.00')],
    ]
    for index,lines in enumerate(legs,1):
        body=request(cfg,ref=f'synthetic-effects-{index}');body['lines']=lines;assert post(seed,cfg,body)['seq']==str(index)
    replay=replay_events(events(app,cfg));assert replay['entries']==3
    expected={'BANK_OPERATING':Decimal('49000'),'PROCESSOR_FEE_EXPENSE':Decimal('1000'),'REVENUE_DEFAULT':Decimal('-50000'),'PROCESSOR_CLEARING':Decimal('0'),'AR_CONTROL':Decimal('0')}
    with tenant_transaction(app,cfg['tenant']):
        for role,balance in expected.items():
            assert app.execute('SELECT sum(debit_total-credit_total) FROM lv.account_balance_current WHERE ledger_id=%s AND account_id=%s',(cfg['ledger'],cfg['chart'][role])).fetchone()==(balance,)
        assert app.execute('SELECT ar_balance FROM lv.party_balance_current WHERE ledger_id=%s AND party_id=%s',(cfg['ledger'],cfg['party'])).fetchone()==(Decimal(0),)
        rows=app.execute('SELECT account_id::text,party_id::text,debit_total,credit_total FROM lv.account_balance_current WHERE ledger_id=%s',(cfg['ledger'],)).fetchall()
        assert {(a,p):[d,c] for a,p,d,c in rows}==replay['balances']

def test_corrupt_projection_cannot_commit(database,app,seed,fixture_ledger):
    cfg=fixture_ledger
    # Disposable DB only: inject a broken internal projection write. Production
    # seed/API roles never receive this grant from migration 0002.
    with connection(database=database) as administrator:
        administrator.execute('GRANT UPDATE ON lv.account_balance_current TO lv_seed')
        # 0012 invalidates the account dependency counter on projection writes. This
        # disposable-only name-column grant lets this deliberate fault reach the
        # deferred projection check; production lv_seed receives neither grant.
        administrator.execute('GRANT UPDATE(name) ON lv.account TO lv_seed')
    try:
        with pytest.raises(psycopg.errors.CheckViolation,match='projection'):
            with tenant_transaction(seed,cfg['tenant']):
                seed.execute('SELECT lv.seed_manual_journal(%s,%s)',(cfg['ledger'],Jsonb(request(cfg))))
                seed.execute('UPDATE lv.account_balance_current SET debit_total=debit_total+1 WHERE ledger_id=%s',(cfg['ledger'],))
        assert counts(app,cfg)['commit_batch']==0
    finally:
        with connection(database=database) as administrator:
            administrator.execute('REVOKE UPDATE ON lv.account_balance_current FROM lv_seed')
            administrator.execute('REVOKE UPDATE(name) ON lv.account FROM lv_seed')

def test_inactive_unbound_account_refused(configuration,seed,fixture_ledger):
    cfg=fixture_ledger
    with tenant_transaction(configuration,cfg['tenant']):
        aid=configuration.execute("INSERT INTO lv.account(tenant_id,ledger_id,code,name,type,currency,currency_exponent,active) VALUES(%s,%s,'inactive','inactive','ASSET','INR',2,false) RETURNING id",(cfg['tenant'],cfg['ledger'])).fetchone()[0]
    body=request(cfg);body['lines'][0]['account_id']=str(aid)
    with pytest.raises(psycopg.errors.CheckViolation,match='unavailable'):post(seed,cfg,body)
    assert counts(configuration,cfg)['commit_batch']==0

def test_seed_trigger_independently_refuses_books(database,app,tenants):
    import hashlib
    cfg=provision(app,tenants[0],purpose='BOOKS');actor=register(database,tenants[0],role='lv_migrator')
    previous='0'*64;body='{}';digest=hashlib.sha256((body+previous).encode()).hexdigest()
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner');owner.execute("SELECT set_config('app.tenant_id',%s,true)",(str(cfg['tenant']),))
        with pytest.raises(psycopg.errors.CheckViolation,match='native SANDBOX'):
            with owner.transaction():
                owner.execute("INSERT INTO lv.commit_batch(tenant_id,ledger_id,seq,kind,created_by,previous_hash,canonical_content,hash) VALUES(%s,%s,1,'SEED',%s,%s,%s,%s)",(cfg['tenant'],cfg['ledger'],actor,previous,body,digest))

def test_cross_tenant_and_cross_ledger_accounts_are_refused(database,app,seed,fixture_ledger,tenants):
    cfg=fixture_ledger;other=provision(app,tenants[1]);register(database,tenants[1])
    with pytest.raises(psycopg.errors.CheckViolation):seed_manual_journal(seed,tenants[1],cfg['ledger'],request(cfg))
    for foreign in (other,provision(app,cfg['tenant'])):
        body=request(cfg);body['lines'][0]['account_id']=foreign['chart']['BANK_OPERATING']
        with pytest.raises(psycopg.errors.CheckViolation):post(seed,cfg,body)
        body=request(cfg);body['lines'][0]['party_id']=foreign['party']
        with pytest.raises(psycopg.errors.ForeignKeyViolation):post(seed,cfg,body)
    assert counts(app,cfg)['commit_batch']==0
