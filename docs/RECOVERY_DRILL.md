# Local database recovery drill

Run from app/ using the existing local PostgreSQL container and private role files:

```sh
PYTHONPATH=src:. .venv/bin/python -m tools.verify_recovery \
  --output /private/tmp/ledgervigil-new-private-recovery-drill
```

Use a new directory. The command refuses to overwrite existing output and creates
its directory mode 0700 and database.dump mode 0600. The archive contains sensitive
application/session data: retain it privately; never publish or include it in a
review package. REPORT.json contains counts and fingerprints rather than raw rows.

The source database is read at one exported REPEATABLE READ, READ ONLY snapshot.
pg_dump imports that same snapshot. Only a newly created, guarded disposable
database may receive a restore. Its data, semantic schema catalogs, ownership,
table/function grants, policies, indexes, triggers, enums and constraints are
compared before independent ledger-event replay checks sequence heads and balances.
Runtime logins are checked for superuser/RLS bypass; runtime reads without tenant
context and with a selected tenant must respect isolation. The restored database
is dropped in finally; PASS is written only after successful checks and cleanup.

PostgreSQL 18 can distribute a varchar-array-to-text-array cast over its constant
members during dump/restore. The schema comparison normalizes only uppercase
constant literals in that specific equivalent form. It preserves every allowed
value and operator. Tests confirm changed constraints remain detectable.

This is a same-host logical database recovery rehearsal. Existing cluster roles,
passwords and extensions are prerequisites. It does not restore external workbook/
plan files, Redis, deployment configuration or offsite infrastructure, and it does
not establish production RPO/RTO or complete application acceptance. Recovery of
those components and restoration into a separate host remain required operational
work. All accounting and proposed-decision statuses remain unchanged.

For optional source/public-asset capture and fresh-process backend acceptance,
see [APPLICATION_RECOVERY.md](APPLICATION_RECOVERY.md). The original database-only
command and its security/cleanup checks are preserved.

The supported local database owner/ACL/connection-limit and database/role-in-database
settings are now reconstructed and verified; see [DATABASE_RECOVERY.md](DATABASE_RECOVERY.md).
Namespace ownership/ACLs and default privileges are included in semantic checks.
Unsupported profiles fail explicitly. Cluster roles/global configuration remain
same-host prerequisites.
