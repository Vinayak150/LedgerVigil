# Offline invoice admission plans

The existing real-data evaluator can now produce an inspectable, immutable plan
for every source invoice group. This is a review artifact under
`lv-invoice-admission-plan/1`, not an authenticated source-import API, native
staged_document, financial invoice, accounting approval or execution capability.
It makes the unresolved input questions visible before a future importer uses them.
Accepted Stage/ADR documents and DATA-1/DATA-2 statuses remain unchanged.

## Create and inspect

From app/, with existing local PostgreSQL and private role configuration:

```sh
PYTHONPATH=src:. .venv/bin/python -m tools.evaluate_retail \
  --input ../data/public/uci-online-retail-ii/online_retail_II.xlsx \
  --sha256 bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980 \
  --output /private/tmp/ledgervigil-new-invoice-plan --admission-plan
PYTHONPATH=src:. .venv/bin/python -m tools.inspect_invoice_plan \
  --plan /private/tmp/ledgervigil-new-invoice-plan/admission-plan \
  --invoice 489434 --lines 10
```

Use a new output directory. Optional `--manifest-sha256` anchors inspection to a
trusted review package's manifest hash. Hash integrity is not digital signing.
The inspector verifies the entire compressed artifact and source coverage before
returning a selected group. The output includes source coordinates, original
lexemes, row hashes, reasons and exact amount only when the whole group qualifies.
No passwords, provider calls or writable financial transaction are involved.

## Conservative whole-invoice rules

The source identity is `(source workbook hash, raw invoice id)`. Missing ids remain
separate orphan observations, keyed by their sheet and row; they are never merged
into an invented invoice. Repeated invoice ids with different customers, countries
or raw timestamps are held as one unresolved group. This is a proposed import
interpretation, not an approved economic identity/duplicate resolution rule.

- Every source line and its original payload/hash is retained.
- A bad, unsupported or incomplete line holds the entire invoice.
- Non-exact-penny prices, including near-penny representations, remain unrounded.
- Repeated-content lines and cross-sheet invoice ids hold the group. No row is
  deleted or counted as a proven duplicate economic event.
- Different raw timestamps hold the group even if their calendar dates match.
- Full-invoice totals must fit SP-1 money bounds. A held group has no partial total.
- A clean group is CANDIDATE_REVIEW. Every group has posting_eligibility UNAVAILABLE
  and accounting_status UNREVIEWED. No VALID/READY/APPROVED state is created.

These engineering safeguards allow investigation while DATA-1 normalization and
DATA-2 source identity/overlap remain PROPOSED. Tax, discounts, shipping semantics,
party mapping, income-account mapping and evidence admission remain unresolved;
CANDIDATE_REVIEW never asserts that these requirements have been satisfied.

## Integrity and atomic export

The exporter reads one REPEATABLE READ, READ ONLY PostgreSQL snapshot. It checks
all retained payload hashes, complete source row count/chain, and the stored line
assessments before constructing the plan. Existing observations are not modified.

Each group has a deterministic id, canonical payload hash and complete raw row
references. The artifact contains attribution, dataset classification, proposed
policy statuses, complete counts and a group hash chain. Gzip output has a fixed
mtime and no path metadata, so the same snapshot produces identical bytes.

An exclusively created destination cannot replace an existing review. Its manifest
is published last after the data file is flushed. A failed export removes only
its own incomplete directory. Budgets bound group size, record bytes, total decoded
bytes and manifest bytes; oversized groups fail rather than being split silently.

The separate file-readback pass recomputes group rules and rational totals, hashes
the compressed bytes as consumed, and uses a temporary SQLite uniqueness index to
verify every physical source row appears exactly once. Its ordered source chain
must equal the retained original chain. It shares the group-rule implementation
with the exporter; it is not an independent accounting oracle. Tests additionally
check known outcomes and deliberately broken safeguards. Trusted manifest hashes
are needed to establish artifact provenance, not just internal consistency.

## Practical evidence and remaining product integration

The full original workbook yields 53,628 groups covering all 1,067,371 source rows:
12,696 CANDIDATE_REVIEW and 40,932 HOLD. The added raw-timestamp check finds 83
conflicting groups. Counts of hold reasons overlap. They are neither financial
accuracy scores nor a judgment that every held source observation is erroneous.

All 52 native tables remain unchanged after evaluation scope setup, including
financial facts and sequence. Temporary PostgreSQL/SQLite state is removed. See
the dated review package for the actual installed-command report, manifest,
inspection example and test logs. The public workbook has no settlement ground
truth, so settlement/fraud accuracy remains unmeasured.

The next product milestone is persistent source-import registration and
session-authorized inspection of raw rows and these holds, under the existing
source.import/read permissions. That work needs its own migration, audit/tenant
boundaries and owning OpenAPI update. This artifact does not bypass those steps or
authorize PostStagedDocuments, review decisions, approval, overrides or execution.
