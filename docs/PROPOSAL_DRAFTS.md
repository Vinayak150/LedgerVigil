# Proposal drafts and revision history (migration 0010) — backend reference

**Status:** DRAFT proposals only, on native SANDBOX ledgers, with `RecordSettlement` operations only. SYNTHETIC data only. Accounting status: **UNREVIEWED**. Nothing here submits, simulates, approves or executes a proposal, and no draft changes the ledger.

## Endpoints (Stage 2B Family 8, draft subset)

| Operation | Route | Permission | Behaviour |
|---|---|---|---|
| `createProposal` | `POST /api/v1/proposals` | `propose` | `Idempotency-Key` is required. Returns 201 `Proposal` (DRAFT, revision 1, version 1), `ETag: "v1"` and `Location` |
| `listProposals` | `GET /api/v1/proposals` | `read` | `?ledger_id&state&created_by&reviewable_by_me&limit&cursor`, newest first |
| `getProposal` | `GET /api/v1/proposals/{id}` | `read` | `ETag: "v<version>"`; `If-None-Match` gives 304 |
| `listProposalRevisions` | `GET /api/v1/proposals/{id}/revisions` | `read` | Oldest first; `?limit&cursor` |
| `editProposal` | `PATCH /api/v1/proposals/{id}` | `propose` | `If-Match` is required; one or more of `operations`, `evidence_refs`, `rationale`. Returns 200 `Proposal` (new revision) |

Every role in Stage 2B §5.1 holds `read`. Only ANALYST and AUTOMATION_DEVELOPER hold `propose`. The ADMINISTRATOR still has no `propose`, `review` or `execute`.

## Create request

```json
{ "ledger_id": "<uuid>", "target_adapter": "NATIVE",
  "operations": [ { "type": "RecordSettlement", "schema_version": 1, "processor_ref": "processor-1", "payout_id": "STL-3001",
                    "payout_date": "2026-10-14",
                    "items": [ { "document_id": "<PAYMENT document>", "kind": "PAYMENT", "amount": { "amount": "50000.00", "currency": "INR" } } ],
                    "gross": { "amount": "50000.00", "currency": "INR" }, "fee": { "amount": "1000.00", "currency": "INR" },
                    "net": { "amount": "49000.00", "currency": "INR" }, "statement_document_id": "<staged SETTLEMENT_STATEMENT>" } ],
  "evidence_refs": [ { "kind": "STAGED_DOCUMENT", "id": "<uuid>" } ],
  "rationale": "free text, at most 4,000 characters" }
```

### What a draft checks (Stage 2A §9.1 order: schema → profile → capability; preconditions wait for submission)

