# Native rejection and shared revision decisions (0020)

Owners: accepted Stage 2A §9.3 (READY_FOR_REVIEW → REJECTED with comment), Stage 2B Family 11 (`POST /proposals/{id}/reject`), §6.3 (one APPROVED or REJECTED decision per proposal/revision, atomic read-set revalidation and audit), §6.1 (release reservations), and tenant isolation/segregation rules. Native SANDBOX RecordSettlement only; accounting/oracles remain UNREVIEWED.

## API and atomicity

The route requires a live verified session with `review`, valid Origin/CSRF, Idempotency-Key, exact If-Match, and `{revision, comment}` with no extra fields or query parameters. The comment is required, nonblank well-formed Unicode, at most 4000 code points, preserved verbatim rather than trimmed. Unicode whitespace-only reasons are refused in both HTTP and database functions. No actor is taken from the body.

Rejection uses the same proposal row lock, authenticated role locks, shared unique revision-decision key, native capture/freshness checks and deferred proof as approval. Simultaneous approve/reject calls produce one decision and one terminal state; the loser gets ALREADY_DECIDED. Same-key retry returns the original receipt only after current authorization checks; changed content is refused. Current-state reads are separate from original receipts. The server hints advertise APPROVE and REJECT together only for the currently supported eligible independent reviewer/capture.

FRESH with unchanged full capture records one REJECTED decision, changes READY_FOR_REVIEW → REJECTED with version+1, releases all that revision's staged reservations with immutable release events, and commits transition, audit and exact cached response together. Rejection is terminal; a maker may start a distinct proposal using the released source. No financial posting, balance change, financial event or sequence advance occurs. Original simulation, verdicts, evidence exception and provenance remain unchanged. No execute endpoint is added.

A changed dependency follows the established review stale path: mark STALE, release claims, store the check/transition/audit/409 atomically, and record no rejection. Otherwise-FRESH full-capture sequence advance retains AP-2's explicit FULL_CAPTURE_CHANGED refusal. This slice cannot recapture or override that limit.

## Storage and reads

`lv.proposal_decision` keeps its unique `(tenant_id, proposal_id, revision)` and FORCE RLS. The kind permits APPROVED or REJECTED; REJECTED requires a nonblank reason. The legacy internal `approved_by` column is retained to avoid rewriting immutable approval rows; for REJECTED rows it holds the reviewer. The public history emits `kind: REJECTION` and `rejected_by`, never an approval label/actor field for a rejection. Both decisions bind exact revision, maker, simulation, check and resulting version. `lv.approval_check` adds a non-null decision_action with default `approve` for old rows; no existing value is rewritten. Runtime identities have no direct history writes.

History reads include approval, rejection and evidence exception with signed proposal/tenant-bound pagination. The frontend shows rejection with its literal comment, reviewer and pinned revision/check; a REJECTED filter is available. The initial 0020 milestone added the backend and readable history. The subsequent NATIVE_REJECTION_UI.md slice adds a dedicated rejection-authoring form and separate recovery namespace; do not reuse an uncertain approval key/body for a rejection.

## Bounded interpretation and limits

- RJ-1 — PROPOSED representation choice: required reason capped at 4000 Unicode code points, aligned with existing review/comment limits. The accepted contract requires a reason but gives no number.
- RJ-2 — PROPOSED conservative scope: rejection reuses the current review_blocker (maker/all editors excluded; no self-grant; latest review grant waits 24 hours) and current native freshness/full-capture gates. Stage 2B's general segregation/cooling language is broader than Stage 2A's explicit approve/execute wording. This slice offers no more-permissive rejection path; the interpretation remains recorded for ADR review, not silently accepted.
- RJ-3 — PROPOSED storage representation: extend the existing decision/check records and preserve the legacy internal reviewer column; public rejection schema uses rejected_by. No accepted document or prior AP/OV/ADR status is edited.

Automatic proof-pack publication, durable operator recovery, member management, execution and providers remain separate unimplemented capabilities. A rejection history entry is not a sealed proof pack or accounting review.

## Migration safety

0020 adds one metadata/default-backed check column and constraints/functions; it does not update existing financial or workflow rows. The frozen 0019 predecessor functions allow a lossless downgrade only when no rejection/rejection-stale history exists. A retained rejected decision/check blocks downgrade transactionally. Fresh empty upgrade/downgrade/upgrade and restored nonempty development-backup rehearsals are required before native application.
