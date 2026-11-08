"""Bounded same-host database security/configuration restoration. No global role writes."""
import hashlib,json
from psycopg import sql
from tools.dev_db import connection,require_dev_database,DEV_DB

FORMAT='lv-local-database-recovery/1'
ROLES=frozenset({'postgres','lv_owner','lv_migrator','lv_app','lv_seed','lv_ingest','lv_worker'})
PRIVILEGES=frozenset({'CREATE','CONNECT','TEMPORARY'})
SETTINGS=frozenset({'statement_timeout','lock_timeout','idle_in_transaction_session_timeout','idle_session_timeout','default_transaction_isolation','default_transaction_read_only','timezone','datestyle','client_encoding','search_path','work_mem','maintenance_work_mem','temp_file_limit'})
FIELDS={'format','owner','connection_limit','encoding','locale_provider','lc_collate','lc_ctype','locale','collation_version','tablespace','allow_connections','is_template','acl','settings'}

def guard_target(name):
 require_dev_database(name)
 if name==DEV_DB:raise ValueError('RESTORE_TARGET_MUST_BE_DISPOSABLE')

def capture(c):
 row=c.execute("SELECT pg_get_userbyid(d.datdba),d.datconnlimit,pg_encoding_to_char(d.encoding),d.datlocprovider,d.datcollate,d.datctype,d.datlocale,d.datcollversion,t.spcname,d.datallowconn,d.datistemplate FROM pg_database d JOIN pg_tablespace t ON t.oid=d.dattablespace WHERE d.datname=current_database()").fetchone()
 profile=dict(zip(('owner','connection_limit','encoding','locale_provider','lc_collate','lc_ctype','locale','collation_version','tablespace','allow_connections','is_template'),row));profile['format']=FORMAT
 profile['acl']=[dict(zip(('grantor','grantee','privilege','grantable'),r)) for r in c.execute("SELECT pg_get_userbyid(a.grantor),CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,a.privilege_type,a.is_grantable FROM pg_database d CROSS JOIN LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a WHERE d.datname=current_database() ORDER BY 1,2,3,4")]
 settings=[]
 for role,config in c.execute("SELECT CASE WHEN s.setrole=0 THEN NULL ELSE r.rolname END,s.setconfig FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid=s.setrole WHERE s.setdatabase=(SELECT oid FROM pg_database WHERE datname=current_database())"):
  for value in config or []:
   name,sep,v=value.partition('=')
   if not sep:raise ValueError('RECOVERY_INVALID_DATABASE_SETTING')
   settings.append({'role':role,'name':name.lower(),'value':v})
 profile['settings']=sorted(settings,key=lambda r:(r['role'] or '',r['name']))
 validate(profile);return profile

def validate(p):
 if type(p) is not dict or set(p)!=FIELDS or p['format']!=FORMAT:raise ValueError('RECOVERY_PROFILE_FORMAT')
 if p['owner'] not in ROLES or type(p['connection_limit']) is not int or p['connection_limit']< -1:raise ValueError('RECOVERY_UNSUPPORTED_DATABASE_OWNER_OR_LIMIT')
 if p['encoding']!='UTF8' or p['locale_provider']!='c' or p['tablespace']!='pg_default' or p['locale'] is not None or p['allow_connections'] is not True or p['is_template'] is not False:raise ValueError('RECOVERY_UNSUPPORTED_DATABASE_ATTRIBUTES')
 for k in ('lc_collate','lc_ctype'):
  if type(p[k]) is not str or not 1<=len(p[k])<=128:raise ValueError('RECOVERY_INVALID_DATABASE_LOCALE')
 if p['collation_version'] is not None and type(p['collation_version']) is not str:raise ValueError('RECOVERY_INVALID_DATABASE_LOCALE')
 if type(p['acl']) is not list or len(p['acl'])>32:raise ValueError('RECOVERY_INVALID_DATABASE_ACL')
 seen=set()
 for row in p['acl']:
  if type(row) is not dict or set(row)!={'grantor','grantee','privilege','grantable'}:raise ValueError('RECOVERY_INVALID_DATABASE_ACL')
  if row['grantor']!=p['owner']:raise ValueError('RECOVERY_DELEGATED_DATABASE_GRANT_UNSUPPORTED')
  if row['grantee'] not in ROLES|{'PUBLIC'} or row['privilege'] not in PRIVILEGES or type(row['grantable']) is not bool or (row['grantee']=='PUBLIC' and row['grantable']):raise ValueError('RECOVERY_INVALID_DATABASE_ACL')
  key=(row['grantor'],row['grantee'],row['privilege'])
  if key in seen:raise ValueError('RECOVERY_DUPLICATE_DATABASE_ACL')
  seen.add(key)
 if type(p['settings']) is not list or len(p['settings'])>64:raise ValueError('RECOVERY_INVALID_DATABASE_SETTINGS')
 seen=set()
 for row in p['settings']:
  if type(row) is not dict or set(row)!={'role','name','value'} or (row['role'] is not None and row['role'] not in ROLES) or row['name'] not in SETTINGS or type(row['value']) is not str or len(row['value'])>4096 or '\x00' in row['value']:raise ValueError('RECOVERY_UNSUPPORTED_DATABASE_SETTING')
  key=(row['role'],row['name'])
  if key in seen:raise ValueError('RECOVERY_DUPLICATE_DATABASE_SETTING')
  seen.add(key)

def canonical(p):
 validate(p);p={**p,'acl':sorted(p['acl'],key=lambda r:(r['grantor'],r['grantee'],r['privilege'])),'settings':sorted(p['settings'],key=lambda r:(r['role'] or '',r['name']))}
 return json.dumps(p,sort_keys=True,separators=(',',':'))

