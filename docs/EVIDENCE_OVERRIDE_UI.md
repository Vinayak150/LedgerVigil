# Controller evidence-exception authoring

This frontend slice uses the already installed ADR-014 exception API at migration 0018. It changes no backend code, database schema, permission, accounting rule or accepted decision. OV-1/OV-2 remain PROPOSED; accounting remains UNREVIEWED.

## Visible action

The verified session must contain CONTROLLER and review permission, and the user must differ from the proposal maker. The form checks the exact pinned native RecordSettlement 0.3.0 run, proposal/revision/ledger identity, BLOCKED state, single operation, sole P-EVD-01 insufficiency, no FAIL and exact version ETag. These are presentation gates; the existing server remains authoritative for permission, role, maker, version, freshness and full-capture checks.

A controller supplies 1–4000 Unicode code points of nonblank, well-formed, NUL-free plain text and explicitly acknowledges that the action records an evidence exception only. No approval, accounting sign-off, posting or provenance relabeling is implied. The original blocked simulation stays visible separately. New authoring is withheld when the pinned result cannot be verified or the proposal is unsupported. A pending request can still be resumed after the proposal head changes.

## Recovery contract

Before sending, persist the exact reason, original ETag, revision, simulation ID, UUID request key and start time to sessionStorage, scoped by verified tenant, user, proposal and ledger. No credentials are stored. If persistence fails, no request is sent. Invalid recovery data blocks new requests instead of silently creating a new key.

While a request is unconfirmed the reason and version are frozen. Reload and session refresh preserve that original request. Only an explicit retry sends it again. Network failures, 5xx, unexpected/mismatched success bodies, in-progress responses, authentication failures and idempotency-key conflicts retain the request. A read of current head/history is displayed as a separate observation and does not settle the original attempt. No automatic POST or new-key retry occurs.

Retry stops at 23 hours or clock rollback, before the server's 24-hour cache expiry. The preserved request then requires operator reconciliation; this form provides no discard-pending or forced replacement-key control. Browser-tab storage is not a durable server-side request registry; closing the tab/browser can lose its recovery data. Keep the tab open until a receipt is confirmed. Backend uniqueness/version guards remain the final protection.

Known typed version/state/capability/schema refusals release the refused request key while retaining the written reason, then block another attempt at the displayed version. Refresh and inspect the newer head before a deliberate new action. A stale dependency requires maker revalidation. FULL_CAPTURE_CHANGED cannot be bypassed here; use the owning withdrawal/new-proposal path.

A success is confirmed only by the exact proposal/ledger, original revision/run, READY_FOR_REVIEW state and original version + 1. The receipt is explicitly point-in-time, including idempotent replay when reported. Closing the receipt refreshes current proposal data and immutable exception history. If a receipt was confirmed but clearing local recovery storage fails, the confirmed receipt remains visible; any retained request still replays under its original key.

## Verification scope

Helper tests exercise scope isolation, invalid recovery, Unicode bounds, retry expiry, narrow policy presentation, exact receipt validation and refusal classification. Browser verification uses actual authenticated HTTP and PostgreSQL in a disposable synthetic database. It covers direct success, committed response loss, reload/retry with identical key/body/version, session refusal during recovery, competing version change, actual FAIL refusal, mobile/keyboard and tenant reset. Helper tests additionally isolate maker exclusion and the role/permission predicate; existing backend tests cover their enforcement. Database fingerprints check that authoring changes only workflow/audit/cache records and leaves authoritative financial records, raw simulations and counters unchanged.

These checks establish software-control behavior. They do not measure accounting accuracy, real customer outcomes or production readiness. No provider access, approval or execution is added.
