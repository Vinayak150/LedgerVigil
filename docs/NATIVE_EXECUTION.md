# Native SANDBOX execution — 0021

## Scope and authority

This implementation adds synchronous execution for exactly one approved native SANDBOX RecordSettlement operation, using the existing 0.3.0 simulation and typed settlement rules. It does not enable BOOKS ledgers, external providers, BootstrapLedger, partial-capture execution, refunds, voids, arbitrary journals or production use. All accounting/oracles remain UNREVIEWED. Existing proposed decisions, including D-1–D-11 and PA-011-1, retain their statuses. No accepted source architecture/ADR is edited by this delivery.

Owning accepted execution requirements are the Stage 2A/2B native execution, role segregation, exact-money, immutable journal, optimistic concurrency and atomic commit/event contracts. Existing implementation ownership is preserved: docs/NATIVE_APPROVAL.md (exact decision and cooling), docs/SUBMISSION.md (claims), docs/RESIMULATION.md (pinned freshness), docs/PERIOD_CLOSE.md (ledger serialization), docs/SIMULATION.md and the typed settlement adapter. This document records implementation detail, not a new approval of disputed provider recovery semantics.

## API and transaction

POST /api/v1/proposals/{id}/execute requires the verified session, Origin/CSRF, execute permission, an exact positive integer revision body, If-Match and Idempotency-Key. Actor and tenant come from the session. The creator/submitting maker cannot execute. The current execute grant must be at least 24 hours old and not self-granted. Revocation is checked before a cached success can be read. The accepted native separation rule does not require executor and reviewer to be different people; it does require an independent maker. Administrator permissions are unchanged.

The proposal, ledger, sequence, periods, bindings, accounts and identity dependencies are locked. A current exact approval, supported simulator, native SANDBOX and full captured sequence are required. A changed dependency atomically marks the proposal STALE, releases its statement claims, and stores check/audit/transition/409 response without posting. A changed full capture that is otherwise fresh fails closed; selective rebasing remains outside scope.

A fresh request creates one transient BUILDING execution, calls a private human-attributed settlement writer and compares predicted postings with independently derived new-document postings. SQL and the pure Python verifier agree on exact canonical minor-unit hashes under lv-native-posting-match/1. A deferred constraint independently validates actual journal lines, current projections, inclusions, document versions, dates and rule versions. RECONCILED here means these bounded native facts agree; it is not independent accounting certification or external-provider reconciliation. The mathematical proof's authorizes_execution:false is deliberate: equality alone does not grant session authority.

The same transaction stores the RECONCILED receipt, proposal EXECUTED transition, POSTED statement claim, human actor, financial history/projections/counters, event chain, audit, idempotency response and durable completion notification. A unique (tenant, proposal, revision) execution prevents another key from posting the same revision. The response is sent only after transaction completion. GET /api/v1/executions/{id} reads the immutable receipt with normal tenant isolation, 404 masking and conditional representation ETags. The POST response ETag describes the resulting proposal version; GET uses its representation ETag.

## Failure and retry semantics

Concurrent duplicate keys use the existing 24-hour principal/tenant/route cache. A safe retry must preserve proposal, revision, key, body and original If-Match. It returns the original receipt even though the proposal version has advanced. A different key sees ALREADY_EXECUTED under INVALID_STATE_TRANSITION. After cache expiry, the permanent execution remains retained and readable when its execution id is known. The later execution-UI slice exposes a read-only current-revision lookup at GET /api/v1/proposals/{id}/execution, so a lost receipt id can be recovered independently of cache retention. Do not create a new proposal merely to recover a receipt. This is a local native transaction guarantee, not an exactly-once guarantee across external systems.

Known invariant/deferred-constraint failures roll the financial transaction back. A separate authenticated transaction attempts to record an immutable incident and audit. If identity or the database is unavailable, incident persistence can fail; only a redacted request-id log is guaranteed by the process while it remains running. No 500 response is cached as a successful effect.

A database transport error or unconfirmed commit returns INTERNAL / NATIVE_EXECUTION_OUTCOME_UNKNOWN with retryable:false and instructions to read back and retain the original request identity. It must never claim rollback merely because the COMMIT acknowledgement was lost. Tests inject both a rollback before commit and an exception after a real successful commit, then recover with the original key and prove one financial effect. Automatic retry with a fresh key is not implemented.

## Ingestion/execution ordering

The existing synchronous ingestion path obtains a shared ledger lock through lv.ingestion_ledger; execution holds an exclusive ledger lock. BEFORE INSERT fences cover source_file, import_batch, source_record, staged_document and staged_statement_item, including direct lv_ingest writes. New file/batch metadata records its actual transaction id; children must share that parent transaction. This prevents a later append from changing an already frozen statement. Historical metadata stays NULL; no fabricated provenance or historical financial fields are backfilled. Production code has no historical unsafe fallback. tests/legacy_ingestion_0020.py is a frozen predecessor used only to construct explicit old-schema migration fixtures. Asynchronous multi-transaction ingestion is not supported by this synchronous contract.

