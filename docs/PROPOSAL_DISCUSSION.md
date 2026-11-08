# Proposal discussion — Stage 2B Family 11

POST `/api/v1/proposals/{id}/comments` accepts `{body}` with propose **or** review permission, verified session/tenant, CSRF/Origin and Idempotency-Key. It returns a Comment (201). GET on the same path requires read, accepts the existing limit/cursor conventions and returns a page of comments. There is no approval, rejection, override or state transition in these endpoints.

## Bounded representation choices — still PROPOSED

The accepted Family 11 names the routes and `{body}` but does not define a complete Comment schema, text budget or revision binding. These implementation choices are recorded as **CM-1/CM-2 PROPOSED**, not acceptance of new architectural or accounting decisions:

- **CM-1:** plain UTF-8 text, 1–4,000 Unicode code points with non-whitespace content. NUL and unpaired surrogates are invalid. Leading/trailing whitespace, Unicode and literal HTML remain unchanged; there is no Markdown or HTML interpretation, attachment, edit or delete endpoint. The response identifies comment/proposal/ledger, server-derived author, revision/version current at insertion, a per-proposal ordinal and UTC recording time. It does not attest which content a person actually read.
- **CM-2:** optional If-Match guards the proposal resource version. The browser supplies it to avoid posting against an unseen revision. A mismatch is 412 with no comment. Omitting it follows the base `{body}` contract and records the then-current server revision. GET pages are separate current reads in ordinal order, not a frozen discussion snapshot. A changed proposal version does not rewrite old comments.

Neither choice alters accepted approval/rejection eligibility. Stage 2A permits READY_FOR_REVIEW → APPROVED/REJECTED; current synthetic statements keep proposals BLOCKED. Comments cannot turn them into READY_FOR_REVIEW. Grant cooling-off and maker/editor exclusions govern financial decisions, not the expressly allowed propose-or-review discussion permission. Administrators can read but cannot post by default. Comments may remain attached to visible terminal/blocked proposals as historical discussion.

## Transaction and database boundary

Migration 0017 adds one append-only FORCE-RLS tenant table with a composite FK to the exact immutable proposal revision. Runtime receives SELECT, a pure JSON renderer and one narrowly scoped SECURITY DEFINER write wrapper. It has no direct INSERT/UPDATE/DELETE or TRUNCATE grant. Functions pin `search_path=pg_catalog,pg_temp`; there are no new dependencies or financial-table privileges.

The wrapper binds the verified session; checks propose-or-review; obtains the nonblocking idempotency lock before touching session activity; locks/rechecks user, membership and roles through commit; then locks the proposal row. It allocates the next thread ordinal under that proposal lock. No proposal row is updated, so content, version, updated_at, state, statement reservations and financial sequence stay unchanged. The ordinary proposal ETag is therefore unchanged by comments; the discussion representation has its own ETag.

The comment, audit entry and cached response commit together. A deferred validator verifies the exact revision/head observed in this transaction, preceding ordinal, author/session attribution, comment-body hash in the audit and full response body in the idempotency cache. Removing either audit or cache aborts the transaction. The audit stores the body hash, not a second copy of untrusted text. Both successful and validated 4xx refusals use the existing 24-hour idempotency window; live permission is checked before any replay. The hash binds proposal, original If-Match, canonical body and query. Same-key/different-request is refused. No automatic retry uses a new key.

The proposal lock only orders discussion against proposal editing/transitions; it does not acquire ledger/account/period locks or authorize posting. Concurrent commits get gapless comment positions. Reads use the tenant/proposal/position unique index with signed cursors bound to tenant, ledger, proposal and ordering. Absent/foreign proposal IDs have the same 404. Conditional 304 responses still authenticate first. Freshness of a discussion page does not imply freshness of a simulation or eligibility to execute.

## Browser behavior

The discussion is shown only with current proposal content; each comment explicitly names its recorded revision. Author names are not invented: the current user is labelled You and others by verified stored author ID. Text is rendered as a React text node with preserved whitespace. No comment is interpreted as an instruction, capability evidence, accounting review or decision.

Draft text and uncertain request metadata are kept in sessionStorage, scoped to the verified tenant/user/proposal. No password, cookie, CSRF token or other session credential is stored with the draft. Confirmed writes clear recovery data. A network/5xx/in-progress response retains the exact key/body/version and locks text for same-request retry. A definitive 412 retains draft text, releases the rejected key and requires refreshed current content. Storage failure is visible; in-memory retry works, but browser reload recovery is not claimed in that condition. Session/tenant changes unmount the view and abort reads/writes; records from another scope are refused on recovery.

## Verification and limits

Run `PYTHONPATH=src:.:tests .venv/bin/python -m pytest -q tests/test_proposal_comments.py` on the local PostgreSQL test environment. Tests cover exact text, authorship, permission union, cross-tenant masking, read-only GET, conditional auth, immutable rows, narrow SQL privilege, body bounds, Unicode errors, competing posts, same-session in-flight keys, original-revision replay, cached refusals, revocation/disable races, missing audit/cache, injected rollback and retained-history downgrade refusal. The dated package records actual full-suite results, migration-preservation rehearsal and HTTP/browser evidence.

This milestone does not admit public retail observations as financial evidence, claim accounting accuracy, approve a proposal, run provider experiments, enable MFA/production or resolve earlier proposed decisions. Accounting and oracles remain UNREVIEWED. The in-database hash/audit chain is not off-host anchoring.
