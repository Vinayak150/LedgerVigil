# Native financial read API — backend reference

**Status:** implemented for native ledgers in the isolated read-API candidate (built on the period-close candidate). Trial balances are served for native **SANDBOX** ledgers only. All data is SYNTHETIC. Accounting status: **UNREVIEWED**. This is not production-ready accounting.

## Endpoints (Stage 2B Family 4, read subset)

All routes are `GET` under `/api/v1` and need permission `read`. Every role in Stage 2B §5.1 holds `read`.

| Operation | Path | Query parameters | Response |
|---|---|---|---|
| `listLedgers` | `/ledgers` | `limit`, `cursor` | `{items: Ledger[], next_cursor}`, ordered by `name` (bytewise), then `id` |
| `getLedger` | `/ledgers/{ledger_id}` | none | `Ledger` |
| `listLedgerAccounts` | `/ledgers/{ledger_id}/accounts` | `limit`, `cursor` | `{ledger_id, items: Account[], next_cursor}`, ordered by `code` (bytewise), then `id` |
| `listLedgerPeriods` | `/ledgers/{ledger_id}/periods` | `limit`, `cursor` | `{ledger_id, items: Period[], next_cursor}`, ordered by `start_date`, then `id` |
| `getTrialBalance` | `/ledgers/{ledger_id}/trial-balance` | `as_of_seq`, `as_of_date` | `TrialBalance` |

The exact schemas are in `api/openapi.json`. That file is regenerated from the application.

### Ledger

```
id, name, purpose (BOOKS | SANDBOX), source (NATIVE | PROVIDER_CONNECTED), functional_currency, currency_exponent,
profile_version, state (EMPTY | ACTIVE), current_seq (integer), created_at (RFC 3339 UTC),
data_label ("SANDBOX — not accounting records" for SANDBOX, else null)
```

- `state` follows Stage 2A §19.2 literally: a ledger is **EMPTY** when its sequence is 0 and it has no documents. Any other ledger is **ACTIVE**.
- `current_seq` is the committed `ledger_counter.last_seq`, or 0 if no counter row exists yet.

### Account

```
id, ledger_id, code, name, type, subtype, is_contra, normal_side (DEBIT | CREDIT), requires_party, currency, active, parent_id
```

`normal_side` is the database's generated column (Stage 2A §2.2): DEBIT for ASSET and EXPENSE, CREDIT for the other types, inverted when `is_contra`.

### Period

The period schema is unchanged from period close; it is implementation choice **C-4**, still unapproved. Periods are half-open, `[start_date, end_date)`: `end_date` is the first day **after** the period. This follows the schema constraint `daterange(start_date, end_date, '[)')`, `end_date = start_date + 1 month`, and the posting guards `date >= start_date AND date < end_date`.

### TrialBalance

```
ledger_id, purpose, data_label, accounting_status ("UNREVIEWED"), as_of_seq (integer), currency,
lines: [{account_id, code, name, type, is_contra, normal_side, debit: Money, credit: Money, balance: SignedMoney}],
total_debit: Money, total_credit: Money
```

| Rule | Source |
|---|---|
| Balance(account, seq ≤ N) = Σ `journal_line.signed_amount` over `seq ≤ N`. The generated column is `amount` for D and `-amount` for C. No `balance_checkpoint` table exists, so the checkpoint term is 0 | 2A §8.3, §4.2 |
| Balances are signed debit-positive | 2A §7.1 |
| Each line shows its net balance in one column: `debit` = balance if positive, `credit` = −balance if negative, both 0 if zero. The totals are the column sums, which is how the 2A oracle rows state "TB" | 2A §7.1–7.3 (e.g. OC-01 end: TB 50,000.00) |
| Money is `{"amount": "<decimal string>", "currency": "INR"}` at exactly the currency exponent. Only `balance` is signed | 2B Part 2, Part 3 (`Money`, `SignedMoney`) |
| Amounts leave PostgreSQL as text and become the vendored kernel's `Money` (Decimal, quantized, range-checked). Totals use the kernel's `total`. No float is used anywhere | 2A §3, ADR-006 |
| Lines: one per account with at least one posting at or before `as_of_seq`, including zero balances. Accounts with no postings contribute nothing and are omitted | Implementation choice **RC-3** |
| `total_debit` must equal `total_credit` (I-04). If they differ the request is refused with 500 `INTERNAL` and no figures are shown | 2A I-04 |

## Snapshot and historical behaviour

