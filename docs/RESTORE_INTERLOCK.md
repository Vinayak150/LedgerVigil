# Restore interlock (migration 0037)

**Owning requirement:** Stage 2C §4.3 condition 5 (recovery verification before writes resume); previously listed as "no
restore interlock" in the recovery reviews and as release blocker OP-15.

**Mechanism.** `lv.database_binding` records the PostgreSQL database (its `pg_database` oid) that financial writes belong to;
migration 0037 binds the database it runs in. A logical restore (`pg_dump`/`pg_restore`) always creates a new database with a
new oid, so a restored copy carries a binding that names another database. Statement-level triggers on the ledger, posting,
balance, settlement, period, account, ledger, approval, override and execution tables then refuse every write made through a
runtime login (`lv_app`, `lv_worker`, `lv_seed`, `lv_ingest`) with SQLSTATE `LV503`; the API answers 503
`RECOVERY_VERIFICATION_REQUIRED`. Reads, exports, diagnostics and `lv.recovery_state()` keep working. Migrations run as
`lv_migrator` and are not blocked, so a restored copy can be upgraded and verified.

**Resuming writes.** Only after the recovery verification passes for this database:

```
python -m tools.deploy_entry bind-restored --report <verification REPORT.json>
```

It refuses a report whose status is not PASS, and records the report's SHA-256 in `lv.database_binding` and the append-only
`lv.database_binding_history` (owner-only function `lv.recovery_bind`; no runtime grant). `deploy_entry readiness` reports
`NOT_READY` while the interlock is engaged. Every recovery drill now also checks that the restored copy reports
`RESTORED_UNVERIFIED` (`tools/verify_recovery.py`, report field `restore_interlock`).

**Limits (stated, not hidden).** A physical restore (file-level copy or base backup of the whole cluster) keeps the database
oid and is **not** detected; the runbook requires logical restores into a new database. A superuser can bypass any trigger.
The interlock does not decide whether a backup is current; the anchor checks do that.

**Tests:** `tests/test_restore_interlock.py` (3 tests: seed refused then allowed after binding, owner-only binding with a
report digest, API 503 with nothing written). Workspace only.


### 0038 candidate

The logical binding compares database name, OID and PostgreSQL system
identifier. A 0037 binding that predates the 0038 migration transaction has
no trustworthy cluster identity and requires verification. Physical restores
can preserve all three values. Keep services stopped, restore logically into
a fresh database, and bind the exact verified snapshot using the operator
procedure in deploy/RUNBOOK.md. Same-host and fresh-cluster drills are required
before claiming this behavior as verified.
