> **Detection 1.1.0 candidate: `0033_detection_overlap_dates`** (on the unapplied combined `0032_detection_findings`): two advisory, deterministic rules: overlapping payment claims and payout-date differences (PROPOSED DET-9, DET-10; docs/AI_DETECTION.md §8). Detection remains off unless `LV_DETECTION_ENABLED=1`. Not installed; the installed application is unchanged.

> **Combined integration candidate: `0032_detection_findings`** (on installed `0029_account_patch`): `0030_override_independence` (RG-08, user-approved exception-grantor independence), `0031_semantic_revalidation` (RG-03, user-approved semantic revalidation; see docs/SEMANTIC_REVALIDATION.md and docs/RG08_DECISION.md) and `0032_detection_findings` (first detection slice, off unless `LV_DETECTION_ENABLED=1`; PROPOSED DET-1..DET-8 in docs/AI_DETECTION.md; D1 adds the accepted Stage 2B `Problem` schema to the owning OpenAPI). Whether this revision is installed is recorded only by the integration receipt in docs/reviews/20261106-133000-claude-combined-integration/; until that receipt reports the live apply as PASS the installed application remains `0029_account_patch` (integration receipt: docs/reviews/20261106-033636-codex-proof-integration/REVIEW.md). Accounting is UNREVIEWED; provider/BOOKS/production capabilities, offsite recovery and detection effectiveness on real data are not established. Later sections retain the original milestone history.

# LedgerVigil native application

**Current checkpoint: `0021_native_execution`**, 5 November 2026. The authenticated native SANDBOX RecordSettlement API now commits an approved settlement, immutable human-attributed documents, postings, balance projections, event chain, audit, statement completion and retry receipt in one transaction. The frontend now offers deliberate native SANDBOX execution with immutable receipt readback and original-request recovery; see [execution UI](docs/NATIVE_EXECUTION_UI.md). Read-only integrity bundle download and an offline verifier are now available from the execution receipt; see [proof export](docs/NATIVE_EXECUTION_PROOF.md). Provider/BOOKS/production writes remain disabled; accounting/oracles remain UNREVIEWED. See [native execution](docs/NATIVE_EXECUTION.md) for gates, evidence and unresolved boundaries. The milestone sections below retain implementation history.

## Local setup and verification

Requirements: Python 3.14, Docker CLI, Docker Compose and the existing Colima profile. From `/Users/vinayakmahindrakar/Desktop/LedgerVigil/app`:

```sh
colima start
scripts/setup
scripts/dev up
scripts/dev db-prepare
scripts/dev migrate
scripts/dev test
scripts/dev guards
# Optional: creates a NEW synthetic SANDBOX and writes three manual journals
scripts/dev demo
```

`db-prepare` creates/verifies development roles (including the separate `lv_seed` login) and PostgreSQL prerequisites. `migrate` connects through `lv_migrator` and runs Alembic as non-superuser `lv_owner`. `test` creates, migrates and removes randomly named disposable test databases; it does not reset the development database. `guards` intentionally disables one safeguard in each disposable database and checks that the corresponding invariant test fails. The expected failures are recorded as such.

`db-prepare` and `migrate` are idempotent. Run them before tests on a fresh installation. The scripts support only the dedicated loopback development database and generated test database names. The local `postgres` credential is used for provisioning/test-database lifecycle, never by application code.

`requirements.lock` freezes runtime/test versions observed on Python 3.14.6 / macOS arm64. It is not a hash-verified dependency lock or a cross-platform compatibility claim. PostgreSQL 18.6 is pinned by image digest in `compose.yaml`.

## Running services

- Database: `127.0.0.1:55432`, database `ledgervigil_dev`, isolated volume `ledgervigil-native-dev_pgdata`.
- `scripts/dev status` inspects the container; `scripts/dev stop` stops it while keeping data; `scripts/dev up` resumes it.
- `scripts/dev api` starts the liveness-only API at `http://127.0.0.1:8000/health`; Ctrl+C stops it. `/health` is not a readiness or financial-correctness check.
- ` .venv/bin/python scripts/verify_http.py` checks a temporary loopback API process and stops it afterward.

Colima and the database are left running. No API, worker or web process is left running by the tests. Web, Celery, scheduler and Redis remain part of the later integrated stack under ADR-019.

## Database boundary