- Omitting `as_of_seq` means the committed head read at request time, and the response states it in `as_of_seq` (2B Part 2).
- `as_of_seq=N`, with N ≤ head, gives the trial balance as committed at N. The figures come from postings with `seq ≤ N`. Current figures are never substituted: a read at N is byte-identical to the read made when N was the head (tested).
- `as_of_seq` beyond the committed head gives 422 `SCHEMA_INVALID`, with field error `beyond_committed_sequence`.
- A malformed value (`-1`, `01`, `1.0`, …) gives 422 `SCHEMA_INVALID`.
- **One committed sequence.** The aggregate is always bounded by `seq ≤ as_of_seq`. Sequences are gapless and every sequence commits atomically (2A §8.1), so a commit landing during the request cannot enter the result: the lines, totals and `as_of_seq` describe the same committed sequence. This is tested by forcing a commit between resolving the sequence and aggregating it, and by 40 reads racing 15 postings.
- **Projection cross-check.** When the requested sequence is also the head seen by the aggregate statement, the current-state projection (`account_balance_current`) must equal the postings. If it does not, the request fails closed with 500 `INTERNAL` and nothing is reported. Historical reads do not depend on the projection.
- **`as_of_date`** is in the contract, but no owning source says whether the date is inclusive (gap **RG-1**).
  - A well-formed value gives 422 `CAPABILITY_UNAVAILABLE`, even when combined with `as_of_seq`.
  - A malformed value gives 422 `SCHEMA_INVALID`.
  - It is never ignored.
- **Ledger scope.** Trial balances are available for native SANDBOX ledgers only. BOOKS or provider-connected ledgers give 422 `CAPABILITY_UNAVAILABLE` (choice **RC-1**). Ledger, account and period metadata are readable for every ledger of the tenant.
- **Size and range limits.**
  - More than 2,000 lines gives 422 `CAPABILITY_UNAVAILABLE`; trial-balance paging is not contracted (choice **RC-4**).
  - An amount that cannot be expressed as contract Money (more than 16 integer digits) gives 422 `CAPABILITY_UNAVAILABLE`.

## Authentication, isolation and read safety

- Every route depends on `auth.boundary.require_read_only('read')`:
  - the verified-session boundary binds tenant, user and session from the session row;
  - it requires a selected tenant and `read`;
  - it then runs `SET TRANSACTION READ ONLY`.
