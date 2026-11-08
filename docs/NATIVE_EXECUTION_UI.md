# Native execution confirmation and recovery UI

## Installed scope

This slice adds one read-only backend route, GET /api/v1/proposals/{id}/execution, and connects the existing 0021 native execution API to the frontend. The migration head stays 0021_native_execution; no migration, financial writer, authorization function, accounting rule or provider capability is changed. Accepted source architecture documents and ADRs remain untouched. NE-1–NE-6, earlier D/PA decisions and accounting/oracle UNREVIEWED statuses are unchanged. Local SANDBOX only; BOOKS/provider/production writes stay disabled.

The lookup joins the current proposal revision to its RECONCILED execution under the existing read-only authenticated transaction and FORCE RLS. Missing, inaccessible and not-yet-executed proposals yield 404. An authorized current receipt supports representation ETags and 304; authorization runs first. No cache record or receipt id is required to recover an already committed record. Unknown query parameters are refused.

## Deliberate confirmation

Verification and approvals → Approved proposal → Native settlement execution. The UI requires the server's EXECUTE hint, independent maker identity, native SANDBOX, exact version, one RecordSettlement and pinned supported simulation. It does not replace server authorization with browser logic or historical simulation capability flags. The checkbox binds to the displayed revision. The saved payout/date/gross/fee/net are exact decimal strings; no binary float conversion is used. Original policy verdicts, evidence exceptions and approval records stay visible separately.

Before a new POST the form re-reads the current proposal and pinned simulation in parallel, confirms the version/id/hashes and checks unresolved peer review recovery. The browser saves the exact original key, revision, If-Match, simulation hashes and time in tenant/user/proposal/ledger-scoped tab storage before sending. The body is only {revision}. Actor, tenant, permissions, approval and financial eligibility are independently rechecked by the existing backend.

A successful response is validated for scope, human actor, target simulation/revision/version, canonical MATCHED proof, equal hashes, exact sequence string and UNREVIEWED status. The full proposal refreshes so the page cannot keep claiming APPROVED/unposted alongside a committed receipt. Historical simulation text describes predictions and directs current execution questions to the receipt and ledger; it is never silently rewritten as an accounting approval.

## Uncertainty and recovery

Network, timeout, unexpected 2xx, 5xx, in-progress, authorization and reused-key uncertainty retain the exact original request. There is no automatic posting retry, rebasing or replacement key. A readback of current proposal and permanent receipt is required before an explicit original retry can be offered. If the original target is still current and offered, receipt lookup is 404 and the conservative 23-hour window is open, the retry uses the saved original If-Match even though other attempts may still be in flight. A 404 never proves an earlier request did not commit or finish; backend locking, the cache and permanent unique execution guard decide the outcome.

A found receipt must agree with the current EXECUTED proposal before display. Readback proves the committed settlement; it does not prove which concurrent key won. Another executor's attribution is preserved. Once a receipt is found, all posting/retry controls disappear, while the original attempt remains retained. The receipt can be recovered after cache expiry and without the execution id. The UI never creates another proposal as a recovery mechanism.

A definitive version, schema, capability or staleness refusal is retained and not retried. When a supported newer approved target exists, an explicit archive action must save the refused attempt separately before clearing that form for fresh review/confirmation. An unconfirmed request cannot use that archive path. Refusal/storage corruption is not treated as permission to overwrite a prior identity.

Approval, rejection and execution controls share live locks and directly re-check peer tab records before a new request. Corrupt or unconfirmed execution data blocks new reviews; a definitive refusal does not block later supported review of a newer target. Existing review comments and recovery schemas remain unchanged.

## Identity, storage and limits

Tenant changes unmount scoped details, abort requests and clear the old receipt. Returning to the original verified tenant reads its record again. Live revocation prevents a preflight/POST; session refresh does not authorize a replacement key. Credentials are never stored with recovery. Plain receipt ids, pinned hashes and the original idempotency key are not authentication credentials.

Storage denial blocks a new POST. A failed storage update does not invalidate an already verified committed receipt. Tab storage can be lost when the tab/browser is closed and is not shared across devices/tabs. The backend's permanent per-revision execution uniqueness remains the financial effect guard across tabs and callers. Closing a tab may lose the original retry identity; permanent proposal receipt lookup can still resolve a committed settlement. If the record is absent and the original identity is lost or expired, reconciliation is required instead of assuming absence.

Receipt comparison is bounded native consistency verification, not independent accounting certification. The outbox still has no execution notification/proof-pack consumer; the UI does not claim publication. The event chain still lacks an external anchor. MFA/password recovery, production operations and independent accounting review remain separate gates.

## Evidence

The dated review package includes the full source baseline/delta, real native PostgreSQL backend logs, OpenAPI/types parity, frontend helper results, build and browser screenshots. The browser flow used a disposable synthetic database, explicitly aged synthetic role grants, explicit synthetic evidence exceptions and controlled response/storage faults. It is not real customer financial activity or a business-impact/accuracy study.

The browser verifies normal execution; a committed response replaced by a controlled 503; a pre-write failure with exact key/body/If-Match retained across reload; unavailable receipt readback; a stale dependency inserted after preflight; a competing executor; denied/corrupt storage; a corrupt peer record; receipt recovery after cache removal/expired retry identity; unavailable simulation; real membership revocation; maker exclusion even with an execute role; a cross-tab tenant switch; 390px layout, keyboard flow and committed financial API balances.

At shutdown, independent database assertions require exactly the four named new executed cases (one audit/effect each), one pre-existing expired-cache execution, no posted protected cases, exact 51,500.50 debit to bank / 1,000.00 debit to fees / 52,500.50 credit to clearing, correct replayed chain heads, and only the expected ledger counters advancing once. All 33 unrelated table fingerprints stay unchanged. Source/evidence, pinned simulations and decisions remain unchanged. An explicit account-name/version mutation is allowed only for the stale-race case.

## Proposed implementation records

EU-1 PROPOSED: permanent current-revision receipt lookup by proposal and existing representation ETag convention. EU-2 PROPOSED: versioned tab recovery schema, conservative 23-hour retry window and explicit refusal archive. EU-3 PROPOSED: readback proves committed record without assigning the winning request key. These do not amend accepted financial/accounting decisions.

## Next bounded work

The subsequent proof-export slice now adds a read-only integrity bundle and offline verifier; see NATIVE_EXECUTION_PROOF.md. Keep exports distinct from the still-unimplemented completion-event consumer. Remaining work includes independent accounting review, practical labeled evaluation and complete application recovery/release gates before broader readiness claims.
