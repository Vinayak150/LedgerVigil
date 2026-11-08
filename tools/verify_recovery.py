"""Local disaster-recovery drill. Export a consistent snapshot, restore into a
new disposable database, verify data/schema/security and independently replay
ledger events. No source database migration or write; no role/password restore.
Private archive remains local. A failed check is never reported as recovered.
"""
from pathlib import Path
from uuid import uuid4
import argparse,hashlib,json,os,subprocess,time,re
from psycopg import sql
from .dev_db import connection,require_dev_database,ROOT
from ledgervigil.ledger.replay import replay_events
from tools import recovery_database as database_profile


def restore_target(name):
 if name=='ledgervigil_dev':raise ValueError('RESTORE_TARGET_MUST_BE_DISPOSABLE')
 require_dev_database(name)


def docker_args():
 return ['/opt/homebrew/bin/docker','--config',str(ROOT/'.docker-local'),'--host','unix:///Users/vinayakmahindrakar/.colima/default/docker.sock','exec','-i','ledgervigil-native-dev-db-1']


def normalize_constraint(text):
 # PG18 also flattens these two exact associative AND checks. No generic SQL rewriting.
 for column in ('body','reason'):
  nested="CHECK ((((length("+column+") >= 1) AND (length("+column+") <= 4000)) AND ("+column+" ~ '[^[:space:]]'::text)))"
  flat="CHECK (((length("+column+") >= 1) AND (length("+column+") <= 4000) AND ("+column+" ~ '[^[:space:]]'::text)))"
  if text==nested:return flat
 # PG18 dump/restore distributes varchar[]->text[] over constant elements.
 # Normalize only uppercase constant literals. Preserve every value, operator and guard.
 text=re.sub(r"\(ARRAY\[((?:'[A-Z_]+'::character varying)(?:, '[A-Z_]+'::character varying)*)\]\)::text\[\]",lambda m:"ARRAY["+m[1].replace("::character varying","::text")+"]",text)
 return re.sub(r"\('([A-Z_]+)'::character varying\)::text",r"'\1'::text",text)


def schema_catalog(c):
 # Compare semantic catalogs, not pg_dump statement ordering or generated tokens.
 scope="n.nspname IN ('lv','lv_identity','lv_meta')"
 queries={
 'namespaces':"SELECT n.nspname,pg_get_userbyid(n.nspowner) FROM pg_namespace n WHERE "+scope+" ORDER BY 1",
 'namespace_grants':"SELECT n.nspname,pg_get_userbyid(a.grantor),CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,a.privilege_type,a.is_grantable FROM pg_namespace n CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a WHERE "+scope+" ORDER BY 1,2,3,4,5",
 'default_privileges':"SELECT pg_get_userbyid(d.defaclrole),coalesce(n.nspname,'ALL_SCHEMAS'),d.defaclobjtype,pg_get_userbyid(a.grantor),CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,a.privilege_type,a.is_grantable FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace CROSS JOIN LATERAL aclexplode(d.defaclacl) a WHERE d.defaclnamespace=0 OR "+scope+" ORDER BY 1,2,3,4,5,6,7",
 'tables':"SELECT n.nspname,cl.relname,pg_get_userbyid(cl.relowner),cl.relrowsecurity,cl.relforcerowsecurity,cl.reloptions FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace WHERE "+scope+" AND cl.relkind IN ('r','p','S') ORDER BY 1,2",
 'columns':"SELECT n.nspname,cl.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a JOIN pg_class cl ON cl.oid=a.attrelid JOIN pg_namespace n ON n.oid=cl.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=cl.oid AND d.adnum=a.attnum WHERE "+scope+" AND a.attnum>0 AND NOT a.attisdropped AND cl.relkind IN ('r','p') ORDER BY 1,2,3",
 'constraints':"SELECT n.nspname,cl.relname,co.conname,pg_get_constraintdef(co.oid),co.convalidated FROM pg_constraint co JOIN pg_class cl ON cl.oid=co.conrelid JOIN pg_namespace n ON n.oid=cl.relnamespace WHERE "+scope+" ORDER BY 1,2,3",
 'indexes':"SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes WHERE schemaname IN ('lv','lv_identity','lv_meta') ORDER BY 1,2,3",
 'policies':"SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname IN ('lv','lv_identity','lv_meta') ORDER BY 1,2,3",
 'triggers':"SELECT n.nspname,cl.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class cl ON cl.oid=t.tgrelid JOIN pg_namespace n ON n.oid=cl.relnamespace WHERE "+scope+" AND NOT t.tgisinternal ORDER BY 1,2,3",
 'functions':"SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),pg_get_userbyid(p.proowner),pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE "+scope+" ORDER BY 1,2,3",
 'table_grants':"SELECT table_schema,table_name,grantor,grantee,privilege_type,is_grantable FROM information_schema.table_privileges WHERE table_schema IN ('lv','lv_identity','lv_meta') ORDER BY 1,2,3,4,5,6",
 'function_grants':"SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),a.grantee::regrole::text,a.privilege_type,a.is_grantable FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE "+scope+" ORDER BY 1,2,3,4,5,6",
 'types':"SELECT n.nspname,t.typname,e.enumsortorder,e.enumlabel FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace JOIN pg_enum e ON e.enumtypid=t.oid WHERE "+scope+" ORDER BY 1,2,3"
 }
 data={key:c.execute(query).fetchall() for key,query in queries.items()}
 data['constraints']=[(*r[:3],normalize_constraint(r[3]),r[4]) for r in data['constraints']]
 return data