def assert_profile(expected,actual):
 if canonical(expected)!=canonical(actual):raise ValueError('RECOVERY_DATABASE_PROFILE_MISMATCH')

def role_fingerprint(c):
 roles=c.execute('SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolconnlimit,rolvaliduntil,rolbypassrls FROM pg_roles ORDER BY rolname').fetchall()
 memberships=c.execute('SELECT roleid::regrole::text,member::regrole::text,grantor::regrole::text,admin_option,inherit_option,set_option FROM pg_auth_members ORDER BY 1,2,3').fetchall()
 defaults=c.execute('SELECT setrole::regrole::text,setconfig FROM pg_db_role_setting WHERE setdatabase=0 ORDER BY setrole').fetchall()
 return hashlib.sha256(json.dumps([roles,memberships,defaults],sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()

def create_target(name,profile):
 guard_target(name);validate(profile);created=False
 try:
  with connection(autocommit=True) as c:
   # Superuser pg_restore can connect; other logins are held out until profile restore.
   c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0 ENCODING {} LOCALE_PROVIDER libc LC_COLLATE {} LC_CTYPE {} CONNECTION LIMIT 0').format(sql.Identifier(name),sql.Literal(profile['encoding']),sql.Literal(profile['lc_collate']),sql.Literal(profile['lc_ctype'])));created=True
   oid=c.execute('SELECT oid FROM pg_database WHERE datname=%s',(name,)).fetchone()[0]
   c.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
  return oid
 except BaseException:
  if created:
   with connection(autocommit=True) as c:c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
  raise

def apply(source,destination,expected_oid,profile):
 require_dev_database(source);guard_target(destination);validate(profile)
 if source==destination or type(expected_oid) is not int:raise ValueError('RECOVERY_TARGET_IDENTITY_MISMATCH')
 with connection() as c:
  if c.execute('SELECT current_user,session_user').fetchone()!=('postgres','postgres'):raise ValueError('RECOVERY_ADMIN_REQUIRED')
  found=c.execute('SELECT oid FROM pg_database WHERE datname=%s',(destination,)).fetchone()
  if found!=(expected_oid,):raise ValueError('RECOVERY_TARGET_IDENTITY_MISMATCH')
  roles={r[0] for r in c.execute('SELECT rolname FROM pg_roles')}
  required={profile['owner']}|{r['grantee'] for r in profile['acl'] if r['grantee']!='PUBLIC'}|{r['role'] for r in profile['settings'] if r['role']}
  if not required<=roles:raise ValueError('RECOVERY_ROLE_PREREQUISITE_MISSING')
  c.execute(sql.SQL('ALTER DATABASE {} OWNER TO {}').format(sql.Identifier(destination),sql.Identifier(profile['owner'])))
  grantees={r[0] for r in c.execute("SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END FROM pg_database d CROSS JOIN LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a WHERE d.oid=%s",(expected_oid,))}|{'PUBLIC',profile['owner']}
  if not grantees<=ROLES|{'PUBLIC'}:raise ValueError('RECOVERY_UNEXPECTED_TARGET_GRANTEE')
  for role in sorted(grantees):
   c.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM {} CASCADE').format(sql.Identifier(destination),sql.SQL('PUBLIC') if role=='PUBLIC' else sql.Identifier(role)))
  c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(profile['owner'])))
  for row in profile['acl']:
   c.execute(sql.SQL('GRANT {} ON DATABASE {} TO {}{}').format(sql.SQL(row['privilege']),sql.Identifier(destination),sql.SQL('PUBLIC') if row['grantee']=='PUBLIC' else sql.Identifier(row['grantee']),sql.SQL(' WITH GRANT OPTION') if row['grantable'] else sql.SQL('')))
  c.execute('RESET ROLE')
  c.execute(sql.SQL('ALTER DATABASE {} RESET ALL').format(sql.Identifier(destination)))
  scoped_roles=[r[0] for r in c.execute('SELECT r.rolname FROM pg_db_role_setting s JOIN pg_roles r ON r.oid=s.setrole WHERE s.setdatabase=%s',(expected_oid,))]
  if not set(scoped_roles)<=ROLES:raise ValueError('RECOVERY_UNEXPECTED_TARGET_SETTING_ROLE')
  for role in scoped_roles:c.execute(sql.SQL('ALTER ROLE {} IN DATABASE {} RESET ALL').format(sql.Identifier(role),sql.Identifier(destination)))
  for row in profile['settings']:
   # Preserve list-valued GUC syntax; a single SQL string literal changes search_path.
   c.execute('SELECT pg_catalog.set_config(%s,%s,true)',(row['name'],row['value']))
   if row['role'] is None:command=sql.SQL('ALTER DATABASE {} SET {} FROM CURRENT').format(sql.Identifier(destination),sql.Identifier(row['name']))
   else:command=sql.SQL('ALTER ROLE {} IN DATABASE {} SET {} FROM CURRENT').format(sql.Identifier(row['role']),sql.Identifier(destination),sql.Identifier(row['name']))
   c.execute(command)
  c.execute(sql.SQL('ALTER DATABASE {} CONNECTION LIMIT {}').format(sql.Identifier(destination),sql.Literal(profile['connection_limit'])))
 with connection(database=destination) as c:assert_profile(profile,capture(c))
 return {'owner_acl_grantors_and_grant_options_match':True,'database_and_role_in_database_settings_match':True,'attributes_connection_limit_and_locale_match':True,'initial_non_superuser_connections_blocked':True}
