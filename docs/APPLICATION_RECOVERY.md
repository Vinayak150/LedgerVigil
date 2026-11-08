# Same-host application recovery acceptance

This extends the existing logical database drill; it does not replace its single
exported READ ONLY snapshot, schema/ownership/grant/RLS comparisons, independent
ledger replay or guarded disposable restore. It changes no application route,
financial writer, migration, grant or broker task.

From app/, using a reviewed source manifest with `path`, `bytes` and `sha256`
for every source file:

```sh
PYTHONPATH=src:. .venv/bin/python -m tools.verify_recovery \
  --output /private/tmp/ledgervigil-new-private-application-recovery \
  --application-manifest /absolute/review/PACKAGE/INTEGRATED_BASELINE.json \
  --assets /Users/vinayakmahindrakar/Desktop/LedgerVigil/data/public/uci-online-retail-ii
```

Output must not exist. The parent must already exist. The entire directory is
private (0700); database.dump, copied source, proof JSON and private reports are
0600 (owned executable source scripts retain owner execution as 0700). The sealed public index and original workbook copies are read-only (0400).
Do not publish this directory: the database contains sessions and private
financial text. Review packages contain redacted reports, never this backup.
The source manifest is a reviewed input, not a digital signature.

The application extension captures the source against that inventory, refuses
private/generated paths, symlinks, duplicates and unsafe relative paths, then
copies the published index, original workbook and provenance. It requires the
source migration head to match the snapshot and preserves owner execute bits
without retaining group/world access. The index manifest
and database are checked against the source's published hash; the workbook is
checked against the existing public-data hash. It verifies index counts and
sampled group/row bytes. Source, assets and immutable execution proofs are captured
while the database snapshot is open. Every eligible native receipt (maximum 25)
is exported and independently verified; overflow fails closed. Zero receipts is
reported as zero, not presented as an exercised execution restore.

After restoring the database, a fresh Python process loads the copied application
source and its default relative asset path. It verifies source and asset hashes,
public index queries, byte-identical execution exports using the restricted
lv_app role inside READ ONLY transactions, independent proof verification,
durable work/outbox inventory and restricted-worker health. It probes API health
and unauthenticated refusal. It checks all restored table fingerprints and schema
again after acceptance; successful acceptance cannot quietly modify the snapshot.
The disposable database is removed before the final PASS report is written.
Failure or cleanup failure produces no final PASS report; private partial files
remain for diagnosis. Never treat application/ACCEPTANCE.json alone as final PASS.

The bundle records Python/package versions and retains backend/frontend lockfiles.
It does not install dependencies, rebuild the frontend, start persistent services,
or restore credentials, role passwords, extensions, Redis or host configuration.
The same-host cluster roles and the current private .env/.env.roles remain
prerequisites. A fresh cursor key invalidates old pagination cursors. Existing
sessions survive in the logical snapshot; this rehearsal does not define a
production session-revocation policy. Store and protect backups accordingly.
The archived source-plan index supports current inspection; its original plan
NDJSON build stream is not bundled, so index regeneration from that stream is
not claimed. The workbook is preserved for independent re-derivation work.

Broker recovery is a separate acceptance exercise: start an isolated empty Redis,
point a worker only at a disposable restored database, and let the durable
PostgreSQL scheduler republish eligible identifiers. Preserve lease expiry,
fencing/backoff and singleton ownership. Do not replay completion notifications:
the native completion consumer remains unimplemented. Never use development
Redis for a recovery experiment. Authenticated HTTP/worker exercises intentionally
mutate only a separately restored disposable copy after snapshot equality checks;
they are reported separately from the generic read-only acceptance.

Evidence from this drill establishes same-host logical recoverability with the
installed dependencies, not a fresh-machine installation, offsite resilience,
production RPO/RTO, external authenticity or independent accounting sign-off.
Exports remain unsigned and accounting remains UNREVIEWED. Provider/BOOKS and
production write gates and every proposed decision remain unchanged. AR-1, the
bounded application recovery acceptance profile, is PROPOSED.

PostgreSQL 18 flattens the exact nested AND form in the 1..4000 non-whitespace
checks for comment body and override reason during restore. Catalog comparison
normalizes only those two complete equivalent strings. Different bounds, boolean
operators, column names and whitespace predicates remain detectable.

The database foundation now includes the bounded
[database security/configuration restore](DATABASE_RECOVERY.md): owner, exact ACLs,
connection limit and supported scoped defaults are restored before acceptance.
Namespace ownership/grants and default privileges are compared as well. Global
roles/credentials/host configuration remain prerequisites, not restored assets.