Configuration migration 0001 creates seven tables in schema `lv`: global `currency`, plus tenant-scoped `tenant`, `ledger`, `account`, `account_role_binding`, `party` and `fiscal_period`. Every scoped table has ENABLE/FORCE RLS and tenant-leading keys. Ledger-scoped references include both tenant and ledger.

`lv_app` is an actual LOGIN with no superuser, RLS bypass, role-management or database-creation capability. It cannot assume `lv_owner`, create schema objects, change policies, access migration metadata, create tenants, modify currencies, delete or truncate these tables. It can read its tenant and insert/update the five configuration tables. These permissions are **not user-level authorization**: no configuration HTTP endpoints are exposed yet. Migration 0002 adds eleven financial/principal tables. `lv_app` and `lv_seed` can read their tenant rows but cannot directly insert, update, delete or truncate the new tables. Only `lv_seed` can execute the narrow developer seed function.

`tenant_transaction` is an internal helper for one trusted tenant per transaction. It rejects nested context switching, uses transaction-local settings and resets on success/failure. Future API/job callers must supply the tenant from authenticated session/job context. RLS does not authenticate a UUID or defend against a compromised service credential that deliberately selects another tenant.

The database checks account role/type/contra/party/active consistency, prevents a bound account from drifting, derives normal debit/credit side, enforces functional currency/exponent references, prevents tenant/ledger identity moves and purpose changes, and enforces finite non-overlapping calendar months with no reopening.

An incomplete chart can be staged during configuration. There is no “ledger operationally ready” endpoint or posting service yet; required-role completeness must be checked before operations are exposed. Parent references cannot cross ledgers or self-reference; longer reporting-parent cycles are not validated in this checkpoint.

## Secrets and migration lifecycle

`.env` contains the local administrative password; `.env.roles` contains separate application/migrator/seed passwords. Both are ignored and created with mode 0600. Do not distribute them. `.docker-local` isolates Docker settings from the obsolete global Docker Desktop credential helper. Existing volume data and its password must stay together.

Alembic is the schema authority. Revisions `0001_configuration` and `0002_posting` are applied to the development database. `lv_meta` and the global roles/`btree_gist` prerequisite are provisioned separately; the revision owns schema `lv` and its objects. Downgrade to base removes all `lv` configuration and financial data and is allowed by the runner only in explicitly disposable test databases. It does not remove roles, extension or migration metadata. The development database has no automatic reset command.

## Evidence and remaining work

`evidence/posting/tests.txt` records **135 passed**. Eight deliberate guard removals were caught in disposable databases; see `evidence/posting/guard-sensitivity.json`. Tests cover deferred balance enforcement, exact inputs before NUMERIC coercion, commit rollback, gapless numbering, real period-close races, duplicate retries, RLS/privileges and immutable history. The earlier evidence folders remain historical snapshots. One upstream Starlette/TestClient httpx deprecation warning remains.

A new local synthetic SANDBOX demonstration has three manual journals and replayed balances of INR 49,000 bank debit, 1,000 fee debit, 50,000 revenue credit, and zero clearing/customer AR. Its evidence is under `evidence/posting/demo-*`. `scripts/dev demo` creates a NEW fixture ledger each time; it does not overwrite or reset an existing one. Data is explicitly SYNTHETIC and accounting oracle status is UNREVIEWED.

The only posting capability is the accepted developer SANDBOX seeding exception. BOOKS and provider ledgers are refused. No authenticated financial API, approval workflow, Invoice/Payment/Settlement operation, reversal, policy verdict, outbox or externally anchored proof pack is implemented. Do not expose the seed function as execution. A balanced entry still requires business/evidence validation in the future policy workflow.

See [native posting review and limitations](docs/NATIVE_POSTING_FOUNDATION.md) for dependency analysis, test traceability, security boundary, the limited canonical event format and the next milestone. [Configuration review](docs/CONFIGURATION_FOUNDATION.md) is the previous checkpoint; its “not implemented” statements describe that earlier revision.

## Independent review checkpoint — 3 November 2026

An independent review (package `docs/reviews/20261103-000716-claude-native-foundation-review/`) added forward migration **`0003_pin_function_search_path`**. It changes no data and no function bodies, and it leaves `0001_configuration` and `0002_posting` byte-for-byte unchanged. It sets `search_path = pg_catalog, pg_temp` on every `lv` function. Before it, a login holding PostgreSQL's default `TEMPORARY` privilege could shadow a type such as `uuid` with a temporary domain. That domain's check then ran the caller's own code as `lv_owner` inside the `SECURITY DEFINER` seed function (finding R-01). The development setup here revokes `TEMPORARY`, so this database was not exposed, but its protection depended on provisioning outside the migrations.

