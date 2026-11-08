# Practical evaluation with public retail transactions

The offline command `tools.evaluate_retail` reads the checksum-pinned, original
UCI Online Retail II workbook. This implements the first real-data evaluation
track under ADR-021 and ADR-022, alongside the existing controlled regression and
provider-sandbox tracks. It is an ingestion/compatibility evaluation, not a
settlement accuracy benchmark or a claim of public HTTP invoice import support.

Source: Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository.
https://doi.org/10.24432/C5CG6D. CC BY 4.0. Currency GBP. Dataset source, archive and
workbook checksums, retrieval time and transformations are in
`datasets/UCI_ONLINE_RETAIL_II.json`.

## Running it

Keep original bytes under `data/public/uci-online-retail-ii/online_retail_II.xlsx`
in the project root, outside app/. From app/ with existing local PostgreSQL and
private role files:

```sh
PYTHONPATH=src:. .venv/bin/python -m tools.evaluate_retail \
  --input ../data/public/uci-online-retail-ii/online_retail_II.xlsx \
  --sha256 bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980 \
  --output /private/tmp/ledgervigil-public-data-new-run
```

Use a new output directory for a new run. The command refuses to replace an
existing report. It creates only a disposable lv_migration_test_* database on
localhost:55432 using the existing development helper. It drops that database in
finally, including on a refused evaluation. It never migrates the shared
development database or starts a web route/worker.

A native GBP SANDBOX ledger identifies the evaluation scope. Real source rows are
classified PUBLIC_OBSERVATIONAL in an owner-only `lv_eval` staging schema, never
relabeled SYNTHETIC or SOURCE-trusted evidence. Controlled safeguard fixtures are
explicitly SYNTHETIC_TEST. Runtime app/seed/ingest/worker roles cannot read this
schema. No financial document, journal entry, payment or settlement is produced.
The source workbook is read-only; no spreadsheet formulas/macros are executed.

## What is measured

The reader preserves numeric XML lexemes, sheet and physical row coordinates,
content hashes, missing values and repeated lines. It refuses unsupported sheet/
column layouts and external relationships. Formulas and incompatible rows remain
quarantined, not interpreted from cached results. Money conversion reuses the
existing kernel and compares each supported line with an independent Fraction
oracle and PostgreSQL NUMERIC. Parsing is bounded and streaming.

All 1,067,371 source lines were retained across both sheets. The measured profile
classified 726,672 positive invoice-line candidates and retained 340,699 other
lines in quarantine. A candidate is not an approved/validated invoice. Any bad
line or inconsistent customer/date/country holds its entire invoice group. No
customer id or cancellation-to-original mapping is invented. Reason counts can
overlap and must not be summed as exclusive categories.

Observed data includes 243,007 lines with missing customer ids, 19,494 cancellation
lines, and 34,335 additional repeated-content lines, all retained. There are
1,088 invoice ids present in both sheets; none is silently deduplicated or posted.
Repeated content is not automatically a duplicate economic event.

## Precision finding and open decisions

112,897 stored numeric prices are not exact whole pennies. Of these, 112,879 are
within 0.000000001 penny of a whole penny and 18 differ by more. This threshold is
only a diagnostic. Near-penny values may reflect Excel representation artifacts;
the source alone does not establish the intended accounting amount. All raw
values are preserved and the existing exact-money gate remains enforced.

- DATA-1 PROPOSED: define and independently review an explicit normalization rule
  for Excel representations before building invoice source admission. No automatic
  rounding has been approved or enabled.
- DATA-2 PROPOSED: define source snapshot/duplicate identity handling across the
  two overlapping sheets before importing economic events. No line is deleted
  merely because its values repeat.

These are new proposed data-import questions; they do not change accepted ADRs,
D-1–D-11, PA-011-1, accounting UNREVIEWED status, or RC-1's status.

## Reliability evidence and limits

The full-data run injects an interruption after 10,000 rows and verifies that zero
staging rows/runs survive. A retry imports all rows; another same-file retry
returns the same stored result without reparsing. Every retained payload hash is
read back and the ordered row-chain hash must equal the source traversal. All 52
native tables are fingerprinted after scope setup and remain unchanged.

954,474 supported exact money calculations agreed with independent rational and
PostgreSQL arithmetic; unsupported/sub-penny rows are not counted as successes.
Reports include measured duration, machine/runtime details, memory, query plan,
implementation hashes and all denominators. Runtime is a point-in-time local
measurement, excluding download, database setup and the preliminary rollback test;
it is not a production SLA. Source authenticity is bounded by the published UCI
source and the pinned retrieved artifact, not external accounting review.

Retail invoice lines have no processor fees, bank deposits, payment allocations
or matched settlement truth. Settlement/fraud accuracy and customer business
impact remain unmeasured. Native invoice ingestion, source-evidence admission,
accountant review and real provider experiments remain separate work. Existing
synthetic fault tests still matter for states the public data cannot label.

Reviewer rejection remains unimplemented: the accepted lifecycle permits rejection
from READY_FOR_REVIEW, while the currently reachable synthetic-evidence flow is
BLOCKED. This evaluation does not manufacture readiness or bypass that gate.

## Invoice-level review artifact

Add `--admission-plan` to export complete invoice groups with explicit holds and
source references. The plan has stricter group-level duplicate/timestamp guards
than the evaluation summary above and remains non-posting. See
[INVOICE_ADMISSION.md](INVOICE_ADMISSION.md) for the inspector and scope.
