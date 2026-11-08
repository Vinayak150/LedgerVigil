# Source read APIs (documents and imports)

Read-only APIs that let a user find the typed payments, staged settlement statements and evidence a `RecordSettlement` draft cites, instead of pasting identifiers. SANDBOX, SYNTHETIC data only. Accounting status: **UNREVIEWED**.

No migration: the runtime role `lv_app` already had `SELECT` on every table read here. Code: `src/ledgervigil/reads/sources.py`. Tests: `tests/test_source_reads.py`.

## Routes

All routes require `read` and run inside the verified-session boundary in a **READ ONLY** transaction (`auth.boundary.require_read_only`).

| Route | Contract | Returns | Query |
|---|---|---|---|
| `GET /api/v1/ledgers/{ledger_id}/documents` | Family 5 | page of `DocumentSummary`, plus `as_of_seq`, `purpose`, `data_label` | `type`, `status`, `party_id`, `limit`, `cursor`; `date_from`, `date_to`, `q`, `as_of_seq` are refused (SG-1) |
| `GET /api/v1/documents/{document_id}` | Family 5 | `Document`: current version, open amount, settlement links, postings summary | none |
| `GET /api/v1/imports` | Family 3 | page of `ImportBatch` | `ledger_id` (PC-S1), `limit`, `cursor` |
| `GET /api/v1/imports/{import_id}` | Family 3 | `ImportBatch` | none |
| `GET /api/v1/imports/{import_id}/records` | Family 3 | page of `SourceRecord` | `status` = `VALID` or `INVALID`, `limit`, `cursor` |
| `GET /api/v1/imports/{import_id}/staged-documents` | Family 3 | page of `StagedDocument` with its items | `limit`, `cursor` |

**Isolation.**
- The tenant comes only from the session, and FORCE row-level security is the tenant filter.
- Another tenant's id behaves exactly like an absent id: 404 `NOT_FOUND` with an identical body. This holds in paths and in the `ledger_id` filter.
- Every join between ingestion, document and projection rows uses `(tenant_id, ledger_id, id)`, so no reference crosses ledgers.

**Lists.**
- Unknown parameters are 400; a repeated parameter is 422.
- `limit` is 1–200 (default 50).
- Cursors are opaque and HMAC-signed. Each is bound to the operation, tenant, ledger or batch, filters and order. Any cursor that fails verification is 400 `CURSOR_MISMATCH`.

**Money.** Amounts are exact decimal strings at the ledger's currency exponent, produced by the kernel's `Money` (for example `"2507.5"` stored becomes `"2507.50"`). A stored value the kernel refuses is a 500. Nothing is rounded.

## Where each id goes in a draft

| Draft field | Take it from |
|---|---|
| `operations[].statement_document_id` | `StagedDocument.id` |
| `operations[].processor_ref`, `payout_id`, `payout_date`, `gross`, `fee`, `net` | the same `StagedDocument` (values as stated by the source) |
| `operations[].items[].document_id` | `DocumentSummary.id` of a `PAYMENT` |
| `operations[].items[].amount` | the payment's `amount` (SP-1 settles each item at its full amount) |
| `evidence_refs[]` | any `evidence_ref` / `source_record_evidence_ref`, sending **only** `{kind, id}`; `provenance` and `sha256` are read-only and the proposal API refuses them |

Statement items carry the processor's item id as `item_ref`; payments carry it as `processor_item_id`. The API does not pair them (SG-2). The proposal API remains the authority that accepts or refuses every reference.

## Implementation choices awaiting confirmation

| ID | Choice | Reason |
|---|---|---|
| PC-S1 | `GET /imports?ledger_id=` filter | The Family 3 table lists no filter, but a selection control needs the ledger's batches. Additive, equality only, and a 404 for absent or other-tenant ledgers (CT-04) |
| PC-S2 | Document `ETag` is a representation hash (`"r…"`), not `"v<version>"` | The representation includes rows that can change without a document version (party name, settlement link, ledger label). `version` is still returned. No mutation accepts it as `If-Match` |
| PC-S3 | Order: documents and imports newest first by UUIDv7 `id`; records by `record_index, id`; staged documents by `id` | Part 2 requires an allow-listed field plus `id`; the contract names none |
| PC-S4 | `SourceRecord.validation_status` comes from its batch: `STAGED` gives `VALID`; `REJECTED` gives `INVALID`, with the batch's failure code and detail as the message | No per-record status column exists. A `DUPLICATE` batch stores no record |
| PC-S5 | Page budgets: 1,000 statement items per staged-documents page and 1 MiB of record content per records page. A page always holds at least one row and may hold fewer than `limit` | Bounds response size. One batch currently holds one statement and one record |
| PC-S6 | `MANUAL_JOURNAL` documents are listed, with `status`, `amount` and `party` null | They exist in `lv.document`; hiding them would misstate the ledger |
| PC-S7 | `type=INVOICE`, `CREDIT_NOTE` or `REFUND` is 422 `CAPABILITY_UNAVAILABLE`, not an empty page | Those document families have no native records; an empty page would read as "none exist" |
| PC-S8 | `evidence_ref` of a document is its **current** version | `/documents/{id}/versions` is not built |
| PC-S9 | The documents page states `as_of_seq`: the ledger counter read in the same statement as the rows | Part 2 snapshot convention. A later page may be read at a later sequence |

## Gaps (stated, not resolved)

| ID | Gap | Current behaviour |
|---|---|---|
| SG-1 | `date_from`/`date_to` (which date, inclusive or not), `q` (fields, matching) and `as_of_seq` on documents (historical status semantics) are undecided | Well-formed values are refused with 422 `CAPABILITY_UNAVAILABLE`; malformed values with 422 `SCHEMA_INVALID`. Never ignored |
| SG-2 | No eligibility, matching or reconciliation: nothing decides which payment may settle in which statement, and there is no `processor_ref` filter (not contracted) | Actual statuses (`RECEIVED`, `SETTLED`, `RECORDED`) are shown. Pairing `item_ref` with `processor_item_id` is the user's choice, and the proposal API validates the draft |
| SG-3 | The Part 7 import lifecycle (`RECEIVED`, `VALIDATED`, `RECONCILED`, `MISMATCH`, `DISCARDED`), reconciliation results and `allowed_actions` on `ImportBatch` | The stored outcome (`STAGED`, `REJECTED`, `DUPLICATE`) is shown. No reconcile or discard exists |
| SG-4 | `StagedDocument.customer` and `posted_by_proposal_id` | Omitted: statements have no customer, and nothing posts staged documents. Duplicate hints are reported as counts of related batches |
| SG-5 | `/documents/{id}/versions`, `/relationships`, `/ledgers/{id}/parties` and journal-entry reads | Not built |
| SG-6 | Prefixed identifiers (`doc_…`) | Bare UUIDs, as RG-2 and PG-5 |
| SG-7 | The draft API accepts a `SETTLED` payment as an item | Unchanged here. Must be decided with the submission and read-set contract, before submission is built |
| SG-8 | `ATTACHMENT` evidence and `EVIDENCE_FILE` imports | Not available |
| SG-9 | Imports have no upload API; batches come only from the restricted `lv_ingest` path | Unchanged (no uploads in this milestone) |
