# Operations runbook (deployment validation)

Prepared, not exercised on a deployed host. Each section says what has been verified and where.

## Pre-flight (before every start or upgrade)

1. `docker compose exec api python -m tools.deploy_entry readiness` reports `READY`: restricted logins, migration head
   `0037_restore_interlock`, restore interlock `BOUND`, anchor directory present, gates as intended.
2. Gates: `LV_DETECTION_ENABLED` and `LV_PROVIDER_SANDBOX_ENABLED` unset unless a validation session needs them.
3. Logs: confirm that no proxy, load balancer or hosting layer in front of Caddy logs query strings for
   `/connections/quickbooks/callback`. Caddy, the frontend server and the Python processes already redact.
4. Backups: last backup is less than 24 hours old and its last restore drill passed (below).
5. Images are pinned by digest.

## Start, stop and graceful shutdown

- Start order: `db`, `broker`, then `migrate` (only when upgrading), then `api web maintenance scheduler proxy`
  (and `provider-worker` with `--profile provider-sandbox`).
- Stop: `docker compose stop scheduler` first (it stops relaying work), then the workers (their `stop_grace_period` lets
  Celery finish the current task; unfinished jobs keep their PostgreSQL lease and are re-queued by fencing when it expires),
  then `api web proxy`, then `broker db`.
- The scheduler takes a PostgreSQL advisory lock: a second scheduler exits instead of double-relaying.
- Dead jobs: the scheduler logs `maintenance DEAD jobs require operator attention: count=N`. Inspect with
  `python -m tools.run_maintenance status` (local) or the `lv.maintenance_health()` function. QuickBooks revocations that could
  not reach Intuit stay pending and retry hourly (connection shows `REVOCATION_PENDING`).

## Backups

Proposed policy (no owning RPO/RTO target exists in Stage 2A-2C; these are proposals, not measured guarantees):
- Daily logical backup (`pg_dump -Fc` of the application database) plus continuous WAL archiving if the host supports it.
  Proposed RPO: 24 h without WAL archiving, 15 min with it. Proposed RTO: 4 h.
- Encrypt every backup at rest with a key held outside the backup store (for example `age` or the storage provider's KMS),
  restrict read access to the operator, and record each backup's SHA-256.
- Keep 35 days. Never delete backups to remove old ciphertext; rotate and destroy keys instead (docs/QUICKBOOKS_SANDBOX.md).
- Measured so far: only workspace and Mac-local drills (`tools.verify_recovery`, `tools.application_recovery`). Their
  timings are in the release package; they are not offsite or production measurements.

## Restore

1. Restore into a **new** database (`tools.verify_recovery` drill flow): export snapshot, restore, compare fingerprints,
   schema digest and independent ledger replay, check anchors. A failed check is never reported as recovered.
2. Older-backup refusal: anchors newer than the restored data make verification fail (tested in
   `tests/test_application_recovery.py`, `tests/test_database_recovery.py`).
3. A restored copy refuses financial writes by every runtime login until its verification is recorded (restore interlock,
   docs/RESTORE_INTERLOCK.md); reads and diagnostics keep working. After the verification report says PASS:
   `python -m tools.deploy_entry bind-restored --report REPORT.json`. Use logical restores into a new database; a physical
   file-level restore keeps the database identity and is not detected.
4. Recovery client: `LV_PG_CLIENT=local LV_PG_BIN=<PostgreSQL 18 bin>` or `LV_PG_CLIENT=docker LV_DB_CONTAINER=<name>`; no
   Mac-specific path is required any more.

## Anchors

Trusted anchors (proof-pack and export anchors) must be stored independently of the database backup, in a location the
database host cannot rewrite (for example a separate Git remote with protected branches, or write-once object storage).
Local Git alone is neither offsite storage nor a signature. No offsite destination has been configured or verified.

## Key rotation (QuickBooks KEK)

See docs/QUICKBOOKS_SANDBOX.md "Key rotation, retention and deletion". Summary: new KEK → worker gets new private key plus
the old one as previous → API gets the new public key → `python -m ledgervigil.provider.worker rotate-kek` until
`remaining: 0` → remove the old private key from the worker → destroy it when the last backup sealed under it expires.
Also rotate: database role passwords (`ALTER ROLE ... PASSWORD`, update the secret file, restart the service), the cursor key
(restart invalidates outstanding list cursors only), and the Intuit client secret (Intuit developer portal, then
`provider_setup` into a new directory).

## Incidents

| Symptom | First action |
|---|---|
| Suspected token or key exposure | Disconnect QuickBooks (revokes at Intuit), rotate the KEK and the Intuit client secret, review `PROVIDER_*` audit events |
| Suspected `lv_app` credential exposure | Rotate the role password; revoke all sessions (`UPDATE` through the identity admin tool); see Q-16 in the completion matrix: a stolen `lv_app` login can mint sessions for verified members |
| Ledger replay or anchor mismatch | Stop writes (stop api and workers), keep the database, run the recovery drill on a copy, do not "fix" history in place |
| Dead jobs accumulating | Check broker and database health; jobs are fenced, so restarting workers is safe |

## Rollback

- Before a migration commits, `install_v3.py` restores the previous source automatically; an interrupted apply leaves
  `MIXED` source and `rollback` restores it (rehearsed in the workspace).
- After a successful upgrade, schema downgrades are refused once detection runs or provider connections exist; roll back by
  restoring the pre-upgrade backup into a new database and repointing the services, after verification.
