# Database security and configuration recovery

The same-host recovery drill now reconstructs database-level access and supported
session defaults, then verifies them separately from schema-object/data equality.
It never restores with `pg_restore --create`, never targets development for a
restore, and never modifies global roles, passwords, memberships or role defaults.

The existing command is unchanged:

```sh
PYTHONPATH=src:. .venv/bin/python -m tools.verify_recovery \
  --output /private/tmp/ledgervigil-new-private-database-drill
```

Optional application-manifest/assets flags still enable source/public-asset and
fresh-process proof acceptance. Use the current reviewed source manifest.

The source's effective database ACL is captured (including default ACL semantics
when datacl is NULL), preserving owner, grantor, grantee, privilege and grant option.
A guarded fresh target is created from template0 with matching UTF8/libc locale and
connection limit zero. PUBLIC database privileges are revoked before archive
restoration. Ordinary logins cannot connect during the restore; the existing local
superuser is the only restoration actor. After schema/data restoration, one
transaction restores ownership, exact ACLs, supported database-wide defaults,
role-in-database defaults and the source connection limit. Schema namespace
ownership/ACLs and default privileges now also participate in semantic comparison.

Targets must match the disposable name guard and the OID recorded at creation;
source=target and stale OIDs fail before metadata changes. All SQL identifiers are
quoted; settings values use bound set_config in the restoration transaction and
SET FROM CURRENT to preserve list-valued GUC syntax. Only ALTER ROLE ... IN
DATABASE target is used. No global ALTER ROLE settings or ALTER SYSTEM is issued.

Fresh connections verify catalog equivalence and effective role-specific settings
in tests. Source profile and global role/membership/global-default fingerprints are
rechecked before PASS. The profile is also rechecked after application acceptance.
Data, ledger replay, role/RLS security and finally cleanup remain mandatory.
DATABASE_PROFILE.json and REPORT.json are mode 0600 under a new mode-0700 directory.
A failed check, unsupported source or cleanup failure cannot produce final PASS.

The bounded profile supports the existing seven local roles (postgres and six lv
roles), PUBLIC, owner-issued database CREATE/CONNECT/TEMPORARY grants with grant
options, non-template connectable UTF8/libc databases in pg_default, matching locale
and collation version, and the following scoped defaults: statement_timeout,
lock_timeout, idle_in_transaction_session_timeout, idle_session_timeout,
default_transaction_isolation, default_transaction_read_only, timezone, datestyle,
client_encoding, search_path, work_mem, maintenance_work_mem and temp_file_limit.

Delegated grant chains, unknown grantees/settings, preload libraries, other locale
providers/encodings/tablespaces, templates and disabled connections fail closed.
They require reviewed support, not silent omission. Cluster role attributes/global
role defaults, passwords, authentication rules, extensions/infrastructure and
cluster-wide settings remain existing same-host prerequisites. Exact representation
is required; semantically different settings must never be normalized away.

This closes the previous database-ACL/settings gap for this supported local
profile. It does not establish a separate-host restore, offsite resilience,
production RPO/RTO or independent accounting approval. Accounting stays UNREVIEWED;
provider/BOOKS/production gates and proposed decisions remain unchanged. DR-1, this
bounded recovery profile, is PROPOSED.

Primary references used to implement the exact SQL behavior:
[ALTER DATABASE](https://www.postgresql.org/docs/18/sql-alterdatabase.html),
[ALTER ROLE](https://www.postgresql.org/docs/18/sql-alterrole.html), and
[GRANT](https://www.postgresql.org/docs/18/sql-grant.html).
