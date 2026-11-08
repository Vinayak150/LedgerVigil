"""Infrastructure probes; no claim that the financial schema is implemented."""
import os
from pathlib import Path
from uuid import uuid4
import psycopg
import pytest
from fastapi.testclient import TestClient
from ledgervigil.app import app
from tools.dev_db import DEV_PORT

ROOT = Path(__file__).resolve().parents[1]

def config():
    return dict(line.split("=",1) for line in (ROOT/".env").read_text().splitlines() if line and not line.startswith("#"))

@pytest.fixture
def conn():
    c=config()
    connection=psycopg.connect(host="127.0.0.1",port=DEV_PORT,dbname="ledgervigil_dev",user="postgres",password=c["POSTGRES_PASSWORD"])
    try:
        with connection.transaction():
            yield connection
            raise psycopg.Rollback()
    finally:
        connection.close()

def test_api_health():
    response=TestClient(app).get("/health")
    assert response.status_code==200
    assert response.json()["milestone"]=="development-foundation"

def test_actual_postgresql(conn):
    version=int(conn.execute("SHOW server_version_num").fetchone()[0])
    assert 180000 <= version < 190000

def test_numeric_precast_rounding_hazard(conn):
    # This proves why a later financial migration needs validation before typmod coercion.
    row=conn.execute("SELECT '1.00001'::numeric(20,4), '1.00001'::numeric").fetchone()
    assert str(row[0])=='1.0000' and str(row[1])=='1.00001'

def test_rollback_is_real(conn):
    conn.execute("CREATE TEMP TABLE rollback_probe(value integer)")
    with conn.transaction():
        conn.execute("INSERT INTO rollback_probe VALUES(1)")
        raise psycopg.Rollback()
    assert conn.execute("SELECT count(*) FROM rollback_probe").fetchone()[0]==0

def test_force_rls_as_non_owner_role(conn):
    # Transactional scratch objects are rolled back; this is not the product migration.
    role='lv_probe_'+uuid4().hex
    schema='lv_probe_'+uuid4().hex
    from psycopg import sql
    conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    table=sql.Identifier(schema,'records')
    conn.execute(sql.SQL('CREATE TABLE {}(tenant_id uuid NOT NULL, value text)').format(table))
    conn.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(table))
    conn.execute(sql.SQL('ALTER TABLE {} FORCE ROW LEVEL SECURITY').format(table))
    conn.execute(sql.SQL("CREATE POLICY scope ON {} USING (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid)").format(table))
    conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    conn.execute(sql.SQL('GRANT SELECT,INSERT ON {} TO {}').format(table,sql.Identifier(role)))
    a,b=uuid4(),uuid4()
    conn.execute(sql.SQL('INSERT INTO {} VALUES(%s,%s),(%s,%s)').format(table),(a,'a',b,'b'))
    conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    assert conn.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()==(False,False)
    assert conn.execute(sql.SQL('SELECT count(*) FROM {}').format(table)).fetchone()[0]==0
    conn.execute("SELECT set_config('app.tenant_id',%s,true)",(str(a),))
    assert conn.execute(sql.SQL('SELECT value FROM {}').format(table)).fetchall()==[('a',)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute(sql.SQL('INSERT INTO {} VALUES(%s,%s)').format(table),(b,'forbidden'))