- Session bookkeeping (`session_context`'s `last_seen_at` touch) happens before the switch and still commits.
- After the switch, PostgreSQL refuses every write to a non-temporary table for the rest of the request (`ReadOnlySqlTransaction` → 500, nothing written). This holds whatever route code or function is called.
- Row-level security is the only tenant filter in these queries. Another tenant's ledger id gets the same 404 `NOT_FOUND` body as an absent id.
- Revoked memberships and disabled users are rejected on the next request (401), because `session_context` re-checks the live rows. A membership with no roles has no `read` permission and gets 403.
- No migration, grant, role or definer function was added. The routes use the existing `SELECT` grants of `lv_app`.

## Query conventions (2B Part 2, Part 4)

| Situation | Response |
|---|---|
| Parameter not listed for the operation | 400 `UNKNOWN_QUERY_PARAMETER`, with `errors[]` naming it |
| A parameter repeated | 422 `SCHEMA_INVALID` (`repeated`) |
| `limit` not an integer from 1 to 200 | 422 `SCHEMA_INVALID`; the default is 50 |
| Cursor malformed, tampered, longer than 512 characters, or issued for another operation, tenant, ledger, order or key | 400 `CURSOR_MISMATCH`: one indistinguishable answer |
| No session or an invalid one | 401 `UNAUTHENTICATED`. This is checked before any query validation, so an anonymous caller learns nothing |
| No `read`, or no tenant selected | 403 `PERMISSION_DENIED` |
| Ledger absent or another tenant's | 404 `NOT_FOUND` |

### Cursors (choices RC-5, RC-6)

- **Format.** `base64url(payload) "." base64url(HMAC-SHA256(key, domain ‖ context ‖ payload))`.
  - The payload holds only the keyset position: the sort value plus the row `id`.
  - The context holds the operation, tenant, ledger, order and filters. It is covered by the MAC but not written into the cursor.
- **Key.** It comes from `LV_CURSOR_KEY`: at least 64 hexadecimal characters.
  - Without it, each process generates a random key. Cursors then stop verifying after a restart and do not work across processes; the client gets 400 and restarts from the first page.
  - Multi-process deployments must set `LV_CURSOR_KEY`.
- **Binding.** The limit is not part of the binding, so a client may change page size between pages.
- **Paging.** Pages use `(sort value, id) > (last sort value, last id)` with `LIMIT limit + 1`, so no request reads more than 201 rows.
- **Ordering.** Text sort keys use `COLLATE "C"` (bytewise), so the order is the same on macOS and Linux.

### Conditional requests

- Every successful read returns a strong `ETag: "r<40 hex>"`: the SHA-256 of the exact response bytes. These are representation validators, not resource versions (`"v<n>"`), and no mutation accepts them as `If-Match`.
- `If-None-Match` is evaluated inside the route, after authentication, permission and the RLS-scoped read. A match (weak comparison, list form, or `*`) returns 304 with the `ETag` and no body.
- An unauthenticated caller, or another tenant's session, never gets a 304.
- `Cache-Control: no-store` is unchanged (app middleware).

## Gaps and dependencies

These are recorded, not resolved.

- **RG-1 — `as_of_date` semantics.**
  - 2B Family 4 lists `?as_of_date`, and 2A §8.3 says reports "filter lines by `effective_date` within a snapshot". Neither says whether the date is inclusive, or how a date relates to periods.
  - The parameter is refused until this is decided.
  - Suggested, not adopted: inclusive, `effective_date <= as_of_date`, combined with `seq <= as_of_seq`.
- **RG-2 — Identifier format.** 2B Part 2 specifies typed, prefixed ids (`led_…`). Every existing route, including period close, uses bare UUIDs. This milestone keeps that.
- **RG-3 — No `Ledger.version`.** The ledger table has no version column, so `getLedger` has a representation ETag but no `"v<n>"` resource version. No ledger mutation exists yet.
- **RG-4 — EMPTY versus bootstrap.**
  - 2A §19.2 defines EMPTY as "sequence 0, no documents" and requires an EMPTY ledger with "all earlier periods CLOSED" for BootstrapLedger.
  - Since 0009, a period close is a `PERIOD_CLOSE` commit that advances the sequence (2A §8.1). A ledger whose earlier periods were closed is therefore never EMPTY by the literal definition.
  - The API reports the literal definition. The conflict needs an owner decision before BootstrapLedger is built.
- **RG-5 — Trial-balance line shape.** 2B gives `lines` without a schema. The line fields above are an additive implementation choice (**RC-2**).
- **RG-6 — No balance checkpoints.** Every trial balance aggregates the ledger's lines up to N, using the `(tenant, ledger, account, seq)` index. `balance_checkpoint` (2A §8.3) is not built, so cost grows with ledger size. This has not been measured at UCI scale.
- **Carried forward unchanged:**
  - G-1: the STALENESS_SCAN outbox row and work item have no consumer, so no job completes and no proposal invalidation exists;
  - G-2: no `GET /jobs/{id}`;
  - G-4: no `POST /ledgers`;
  - C-2 to C-4 remain unapproved period-close implementation choices;
  - L-1 is still undetected;
  - Q-16 is unchanged.

The G-3 gap (period-list pagination) is closed by this milestone.

## Implementation choices (please confirm)

| ID | Choice | Why |
|---|---|---|
| RC-1 | Trial balance only for native SANDBOX ledgers (422 otherwise) | ADR-027 MFA gate (no BOOKS data before MFA); provider-connected ledgers are authoritative at the provider (ADR-003); consistent with C-1 |
| RC-2 | Line fields `account_id, code, name, type, is_contra, normal_side, debit, credit, balance`; response adds `ledger_id, purpose, data_label, accounting_status, currency` | Additive under the Part 2 versioning rule; the ADR-024 label and the UNREVIEWED status travel with the figures |
| RC-3 | One line per account with postings at or before N, including zero net balances | Determined by the snapshot alone; accounts have no creation sequence, so including all accounts would mix current configuration into history |
| RC-4 | Hard cap of 2,000 lines (422) | No contracted pagination; avoids an unbounded response |
| RC-5 | Every cursor failure is 400 `CURSOR_MISMATCH` | Part 4 names it for list misuse; one answer avoids telling the caller why |
| RC-6 | Cursor key from `LV_CURSOR_KEY`, else random per process | The contract requires signed cursors; no key-storage decision exists |
| RC-7 | `as_of_seq` beyond the head is 422 `SCHEMA_INVALID` | A request that cannot be answered without substituting other figures; Part 4 has no dedicated code |
| RC-8 | Representation ETags `"r…"` on all five reads; 304 after authentication | Part 10 freshness; `"v<n>"` is reserved for resource versions |
| RC-9 | Unbalanced snapshot or projection disagreement → 500 `INTERNAL`, `retryable: false`, no figures | Fail closed; displaying corrupted figures is worse than an error |
