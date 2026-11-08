# LedgerVigil frontend — authenticated workspace

This is the first integrated frontend slice, using the existing FastAPI identity service and real PostgreSQL. It is local SANDBOX software, not a production release. The command center reads native ledger, trial-balance, account and period data through the authenticated backend. The backend now supports bounded native SANDBOX execution through its authenticated API; the frontend now provides deliberate confirmation, permanent receipt readback and exact original-request recovery. It can read EXECUTED proposals and committed ledger balances. See ../docs/NATIVE_EXECUTION.md. Historical milestone notes below describe the scope at delivery.

## Start on this Mac

From `/Users/vinayakmahindrakar/Desktop/LedgerVigil/app/web`:

```sh
npm ci
python3 scripts/local-stack.py
```

Open **http://127.0.0.1:3000**. Use `127.0.0.1` consistently rather than alternating with `localhost`. Ctrl+C stops both servers. The database must already be running (`../scripts/dev up`). The launcher refuses occupied ports and supplies the exact permitted frontend origin to the API. It does not change authentication defaults or database credentials.

Sign in with a previously provisioned synthetic account. Random credentials for this Mac's browser verification remain in the existing private file:
`/Users/vinayakmahindrakar/Documents/Codex/2026-11-02/referenced-chatgpt-conversation-this-is-an/work/native-integration-gate/private/frontend-demo-credentials.json`

Passwords are not included in source, review evidence, screenshots or this README. An administrator can provision a new synthetic account set using the existing backend's `tools.identity_admin provision-demo` command and a private credentials path. There is no default login or authentication bypass.

## Delivered behavior

- Sign-in, session restore, multi-organization selection, organization switching and confirmed sign-out use the real API.
- Organization and account details show server-issued roles and permissions. ADMINISTRATOR receives no financial approval permissions from the UI.
- Seven workspaces follow Stage 2B §10.2. Unconnected financial workspaces show explicit availability states; there are no fabricated balances, AI findings, approvals or operational buttons.
- Tenant changes immediately clear visible context. A server result must match the selected tenant before rendering.
- HttpOnly cookies stay browser-managed. CSRF tokens exist only in memory. Nothing is stored in localStorage/sessionStorage. Cross-tab messages contain only a change signal, never credentials or identifiers.
- Revalidate on focus/visibility return. Local expiry clears visible identity; every backend request remains authoritative. No background polling extends idle sessions.
- Generic login errors, Retry-After countdown, connection recovery, 15-second request timeout and explicit uncertain-sign-out recovery.
- Responsive desktop/mobile layouts, visible keyboard focus, mobile navigation focus containment, Escape dismissal, skip link, reduced-motion support and accessible form labels.

## Interface refinement — 2026-11-03

The workspace now includes keyboard-searchable navigation (⌘/Ctrl K), a filterable organization picker with current-context and cancel controls, plain-language permission descriptions, password visibility and Caps Lock feedback, and a compact organization context card. Mobile navigation restores focus after closing and suppresses the search shortcut while its drawer owns focus. Narrow layouts wrap long account and organization values.