| Check | Failure |
|---|---|
| JSON object, at most 1 MiB, no duplicate keys, no NaN/Infinity | 422 `SCHEMA_INVALID` |
| Fields exactly as in the 2B schema (`additionalProperties: false`); `schema_version` 1; canonical lower-case UUIDs; `payout_date` a real date; `processor_ref` and `payout_id` 1–64 characters `[A-Za-z0-9_.:-]` (the kernel's identifier rule); 1–10 operations, at most 1,000 items in total, at most 100 evidence references; no duplicate item or evidence | 422 `SCHEMA_INVALID`, with `errors[]` JSON pointers |
| A money amount sent as a JSON number | 422 `MONEY_MUST_BE_STRING` |
| More decimals than the ledger currency's exponent | 422 `MONEY_SCALE_EXCEEDED` |
| A currency other than the ledger's | 422 `CURRENCY_MISMATCH` |
| The ledger is absent or another tenant's | 404 `NOT_FOUND`, pointer `/ledger_id` |
| BOOKS or provider-connected ledger; `QUICKBOOKS` target; operation types other than RecordSettlement; `REFUND` items; `ATTACHMENT` evidence | 422 `CAPABILITY_UNAVAILABLE` |
| `BootstrapLedger` | 422 `CAPABILITY_UNAVAILABLE`, because EMPTY-ledger eligibility is unresolved (RG-4). The administrator, who would need `ledger.bootstrap`, gets 403 first because they lack `propose` |
| An item, statement or evidence id that is not in the proposal's ledger: absent, another tenant's, or another ledger of the same tenant | 404 `NOT_FOUND`, with the pointer of each unresolved id. All three cases give the same answer |
| An item that is not a PAYMENT document, or a statement id that is not a SETTLEMENT_STATEMENT | 422 `SCHEMA_INVALID` (`wrong_reference_type`) |
| An item amount different from the payment's full amount (SP-1 settles items whole) | 422 `UNSUPPORTED_BY_PROFILE`, `reason: PARTIAL_ITEM_SETTLEMENT` |

**Checked at submission, not at draft** (the 2A §19.3 preconditions):
- gross = Σ items;
- net = gross − fee;
- items unsettled and of the same processor;
- payout id unused;
- payout date in an OPEN period.

Amounts are stored and returned at exactly the currency exponent (`"750.5"` becomes `"750.50"`).

### Reuse

- The operation is checked by the vendored kernel's `Money.parse`, `iso_date`, `token` and `Settlement` constructor.
- It is also checked by the adapter's `validate_request`, applied to the internal request the operation will become at submission.
- No "validation registry" exists in the source.

## Write path (one transaction)

The HTTP boundary verifies the session, CSRF, Origin and `propose`. Then one call to a SECURITY DEFINER function (`lv.proposal_create` or `lv.proposal_edit`: owner `lv_owner`, `search_path = pg_catalog, pg_temp`, executable only by `lv_app`) does the following:

1. Re-resolves the session from its token hash and binds `app.tenant_id`, `app.user_id` and `app.session_id`. It refuses a transaction that is already bound to another session.
2. Re-checks `propose` against the live roles. A refusal is audited `PROPOSAL_CREATE_DENIED` or `PROPOSAL_EDIT_DENIED`.
3. **Create:** Part 8.1 idempotency through `lv.api_idempotency` (the 0009 table), with route `/api/v1/proposals` and the request hash = SHA-256 of the canonical body:
   - a running duplicate is 409 `REQUEST_IN_PROGRESS` (`Retry-After: 1`);
   - same key and same body replays the stored response with `Idempotent-Replayed: true`;
   - same key with a different body is 422 `IDEMPOTENCY_KEY_REUSED`;
   - 4xx outcomes are stored; a 5xx stores nothing.
4. **Edit:**
   - no `If-Match` gives 428;
   - the proposal row is locked `FOR UPDATE` (absent or another tenant's gives 404);
   - not DRAFT gives 409 `INVALID_STATE_TRANSITION`;
   - `If-Match` ≠ `"v<version>"` gives 412 with `current_version`;
   - the patch is merged with the **locked** head revision.
5. Resolves every reference within (tenant, proposal ledger).
6. Writes, all in that one transaction:
   - the proposal row (revision 1) or its +1 revision/version update;
   - an immutable `proposal_revision` (content, author, session, content SHA-256);
   - one `proposal_reference` row per referenced id. Composite foreign keys tie each reference to the same tenant and ledger;
   - the identity audit event `PROPOSAL_CREATED` or `PROPOSAL_EDITED` (proposal, revision, ledger, content hash).
   Refusals inside the function roll their block back and are audited as `…_REFUSED`.
7. **At COMMIT,** a deferred validator re-checks:
   - revisions are contiguous;
   - the proposal head equals the latest revision;
   - the maker is the author of revision 1;
   - reference rows match the content;
   - the matching audit event exists.

The route responds only after COMMIT.

## Guarantees

- **Maker and editors.**
  - `created_by` is the session user of revision 1.
  - Every revision records `authored_by` and `session_id`.
  - `editors` lists every author, maker first.
  - These are the inputs for the future "approver ≠ submitter or any editor" rule (2A §9.3, 2B §5.1). No approval exists yet.
- **Immutability.**
  - `lv_app` has SELECT only on the three tables (FORCE RLS, tenant policy).
  - Revision and reference rows reject UPDATE and DELETE, even for the owner.
  - The proposal row can change only by advancing exactly one revision and one version; its identity, ledger, maker and state are fixed. While DRAFT, `version = revision`.
- **No financial effect.**
  - No document, journal, projection, counter, commit batch or ledger event is written.
  - The tests compare a full row fingerprint of every other `lv` and identity table, and every ledger counter, before and after an HTTP create, edit and read flow.
- **Isolation.** Absent and other-tenant proposal ids give the same 404, and lists contain only the session tenant's proposals.

## Reads

- **Ordering.** `listProposals` is ordered by `id` descending (UUIDv7: newest first, and unique).
- **Cursors.** They are signed and bound to the tenant and the filters (2B Part 2).
- **`reviewable_by_me=true`** returns READY_FOR_REVIEW proposals the caller may review: the caller holds `review` and is neither the maker nor an editor. It is always empty in this milestone, because only DRAFT exists.
- **`allowed_actions`.** The response is `["EDIT"]` when the proposal is DRAFT and the caller holds `propose`, otherwise `[]`. SUBMIT and WITHDRAW are not offered; they are not implemented.

## Gaps and choices (please confirm)

| ID | Point | Chosen | Why |
|---|---|---|---|
| PC-1 | Does an edit create a revision? | Every successful PATCH creates a new immutable revision. While DRAFT, `version = revision` | The request asked for immutable revision history. 2A §10.3 says re-simulation creates a revision but is silent on draft edits. An approval can then bind to exactly the content reviewed |
| PC-2 | Who may edit a draft? | Any user of the tenant with `propose`; each is recorded as an editor | Family 8 lists `propose` and "Editor recorded (blocks later approval by that user)". Only *withdraw* is maker-only |
| PC-3 | Body references outside the ledger | 404 `NOT_FOUND` with pointers (absent, other tenant and other ledger alike) | CT-04 ("404 everywhere … path, body and filter"); Family 8 lists only 422 codes for POST |
| PC-4 | `statement_document_id` | Must be a staged `SETTLEMENT_STATEMENT` of the same ledger | Native statements exist only as staged documents (0004); there is no posted statement document |
| PC-5 | Limits | ≤ 10 operations, ≤ 1,000 items in total, ≤ 100 evidence references, rationale ≤ 4,000 characters, body ≤ 1 MiB | Part 2 "Limits" gives 1 MiB; the other caps bound the work per request and are not contracted |
| PC-6 | Identifier characters | `processor_ref` and `payout_id` follow the kernel and database identifier rule `[A-Za-z0-9_.:-]` | Stricter than 2B's `maxLength: 64`; a value outside it could never match a stored payment identity |
| PC-7 | Evidence `provenance` and `sha256` | Server-filled where the record has them (staged document and document version provenance; source-record content hash); clients may not send them | 2B marks both `readOnly`. Their exact semantics are not defined |
| PC-8 | Draft-time profile check | PARTIAL_ITEM_SETTLEMENT is checked at draft (422 `UNSUPPORTED_BY_PROFILE`) | Family 8 lists that code for POST; the other 2A §19.3 preconditions wait for submission |
| PC-9 | No-op edit | Still creates a revision | Every accepted PATCH is an attributable change; simplest to audit |
| PC-10 | Draft audit | Identity audit events (`PROPOSAL_*`); no `proposal_transition` table yet | No state transition happens for drafts |

## Dependencies and not built

- **Not built:**
  - submission, `snapshot` and `read_set` pinning;
  - simulation and verdicts;
  - approval, rejection and overrides;
  - execution, withdrawal and resimulation;
  - AI drafts;
  - provider adapters;
  - background jobs.
- **Staleness.** The period-close STALENESS_SCAN work item still has **no consumer**. Drafts are never marked STALE, and nothing here invalidates proposals.
- **Identifiers.** They are bare UUIDs (RG-2), not the `prp_…` prefix.
- **BootstrapLedger** is refused until RG-4 is resolved.
- **Performance.** The commit-time validator looks up the audit event within the tenant's audit chain; the cost at scale is not measured.