## Database privilege and migration boundary

All three new execution/check/incident tables use FORCE RLS. Runtime has no direct write privilege and cannot call the private native settlement writer. Seed writers and grants remain separate; accepted seed history is not relabeled as human execution. New journal/document actor columns enforce exactly one seed or human actor. The migration adds no financial rows. A restored development backup and the native integration compare all 57 original tables on their original columns.

Downgrade refuses any retained execution, check or incident instead of discarding evidence. Empty downgrade restores frozen predecessor functions, constraints and grants. PostgreSQL's unused EXECUTION enum label remains after empty downgrade; no old writer can use it. The private development dump is excluded from review packages.

The completion notification is persisted in the existing outbox with a separate native subject. The subsequent 0026 candidate consumes this immutable notification through the native proof-pack worker and records its notification id in the anchor audit; the scheduler also discovers predecessor execution notifications. See PROOF_PACKS.md for canonical sealing, independent local Git anchoring and restore verification. No external notification delivery or offsite durability is claimed.

## Preserved historical simulation

The original pinned 0.3.0 result is byte-identical after execution, including its historical eligible_for_execution=false/adapter availability metadata. Those fields described the runtime at simulation time. Current authorization is the exact native approval plus live freshness and execution gates above; neither the stored policy verdict nor accounting status is rewritten to imply approval. Native capability remains unavailable outside this installed bounded path. A dedicated execution UI must present historical prediction separately from the current committed receipt.

## Implementation choices awaiting architectural ratification

| ID | Status | Bounded implementation choice |
| --- | --- | --- |
| NE-1 | PROPOSED | Native-only receipt/check/incident tables and BUILDING transient / RECONCILED durable shape; no external recovery states inferred. |
| NE-2 | PROPOSED | Independent POSTED source-workflow state, retaining exact proposal/revision ownership. |
| NE-3 | PROPOSED | Durable native completion subject in outbox without dispatching it through the maintenance consumer. |
| NE-4 | PROPOSED | Current authenticated native gates may enable this installed slice while preserving historical simulator capability metadata. |
| NE-5 | PROPOSED | Existing synchronous source ingestion requires file, batch and children in the same transaction, with historical transaction metadata left NULL. |
| NE-6 | PROPOSED | INTERNAL reason extensions distinguish invariant rollback from unconfirmed transport/commit; only original request identity is recoverable automatically by the API cache. |

These are implementation records, not approvals of earlier proposed ADR changes or authorization for production.

## Verification and evidence

The dated native-execution review package records real native PostgreSQL test logs, exact baseline/source hashes, upgrade fingerprints, live HTTP request-loss proof and browser screenshots. It includes the initial failures and their corrected runs. A public invoice dataset remains separately available for observation/held-out evaluation; public source rows have not been represented as real customer settlement executions. This execution slice uses explicitly synthetic actors and financial inputs with independently stated expected lines. It establishes functional/integrity behavior, not business impact, detection accuracy or production readiness.

| Requirement | Primary evidence |
| --- | --- |
| Permission, maker, cooling, revocation and tenant gates | tests/test_native_execution.py authorization/cache/foreign cases |
| One financial effect under 20 competing executors / 12 same-key callers | native execution concurrency tests |
| Exact amounts, human attribution, replay and immutable history | native execution finance/privilege tests; HTTP_DEMO.json |
| Predicted/new-document equality independent of row order and Decimal context | tests/test_execution_posting_match.py (20 cases) |
| Missing audit/cache/transition/outbox/posted/proof refuses commit | six deliberately removed safeguard cases |
| Balanced wrong account, altered approved date and wrong rule version | three valid-SQL fault cases assert the expected database diagnostic |
| Ingestion before/after execution, late append and period close races | native execution lock-wait tests with actual PostgreSQL wait evidence |
| Lost COMMIT acknowledgement, before and after durable commit | parameterized transport recovery test |
| Populated upgrade, preserved original rows, safe downgrade | migration roundtrip tests; restored-upgrade.json; DELIVERY.json |
| UI reads committed state and balances without inventing execution controls | browser/checks.json and screenshots; BROWSER_DATA_CHECK.json |

## Next bounded work

The receipt lookup and deliberate confirmation/recovery UI are now installed by the subsequent slice; see docs/NATIVE_EXECUTION_UI.md. The subsequent 0026 candidate adds the proof-pack consumer and bounded same-host recovery checks. Independent accounting review is still required before broader execution claims. Provider sandbox experiments, MFA/password recovery, production readiness, and practical independently labeled evaluation remain separate release gates.
