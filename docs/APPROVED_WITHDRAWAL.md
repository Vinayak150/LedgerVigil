# Maker withdrawal after approval — RG-01

Accepted Stage 2A §10.4 permits maker withdrawal from any non-terminal state.
The native implementation now supports the previously omitted APPROVED state.
The native execution path remains atomic, so EXECUTING is not a persisted native
state exposed for withdrawal; terminal executed/rejected/withdrawn states remain
closed. This adds no provider, BOOKS or production capability.

Migration 0022 patches only the existing proposal-update guard and maker withdrawal
function using checked predecessor fragments. Existing session/permission checks,
proposal-first locking, If-Match, idempotency, ledger lock, reservation CAS and
immutable release/transition/audit/cache validation remain intact. An explicit
execution-evidence fence refuses withdrawal when a native execution for the
revision exists. No approval, approval-check or simulation row is edited/deleted.
The generic workflow UI consumes server allowed_actions, so the maker now sees
WITHDRAW for APPROVED; executor authorization remains separate. Refusal text for
FULL_CAPTURE_CHANGED explains withdrawal followed by a fresh proposal.

The September-close / October-settlement case remains a FULL_CAPTURE_CHANGED
execution refusal. Maker withdrawal releases its reserved statement atomically,
allowing a new proposal to submit with a fresh simulation and approval process.
AP-2/OV-2 full-capture semantics and account-version invalidation are unchanged.

Downgrade validates a temporary CHECK constraint over the transition table before
reversing the two patches. This scans all tenant rows under FORCE RLS, unlike a
normal owner SELECT. Any APPROVED→WITHDRAWN history blocks downgrade transactionally;
no history is discarded. An empty round trip restores both predecessor function
definitions exactly. Populated upgrade preserves all application/financial rows.

Tests include real PostgreSQL/action HTTP, permission/tenant/version/terminal
boundaries, original-key replay and key misuse, precommit interruption and committed
response loss, reservation/audit/transition/cache fault injection, and two forced
20-request races (ten executors, ten maker withdrawals) covering either winner.
Only one terminal action may commit; withdrawal posts nothing. Browser acceptance
uses synthetic settlement data in a disposable database and verifies authoritative
state/counters, retained evidence and replacement submission.

The schema-head assertions in existing migration tests advance to 0022. Legacy
statement-ingestion fixtures explicitly recognize both 0021 and 0022 for the
native ingestion writer, while retaining the old writer for historical heads.
No accounting/kernel/ingestion writer or native execution SQL is modified.

Accounting remains UNREVIEWED. All prior proposed decisions stay PROPOSED.
RG-02/03/04/05/06/07/08 are outside this fix and retain their review status; no
completion/export/security-policy decisions are silently approved here.