The development database is still at `0002_posting` until you run `scripts/dev migrate`. `scripts/dev test` then runs **154 tests**: the earlier 135, 18 independent boundary scenarios in `tests/test_independent_review.py`, and 1 upgrade-preservation test. `scripts/dev guards` now checks 9 deliberate guard removals. These results were reproduced on PostgreSQL 18.6 in the reviewer's environment, not yet on this Colima instance.

A proposed resolution, not yet accepted, is recorded for the remaining open finding. `lv_app` can close a period, or create one already closed, without a commit batch or ledger event (R-02, Stage 2A §8.1). The `PERIOD_CLOSE` workflow it needs is unimplemented.

## Read-only settlement adapter — 3 November 2026

`src/ledgervigil/kernel_adapter/` connects the native ledger to the existing accounting kernel, which is vendored byte-for-byte unchanged as `src/ledger_kernel/` with provenance in `docs/KERNEL_PROVENANCE.json`. It runs as the restricted `lv_app` login, in one REPEATABLE READ, READ ONLY transaction, and records the snapshot's ledger sequence and chain heads. It returns the kernel's predicted settlement postings, its balance effects and the checks the kernel actually supports, labelled SYNTHETIC / UNREVIEWED / `eligible_for_execution: false`.

What it does not do:
- write anything;
- call the seed function;
- expose an HTTP endpoint;
- create proposals, approvals or executions.

Because neither processor statements nor settlement inclusions are persisted yet, the evidence checks report `INSUFFICIENT_EVIDENCE`, and the kernel outcome is `BLOCKED` by design.

`.venv/bin/python -m tools.settlement_adapter_demo` writes a new synthetic demonstration under `evidence/adapter/`. Details and limitations are in `docs/reviews/20261103-005514-claude-kernel-adapter/`.

## Synthetic statement ingestion — 3 November 2026

Migration **`0004_statement_ingestion`** adds the four tables Stage 2A §19.2 path A allows: `source_file`, `import_batch`, `source_record` and `staged_document`, plus `staged_statement_item`. It also adds a narrowly scoped **`lv_ingest`** login. That login can only INSERT into those tables. It has no privilege on documents, postings, projections, counters or history, and no EXECUTE on any function. Ingested rows are immutable. Raw bytes are kept with a database-checked SHA-256. Amounts are stored exactly as received, as decimal text. Only the LedgerVigil-defined fixture format **`ledgervigil.synthetic-processor-statement` v1** is accepted (`src/ledgervigil/ingestion/statement_format.py`, example in `fixtures/statements/`), and only into native SANDBOX ledgers. It claims no compatibility with any real processor.

**Before migrating an existing database, run `scripts/dev db-prepare` once.** It creates `lv_ingest` and appends an `INGEST_PASSWORD` line to `.env.roles`, leaving existing credentials unchanged.

Commands:
- `.venv/bin/python -m tools.ingest_statement --tenant … --ledger … --format ledgervigil.synthetic-processor-statement --version 1 FILE` ingests one file.
- `.venv/bin/python -m tools.statement_ingestion_demo` writes a new end-to-end demonstration under `evidence/ingestion/`.

The settlement adapter now loads the staged statement for the requested processor/payout in the same snapshot as the ledger facts. It reports evidence checks (`E-*`) that each trace to a source record and file hash. Statement values are reported as observations, not ledger facts. Item identity stays **UNRESOLVED**, because there are no typed Payment documents. Duplicate settlement stays **NOT_ESTABLISHED**. The outcome therefore remains **BLOCKED**. Details: `docs/reviews/20261103-012517-claude-statement-ingestion/`.

## Typed synthetic processor Payments — 3 November 2026

Migration **`0005_processor_payments`** adds typed `PAYMENT` documents for native SANDBOX ledgers, created only by the developer seed function **`lv.seed_processor_payment(ledger, request)`**. The request contains only `processor_ref`, `processor_item_id`, `party_id`, `currency`, `amount`, `received_date` and `description`, all as strings. The database then, in one transaction:
- writes the document, its immutable version 1 (origin SEED, provenance SYNTHETIC, actor from the seed login), `document_current`, the authoritative identity row `processor_payment_identity` and the `payment_current` projection (RECEIVED, applied 0);
- derives the Stage 2A §5.1 `DOCUMENT_POSTING` (rule `sp1-posting/1.0`): Dr PROCESSOR_CLEARING / Cr AR_CONTROL for the customer, from the ledger's role bindings;
- updates the balance projections, ledger event and counter under the existing lock, period and deferred-validation protections.

