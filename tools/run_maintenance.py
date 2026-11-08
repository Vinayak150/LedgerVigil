"""Local SANDBOX maintenance launcher; private worker DSN stays in-process, never printed."""
import argparse
import os
import signal
import threading
from psycopg.conninfo import make_conninfo
from tools.dev_db import DEV_DB,env_file,require_dev_database

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['worker','scheduler','once','status'])
    p.add_argument('--database',default=DEV_DB,help='Dedicated local development or guarded disposable test database')
    p.add_argument('--exports-only',action='store_true',help='Worker listens only on the native export queue')
    args=p.parse_args()
    require_dev_database(args.database)
    os.environ['LV_WORKER_DATABASE_URL']=make_conninfo(host='127.0.0.1',port=55432,dbname=args.database,user='lv_worker',password=env_file('.env.roles')['WORKER_PASSWORD'])
    os.environ.setdefault('LV_MAINTENANCE_BROKER_URL','redis://127.0.0.1:56379/0')
    from ledgervigil.maintenance.celery_app import application,publish,publish_export,publish_pack
    from ledgervigil.maintenance.runtime import connect,scheduler,scheduler_once
    if args.action=='worker':
        # Solo is portable for the Mac demo. PostgreSQL fencing also covers multiple independent workers.
        application.worker_main(['worker','--pool=solo','--concurrency=1','--queues='+((os.environ.get('LV_EXPORT_QUEUE','native-exports')) if args.exports_only else 'maintenance,'+os.environ.get('LV_EXPORT_QUEUE','native-exports')),'--loglevel=WARNING','--without-gossip','--without-mingle','--without-heartbeat'])
    elif args.action=='status':
        with connect() as c: print(c.execute('SELECT lv.maintenance_health()').fetchone()[0])
    elif args.action=='once':
        with connect() as c:print(scheduler_once(c,publish,publish_exports=publish_export,publish_packs=publish_pack))
    else:
        stop=threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
        if not scheduler(connect,publish,stop):raise SystemExit('Another maintenance scheduler already owns the lock')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        import sys
        print('Maintenance launcher failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
