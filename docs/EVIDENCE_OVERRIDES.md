# Native controller evidence exceptions

ADR-014 permits a controller to accept INSUFFICIENT_EVIDENCE with a written reason; FAIL remains non-overridable. Stage 2A §6.2/§9.3/§10.4 and Stage 2B API family 8 own that behavior. Migration `0018_evidence_overrides` implements one bounded native SANDBOX RecordSettlement case: the sole outstanding `P-EVD-01` insufficiency.

## API and scope

`POST /api/v1/proposals/{proposal_id}/overrides` requires a verified session, CSRF, controller role plus `review`, an `Idempotency-Key`, and the exact `If-Match` proposal version. The controller must differ from the proposal maker, submitter and every editor across the complete revision history. The body is exactly `{ "policy_ids": ["P-EVD-01"], "reason": "written explanation" }`. The reason is nonblank UTF-8, 1–4000 Unicode code points, stored as plain text without trimming. Actor/session identity comes from the verified session. An administrator without controller/review cannot use it. Migration 0030_override_independence applies the existing review_blocker to exception grants: self-granted review permission is refused, and every current review grant must have cooled for at least 24 hours. These checks run under the existing identity locks before cached-success replay and at the deferred completeness check. Earlier exception records remain immutable; this policy governs new actions and retries, not retrospective erasure of history.

The proposal must be BLOCKED with the current native simulator's single RecordSettlement, no FAIL verdict and no other insufficiency. Other selections, multiple policies and unsupported adapters explicitly return 422 CAPABILITY_UNAVAILABLE. Missing/inaccessible identifiers remain indistinguishable. Immutable exceptions use tenant FORCE RLS and the runtime role cannot directly insert, update, delete or truncate them.

`GET /api/v1/proposals/{proposal_id}/reviews` is authenticated, read-only, cursor-paginated and proposal/tenant bound. It currently returns only INSUFFICIENT_EVIDENCE_OVERRIDE records; APPROVAL and REJECTION are explicitly unsupported. Records retain exact revision, simulation, policy version, original verdict, controller, reason, time and observed/resulting proposal versions. A record matching the current revision is a historical fact, not a claim of current freshness or execution eligibility.

## State and evidence

Success atomically records the exception, BLOCKED → READY_FOR_REVIEW transition, version increment, audit entry and idempotent receipt. Deferred database constraints reject incomplete writes. Existing statement reservations remain held. A same-key replay returns its original receipt only after current session/permission checks. It does not prove that today's proposal head or dependencies still match that historical receipt. Change the request only with an explicit new action; no client automatically generates a replacement key after an uncertain response.

Simulation artifacts and raw verdicts never change. The original aggregate remains BLOCKED_INSUFFICIENT and `eligible_for_execution` remains false. SYNTHETIC provenance stays SYNTHETIC; accounting/oracles stay UNREVIEWED. READY_FOR_REVIEW permits a later independent review workflow; this slice adds no approval, execution, accounting sign-off or provider capability.

An override never carries into a new revision or simulation. Withdrawal retains its history and releases the existing reservations through the owning withdrawal path.

## Freshness and bounded decisions

The wrapper locks proposal, ledger, counter and captured dependencies using the existing lock order before checking `lv.proposal_freshness`. Changed dependencies return 409 PROPOSAL_STALE without silently updating proposal state. The maker can use the existing revalidation/resimulation flow.

The existing full-capture submission validator also requires identical pinned and observed ledger sequences. If dependency versions are unchanged but the sequence advanced, this action returns 422 CAPABILITY_UNAVAILABLE with reason FULL_CAPTURE_CHANGED, leaving the proposal unchanged. This does **not** redefine ADR-009 to mark every unrelated sequence change STALE. Current recovery is an explicit maker withdrawal and a newly created proposal with a fresh capture. General recapture of an otherwise FRESH blocked proposal is not added here.

- **OV-1 — PROPOSED:** bounded request/response representation, reason length and sole P-EVD-01 native capability. This operational slice does not accept new policy semantics or modify accepted decisions.
- **OV-2 — PROPOSED:** retain the current full-capture validator and explicitly refuse changed sequences. Broader recapture semantics remain unresolved.

## UI

Proposal details read and display recorded policy exceptions separately from frozen simulation results. Reasons render as plain text, including literal markup. Attribution/dependency details show the check at recording time. A different revision is labeled historical. The subsequent frontend slice adds a bounded controller form; see EVIDENCE_OVERRIDE_UI.md for presentation gates and same-key recovery. Approval and execution remain unavailable.

## Verification

`tests/test_evidence_overrides.py` covers real PostgreSQL controller/maker/tenant boundaries, raw verdict preservation, actual FAIL refusal, reason validation, idempotency and concurrent requests, version conflicts, revocation races, freshness limits, new revision history, FORCE RLS, immutability, atomic rollback/deferred guards and retained-history downgrade refusal. A loopback HTTP demonstration fingerprints 47 unrelated tables and financial counters before/after the workflow and replays the original simulation. Synthetic examples test software controls; they establish no accounting accuracy or production impact.
