"""Real Redis and independent Celery processes. Disposable broker and migrated PostgreSQL only."""
import base64
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4,UUID
import pytest
import redis
from psycopg.conninfo import make_conninfo
from tools.dev_db import env_file,connection
from ledgervigil.maintenance import runtime
from test_period_staleness import world,seed,api,setup_job,worker,age  # noqa:F401

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='session')
def broker():
    name='lv-staleness-test-'+uuid4().hex[:10];docker='/opt/homebrew/bin/docker'
    subprocess.run([docker,'run','-d','--rm','--name',name,'-p','127.0.0.1::6379','redis:7.4-alpine','redis-server','--save','','--appendonly','no'],check=True,capture_output=True)
    try:
        port=int(subprocess.check_output([docker,'port',name,'6379/tcp'],text=True).strip().rsplit(':',1)[1]);url=f'redis://127.0.0.1:{port}/0'
        client=redis.Redis.from_url(url)
        wait(lambda:client.ping())
        yield url,client
    finally:subprocess.run([docker,'stop',name],check=True,capture_output=True)

def wait(check,timeout=15):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            if check():return
        except (redis.ConnectionError,redis.TimeoutError):pass
        time.sleep(.05)
    raise AssertionError('Condition not reached within bounded wait')

def state(db,j):
    with connection(database=db) as c:return c.execute('SELECT state FROM lv.work_item WHERE id=%s',(j,)).fetchone()[0]

@contextlib.contextmanager
def celery_worker(db,url,tmp_path,*,kill_after_claim=False):
    env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src')+os.pathsep+str(ROOT)
    env['LV_WORKER_DATABASE_URL']=make_conninfo(host='127.0.0.1',port=55432,dbname=db,user='lv_worker',password=env_file('.env.roles')['WORKER_PASSWORD'])
    env['LV_MAINTENANCE_BROKER_URL']=url
    code="from ledgervigil.maintenance.celery_app import application; application.worker_main(['worker','-P','solo','-c','1','-Q','maintenance','--loglevel=WARNING','--without-gossip','--without-mingle','--without-heartbeat'])"
    if kill_after_claim:
        marker=tmp_path/'durable-claim'
        code="import os,signal; from pathlib import Path; from ledgervigil.maintenance import runtime; runtime._after_claim=lambda _: (Path("+repr(str(marker))+").write_text('claimed'), os.kill(os.getpid(),signal.SIGKILL)); "+code
    with (tmp_path/'celery.log').open('a') as log:
        p=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:yield p
        finally:
            if p.poll() is None:p.terminate()
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)

def publisher(url):
    from ledgervigil.maintenance.celery_app import application
    application.conf.broker_url=url
    return lambda t,j:application.send_task('ledgervigil.staleness_scan',args=[t,j],queue='maintenance',retry=False)


def test_real_celery_delivery_duplicate_publish_and_identifier_only_payload(database,api,world,broker,tmp_path):
    url,r=broker;r.flushdb();a,pid,j,_=setup_job(database,api,world);send=publisher(url)
    def crash(row):
        if row['job_id']==j:raise RuntimeError('injected relay crash after broker accepted message')
    with pytest.raises(RuntimeError):
        with worker(database) as c:runtime.relay(c,send,after_publish=crash)
    with worker(database) as c:runtime.relay(c,send)
    messages=r.lrange('maintenance',0,-1);assert len(messages)>=2
    for raw in messages:
        message=json.loads(raw);args,kwargs,_=json.loads(base64.b64decode(message['body']));assert len(args)==2 and kwargs=={}
        assert all(str(UUID(x))==x for x in args)
    with celery_worker(database,url,tmp_path):wait(lambda:state(database,j)=='DONE')
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE'
    with connection(database=database) as c:
        assert c.execute("SELECT count(*) FROM lv.maintenance_event WHERE job_id=%s AND event='CLAIMED'",(j,)).fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM lv.proposal_transition WHERE proposal_id=%s AND action='stale'",(pid,)).fetchone()[0]==1


def test_real_worker_killed_after_durable_claim_is_recovered_and_fenced(database,api,world,broker,tmp_path):
    url,r=broker;r.flushdb();a,pid,j,_=setup_job(database,api,world);send=publisher(url)
    with worker(database) as c:runtime.relay(c,send)
    with celery_worker(database,url,tmp_path,kill_after_claim=True) as p:
        wait(lambda:(tmp_path/'durable-claim').exists() and p.poll() is not None)
        assert p.returncode==-signal.SIGKILL
    assert state(database,j)=='CLAIMED' and a.get('/api/v1/proposals/'+pid).json()['state']=='BLOCKED'
    age(database,j,lease=True)
    with worker(database) as c:c.execute('SELECT lv.maintenance_sweep(100)')
    age(database,j)
    with worker(database) as c:runtime.relay(c,send)
    with celery_worker(database,url,tmp_path):wait(lambda:state(database,j)=='DONE')
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE'
    with connection(database=database) as c:
        assert c.execute('SELECT attempts FROM lv.work_item WHERE id=%s',(j,)).fetchone()[0]==2
        assert c.execute("SELECT count(*) FROM lv.proposal_transition WHERE proposal_id=%s AND action='stale'",(pid,)).fetchone()[0]==1


def test_real_redis_loss_recovers_dispatched_unclaimed_work(database,api,world,broker,tmp_path):
    url,r=broker;r.flushdb();a,pid,j,_=setup_job(database,api,world);send=publisher(url)
    with worker(database) as c:runtime.relay(c,send)
    assert r.llen('maintenance')>0;r.flushdb();assert r.llen('maintenance')==0
    with connection(database=database) as c:c.execute("UPDATE lv.outbox SET dispatched_at=clock_timestamp()-interval '31 seconds' WHERE work_item_id=%s",(j,))
    with worker(database) as c:runtime.scheduler_once(c,send)
    with celery_worker(database,url,tmp_path):wait(lambda:state(database,j)=='DONE')
    assert a.get('/api/v1/proposals/'+pid).json()['state']=='STALE'
    with connection(database=database) as c:assert c.execute('SELECT attempts,delivery_generation FROM lv.work_item WHERE id=%s',(j,)).fetchone()==(1,1)
