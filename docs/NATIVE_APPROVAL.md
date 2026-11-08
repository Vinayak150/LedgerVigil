# Independent native approval (0019)

Accepted owners: Stage 2A §9.3/§10.3/§11.4/§12.2, Stage 2B Family 11/§7.4/Workflow 4, and ADR-015 segregation. This implements approval of the current native SANDBOX RecordSettlement revision. It creates no execution capability, posting, provider request, rejection or role-management route. Accounting/oracles stay UNREVIEWED; original simulations, verdicts and provenance are immutable.

## Authorization and grant history

`POST /api/v1/proposals/{id}/approve` requires a verified session with review permission, valid Origin/CSRF, Idempotency-Key, exact If-Match, and `{revision, comment?}`. A comment is optional plain UTF-8 text or null, at most 4000 Unicode code points; actor fields are never accepted. The approver must differ from the maker/submitter and every author of any saved proposal revision. Role combinations cannot bypass those exclusions. An administrator has no implicit review right.

All review-bearing role grants, including LOCAL_PROVISIONING, enforce the accepted initial 24-hour cooling-off. The latest review grant determines the wait; a self-granted review role is refused even after the wait. User, membership and role rows are locked/rechecked in the acting transaction, and permission/cooling/exclusions are checked again before cached replay. No real grant time is backdated by migration or application code. Disposable tests explicitly arrange old grant timestamps as synthetic fixtures.

The existing project has no tenant-settings API or role-management workflow. The initial 24-hour value is fixed in this bounded slice; tenant configuration and safe grant management remain separate work. No operator or local-grant exemption was invented.

## Atomic state and freshness

The wrapper takes the established proposal → ledger/counter → bindings/accounts/periods lock order and re-reads supported dependencies. FRESH with an unchanged full capture inserts one immutable decision plus one immutable freshness check, moves READY_FOR_REVIEW → APPROVED, increments version, and records transition/audit/cache in one transaction. The decision is unique per proposal/revision and binds maker, approver, revision, simulation and check. Deferred constraints refuse incomplete or inconsistent artifacts. Runtime roles cannot directly mutate these tables; both use FORCE RLS.

Changed dependencies mark the proposal STALE, release its reservations, record the check, transition, release history, audit and 409 PROPOSAL_STALE response atomically. No decision is created. The controller is the real actor of this stale check/release; it is not attributed to the maker. A revision mismatch returns 412 without changing a different current revision. A second decision attempt returns 409 ALREADY_DECIDED. Same-key retries return the original receipt after live authorization checks; a historical success does not prove current eligibility.

As with the existing evidence-exception slice, a changed sequence with otherwise FRESH narrow dependencies is refused explicitly with 422 CAPABILITY_UNAVAILABLE / FULL_CAPTURE_CHANGED. It does not mark unrelated changes globally STALE or invent recapture semantics. This preserves the existing full-capture commit validator. Review action hints are computed inside the request transaction and cached before releasing its database connection; response rendering performs no database query on a released pooled connection.

APPROVED proposals participate in the existing fenced period-staleness worker. Period close can move them to STALE while retaining their decision history. The maker can explicitly revalidate an approved proposal. An approved proposal has no advertised withdrawal or execution action in this slice. Re-simulation remains available only after the owning stale flow and does not inherit a past decision.

## Reads and UI

`GET /proposals/{id}/reviews` now returns ordered APPROVAL and INSUFFICIENT_EVIDENCE_OVERRIDE records; rejection was unsupported in 0019; the subsequent NATIVE_REJECTION.md slice adds it. Signed pagination remains proposal/tenant bound. `reviewable_by_me=true` and APPROVE action hints include current independence, cooling-off and supported freshness checks. These are point-in-time hints; the POST rechecks everything.

Proposal details display approvals and exceptions separately from the raw simulation. Comments/reasons are literal text. The state filter includes APPROVED. The subsequent authoring slice adds a server-hint-gated approval form; see NATIVE_APPROVAL_UI.md for exact confirmation and recovery behavior. The displayed approval does not change the original BLOCKED_INSUFFICIENT aggregate or `eligible_for_execution=false` flag.

## Bounded proposed choices

- **AP-1 — PROPOSED:** preserve the existing revision schema; use a composite FK from decision to the immutable proposal maker identity plus a separate exact revision FK and CHECK approver ≠ maker. The owning contract describes a maker-bearing revision FK; the implemented two-FK representation enforces the same identity relationship without inventing a new revision author or rewriting history. All recorded editors are additionally checked in the domain and deferred validator.
- **AP-2 — PROPOSED:** retain full-capture refusal for an otherwise FRESH sequence advance, matching the earlier bounded exception implementation. General recapture semantics remain unresolved.
- **AP-3 — PROPOSED:** optional comment's 4000-character representation limit and the fixed initial 24-hour cooling-off until tenant settings exist. The accepted cooling-off itself and maker/editor exclusion are enforced, not treated as optional.

No accepted specification or proposed decision status is changed by this implementation.