Identity is **(tenant, ledger, processor_ref, processor_item_id)** and is unique. An identical repeat returns the existing payment (`replayed: true`); different content for the same identity is refused, with nothing written. BOOKS and provider-connected ledgers are refused. There are no invoices, applications, settlements, voids, refunds, approvals or executions. `lv_ingest` has no access to these tables. Existing manual journals are not converted.

The settlement adapter accepts typed items `{"document_id": "<payment uuid>", "kind": "PAYMENT"}`. Each statement item is resolved by exact processor identity to a typed Payment version, then currency and amount are compared. An equal amount never establishes identity. For the INR 50,000 / PMT-2001 example, `E-ITEM-01` now **PASSES**. The result stays **BLOCKED** because duplicate settlement is `NOT_ESTABLISHED` (no inclusions or execution records) and execution is `NOT_AVAILABLE`. Legacy `source_ref` items still work for older manual-journal fixtures and stay UNRESOLVED.

`.venv/bin/python -m tools.payment_matching_demo` writes a new demonstration under `evidence/payments/`, from `fixtures/payments/typed-payment-matching-v1.json`. Details: `docs/reviews/20261103-015810-claude-typed-payments/`.

## Typed synthetic processor Settlements — 3 November 2026

Migration **`0006_processor_settlements`** adds typed `SETTLEMENT` documents for native SANDBOX ledgers, created only by the developer seed function **`lv.seed_processor_settlement(ledger, request)`**. The request holds `processor_ref`, `payout_id`, `payout_date`, `currency`, `gross`, `fee`, `net`, `items` (`{document_id, kind: "PAYMENT", amount}`, each at the payment's full amount) and `description`, all as strings.

In one transaction under the ledger lock, the database:
- checks every item: a typed processor Payment of this tenant and ledger, same processor, RECEIVED, full amount, not already in an active settlement, and not repeated in the request;
- requires gross = Σ items, net = gross − fee, fee ≥ 0, net ≥ 0, gross > 0, and an OPEN period;
- writes the settlement document and its immutable version 1, the payout identity `processor_settlement_identity` (unique per tenant, ledger, processor and payout), `settlement_current` (with the I-09 CHECK), the immutable inclusion history `settlement_inclusion`, and the active-inclusion projection `settlement_inclusion_current` (its primary key is I-08);
- gives each included Payment a new immutable version 2 (status SETTLED plus the settlement link) and advances its current pointer by compare-and-swap. The Payment's posting is unchanged, still names version 1, and no reversal is written;
- derives Dr BANK net / Dr FEE fee / Cr CLR gross (`sp1-posting/1.0`), omitting zero lines, and updates balances, event chain and counter.

An identical repeat returns the stored result. A changed body for the same payout, or a payment already in another settlement, is refused with nothing written. Refund items are refused as unsupported. Inclusions cannot be updated or deleted; releasing one needs a future audited VoidDocument.

The settlement adapter now loads the payout identity and the active inclusions in its snapshot. `E-DUP-01` is **PASS** when the payout is unused and no requested payment is included. A duplicate preview is refused by the kernel (`DUPLICATE_SETTLEMENT` / `ITEM_ALREADY_SETTLED`) with the evidence attached. This is a **preview** check only: it reserves nothing and is not execution-time revalidation. Execution stays `NOT_AVAILABLE`, and the kernel's SOURCE-statement checks stay `INSUFFICIENT_EVIDENCE` for synthetic statements.

`.venv/bin/python -m tools.settlement_demo` writes a new demonstration under `evidence/settlements/`, from `fixtures/settlements/typed-settlement-v1.json`. Details: `docs/reviews/20261103-024100-claude-typed-settlements/`.

## Authenticated sessions, verified tenant context and server-side permissions — 3 November 2026

Migration **`0007_identity_sessions`** adds the schema **`lv_identity`**: users (Argon2id password hashes), tenant memberships, role grants, the Stage 2B §5.1 role→permission table, server-side sessions (only the SHA-256 of the token is stored; 30-minute idle and 12-hour absolute expiry; revocation is permanent), login attempts for throttling, and a per-tenant hash-chained security audit log. The runtime login `lv_app` has **no table privileges** in this schema; it can only execute five pinned SECURITY DEFINER entry points (`login_prepare`, `login_finish`, `session_context`, `logout`, `switch_tenant`). Administration functions are owner-only and reached through the local command below, never through HTTP.

FastAPI owns authentication. Routes (generated OpenAPI in `api/openapi.json`):
- `POST /api/v1/auth/login` → session cookie `__Host-lv_session` (Secure, HttpOnly, SameSite=Lax, Path=/) and the session body, including its CSRF token. Every refusal is the same `401 UNAUTHENTICATED`; repeated failures give `429 RATE_LIMITED`.
- `POST /api/v1/auth/logout`, `POST /api/v1/auth/switch-tenant` (need `X-CSRF-Token`); switching tenant verifies membership and issues a **new** session.
- `GET /api/v1/me`, `GET /api/v1/tenants/current`, `GET /api/v1/permissions`.

Every authenticated request resolves its session **inside the database transaction it uses**: `lv_identity.session_context` binds `app.tenant_id`, `app.user_id` and `app.session_id` from the session row, so tenant and user never come from headers, bodies or the frontend. Future routes declare `Depends(require_permission('<verb>'))`. Administrators have no `propose`, `review` or `execute` permission. No seed, ingestion-administration, period, execution or role-management route exists.

Local development over plain HTTP is explicit: `scripts/dev api` runs `tools.serve_api --local-http`, which sets `LV_ENVIRONMENT=development` and `LV_COOKIE_MODE=local-http` (cookie `lv_session_local`, not Secure, loopback hosts only). Without both settings the secure configuration applies.

Synthetic demonstration users: `.venv/bin/python -m tools.identity_admin provision-demo --credentials-file <path outside the repository>` (random passwords, written only to that 0600 file). `.venv/bin/python -m tools.auth_demo` records redacted example flows under `evidence/identity/`. Details: `docs/reviews/20261103-032400-claude-identity-sessions/`.


## Native integration gate — 3 November 2026

The baseline and identity package were reproduced on this Mac. Identity is now integrated with forward migration `0008_identity_force_rls`, explicit owner-only FORCE RLS policies, cookie-free idempotent logout, and executable startup scripts. The shared app passes 435 tests. Historical evidence remains unchanged. The local-only boundary and Q-16 limitation remain; no production or BOOKS use is enabled. See `/Users/vinayakmahindrakar/Desktop/LedgerVigil/docs/reviews/20261103-035255-codex-native-integration-review/PACKAGE/REVIEW.md` and its FRONTEND_NEXT_SLICE.md. Period close permission is already `administer` in Stage 2B Family 4; R-02 implementation remains outstanding.

## Authenticated, atomic period close (R-02 remediation) — 3 November 2026

Migration **`0009_period_close`** implements Stage 2B Family 4 `POST /api/v1/periods/{period_id}/close` under the existing `administer` permission, plus `GET /api/v1/ledgers/{ledger_id}/periods` (`read`).

A close is one database transaction through `lv.close_period`, a pinned SECURITY DEFINER function and the only runtime entry point. The transaction:
- resolves the session from its token hash and re-checks `administer`;
- takes the ledger counter lock, as every posting path does;
- writes a `PERIOD_CLOSE` commit batch and ledger event;
- applies the period CAS (`OPEN → CLOSED`, version + 1);
- queues a `STALENESS_SCAN` work item through the outbox;
- records the audit event and the stored idempotent response.

A deferred validator re-checks all of this at COMMIT.

`Idempotency-Key` and `If-Match` follow Stage 2B Parts 2 and 8.1. The route commits before it responds.

**R-02 is closed:**
- `lv_app` has no `UPDATE` on `lv.fiscal_period`;
- periods can only be created OPEN, and never before a closed period;
- even the owner cannot change status without the audited commit;
- earlier unaudited closes are labelled `LEGACY_UNAUDITED`.

Only native SANDBOX ledgers can be closed. The staleness job is queued, but no relay or worker consumes it yet.

Details: `docs/PERIOD_CLOSE.md`.

## Authenticated native financial reads — 3 November 2026

Stage 2B Family 4 read routes, permission `read`:
- `GET /api/v1/ledgers`
- `GET /api/v1/ledgers/{ledger_id}`
- `GET /api/v1/ledgers/{ledger_id}/accounts`
- `GET /api/v1/ledgers/{ledger_id}/periods` (now cursor-paginated, closing gap G-3)
- `GET /api/v1/ledgers/{ledger_id}/trial-balance`

**Reading:**
- Every route runs in the verified-session boundary, then in a READ ONLY transaction. Row-level security is the tenant filter, and other-tenant ids are 404.
- Lists use signed, context-bound cursors (`LV_CURSOR_KEY`, at least 64 hex characters, for multi-process deployments), `limit` 1–200 and stable `(field, id)` ordering. Unknown query parameters are 400.
- Every read returns a representation `ETag`. `If-None-Match` gives a 304 only after authentication.

**Trial balance:**
- It reports exact Money strings at `as_of_seq`, the committed head or a requested earlier sequence.
- The figures are bounded by `seq <= as_of_seq`, so the lines, totals and sequence always agree.
- At the head, the current-state projection must agree with the postings.
- `as_of_date` is refused until its inclusivity is decided.
- It is served for native SANDBOX ledgers only.

No migration, grant or new database function was added. Accounting status stays UNREVIEWED.

Details: `docs/LEDGER_READS.md`.

## Proposal drafts and revision history — 3 November 2026

Migration **`0010_proposal_drafts`** implements the draft subset of Stage 2B Family 8:
- `POST /api/v1/proposals` (`propose`; `Idempotency-Key`);
- `GET /api/v1/proposals`, `GET /api/v1/proposals/{id}` and `GET /api/v1/proposals/{id}/revisions` (`read`);
- `PATCH /api/v1/proposals/{id}` (`propose`; `If-Match`).

This initial milestone introduced DRAFT proposals on native SANDBOX ledgers, with `RecordSettlement` operations. Unsupported operations, adapters, refund items, attachment evidence and BootstrapLedger are refused explicitly.

**How writes work:**
- Every write goes through a pinned SECURITY DEFINER function. It re-binds the session, re-checks `propose`, and resolves every referenced payment, statement and evidence id inside the proposal's ledger.
- It then writes, in one transaction: the proposal, an immutable revision (author, session, content hash), reference rows with tenant- and ledger-scoped foreign keys, the audit event and the stored idempotent response. A deferred validator re-checks this at COMMIT.
- The runtime role can only read the proposal tables.
- The maker and every editor are recorded for future approval exclusions.

Drafts do not change any financial table, balance, ledger sequence or ledger event. Later milestones below add submission, simulation and independent approval; execution remains unavailable. Accounting status remains UNREVIEWED.

Details: `docs/PROPOSAL_DRAFTS.md`.

## Source reads for settlement-draft selection — 3 November 2026

Six read-only routes (`read`; READ ONLY transactions; no migration) let a user find what a `RecordSettlement` draft cites without pasting identifiers:
- `GET /api/v1/ledgers/{id}/documents` and `GET /api/v1/documents/{id}` (Family 5): typed payments and settlements with actual statuses, versions, exact Money, settlement links, postings and a citable `DOCUMENT_VERSION` evidence reference;
- `GET /api/v1/imports`, `/imports/{id}`, `/imports/{id}/records` and `/imports/{id}/staged-documents` (Family 3): batches with their stored outcome and file hash, raw source records with their SHA-256, and staged settlement statements with their items.

Isolation is the session's tenant under FORCE row-level security, with ledger-scoped joins; other tenants' ids are 404 like absent ids. Lists use strict queries and signed, filter-bound cursors. Undecided filters (`date_from`, `date_to`, `q`, `as_of_seq` on documents) are refused, never ignored. Nothing here decides eligibility, matching, reconciliation or posting. Reads change no financial, ingestion, proposal or audit state.

Details: `docs/SOURCE_READS.md`.

## Simulation foundation (native RecordSettlement) — 3 November 2026

Migration **`0011_simulation_runs`** adds an immutable `lv.simulation_run` table, written only by the SECURITY DEFINER `lv.simulation_record` (it re-binds the session and re-checks `propose`). The package `ledgervigil.simulation` captures a draft revision and its native SANDBOX ledger facts from **one** REPEATABLE READ snapshot into a canonical input bundle. It pins the snapshot, a server-computed read set (payment documents and the payout period, with versions), the cited statement and evidence hashes, and every installed version. It then evaluates the bundle purely with the existing adapter and kernel.

**What a run records.** Predicted postings, account deltas and document effects — or a typed rejection: `PRECONDITION_FAILED` (DUPLICATE_SETTLEMENT, ITEM_ALREADY_SETTLED, PERIOD_CLOSED, ...), `UNSUPPORTED_BY_PROFILE` or `CAPABILITY_UNAVAILABLE`. Each run is stored with SHA-256 hashes of its inputs and outcome.

**What it does not do.** No policy set is installed, so a run carries no verdicts and verification is NOT_EVALUATED. Accounting is UNREVIEWED.

**Reading and replaying runs.**
- `GET /api/v1/simulations/{id}` reads a run.
- `POST /api/v1/simulations/{id}/replay` recomputes from the stored bundle only and compares hashes, with a 409 if a pinned version is not installed.

**Not built:** there is no creation route, `/submit`, proposal state change, approval or execution. Simulation changes no financial table, balance, counter or event.

Details: `docs/SIMULATION.md`.

## Native simulation and policy integration — 4 November 2026

Simulation and policy packages were independently verified on the Mac. New captures use simulator 0.2.1 with an isolated precision-34 Decimal context; installed 0.1.0 and 0.2.0 profiles preserve frozen replay compatibility. The SP-1 registry supplies explicit verdicts and aggregates from captured facts. SYNTHETIC statement evidence remains BLOCKED_INSUFFICIENT; accounting stays UNREVIEWED and eligible_for_execution is false. Runs are immutable; read and replay routes exist, while creation remains an internal service with no public simulation/start or submission endpoint.

SM-C and PV-C choices remain PROPOSED. Submission still depends on account-version read sets, statement reservations and the other documented workflow contracts. Integration does not approve decisions or enable production, approvals, execution, provider access or workers. See docs/SIMULATION.md and docs/POLICY_VERIFICATION.md.

## Versioned account dependencies — 4 November 2026

Migration 0012_account_versions and simulator 0.3.0 add account configuration/balance dependency versions to native settlement read sets. Changes to bindings and financial balance projections invalidate those counters atomically. Existing historical profiles remain replayable. Submission, statement reservations and stale-state transitions are not enabled. See docs/ACCOUNT_READ_SETS.md. Accounting remains UNREVIEWED.

## Atomic settlement submission — 4 November 2026

Migration 0013_submission adds native SANDBOX single-settlement submission, immutable snapshot/read-set/verdict/transition artifacts, statement reservations and maker withdrawal/release. Matching synthetic evidence stays BLOCKED_INSUFFICIENT. No approval/execution/provider/worker capability is enabled. See docs/SUBMISSION.md for transaction boundaries, conservative lock scope, idempotency, limitations and proposed choices.

## Period-close staleness maintenance

The native period-close STALENESS_SCAN queue now has a fenced Celery/Redis consumer.
It marks affected pending proposals STALE and releases their statement claims without
financial posting. See docs/PERIOD_STALENESS.md for the restricted worker role, pinned
local broker profile, explicit startup commands, recovery protocol and scope limits.
The worker/scheduler are separate processes; launching the normal API/web stack does
not silently start them. General source/configuration staleness and human review,
re-simulation, approval and execution remain separate work.

## Native re-simulation and dependency checks (0015)

Maker-only STALE re-simulation, explicit point-in-time freshness checks (RC-1
PROPOSED additive API) and immutable simulation history are connected end to end.
See [docs/RESIMULATION.md](docs/RESIMULATION.md) for scope, evidence and limitations.
The later native approval milestone below adds independent decisions. Execution remains unavailable; accounting remains UNREVIEWED.

## Public-data practical evaluation

The checksum-pinned UCI Online Retail II reader and isolated PostgreSQL evaluation
command now exercise real invoice-line data. Original rows, precision issues and
duplicates are retained; no settlement truth or financial posting is invented.
See [docs/PRACTICAL_EVALUATION.md](docs/PRACTICAL_EVALUATION.md) for the command,
measured scope and proposed normalization/duplicate decisions.

## Whole-invoice admission review

The real-data command now supports `--admission-plan` and a complete-file inspector.
Whole invoices are held for incompatible lines, repeated content, cross-sheet
identities or raw timestamp conflicts. No rounding or posting is enabled. See
[docs/INVOICE_ADMISSION.md](docs/INVOICE_ADMISSION.md).

## Public source-plan registration

Migration 0016 adds a tenant-isolated, audited metadata registry for the pinned
public UCI admission plan. See [docs/SOURCE_REGISTRY.md](docs/SOURCE_REGISTRY.md)
for the proposed additive API and its bounded scope.

## Indexed public source inspection (SR-2 PROPOSED)

Authenticated group, hold and original-row read APIs are available for the pinned public observation plan. This adds no financial eligibility, posting or private-upload support. See [PUBLIC_PLAN_INSPECTION.md](docs/PUBLIC_PLAN_INSPECTION.md). The frontend API types and the read-only invoice/hold inspector are connected. In Transactions, choose a GBP sandbox ledger and open Public dataset; see [the frontend guide](web/README.md).

## Proposal discussion

Authenticated, append-only proposal comments are available in the current-content view. Analysts and controllers can discuss evidence with immutable author/revision attribution; comments cannot approve, reject or execute. See [PROPOSAL_DISCUSSION.md](docs/PROPOSAL_DISCUSSION.md) for the accepted route contract, CM-1/CM-2 proposed representation choices and verification limits. Accounting remains UNREVIEWED.

### Controller evidence exceptions (native SANDBOX)

Migration `0018_evidence_overrides` adds the bounded ADR-014 controller exception and read-only exception history in proposal details. The original simulation/verdicts and synthetic provenance stay unchanged; READY_FOR_REVIEW is not approval or execution. See [EVIDENCE_OVERRIDES.md](docs/EVIDENCE_OVERRIDES.md) for limits, freshness refusals and proposed OV-1/OV-2. The controller-authoring slice below uses this API. The later native approval slice adds independent decisions; execution remains unavailable.

### Controller exception authoring UI

Authorized controllers can now record the bounded evidence exception from proposal details, with explicit acknowledgement, version guarding and preserved same-key recovery for uncertain responses. The existing API remains authoritative. See [EVIDENCE_OVERRIDE_UI.md](docs/EVIDENCE_OVERRIDE_UI.md). This adds no approval, execution or accounting sign-off.

### Independent native approval

Migration `0019_native_approval` adds the accepted approval API with maker/all-editor exclusion, 24-hour review-grant cooling-off, atomic freshness checks and immutable decision history. Read-only approval records are visible in proposal details. The authoring slice below uses this API; rejection and execution remain separate milestones. See [NATIVE_APPROVAL.md](docs/NATIVE_APPROVAL.md) for exact scope and AP-1/AP-2/AP-3 proposed implementation choices. No financial posting or provider access is enabled.

### Native approval authoring

Eligible independent reviewers can now approve the server-advertised current revision from proposal details, with an optional comment, explicit review acknowledgement and preserved same-key recovery for uncertain responses. See [NATIVE_APPROVAL_UI.md](docs/NATIVE_APPROVAL_UI.md). Execution and rejection remain unavailable.


## Native rejection (0020)

The authenticated rejection API and immutable readable history share one decision per revision with approval. A required exact reason, atomic reservation release, authorization, freshness, idempotency and rollback are enforced. The rejection authoring slice is now connected; see [NATIVE_REJECTION_UI.md](docs/NATIVE_REJECTION_UI.md). See [NATIVE_REJECTION.md](docs/NATIVE_REJECTION.md) for supported scope, evidence and RJ-1/RJ-2/RJ-3 PROPOSED interpretations. Execution remains unavailable; accounting remains UNREVIEWED.

The optional [same-host application recovery rehearsal](docs/APPLICATION_RECOVERY.md)
extends the database drill with source/public-asset capture and independent
execution-proof verification from a disposable restore. Secrets, dependencies,
Redis and frontend rebuild remain explicit prerequisites or separate exercises.

[Database recovery security](docs/DATABASE_RECOVERY.md) now verifies and restores
the supported local database-level permissions and scoped configuration, in
addition to the existing data/schema and application-asset recovery checks.

Current migration head is `0022_approved_withdrawal`.
[Approved maker withdrawal](docs/APPROVED_WITHDRAWAL.md) closes RG-01: the maker can
release an approved proposal's statement atomically without financial effects or
altering its historical approval/simulation evidence.


## RG-05 candidate: configuration authority

See `docs/CONFIGURATION_BOUNDARY.md`. Migration 0023 removes all direct runtime DML on five configuration tables and adds authenticated administrator account PATCH with required version, immutable change history and atomic audit. Account list responses now include version. This isolated candidate has not been installed in the shared application; the review package records its exact baseline and test evidence. Public configuration creation remains operator-only setup, not a newly implemented HTTP capability.
