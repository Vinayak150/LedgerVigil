# Atomic native settlement submission and withdrawal

Scope: local native SANDBOX, one RecordSettlement with at most 200 items. Accounting remains UNREVIEWED. No override, approval, execution, AI, provider or worker capability is added. Matching SYNTHETIC evidence produces BLOCKED_INSUFFICIENT. No SOURCE provenance is admitted by the current schema, and the database refuses a fabricated evidence PASS.

## API

- POST /api/v1/proposals/{id}/submit: maker with propose; verified session/tenant, CSRF/Origin, Idempotency-Key and If-Match. Empty body or {} only. Returns {proposal, simulation}, ETag and Idempotent-Replayed.
- POST /api/v1/proposals/{id}/withdraw: same authorization/concurrency controls; DRAFT, BLOCKED, READY_FOR_REVIEW or STALE → WITHDRAWN. Releases all reservations owned by this proposal revision in the same transaction.

A submitted revision enters SUBMITTED transiently and finishes BLOCKED or READY_FOR_REVIEW in one transaction. One external mutation advances the resource version once (v1→v2), matching the Stage 2B example; the internal automatic state transition has a second immutable phase at that same version. Neither phase can commit alone. Revision contents/editors remain immutable. New edits remain DRAFT-only.

Staged statement facts remain immutable in the original ingestion tables. staged_workflow holds their separate operational status/version/owner; reservation_event retains immutable claim/release history. No workflow row means VALID/version 1. Reservation advances to RESERVED/version 2 by CAS. Withdrawal releases to VALID/version 3, retaining history. A later proposal can reserve it at version 4. Source read responses now expose status, version and reserved_by_proposal_id independently of validation_status. Representation ETags reflect reservation changes.

## Transaction boundary and locking

One runtime connection is held per request. Authentication initially reads the session without touching it. workflow_request takes the nonblocking idempotency advisory lock first; an in-flight duplicate returns 409 even with the same cookie. Session activity is touched next, before resource locks. This preserves idle-session behavior without a session/proposal lock cycle against existing draft editing.

workflow_begin resolves the maker's proposal, checks If-Match and DRAFT, then locks the ledger FOR UPDATE, its counter, bindings, accounts and periods in a consistent order. The strong ledger lock serializes every existing financial path (which first takes a ledger share lock), new configuration/source rows through their foreign keys, and other submissions. Binding and account locks freeze mutable configuration. This is deliberately conservative for a simulator that reads full ledger history; it is not a claim of maximal throughput.

After tentative reservation and the SUBMITTED phase, capture_submitted and the adapter's private locked mode read through that SAME connection. They require a current-transaction, verified-session transition proof. Their actual isolation/read_only/write-xid metadata is retained: this is READ COMMITTED under locks and a write transaction, not the ordinary readonly capture guarantee. The existing public adapter load_snapshot and draft-simulation capture keep their strict REPEATABLE READ / READ ONLY guards. Calculation/policy evaluation remains pure over the captured bundle.

workflow_finish rechecks active identity, membership and propose permission and pins identity/role rows through commit. It checks the proposal revision/content, ledger sequence, current profile marker, hashes, aggregate and synthetic-evidence refusal; inserts simulation_run, ledger_snapshot, proposal_read_set and verdict; completes the proposal transition, audit and stored response. Deferred validators independently compare the exact stored read set with current bound-account/document/period versions and the pinned ledger sequence, compare verdicts with the outcome, and require matching reservations, audit and idempotent response. A missing artifact or any commit-time failure rolls back all effects. Success is returned only after COMMIT.

A typed precondition/capability refusal rolls back the workflow savepoint before its 4xx response and denial audit are cached in the outer transaction. Infrastructure/integrity failures roll back everything and do not cache a success. Original authoritative financial/source tables, balances, counters and events are unchanged by submission or withdrawal. The only mutable source-related data is the new workflow projection.

## Bounded contract choices and remaining gates

- SS-C1: a separate projection represents staged_document.status while preserving insert-only ingestion facts. This is a physical implementation choice, not an accounting change.
- SS-C2: Maker-only submission follows Stage 2A §10.4. An editor with propose can still edit a DRAFT, but cannot submit/withdraw another maker's proposal.
- SS-C3: empty body and {} normalize to the same idempotency request. Action, proposal id, If-Match and query parameters are included in the hash. Caches use the existing 24-hour contract. A deliberate new attempt after a cached refusal requires a new key; a retry of the same attempt retains its key.
- SS-C4: public submission requires processor, payout and date to equal the cited statement's identity. This is a conservative guard on the supported staged-statement subset; STATEMENT_IDENTITY_MISMATCH is a proposed reason name. It does not approve PV-C2/C3/C4 or modify the existing policy registry. Wider statement/evidence semantics still need review.
- SS-C5: multi-operation or above-budget submission is explicitly CAPABILITY_UNAVAILABLE and stays DRAFT. The contracted asynchronous 202/job path is not implemented.
- SS-C6: verdicts now have immutable rows for new submissions. Old draft-simulation runs keep their original outcome-only verdict representation; no retrospective policy or data rewrite occurs.

Reservations persist while blocked. Maker withdrawal and the bounded period-close STALE transition release them atomically. Rejection, other staleness causes and re-simulation remain unavailable until their full release/audit/version semantics are implemented. Audited period-close jobs now have the consumer described in PERIOD_STALENESS.md; other producers/consumers and general action-time revalidation remain future work. The next approval/execution stage must revalidate pinned versions AND the ledger sequence under locks. Source-ingestion conflict/range dependencies also need action-time revalidation; an immutable cited source id alone does not rule out later conflicting evidence. Do not authorize historical incomplete-profile runs or claim that stored prediction is currently eligible.

Production configuration/audit rights, credential-compromise hardening, MFA/recovery, independently reviewed accounting oracles, real dataset evaluation, source ingestion/posting and release operations remain their existing gates. Earlier proposed ADR/SM/PV decisions retain their status.