`src/app/tokens.css` owns the shared visual foundations. The companion [Figma foundations file](https://www.figma.com/design/36zLbqtazradEHXd3Nawqj) contains primitive and semantic variables and a reference sheet; it is not a complete screen design. Figma specimen labels use Inter because Arial was unavailable there; the app retains its system font stack.

This revision passed TypeScript, production build, owning API contract checks, and 12 explicit browser interaction checks on the native local stack. The earlier Playwright suite and automated axe scan were not rerun for this refinement. See the dated frontend-refinement review for screenshots, current evidence, and limitations.

## Verification commands

```sh
npm run generate:api
npm run check:api
npm run typecheck
npm run build
npm audit
```

`openapi.json` is a byte-identical snapshot of `../api/openapi.json`. The generated OpenAPI TypeScript definitions drive `openapi-fetch`; request/response schemas are not reimplemented in UI code. `check:api` rejects backend snapshot drift or changed generated definitions. An intentional backend change must be reviewed, copied and regenerated together.

Browser checks require both servers, installed Chrome, the private synthetic credentials file and the backend checkout:

```sh
LV_TEST_CREDENTIALS='/absolute/private/synthetic-credentials.json' \
LV_TEST_APP='/Users/vinayakmahindrakar/Desktop/LedgerVigil/app' \
npm run test:e2e
```

The suite performs real sign-ins and revokes sessions for the synthetic automation user. Use dedicated synthetic fixtures only. It respects real login throttling; do not repeatedly loop the suite or disable throttle guards. Traces/video are off to avoid recording secrets. Evidence consists of redacted assertions and non-secret screenshots. The 429/offline/failed-logout/stalled-request UI cases inject faults; the expiry case advances the browser clock. Those are not represented as new backend security proofs.

## Boundaries and follow-up

This package does not change backend source, migrations, role mappings, accepted decisions or accounting rules. No production writes or external provider access exist. The earlier backend limitations (including trusted API password verification, no MFA/password reset/email delivery, and unconsumed period-close staleness jobs) remain.

The local reverse proxy targets only the existing loopback API on port 8000. Cookies use the backend's explicit local-HTTP development mode. Production HTTPS, strict nonce/hash CSP, proxy-aware source attribution, deployment controls and MFA remain future release gates. The current CSP allows Next.js inline scripts; it is not a production security certification.

Claude retains backend ownership: authenticated audited period close (already specified as `administer`), then agreed financial APIs. Codex will connect those APIs to the workspace after their contract and native verification gates. Do not start a second backend foundation.

## Dependencies

Dependencies and lockfile are pinned. The OpenAPI build tool's `minimatch` dependency is overridden to 11.0.1 to resolve GHSA-g3hv-75wg-m249. Generation, type checking and build passed with that override. All dependency audits are point-in-time checks.

Reference: [Next.js installation](https://nextjs.org/docs/app/getting-started/installation), [OpenAPI TypeScript](https://openapi-ts.dev/readme), [typed fetch client](https://openapi-ts.dev/openapi-fetch/api).

## Financial read integration — 2026-11-03

The command center now reads real PostgreSQL ledger data through five authenticated GET endpoints. Choose a ledger and switch between trial balance, chart of accounts and periods. Money is grouped as exact decimal text; totals are never calculated in the browser. The trial balance carries its committed sequence and accounting-review status. Account and period lists support cursor pagination. Ledger and organization changes cancel requests and clear previous financial context.

Refresh is explicit. Automatic polling is deferred because existing GET requests touch session idle time; background polling must not silently keep a user signed in. Date-based trial balances, mutation controls, journal drill-down and financial AI workflows remain unavailable.

Run `node --test scripts/financial-format.test.mjs` for exact formatting and exclusive period-end regression checks. See the dated financial-integration review for native backend, database-upgrade, browser and delivery evidence.

## Proposal draft browser — 2026-11-03

Verification & approvals now provides a read-only draft list, a Created by me filter, full draft detail, evidence references and immutable revision history. Historical content is labelled separately from the current revision. Amounts are proposed values and no simulated balance or approval result is displayed. List and history pages support signed cursors. Tenant switches unmount and cancel the draft context.

This initial read-only milestone preceded the draft editor, source pickers, submission, simulation and independent approval slices documented below. Execution remains unavailable. See the dated proposal-integration review for native tests, independent fixes, backup-upgrade evidence and browser checks.

## Versioned draft editing — 2026-11-03

Users with the server-issued EDIT action and current propose permission can edit existing settlement drafts. The modal edits rationale, processor/payout metadata and proposed gross/fee/net values. Existing payment and evidence references stay attached; selector creation remains dependent on Claude's browsing APIs. Exact amount inputs stay decimal strings. Every save uses the server ETag in If-Match and the in-memory CSRF token.

A successful save creates a new immutable revision and refreshes history. Validation errors retain form values and identify fields. A stale version retains local text, shows the latest rationale and requires an explicit replacement before editing from that version. A timeout is treated as an unconfirmed outcome; check the latest draft before retrying the same content with the original version guard. The UI never automatically retries or merges a financial proposal.

The modal traps focus, guards dirty dismissal and suppresses competing workspace-search dialogs. Normal same-user/same-organization focus revalidation preserves component state; sign-out, failed verification and identity/organization changes clear it. Unsaved changes are kept in memory only and are lost if the authenticated context is invalidated.

Run `node --test scripts/draft-edit.test.mjs` for exact-content and error-mapping checks. See the dated draft-editor review for current browser and native API evidence.


## Source-driven draft creation — 2026-11-03

Verification & approvals now offers **New settlement draft** to sessions with `propose`. Choose a native SANDBOX ledger, a STAGED import and its statement, then explicitly select payments and evidence. No UUID entry, automatic pairing, matching or eligibility claim is offered. The review page displays the source's exact decimal values before POST; fees/net are copied without arithmetic. Existing draft editing and revision history remain connected.

The source services are `GET /imports?ledger_id=`, `GET /imports/{id}/staged-documents`, and `GET /ledgers/{id}/documents?type=PAYMENT`; all page controls follow `next_cursor`. Read-only provenance/hashes are displayed, while evidence is sent as `{kind,id}` only. No uploads or provider calls are available. Changing ledger clears selections only after confirmation. Tenant changes unmount the source controls.

Creation sends the session CSRF token and one random idempotency key. A network failure, 5xx or unresolved 409 locks selections; the explicit recovery button resends the **same body and same key**. No automatic retry or new-key workaround occurs. Retry-After seconds are respected. A verified test proxy hid a committed 201 response; the retry returned the original draft with `Idempotent-Replayed: true`. This recovery request lives in memory only: keep the window open during recovery. Reloading, logout or tenant switching does not retain it; unload protection warns on navigation.

Source API choices PC-S1–PC-S9 and gaps SG-1–SG-9 remain documented in `../docs/SOURCE_READS.md`, including the representation-hash document ETag, batch-derived record validation and unresolved settled-payment eligibility. The UI displays stored statuses and keeps accounting UNREVIEWED. No submit, simulation, approval, execution or staleness consumer is present. Draft reference editing is now available through the version-guarded editor; see the reference-editing section below.

Native integration evidence is in the dated `codex-source-selection` review: 631 PostgreSQL tests, 22/22 source safeguard experiments, 9/9 database guard checks, production build and typed contract checks, 6 helper tests, and 12 browser checks. The existing automated axe and broader Playwright suites were not rerun for this slice.


## Transaction and source explorer — 2026-11-03

Transaction Explorer now connects the existing read APIs. Choose a native SANDBOX ledger, then inspect current PAYMENT, SETTLEMENT or MANUAL_JOURNAL documents, stored import outcomes, staged statements and raw source records. Available type/status filters and backend cursors drive the lists. No date, text-search or historical-snapshot filters are offered. Manual journals retain their absent amount/status model instead of displaying a fabricated zero.

Document inspection includes current version, exact money, provenance, posting references and existing settlement links. Draft payment references open the same inspector, with the proposed amount shown separately. A historical draft revision is never presented as a historical document read. Full journal lines and historical document versions remain unavailable.

Source inspection shows received-file and source-record hashes, parser metadata and stated statement items. Source text remains escaped text. A browser SHA-256 check uses its exact UTF-8 bytes without normalizing Unicode or line endings, and compares the returned byte length. MATCH means consistency with returned metadata, not source authenticity or accounting correctness. MISMATCH displays an explicit warning; missing metadata or unsupported browser cryptography remains UNAVAILABLE. This check does not change batch status, draft eligibility or ledger records.

All reads remain session/tenant scoped. Refused or mismatched responses clear the previous view. Native modal keyboard behavior, trigger focus restoration, responsive layouts and cross-tab tenant changes were checked against the local API. The frontend makes no new financial write request.

Evidence for this slice: 24 browser checks, 3 new source-integrity tests, 8 existing helper regression tests, TypeScript, owning OpenAPI correspondence and production build. Financial/proposal/ingestion hashes across 38 tables, including ledger counters, were preserved. The unchanged backend's earlier 631-test result is historical; its full suite was not rerun for this frontend-only slice. The broader Playwright and automated axe suites were not rerun.

The source APIs' PC-S1–PC-S9 and SG-1–SG-9 remain unchanged. Accounting stays UNREVIEWED. No upload, reconciliation, submission, simulation, approval, execution, provider or worker capability is added by this frontend change.


## Draft reference editing — 2026-11-03

An editable draft now offers **Change source references** for each settlement operation. Statement selection, payment selection and draft-wide evidence selection use the existing authenticated read APIs. The picker starts with saved references without pretending they are current document reads. Evidence is retained or removed explicitly; removing a payment does not silently delete citations that another operation may need.

Selecting a statement keeps the existing proposed header and amounts by default. An explicit unchecked-by-default option replaces processor/payout/date/gross/fee/net with that statement's exact values. Before/after review precedes Apply to editor; this action changes local form state only. Save draft revision performs the existing CSRF-protected If-Match PATCH. Read-only provenance/hash fields never enter evidence inputs.

The existing conflict and uncertain-save recovery now includes evidence changes. An ambiguous save locks the picker until readback. Newer server revisions are never overwritten or automatically merged. Explicit reload restores operations, rationale and evidence together. Save confirmations are tied to the displayed revision. Enter inside the selection step cannot submit the parent save form. Source selection rejects mismatched ledger/import responses and hides choices after read failure.

This slice passed 24 browser checks, five new reference-transformation tests, 11 existing helper regressions, TypeScript, owning contract correspondence and a production build. Real HTTP saves and a native concurrent edit exercised immutable revision history. Test-only injected responses covered lost-response, validation-refusal and mismatched-source UI paths; they are not new backend security proofs. A dedicated synthetic proposal was used, with 35 financial/unrelated table hashes and other proposals/histories preserved. The backend full suite and broad automated accessibility suite were not rerun because the backend is unchanged.

Reference state is held in memory. Logout, tenant switching or page replacement clears it; unload protection warns for unsaved selections. No simulation, matching, approval, execution, provider or background-worker capability is added. Accounting and reference cases remain UNREVIEWED.

## Simulation schema synchronization — 4 November 2026

The backend simulation/policy foundation is integrated; OpenAPI and generated types now include the recorded-run read and replay routes. This does not expose a simulation action or results screen in the frontend. Draft, source and reference-editing components are unchanged. Creation remains an internal backend service; submission and its prerequisites remain pending. Synthetic provenance and UNREVIEWED accounting status are preserved.

## Account dependency schema — 4 November 2026

The recorded simulation read-set schema now describes ACCOUNT dependency versions for simulator 0.3.0. OpenAPI and generated types are synchronized. Frontend behavior is unchanged; no submission or new simulation action is exposed.

## Submission API schema — 4 November 2026

Backend submit/withdraw routes and staged workflow status/version are reflected in OpenAPI and generated types. Frontend behavior is unchanged in this slice: action controls and submitted-result navigation require the next frontend milestone. Existing draft editing and source inspection remain connected. Cached mutation responses are point-in-time results; a future action UI must refresh the current head after replay before enabling another mutation.

## Native submission and pinned results

Verification & approvals now opens the proposal workspace. Filter Drafts, Blocked,
Ready for review, Withdrawn or All states. Only actions returned by the verified
server are displayed. Editing remains a separate DRAFT-only save; submitting
requires explicit confirmation of the saved revision. Submission pins a prediction
and reserves cited statements without posting financial records. Current synthetic
cases remain blocked for insufficient evidence; accounting remains UNREVIEWED.

Pinned results show exact decimal before/change/after balances, proposed journal
lines, policy outcomes, facts/evidence and immutable snapshot/version/source pins.
NOT_APPLICABLE is separate from PASS. Legacy NOT_EVALUATED profiles never imply
clear checks. Replay uses the real read-only endpoint and distinguishes identical,
different and unavailable-version outcomes. These results do not assert current
eligibility, approval or execution readiness.

An uncertain workflow request retains its original action, proposal, If-Match,
idempotency key and start time in sessionStorage, scoped to user/tenant/proposal.
No token, credentials or financial content is stored. Resume requires an explicit
click and never sends automatically. Same-attempt retries retain the original key
and version; retry stops after 23 hours, before the API's 24-hour cache expiry.
A current-state read never proves that an in-flight request did not commit.
Confirmed responses are followed by a fresh GET; a cached BLOCKED response cannot
replace a later WITHDRAWN head. Closing refreshes the current proposal. When browser
storage is unavailable, keep the dialog open until confirmation; reload recovery
cannot be saved. Operator reconciliation is required for expired unconfirmed work.

Maker withdrawal is explicit and atomic: the proposal becomes WITHDRAWN and its
statement claims are released, while pinned results/revisions remain accessible.
It cannot be reopened as a draft. Statement selectors show workflow status/version
and reservation owner separately from immutable validation/provenance. Reservation
does not prevent draft citation; submission is the authoritative reservation check.
No approval, override, execution, provider, AI or staleness consumer is added.

Period-close maintenance now marks affected pending proposals STALE and releases
their statement claims. The state filter and detail notice expose this state; pinned
results remain historical. Maker re-simulation and explicit dependency checks are
now connected; see ../docs/RESIMULATION.md. See ../docs/PERIOD_STALENESS.md for explicit worker
startup and the bounded scope; the web launcher does not start maintenance.


## Public invoice observation inspector

Transactions → choose a native GBP SANDBOX ledger → Public dataset → Open published dataset. Opening records an idempotent, audited source-plan registration; it does not import transactions. The source.import permission is required to open a registration through this entry flow; the read endpoints independently enforce read permission. There is no registration-list endpoint or private-upload support.

The UI shows published counts, HOLD/CANDIDATE REVIEW filters, paged groups, exact original cell strings, sheet/row references and recorded hashes. Held amounts remain unavailable. Candidate amounts remain exact source-derived strings and are not accounting approval. All posting stays UNAVAILABLE and accounting UNREVIEWED. Source data is UCI Online Retail II, Daqing Chen, CC BY 4.0; the workbook has been transformed into an observation plan.

Requests abort on unmount, and cursor/selection state resets with the filter, ledger or verified tenant. Registration retries preserve the request key, including when the success response is lost. The dialog has explicit keyboard focus containment, Escape closure and focus return. Loading, empty, unavailable-index and permission-refusal states expose no stale source rows.

Helper checks: `node --test tests/public-plan.test.mjs` (native Node with TypeScript stripping). The browser review package records real-data paging, original-price preservation, 390 px layout, cross-tab organization changes and isolated fault-preview evidence. Those transport faults are controlled stimuli, not production incidents or independent accounting oracles.

## Proposal discussion

The current proposal detail includes an authenticated discussion thread. Propose or review permission allows posting; administrators can read only by default. Text is plain, whitespace-preserving and never interpreted as HTML. Comments identify the server-derived author and revision/version current at posting, and leave proposal content/state/version unchanged. Historical revision views do not show a current-revision composer.

Draft text and uncertain request details are kept in tab storage under tenant/user/proposal scopes. No credentials are stored with the draft. Network/5xx/in-progress retries retain the exact key/body/If-Match. A 412 preserves text and requires refreshed proposal content; confirming a saved comment clears recovery data. Storage failure is visible and limits reload recovery. These are discussion records, never financial approvals or accounting evidence. See ../docs/PROPOSAL_DISCUSSION.md.

Run `node --test tests/comment-recovery.test.mjs tests/public-plan.test.mjs` with the existing native Node environment.

Proposal details also show controller policy-exception records from the authenticated reviews API. Reasons are plain text and original verdicts remain separate. The controller-authoring slice below adds the bounded write form. The later independent approval slice adds a separate decision control; execution remains unavailable. See the backend `docs/EVIDENCE_OVERRIDES.md` for exact scope and proposed limits.

The controller evidence-exception form uses the installed overrides API. It requires verified controller/review access, independent maker identity and the supported pinned policy result. Exact reason/version/key recovery is tab-scoped; uncertain requests retain their original identity through reload and session errors. See `../docs/EVIDENCE_OVERRIDE_UI.md` for retry limits and no-posting boundaries.

Proposal details can read native approval records alongside evidence exceptions, and the state filter includes Approved. Original verdicts remain separate. The approval-authoring slice below adds its bounded confirmation form; rejection and execution remain unavailable. The backend's current action hints include reviewer independence, grant cooling-off and freshness; see `../docs/NATIVE_APPROVAL.md`.

The native approval form renders new approval only from the API's APPROVE action hint and verifies the pinned target. Optional comments and original revision/version/key/hash metadata persist in this tab before sending. Uncertain results retain their original identity through reload/session errors, with explicit readback and retry. See `../docs/NATIVE_APPROVAL_UI.md` for limits. No execution or rejection control is added.


## Native rejection history

The rejection API is supported; proposal history shows REJECTION records with their exact plain-text reason, rejected_by reviewer, revision, pinned simulation and check. The Rejected filter reads terminal proposals. Approval authoring remains separate; the subsequent rejection-authoring slice adds a required-reason form with separate recovery and an interlock against pending approvals. See docs/NATIVE_REJECTION_UI.md in the owning app. Execution remains unavailable.


## Native execution read compatibility (0021)

The Executed filter and detail read the server's EXECUTED state. Current balances come from the ledger APIs; pinned simulations remain historical predictions with their original verdicts. The simulation view states what that simulation did, rather than claiming that the proposal has never posted. POSTED statement rows are disabled as new draft selections. That earlier compatibility slice introduced no execute button or new recovery storage. The subsequent confirmation/readback UI below now adds deliberate execution and exact-request recovery. Accounting remains UNREVIEWED.


## Native execution confirmation and recovery

The proposal detail now offers execution only when the server supplies EXECUTE for the exact supported independent actor and current approved native SANDBOX target. Pinned simulation, immutable approval and execution receipt remain separate. Confirmation re-reads the target, saves the original revision/key/If-Match before POST, and refreshes the full proposal after success or committed readback. Current balances continue to come from financial read APIs.

Uncertain requests survive reload in this tab. Readback must succeed before an exact original retry is offered; a 404 is not proof that an earlier request finished. Finding a committed receipt prevents another POST. After the conservative 23-hour retry window, only permanent receipt lookup/reconciliation is offered. Storage failures, corruption and unresolved approval/rejection requests fail closed. See ../docs/NATIVE_EXECUTION_UI.md for limits, archive behavior and verification.


## Native execution integrity export

A committed receipt now offers Download integrity bundle. This is a read-only request; it never submits or retries execution. The client checks exact bytes, scoped receipt ids, format and manifest before creating a file. An unavailable export, wrong response digest or substituted tenant scope saves no accepted bundle; the committed receipt remains intact. The displayed digest is the last requested file's digest, not an independent signature.

The bundle contains financial history through that execution and may include earlier activity in the same ledger. Keep it private. Use the standard-library offline verifier in ../tools/verify_execution_proof.py for record relationships. Accounting remains UNREVIEWED. See ../docs/NATIVE_EXECUTION_PROOF.md for the versioned format, limits and explicit trust boundary.
