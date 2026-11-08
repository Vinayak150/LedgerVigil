"""Independent native-gate regressions: Q-10 and the saved Any→204 logout contract."""
from uuid import UUID
import psycopg
import pytest
from tools.dev_db import connection,migrate
from test_identity_sessions import app,ids,client_for,signed_in

TABLES=('role_permission','app_user','membership','membership_role','session','login_attempt','audit_event','audit_chain_head')

def test_identity_force_rls_and_owner_only_policies(database,ids):
    with connection(database=database) as administrator:
        for table in TABLES:
            assert administrator.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",('lv_identity.'+table,)).fetchone()==(True,True)
            row=administrator.execute("SELECT pg_get_expr(polqual,polrelid),pg_get_expr(polwithcheck,polrelid),polroles FROM pg_policy WHERE polrelid=%s::regclass",('lv_identity.'+table,)).fetchall()
            owner=administrator.execute("SELECT oid FROM pg_roles WHERE rolname='lv_owner'").fetchone()[0]
            assert row==[('true','true',[owner])]

@pytest.mark.parametrize('role',['lv_app','lv_seed','lv_ingest'])
def test_accidental_read_grants_still_default_deny(database,ids,role):
    # All grants and their probes roll back in one administrator transaction.
    from psycopg import sql
    with connection(database=database) as administrator:
        administrator.execute(sql.SQL('GRANT USAGE ON SCHEMA lv_identity TO {}').format(sql.Identifier(role)))
        administrator.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA lv_identity TO {}').format(sql.Identifier(role)))
        administrator.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        for table in TABLES:
            assert administrator.execute(sql.SQL('SELECT count(*) FROM lv_identity.{}').format(sql.Identifier(table))).fetchone()==(0,)
        administrator.rollback()

def test_owner_identity_policy_is_required_for_pretenant_resolution(database,ids):
    with connection(database=database) as administrator:
        administrator.execute('ALTER POLICY identity_owner_access ON lv_identity.membership USING (false) WITH CHECK (false)')
        administrator.execute('SET LOCAL ROLE lv_owner')
        assert administrator.execute('SELECT count(*) FROM lv_identity.membership').fetchone()==(0,)
        administrator.rollback()
    # The owner policy is intentionally privileged; application checks enforce
    # user membership. This is not a claim that owner reads are tenant-filtered.
    with connection('lv_migrator',database) as owner:
        owner.execute('SET LOCAL ROLE lv_owner')
        assert owner.execute('SELECT count(*) FROM lv_identity.membership').fetchone()[0]>0

def test_logout_without_cookie_is_idempotent_204(app):
    client=client_for(app)
    assert client.post('/api/v1/auth/logout').status_code==204
    assert client.post('/api/v1/auth/logout').status_code==204
    assert client.post('/api/v1/auth/logout',headers={'Origin':'https://evil.example'}).status_code==403

def test_logout_cookie_still_requires_its_csrf_token(app,ids):
    client,body=signed_in(app,ids,'analyst')
    assert client.post('/api/v1/auth/logout').status_code==403
    assert client.get('/api/v1/me').status_code==200
    assert client.post('/api/v1/auth/logout',headers={'X-CSRF-Token':body['csrf_token']}).status_code==204
    assert client.get('/api/v1/me').status_code==401

def test_q10_upgrade_preserves_populated_identity_and_financial_data():
    import secrets
    from test_processor_payments import disposable
    from tools.identity_admin import owner_transaction,create_tenant,create_user,grant
    with disposable('0007_identity_sessions') as name:
        with owner_transaction(name) as owner:
            tenant=create_tenant(owner,'Synthetic RLS upgrade tenant')
            user=create_user(owner,'rls-native@test.ledgervigil.invalid','Synthetic native',secrets.token_urlsafe(18))
            grant(owner,tenant,user,['ANALYST'])
        def snapshot():
            with connection(database=name) as administrator:
                return {table:administrator.execute('SELECT md5(coalesce(string_agg(row_to_json(t)::text,\'\' ORDER BY row_to_json(t)::text),\'\')) FROM lv_identity.'+table+' t').fetchone()[0] for table in TABLES}
        before=snapshot();migrate(name);assert snapshot()==before
        with connection(database=name) as administrator:
            assert administrator.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='lv_identity' AND c.relkind='r' AND relforcerowsecurity").fetchone()==(8,)
