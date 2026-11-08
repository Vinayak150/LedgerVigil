"""Fresh-process acceptance from recovered code/assets. DSNs exist only in environment."""
from pathlib import Path
import json,os,sys,importlib.metadata
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
import psycopg
from tools.application_recovery import check_source,digest,asset_sample,maintenance_inventory,WORKBOOK,migration_head
from ledgervigil.ingestion.plan_index import IndexReader
from ledgervigil.ingestion.index_publication import INDEX_DIRECTORY,INDEX_MANIFEST_SHA256
from ledgervigil.evaluation.retail import PUBLIC_SHA256
from ledgervigil.execution.proof_export import export_proof
from ledgervigil.execution.proof_verify import verify

def run(bundle):
 import ledgervigil.app as module
 from ledgervigil.auth.settings import AuthSettings
 from fastapi.testclient import TestClient
 bundle=Path(bundle);capture=json.loads((bundle/'CAPTURE.json').read_text());manifest=json.loads((bundle/'SOURCE.json').read_text())
 if {p:importlib.metadata.version(p) for p in capture['runtime']['dependencies']}!=capture['runtime']['dependencies']:raise ValueError('RECOVERY_DEPENDENCY_VERSION_DIFFERENCE')
 if not Path(module.__file__).resolve().is_relative_to(ROOT):raise ValueError('RECOVERY_IMPORTED_WRONG_SOURCE')
 if check_source(ROOT,manifest)!=capture['source']:raise ValueError('RECOVERY_SOURCE_DIFFERENCE')
 for name,mode in capture['source_file_modes'].items():
  if (ROOT/name).stat().st_mode&0o777!=mode:raise ValueError('RECOVERY_SOURCE_MODE_DIFFERENCE')
 for row in capture['asset_files']:
  p=bundle/row['path']
  if p.is_symlink() or p.stat().st_size!=row['bytes'] or digest(p)!=row['sha256']:raise ValueError('RECOVERY_ASSET_DIFFERENCE')
 if digest(INDEX_DIRECTORY.parent/WORKBOOK)!=PUBLIC_SHA256:raise ValueError('RECOVERY_WORKBOOK_DIFFERENCE')
 # Deliberately use the copied source's default path, without index injection.
 sample=asset_sample(IndexReader(INDEX_DIRECTORY,INDEX_MANIFEST_SHA256))
 if sample!=capture['assets']:raise ValueError('RECOVERY_INDEX_DIFFERENCE')
 count=0
 with psycopg.connect(os.environ['LV_RECOVERY_APP_DSN']) as app:
  app.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
  if app.execute('SELECT current_user,session_user').fetchone()!=('lv_app','lv_app'):raise ValueError('RECOVERY_APP_ROLE')
  if app.execute('SELECT count(*) FROM lv.native_execution').fetchone()!=(0,):raise ValueError('RECOVERY_UNSCOPED_LEAK')
  for row in capture['proofs']:
   app.execute("SELECT set_config('app.tenant_id',%s,true)",(row['tenant_id'],));data=export_proof(app,row['execution_id'])
   if data!=(bundle/'proofs'/(row['execution_id']+'.json')).read_bytes():raise ValueError('RECOVERY_PROOF_BYTE_DIFFERENCE')
   verify(data,row['sha256']);count+=1
 with psycopg.connect(os.environ['LV_RECOVERY_ADMIN_DSN']) as admin:
  admin.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
  if migration_head(ROOT,admin)!=capture['migration_head']:raise ValueError('RECOVERY_MIGRATION_DIFFERENCE')
  if maintenance_inventory(admin)!=capture['maintenance']:raise ValueError('RECOVERY_MAINTENANCE_DIFFERENCE')
 with psycopg.connect(os.environ['LV_RECOVERY_WORKER_DSN']) as worker:
  worker.execute('SET TRANSACTION READ ONLY')
  if worker.execute('SELECT current_user,session_user').fetchone()!=('lv_worker','lv_worker'):raise ValueError('RECOVERY_WORKER_ROLE')
  health=worker.execute('SELECT lv.maintenance_health()').fetchone()[0]
 app=module.create_app(AuthSettings(),connect=lambda:psycopg.connect(os.environ['LV_RECOVERY_APP_DSN']))
 try:
  with TestClient(app) as http:
   if http.get('/health').status_code!=200 or http.get('/api/v1/ledgers').status_code!=401:raise ValueError('RECOVERY_API_BOUNDARY')
 finally:app.state.pool.close()
 return {'status':'PASS','fresh_process_from_recovered_source':True,'default_asset_path_restored':True,'source_files_verified':capture['source']['files'],'migration_head':capture['migration_head'],'source_owner_execute_modes_preserved':True,'index_groups':sample['groups'],'index_rows':sample['rows'],'exact_execution_proofs_verified':count,'proof_authenticity':'NOT_ESTABLISHED','accounting':'UNREVIEWED','maintenance_inventory_preserved':True,'restricted_worker_health':health,'api_health_and_unauthenticated_boundary':True,'broker_restore':'NOT_EXERCISED','authenticated_http':'NOT_EXERCISED','frontend_rebuild':'NOT_EXERCISED','runtime_prerequisites':capture['runtime']}

if __name__=='__main__':
 try:print(json.dumps(run(sys.argv[1]),sort_keys=True))
 except Exception as exc:
  print(json.dumps({'status':'FAIL','error_class':type(exc).__name__}),file=sys.stderr);raise SystemExit(1)