def schema_digest(c):
 return hashlib.sha256(json.dumps(schema_catalog(c),sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def fingerprints(c):
 result={}
 for schema,table in c.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('lv','lv_identity','lv_meta') ORDER BY schemaname,tablename").fetchall():
  hashes=[]
  with c.cursor(name='recovery_rows') as cur:
   cur.itersize=5000;cur.execute(sql.SQL('SELECT row_to_json(t)::text FROM {} t').format(sql.Identifier(schema,table)))
   for row in cur:hashes.append(hashlib.sha256(row[0].encode()).hexdigest())
  result[schema+'.'+table]={'rows':len(hashes),'sha256':hashlib.sha256('\n'.join(sorted(hashes)).encode()).hexdigest()}
 return result


def assert_match(expected,actual):
 if expected.keys()!=actual.keys():raise ValueError('RECOVERY_TABLE_INVENTORY_MISMATCH')
 changed=[name for name in expected if expected[name]!=actual[name]]
 if changed:raise ValueError('RECOVERY_DATA_MISMATCH: '+','.join(changed))


def replay(c):
 ledgers=0;events=0;entries=0
 for tenant,ledger,last_seq,last_entry,event_head in c.execute('SELECT tenant_id,ledger_id,last_seq,last_entry_no,event_head FROM lv.ledger_counter ORDER BY tenant_id,ledger_id').fetchall():
  rows=c.execute('SELECT seq,canonical_content,previous_hash,hash FROM lv.ledger_event WHERE tenant_id=%s AND ledger_id=%s ORDER BY seq',(tenant,ledger)).fetchall()
  result=replay_events([dict(zip(('seq','canonical_content','previous_hash','hash'),r)) for r in rows])
  if len(rows)!=last_seq or result['entries']!=last_entry or result['head']!=event_head:raise ValueError('RECOVERY_LEDGER_REPLAY_MISMATCH')
  projected={(str(account),None if party is None else str(party)):(debit,credit) for account,party,debit,credit in c.execute('SELECT account_id,party_id,debit_total,credit_total FROM lv.account_balance_current WHERE tenant_id=%s AND ledger_id=%s',(tenant,ledger))}
  actual={k:tuple(v) for k,v in result['balances'].items()}
  if actual!=projected:raise ValueError('RECOVERY_BALANCE_REPLAY_MISMATCH')
  ledgers+=1;events+=len(rows);entries+=result['entries']
 return {'ledgers':ledgers,'events':events,'posting_entries':entries,'sequence_heads_and_balances_match':True}


def security(database):
 with connection(database=database) as c:
  tables=c.execute("SELECT n.nspname,cl.relname,cl.relrowsecurity,cl.relforcerowsecurity FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace WHERE n.nspname='lv' AND cl.relkind='r' AND cl.relname<>'currency' ORDER BY cl.relname").fetchall()
  if not tables or not all(r[2] and r[3] for r in tables):raise ValueError('RECOVERY_RLS_DISABLED')
  columns={name for (name,) in c.execute("SELECT table_name FROM information_schema.columns WHERE table_schema='lv' AND column_name='tenant_id'")}
  tenants=[r[0] for r in c.execute('SELECT tenant_id FROM lv.tenant ORDER BY tenant_id')]
  for role in ['lv_app','lv_ingest','lv_seed','lv_worker']:
   if c.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s',(role,)).fetchone()!=(False,False):raise ValueError('RECOVERY_ROLE_BYPASS')
 with connection('lv_app',database) as app:
  for schema,table,_,_ in tables:
   if table not in columns:continue
   if app.execute('SELECT has_table_privilege(current_user,%s,\'SELECT\')',(schema+'.'+table,)).fetchone()[0]:
    if app.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(schema,table))).fetchone()[0]!=0:raise ValueError('RECOVERY_TENANT_LEAK_WITHOUT_CONTEXT')
  if tenants:
   app.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenants[0]),))
   for schema,table,_,_ in tables:
    if table not in columns:continue
    if app.execute('SELECT has_table_privilege(current_user,%s,\'SELECT\')',(schema+'.'+table,)).fetchone()[0]:
     if app.execute(sql.SQL('SELECT count(*) FROM {} WHERE tenant_id<>%s').format(sql.Identifier(schema,table)),(tenants[0],)).fetchone()[0]!=0:raise ValueError('RECOVERY_CROSS_TENANT_LEAK')
 return {'force_rls_tables':len(tables),'restricted_roles_checked':4,'tenant_contexts_available':len(tenants),'runtime_tenant_boundary':'PASS'}


