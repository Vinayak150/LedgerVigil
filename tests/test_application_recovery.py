"""Recovery boundary, private-file and actual database lifecycle checks."""
from pathlib import Path
from copy import deepcopy
from uuid import uuid4
import hashlib,json,os,stat
import pytest
from tools import application_recovery as ar,verify_recovery as recovery
from tools.dev_db import connection

def source_manifest(root):
 files=[]
 for name in ['src/ledgervigil/app.py','tools/recovery_probe.py','tools/application_recovery.py','tools/recovery_database.py','requirements.lock','worker-requirements.lock','web/package-lock.json']:
  p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('source\n');files.append({'path':name,'bytes':p.stat().st_size,'sha256':ar.digest(p)})
 return {'files':files}

@pytest.mark.parametrize('name',['../outside','/tmp/outside','src/../tools/a','src//a','.env','web/.env.local','tools/__pycache__/x.pyc','web/node_modules/p/a.js','src/.git/config','web/.next/server.js'])
def test_source_manifest_refuses_escape_secrets_and_generated_files(tmp_path,name):
 m=source_manifest(tmp_path);m['files'][0]['path']=name
 with pytest.raises(ValueError):ar.check_source(tmp_path,m)

def test_source_manifest_detects_bytes_changes_duplicates_and_missing_owning_files(tmp_path):
 m=source_manifest(tmp_path);assert ar.check_source(tmp_path,m)['files']==7
 for fault in ['hash','bytes','duplicate','missing']:
  broken=deepcopy(m)
  if fault=='hash':broken['files'][0]['sha256']='0'*64
  elif fault=='bytes':broken['files'][0]['bytes']+=1
  elif fault=='duplicate':broken['files'].append(broken['files'][0])
  else:broken['files'].pop()
  with pytest.raises(ValueError):ar.check_source(tmp_path,broken)

@pytest.mark.parametrize('parent',[False,True])
def test_source_manifest_rejects_file_and_parent_symlinks(tmp_path,parent):
 root=tmp_path/'source';root.mkdir();m=source_manifest(root);p=root/'src/ledgervigil/app.py'
 if parent:
  saved=tmp_path/'moved';(root/'src').rename(saved);(root/'src').symlink_to(saved,target_is_directory=True)
 else:
  p.unlink();outside=tmp_path/'outside.py';outside.write_text('source\n');p.symlink_to(outside)
 with pytest.raises(ValueError,match='SYMLINK'):ar.check_source(root,m)

def test_copy_is_private_verifiable_and_never_overwrites(tmp_path):
 src=tmp_path/'src';src.write_text('private snapshot');dest=tmp_path/'new'/'copy';ar.copy_private(src,dest)
 assert dest.read_bytes()==src.read_bytes() and stat.S_IMODE(dest.stat().st_mode)==0o600
 with pytest.raises(FileExistsError):ar.copy_private(src,dest)
 ar.private_json(tmp_path/'result',{'status':'PASS'})
 assert stat.S_IMODE((tmp_path/'result').stat().st_mode)==0o600
 with pytest.raises(FileExistsError):ar.private_json(tmp_path/'result',{})

def test_asset_symlink_is_refused(tmp_path):
 p=tmp_path/'raw';p.write_text('data');link=tmp_path/'link';link.symlink_to(p)
 with pytest.raises(ValueError,match='REGULAR_FILE'):ar.copy_private(link,tmp_path/'dest')

def databases():
 with connection() as c:return {r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'lv_migration_test_%'")}

@pytest.mark.parametrize('failure',['capture','verify','mutate'])
def test_failed_acceptance_never_passes_and_drops_only_its_restore(database,tmp_path,failure):
 before=databases();path=tmp_path/'drill'
 class Broken:
  def capture(self,c,output):
   if failure=='capture':raise ValueError('INJECTED_CAPTURE_FAILURE')
   return {'captured':True}
  def verify_restored(self,name):
   recovery.restore_target(name)
   if failure=='verify':raise ValueError('INJECTED_ACCEPTANCE_FAILURE')
   with connection(database=name) as db:db.execute('CREATE TABLE lv_meta.injected_recovery_change(id int)')
   return {'status':'PASS'}
 with pytest.raises(ValueError):recovery.run(database,path,application=Broken())
 assert not(path/'REPORT.json').exists() and databases()==before and stat.S_IMODE(path.stat().st_mode)==0o700
 if (path/'database.dump').exists():assert stat.S_IMODE((path/'database.dump').stat().st_mode)==0o600


def test_snapshot_acceptance_cannot_write_source_database(database,tmp_path):
 class Forbidden:
  def capture(self,c,output):c.execute('CREATE TABLE lv_meta.recovery_source_write(id int)')
 import psycopg
 with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):recovery.run(database,tmp_path/'source-read-only',application=Forbidden())
 with connection(database=database) as c:assert c.execute("SELECT to_regclass('lv_meta.recovery_source_write')").fetchone()==(None,)


def test_owned_operational_scripts_are_valid_source(tmp_path):
 m=source_manifest(tmp_path);p=tmp_path/'scripts/verify_guards.py';p.parent.mkdir();p.write_text('owned script\n');m['files'].append({'path':'scripts/verify_guards.py','bytes':p.stat().st_size,'sha256':ar.digest(p)})
 assert ar.check_source(tmp_path,m)['files']==8


@pytest.mark.parametrize('column',['body','reason'])
def test_pg18_exact_associative_checks_normalize_without_hiding_policy_changes(column):
 nested="CHECK ((((length("+column+") >= 1) AND (length("+column+") <= 4000)) AND ("+column+" ~ '[^[:space:]]'::text)))"
 flat="CHECK (((length("+column+") >= 1) AND (length("+column+") <= 4000) AND ("+column+" ~ '[^[:space:]]'::text)))"
 assert recovery.normalize_constraint(nested)==recovery.normalize_constraint(flat)
 for broken in [flat.replace('4000','3999'),flat.replace('AND','OR',1),flat.replace('>= 1','>= 0'),flat.replace('[^[:space:]]','.*')]:
  assert recovery.normalize_constraint(nested)!=recovery.normalize_constraint(broken)


def test_source_migration_must_match_actual_snapshot_head(database):
 root=Path(__file__).parents[1]
 with connection(database=database) as c:
  assert ar.migration_head(root,c)=='0040_mfa_reset'
  c.execute("UPDATE lv_meta.alembic_version SET version_num='0001_configuration'")
  with pytest.raises(ValueError,match='SOURCE_MIGRATION_MISMATCH'):ar.migration_head(root,c)
  c.rollback()

def test_private_source_copy_preserves_explicit_owner_execute(tmp_path):
 p=tmp_path/'script';p.write_text('#!/bin/sh\nexit 0\n');os.chmod(p,0o755);dest=tmp_path/'private-script';ar.copy_private(p,dest,0o700)
 assert stat.S_IMODE(dest.stat().st_mode)==0o700 and dest.read_bytes()==p.read_bytes()
