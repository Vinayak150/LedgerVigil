"""ADR-010 Celery transport. Never import this module into the financial kernel/API."""
import os
from celery import Celery
from .runtime import run_job

application=Celery('ledgervigil.maintenance',broker=os.environ.get('LV_MAINTENANCE_BROKER_URL','redis://127.0.0.1:6379/0'))
application.conf.update(
    task_serializer='json',accept_content=['json'],result_serializer='json',
    task_ignore_result=True,task_store_errors_even_if_ignored=False,
    task_acks_late=True,task_reject_on_worker_lost=True,worker_prefetch_multiplier=1,
    task_soft_time_limit=300,task_time_limit=330,
    broker_connection_timeout=3,broker_transport_options={'visibility_timeout':600,'socket_connect_timeout':3,'socket_timeout':3},
    task_default_queue='maintenance',task_routes={'ledgervigil.staleness_scan':{'queue':'maintenance'}},
)

@application.task(name='ledgervigil.staleness_scan',ignore_result=True)
def staleness_scan(tenant_id,job_id):
    return run_job(tenant_id,job_id)

def publish(tenant_id,job_id):
    application.send_task('ledgervigil.staleness_scan',args=[tenant_id,job_id],queue='maintenance',retry=False)

@application.task(name='ledgervigil.native_proof_export',ignore_result=True)
def native_proof_export(tenant_id,job_id):
    from ledgervigil.exports.runtime import run_job as export_run
    return export_run(tenant_id,job_id)
def publish_export(tenant_id,job_id):
    application.send_task('ledgervigil.native_proof_export',args=[tenant_id,job_id],queue=os.environ.get('LV_EXPORT_QUEUE','native-exports'),retry=False)

@application.task(name='ledgervigil.proof_pack_build',ignore_result=True)
def proof_pack_build(tenant_id,job_id):
    from ledgervigil.proofpacks.runtime import run_job as pack_run
    return pack_run(tenant_id,job_id)
def publish_pack(tenant_id,job_id):
    application.send_task('ledgervigil.proof_pack_build',args=[tenant_id,job_id],queue=os.environ.get('LV_EXPORT_QUEUE','native-exports'),retry=False)