def run(source,output,*,application=None):
 require_dev_database(source);output=Path(output);output.mkdir(mode=0o700,parents=False,exist_ok=False)
 destination='lv_migration_test_'+uuid4().hex;restore_target(destination);created=False;start=time.monotonic();archive=output/'database.dump'
 try:
  with connection(database=source) as original:
   original.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
   snap=original.execute('SELECT pg_export_snapshot()').fetchone()[0]
   before=fingerprints(original);schema=schema_digest(original);source_replay=replay(original)
   profile=database_profile.capture(original);global_roles=database_profile.role_fingerprint(original)
   fd_profile=os.open(output/'DATABASE_PROFILE.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
   with os.fdopen(fd_profile,'w') as f:json.dump(profile,f,sort_keys=True,indent=2);f.write('\n')
   capture=application.capture(original,output) if application is not None else None
   fd=os.open(archive,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
   with os.fdopen(fd,'wb') as f:subprocess.run(docker_args()+['pg_dump','-U','postgres','-Fc','--snapshot',snap,source],stdout=f,stderr=subprocess.PIPE,check=True)
  target_oid=database_profile.create_target(destination,profile);created=True
  with archive.open('rb') as f:subprocess.run(docker_args()+['pg_restore','-U','postgres','--exit-on-error','-d',destination],stdin=f,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
  restored_profile=database_profile.apply(source,destination,target_oid,profile)
  with connection(database=destination) as recovered:
   recovered.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY');after=fingerprints(recovered);assert_match(before,after);restored_replay=replay(recovered);restored_schema=schema_digest(recovered)
   anchored={'status':'NOT_CONFIGURED','unanchored_tail':'NOT_ATTESTED'}
   if os.environ.get('LV_ANCHOR_REPOSITORY'):
    from ledgervigil.proofpacks.anchors import GitAnchors
    from ledgervigil.proofpacks.recovery import check_database
    anchored=check_database(recovered,GitAnchors(os.environ['LV_ANCHOR_REPOSITORY']))
  if restored_schema!=schema:raise ValueError('RECOVERY_SCHEMA_MISMATCH')
  if restored_replay!=source_replay:raise ValueError('RECOVERY_REPLAY_SUMMARY_MISMATCH')
  boundaries=security(destination)
  acceptance=application.verify_restored(destination) if application is not None else None
  # Acceptance must not mutate the restored financial or identity snapshot.
  if application is not None:
   with connection(database=destination) as checked:
    checked.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY');assert_match(before,fingerprints(checked))
    if schema_digest(checked)!=schema:raise ValueError('RECOVERY_ACCEPTANCE_SCHEMA_CHANGED')
  with connection(database=destination) as verified:database_profile.assert_profile(profile,database_profile.capture(verified))
  with connection(database=source) as unchanged:
   database_profile.assert_profile(profile,database_profile.capture(unchanged))
   if database_profile.role_fingerprint(unchanged)!=global_roles:raise ValueError('RECOVERY_GLOBAL_ROLE_DRIFT')
  restored_profile.update(source_profile_unchanged=True,global_roles_memberships_and_global_role_settings_unchanged=True,profile_sha256=hashlib.sha256(database_profile.canonical(profile).encode()).hexdigest(),scoped_settings_count=len(profile['settings']),effective_database_acl_entries=len(profile['acl']))
  report={'status':'PASS','scope':'local PostgreSQL logical backup/restore; source held at one exported read-only snapshot','source_database':source,'snapshot_consistent':True,'tables_verified':len(before),'total_rows_verified':sum(r['rows'] for r in before.values()),'table_fingerprints':before,'schema_sha256':schema,'schema_owners_grants_policies_constraints_and_functions_match':True,'external_anchor':anchored,'ledger_replay':restored_replay,'security':boundaries,'database_profile':restored_profile,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'archive_bytes':archive.stat().st_size,'archive_private_mode':oct(archive.stat().st_mode&0o777),'observed_seconds':round(time.monotonic()-start,3),'limits':['Same-host logical restore with existing cluster roles and extensions; not offsite recovery','Role passwords, external source/artifact files and broker state are outside the database archive','No production RPO/RTO, failover or full application acceptance claim']}
 finally:
  if created:
   with connection(autocommit=True) as c:c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(destination)))
 report['disposable_restore_database_removed']=True
 if application is not None:
  report['application_capture']=capture;report['application_acceptance']=acceptance
  report['scope']='same-host database + source + pinned public assets recovery; bounded backend acceptance'
  report['limits'].append('Application bundle omits secrets, dependency installations/build artifacts, Redis, frontend rebuild and authenticated HTTP; see application acceptance')
 fd_report=os.open(output/'REPORT.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd_report,'w') as f:json.dump(report,f,indent=2);f.write('\n')
 return report


def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',default='ledgervigil_dev');parser.add_argument('--output',type=Path,required=True);parser.add_argument('--application-manifest',type=Path);parser.add_argument('--assets',type=Path);args=parser.parse_args()
 if bool(args.application_manifest)!=bool(args.assets):parser.error('--application-manifest and --assets must be supplied together')
 application=None
 if args.application_manifest:
  from tools.application_recovery import ApplicationAcceptance
  application=ApplicationAcceptance(args.application_manifest,args.assets)
 report=run(args.source,args.output,application=application);print(json.dumps({k:v for k,v in report.items() if k!='table_fingerprints'},indent=2))

if __name__=='__main__':main()
