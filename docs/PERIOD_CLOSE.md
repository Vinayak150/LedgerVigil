# Period close (migration 0009) — backend reference

**Status:** implemented for native **SANDBOX** ledgers. SYNTHETIC data only. Accounting oracles UNREVIEWED.

## Contract

The contract comes from **Stage 2B Family 4** and **Part 12**: `POST /api/v1/periods/{period_id}/close`, permission `administer`. Stage 2A §12.2 also lists period close under `administer`.

| Rule | Behaviour |
|---|---|
| Authentication | A session cookie plus `X-CSRF-Token`, and an allowed `Origin` if one is sent. A failure here gives 401 or 403 before any database effect |
| Permission | `administer` is checked twice: in the HTTP boundary, and again inside `lv.close_period` against the live membership roles. Administrators still have no `propose`, `review` or `execute` |
| Tenant | Taken only from the session. Another tenant's period, or a missing one, is 404 |
| `Idempotency-Key` | Required, or 400 `IDEMPOTENCY_KEY_REQUIRED`. Behaviour per Part 8.1:<br>• same key and same request: the stored response, with `Idempotent-Replayed: true`;<br>• same key, different request: 422 `IDEMPOTENCY_KEY_REUSED`;<br>• same key while the first request is still running: 409 `REQUEST_IN_PROGRESS` with `Retry-After: 1`;<br>• 4xx outcomes are stored and replayed;<br>• a 5xx stores nothing;<br>• retention is 24 hours.<br>The scope is (tenant, user, `POST`, route template, key). The request identity is the period id plus the raw `If-Match` value |
| `If-Match` | `"v<version>"` is required, or 428 `VERSION_REQUIRED`. A different version gives 412 `VERSION_MISMATCH` with `current_version` |
| State | 409 `INVALID_STATE_TRANSITION` (with `state`) if the period is already closed, or if an earlier period of the ledger is still open |
| Ledger | Only native SANDBOX ledgers. Others give 422 `CAPABILITY_UNAVAILABLE` (ADR-027 gate; provider-connected closing is not specified) |
| Success | 200 `Period` (CLOSED, version + 1), with `ETag: "v<version>"` and `staleness_job` |

`GET /api/v1/ledgers/{ledger_id}/periods` (`read`) lists periods with their `version`, so a client can send `If-Match`.

## One transaction

`lv.close_period(token_hash, period, if_match, idempotency_key, source, request_id)` is pinned to `search_path = pg_catalog, pg_temp`, SECURITY DEFINER, owned by `lv_owner`, and executable by `lv_app` only. It does the following in order:

1. Resolves the session from the token hash, requires a selected tenant, and binds `app.tenant_id`, `app.user_id` and `app.session_id` from the session row. It refuses if the transaction was already bound to a different session.
2. Checks `administer`. A denial is audited as `PERIOD_CLOSE_DENIED`.
3. Takes the idempotency try-lock, then handles a stored response: replay, 422, or deletion when expired.
4. Takes locks in the **same order as every posting path**: ledger `FOR SHARE`, then `ledger_counter` `FOR UPDATE`, then the per-ledger period-configuration advisory lock, then the period `FOR UPDATE`.
5. Checks the version (412), the state (409) and earlier open periods (409).
6. Writes, all in this transaction:
   - a `commit_batch` row of kind `PERIOD_CLOSE`, attributed to `created_by_user`, chained by hash;
   - the period status CAS: `OPEN → CLOSED`, `version + 1`, `closed_seq`, `closed_at`, `closed_by`, `close_provenance = AUDITED_COMMIT`;
   - a `STALENESS_SCAN` `work_item` and its `outbox` row (topic `maintenance`, status PENDING);
   - a `ledger_event`, chained by hash, then the counter heads;
   - the identity audit event `PERIOD_CLOSED`, carrying user, tenant, session, source and request id.
7. Stores the response in `lv.api_idempotency`.

All of steps 4–6 sit in one PL/pgSQL block. A 4xx refusal rolls that block back, including any counter row it created, and then only the refusal is stored and audited (`PERIOD_CLOSE_REFUSED`).

**Commit-time validation.** `lv.validate_period_close_batch()` is a deferred constraint trigger. At COMMIT it re-derives the period, event, work item, outbox, audit, stored response and counter heads, and aborts the commit on any divergence.

**Responding after commit.** The HTTP route commits *before* it builds the response, by using `session_transaction` as a context manager. A commit-time failure is therefore a 500, never a reported success.

## R-02: what changed

- `lv_app` no longer has `UPDATE` on `lv.fiscal_period`. `INSERT` remains for ledger setup, with these limits:
  - a period can only be created `OPEN`;
  - a period can never be created before a closed period, enforced under the same advisory lock as close.
- `period_transition_guard` applies even to the owner. It allows `OPEN → CLOSED` only together with a `PERIOD_CLOSE` commit made in the same transaction for that period and user. Any other change to status or close fields is refused, and the existing guard still forbids reopening and date changes.
- Periods already `CLOSED` before 0009 keep their status and are labelled `close_provenance = LEGACY_UNAUDITED`. No commit or audit is invented for them.
- Posting paths are unchanged and still refuse a closed period:
  - `seed_manual_journal`, `seed_processor_payment` and `seed_processor_settlement` check under the counter lock;
  - the `entry_period_guard` trigger is the backstop.

## What does not exist yet

**The staleness worker.**
- Nothing consumes the `STALENESS_SCAN` work item: there is no outbox relay, no Celery worker, no `lv_dispatcher` role, and no proposal tables to mark STALE.
- The obligation is recorded durably (L1, Stage 2A §1.2) and will be picked up when those components exist.
- Until then the item stays `QUEUED`. This is safe today because there are no proposals.

**Other limits.**
- There is no `GET /jobs/{id}`, and no cursor pagination or unknown-parameter checks on the period list.
- Q-16 still applies: the `lv_app` credential can assert a password verdict. A caller holding that credential and a valid administrator session token can close a period by direct SQL, exactly as the API would.
