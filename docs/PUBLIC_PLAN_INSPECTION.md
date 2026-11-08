# Indexed public source inspection — SR-2 PROPOSED

This additive read API exposes the registered, pinned UCI Online Retail II observation plan. It does not admit financial evidence, stage financial documents, resolve holds or authorize posting. SR-1, SR-2, DATA-1/DATA-2, RC-1 and earlier proposed decisions keep their status. Accounting remains UNREVIEWED.

## Read contract

All paths start with `/api/v1/source-plan-registrations/{registration_id}`:

| Suffix | Result | Parameters |
| --- | --- | --- |
| `/groups` | Ordered invoice groups, reasons, nullable amounts, original payload hash | `status=HOLD` or `CANDIDATE_REVIEW`, `limit`, `cursor` |
| `/groups/{group_id}` | One group; no eligibility inference | None |
| `/groups/{group_id}/rows` | Exact source values, sheet, row number and row hash | `limit`, `cursor` |

Verified session and `read` permission apply on every request. A read-only PostgreSQL transaction resolves the tenant registration under FORCE RLS before opening the artifact. Foreign and missing registrations return the same 404. Revocation is checked even when the artifact reader is cached. Authentication and authorization precede conditional ETag responses.

Limits are 1–200, default inherited from the existing read API. Unknown and repeated parameters are refused. Signed cursors bind tenant, registration, plan manifest, operation, ordering, status or group. Pages resume by immutable ordinal, with UUID tie identifiers compatible with the existing cursor codec. The source payload budget is 1 MiB per page, allowing at least one record; it is not a bound on the full JSON envelope. A missing or changed artifact returns 503 `SOURCE_INDEX_UNAVAILABLE`, never a fictitious empty dataset.

## Derived storage and trust boundary

PostgreSQL remains the ledger and authorization authority. The SQLite file contains only the already public, common observation plan; it contains no tenant registrations or private customer uploads. Reusing this common index for private uploads is outside this design.

The offline builder verifies the pinned plan, then rechecks the exact decompressed stream against the manifest while indexing it. It retains 53,628 groups and all 1,067,371 rows, including repeated content. Raw row JSON is losslessly compressed per row; numeric text never passes through float conversion. Publication refuses an existing output directory and publishes the manifest last. Source and index hashes remain different provenance layers.

The pinned catalog identifies the index manifest and database SHA-256. On first process load the reader hashes the whole database once; subsequent requests check device/inode/size/mtime/read-only bits and open SQLite read-only with immutable mode. Every connection closes after its query. The publisher and local filesystem owner are trusted. Read-only mode is not a digital signature, root-resistant immutability, off-host backup or tamper-proof accounting evidence. Replacing an index requires offline verification, a new pin and process restart; do not modify an in-use immutable SQLite file.

Filtered group paging uses the `(status, ordinal, page_id)` range index. Row paging uses `(group_ordinal, ordinal)`; group identity uses a unique index. The 334,340,096-byte database is rebuildable and kept outside the source review package.

## Operator verification

Default publication: `~/Desktop/LedgerVigil/data/public/uci-online-retail-ii/index-v1/`.

Rebuild into a fresh directory using the existing sealed admission plan:

```sh
PYTHONPATH=src .venv/bin/python -m tools.build_plan_index --plan /absolute/path/to/admission-plan --output /absolute/path/to/new-index
```

The builder requires the pinned public plan and deliberately rejects other manifests. Compare the output manifest and database hashes to `index_publication.py` and the sealed package before publication. Rebuilding with a different SQLite version may produce a different physical file; identical logical input is insufficient to reuse an old physical pin.

Tests use `LV_PUBLIC_INDEX` when supplied, otherwise the work copy or installed default. Run on real PostgreSQL:

```sh
PYTHONPATH=src:.:tests .venv/bin/python -m pytest -q tests/test_plan_inspection.py tests/test_source_registry.py
```

The package records the real-data HTTP demo, query plans, full-suite result and three assertion-based safeguard probes. The index verifier and plan builder share group rules; these are not independent accounting oracles or an accuracy benchmark. Invoice 489435 remains HOLD with no amount; its original `2.5499999999999998` price remains text. CANDIDATE_REVIEW does not mean approved, correct, executable or postable.

## Frontend handoff

Use the tenant's existing source-plan registration ID and generated API types. Show both statuses explicitly, reasons and original source references. Cancel pending reads and clear selection/cursors on tenant or registration change. Keep held amounts absent and label the source as public observational data; never convert raw numeric strings to monetary amounts. Render values as text, not HTML. Support loading, empty, permission, expired session and unavailable-index states. No frontend inspection screen was implemented in this backend slice.
