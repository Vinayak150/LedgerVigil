"""Redis transports identifiers only; PostgreSQL owns every durable transition."""
import logging
import os
import socket
from uuid import UUID,uuid4
import psycopg

log=logging.getLogger(__name__)
_after_claim=None
_after_step=None

def connect():
    dsn=os.environ.get('LV_WORKER_DATABASE_URL')
    if not dsn:raise RuntimeError('LV_WORKER_DATABASE_URL is required')
    c=psycopg.connect(dsn,connect_timeout=3,application_name='lv-maintenance',options='-c statement_timeout=15000 -c lock_timeout=3000')
    if c.execute('SELECT current_user,session_user').fetchone()!=('lv_worker','lv_worker'):
        c.close();raise RuntimeError('maintenance requires the restricted lv_worker login')
    c.commit()
    return c

def claim(c,tenant,job,worker):
    return c.execute('SELECT lv.maintenance_claim(%s,%s,%s)',(UUID(str(tenant)),UUID(str(job)),worker)).fetchone()[0]

def step(c,tenant,job,worker,fence):
    return c.execute('SELECT lv.maintenance_step(%s,%s,%s,%s)',(UUID(str(tenant)),UUID(str(job)),worker,fence)).fetchone()[0]

def run_job(tenant,job,*,connection_factory=connect,worker=None):
    tenant,job=str(UUID(str(tenant))),str(UUID(str(job)))
    worker=worker or 'worker-'+uuid4().hex
    fence=None
    try:
        with connection_factory() as c:
            with c.transaction():token=claim(c,tenant,job,worker)
        if token is None:return {'status':'NOT_CLAIMED'}
        fence=token['fencing_token']
        if _after_claim is not None:_after_claim(token)
        while True:
            with connection_factory() as c:
                with c.transaction():result=step(c,tenant,job,worker,fence)
            if _after_step is not None:_after_step(result)
            if result['status']!='PROGRESS':return result
    except Exception as exc:
        error_class='INFRA_TERMINAL' if isinstance(exc,psycopg.Error) and exc.sqlstate in ('23514','42501','22023') else 'INFRA_RETRYABLE'
        if fence is not None:
            try:
                with connection_factory() as c:
                    c.execute('SELECT lv.maintenance_fail(%s,%s,%s,%s,%s)',(tenant,job,worker,fence,error_class))
            except Exception:
                # Durable claim remains; expiry/sweep recovers when PostgreSQL is available.
                pass
        log.error('maintenance job stopped: class=%s exception=%s',error_class,type(exc).__name__)
        return {'status':'FAILED','error_class':error_class}

def relay(c,publish,*,after_publish=None):
    """Publish before marking dispatched. Any crash republishes; claim fencing deduplicates."""
    sent=0
    with c.transaction():
        rows=[r[0] for r in c.execute('SELECT lv.maintenance_due(25)')]
        for r in rows:
            publish(r['tenant_id'],r['job_id'])
            if after_publish is not None:after_publish(r)
            assert c.execute('SELECT lv.maintenance_dispatched(%s,%s)',(r['tenant_id'],r['outbox_id'])).fetchone()[0]
            sent+=1
    return sent

def scheduler_once(c,publish,*,publish_exports=None,publish_packs=None,publish_provider=None):
    with c.transaction():swept=c.execute('SELECT lv.maintenance_sweep(100)').fetchone()[0]
    from ledgervigil.exports.runtime import relay as export_relay
    answer={'swept':swept,'published':relay(c,publish)}
    if publish_exports is not None:answer['exports_published']=export_relay(c,publish_exports)
    if publish_packs is not None:
        from ledgervigil.proofpacks.runtime import relay as pack_relay
        answer['packs_published']=pack_relay(c,publish_packs)
    if publish_provider is not None:
        from ledgervigil.provider.runtime import relay as provider_relay
        answer['provider_published']=provider_relay(c,publish_provider)
    return answer

SCHEDULER_LOCK=597920714029

def scheduler(connection_factory,publish,stop,*,poll_seconds=1):
    """One local scheduler; advisory ownership is held until this connection closes."""
    last_dead=None
    with connection_factory() as c:
        if not c.execute('SELECT pg_try_advisory_lock(%s)',(SCHEDULER_LOCK,)).fetchone()[0]:
            c.commit();return False
        c.commit()
        while not stop.is_set():
            try:
                from .celery_app import publish_export,publish_pack,publish_provider
                scheduler_once(c,publish,publish_exports=publish_export,publish_packs=publish_pack,
                               publish_provider=publish_provider if os.environ.get('LV_PROVIDER_SANDBOX_ENABLED')=='1' else None)
                with c.transaction():health=c.execute('SELECT lv.maintenance_health()').fetchone()[0]
                dead=health.get('DEAD',0)
                if dead and dead!=last_dead:log.error('maintenance DEAD jobs require operator attention: count=%s',dead)
                last_dead=dead
            except Exception as exc:
                c.rollback();log.error('maintenance scheduler iteration failed: %s',type(exc).__name__)
                if c.closed:raise RuntimeError('scheduler database connection lost; restart required') from None
            stop.wait(poll_seconds)
    return True
