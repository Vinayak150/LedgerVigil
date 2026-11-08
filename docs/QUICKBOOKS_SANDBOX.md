# QuickBooks Online SANDBOX read-only connection (migration 0035; PROPOSED PROV-1..PROV-8)

**Status:** implemented and tested offline only, against a documented-shape mock of Intuit (provenance `RECORDED_MOCK`).
**No live sandbox evidence exists yet.** Claude's workspace and the VM attached to the Mac both get a proxy 403 for every
Intuit host, so the live flow can only run on the Mac (see "Live sandbox run" below). Stage 2C experiments **X-1..X-10 were
not run**: they test writes, replay, voids and webhooks, and every QuickBooks write stays disabled. Accounting UNREVIEWED.
Sandbox data is labelled provider sandbox data everywhere and is never business evidence (ADR-021).

## Owning architecture and how this follows it

The provider design is Stage 2C (`docs/incoming/LedgerVigil-Stage-2C-Provider-Execution-and-Recovery.md`, "complete and awaiting
review" in its export manifest; the revision-3 additions D-1..D-11 remain PROPOSED) with ADR-003, -007, -010, -012, -015, -016,
-021, -024 and -027. Nothing here infers that those documents were accepted beyond what they record; the rules used are:

| Rule (source) | Implementation |
|---|---|
| Connecting needs `administer`; OAuth `state` bound to the initiating session and tenant (2C §11) | `lv.provider_api` checks `administer` after the 0027/0028 identity lock order; the state is 32 random bytes, only its SHA-256 is stored, bound to session, user and tenant, valid 10 minutes, single use |
| A realm is actively connected to only one tenant (2C §11) | partial unique index on (provider, environment, realm) over ACTIVE/DEGRADED/REVOKING only. The realm id in the callback comes from the browser and is unverified, so it reserves nothing: the callback is accepted the same way whatever other organizations hold (no existence oracle), and exclusivity is enforced when the worker records a verified exchange. On a conflict the attempt is marked FAILED `REALM_ALREADY_CONNECTED` and the grant just obtained is revoked at Intuit |
| Accounting scope only; sandbox host only until a production review (2C §11, ADR-027) | scope fixed by a CHECK; `intuit.Guard` refuses the production host, any non-GET to the accounting API, unknown hosts and redirects |
| Envelope encryption, key from environment or secret store; only integration workers decrypt (ADR-016, 2C §11) | AES-256-GCM per secret, data key wrapped with RSA-OAEP-3072 (SHA-256); the API process holds only the public key; associated data binds purpose, tenant, connection, realm and token version |
| Refresh under a per-connection lease; new pair committed before use (2C §2.2, §11) | `provider_refresh_claim` / `provider_refresh_finish` (lease + fence + token-version compare-and-set); a second refresher waits and uses the committed pair |
| Disconnect revokes at Intuit, erases local tokens, sets REVOKED (2C §11) | REVOKE job; tokens are erased in the same transaction that records Intuit's answer (`PROVIDER_CONFIRMED`, or `PROVIDER_UNCONFIRMED` when Intuit answers without confirming). When Intuit cannot be reached, the encrypted tokens are **kept** (they are the only way to revoke the grant), the connection stays REVOKING with `REVOCATION_PENDING`, unusable for reads or refreshes, and revocation is retried hourly or on demand by an administrator (at most 25 revocation jobs per connection in total); after that an administrator can end it explicitly with outcome `ABANDONED` |
| Ambiguity defaults to unknown; 429 is not an outcome; no hidden retries; raw in (2C §2.3) | read-only, so outcomes are RETRYABLE / THROTTLED / TERMINAL / AUTH_INVALID; httpx with retries=0, no redirects; every page stored as received bytes with SHA-256 |
| Celery + Redis with outbox; tasks claim in PostgreSQL (ADR-010) | `lv.provider_job` with fenced leases; relayed by the existing scheduler (`ledgervigil.provider_job`, queue `integration`); `python -m ledgervigil.provider.worker run-due` uses the same claim protocol without a broker |
| Replica from objects LedgerVigil read itself; raw with SHA-256 and (id, SyncToken); dedupe by (entity, id, SyncToken) (2C §9.1-9.2) | `lv.provider_page` (exact bytes) and `lv.provider_object` (extracted by PostgreSQL from those bytes, so amounts keep their decimal text), append-only |

### Deliberately not done (and why)

- **No derived postings, reconciliation or snapshot eligibility (2C §9.1 step 3, §9.3, §9.4).** The replica is raw only. Deriving
  postings would route provider documents into the native ledger model, which the milestone forbids without an accepted
  mapping. No provider data reaches native ledgers, proposals, simulations or detection.
- **No change data capture or webhooks (2C §9.2, ADR-012, X-9).** Each read is a bounded full read.
- **No PROVIDER_CONNECTED ledger.** The connection belongs to the tenant (PROV-2). Stage 2C's "one active connection per ledger"
  applies once a provider-connected ledger exists.
- **No scheduled idle refresh.** Tokens are refreshed when a read runs. Refresh tokens expire after 100 days unused (rolling), so
  an unused connection eventually needs reconnecting.
- **No write capability of any kind.** The capability descriptor reports writes DISABLED; Stage 2C statuses are unchanged.

## PROPOSED decisions

| ID | Proposal |
|---|---|
| PROV-1 | Gate `LV_PROVIDER_SANDBOX_ENABLED=1`, off by default; with it off the routes are absent from the served contract (`api/openapi.json` unchanged; `api/openapi.full.json` lists them) |
| PROV-2 | One open connection per tenant; tenant-owned (no ledger binding); one tenant per realm, enforced only after the worker has verified the realm (CompanyInfo read must return exactly one company) |
| PROV-3 | `administer` to connect, read now and disconnect; `read` to see state, jobs and the replica |
| PROV-4 | Secret split: `<dir>/api/api.env` (client id, redirect URI, KEK public key; names nothing in `worker/`) and `<dir>/worker/worker.env` (adds client secret and KEK private key), written by `tools/provider_setup.py` outside `app/`. Worker files are read through one `O_NOFOLLOW` descriptor and must be regular, owned by the worker's user, without group/other bits, in a directory others cannot write. See "Residual risks" for what this split does not achieve on a single-user Mac |
| PROV-5 | Callback through the frontend page `/connections/quickbooks/callback`: parameters read once, removed from the address bar and history, never stored in the browser, posted once with CSRF; the API seals the code; the worker exchanges it and verifies the realm by reading its CompanyInfo before activating |
| PROV-6 | Read scope Preferences, CompanyInfo, Account, Customer, Invoice, Payment, Deposit, JournalEntry, CreditMemo, RefundReceipt; 100 per page, at most 20 pages per entity (beyond: PARTIAL with the entity named); minor version 75 |
| PROV-7 | 429: wait 60 s and resume from the last committed page without using an attempt (at most 20 throttles); 5xx/timeouts: back-off, at most 5 attempts; 401: one refresh, then AUTH_INVALID (connection DEGRADED); refresh `invalid_grant`: DEGRADED; 400/403: terminal; `intuit_tid` kept per page and per failure |
| PROV-8 | Disconnect revokes the refresh token at Intuit and erases local tokens once Intuit answers (outcome recorded). If Intuit cannot be reached, tokens are retained encrypted and revocation is retried (hourly, bounded, plus manual retry) instead of being erased with the grant still live. The replica and all job history are kept; downgrading 0035 is refused while any connection row exists. (Revised 7 Nov 2026 after the security re-review; still PROPOSED) |

## Provider facts used and their sources (checked 7 Nov 2026)

- OAuth endpoints (authorization `https://appcenter.intuit.com/connect/oauth2`, token
  `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer`, revocation `https://developer.api.intuit.com/v2/oauth2/tokens/revoke`):
  Intuit's sandbox OpenID discovery document, https://developer.api.intuit.com/.well-known/openid_sandbox_configuration
- Authorization parameters, Basic client authentication, `grant_type` values, revoke body `{"token": ...}`, scope string
  `com.intuit.quickbooks.accounting`: Intuit's official Python OAuth client, https://github.com/intuit/oauth-pythonclient
- Access token one hour; refresh token 100 days, rolling; store the latest pair; revoke on disconnect: Intuit developer blog,
  "Introduction to token management", https://blogs.a.intuit.com/?p=34254
- `STARTPOSITION` / `MAXRESULTS`, at most 1000 per response: Intuit developer blog, "Supercharge your queries", https://blogs.a.intuit.com/?p=33734
- Minor versions 1-74 retired from 1 Aug 2025; requests default to 75: Intuit developer blog, https://blogs.intuit.com/?p=34635
- `intuit_tid` response header for support: Intuit API docs, error handling, https://intuitdeveloper.github.io/intuit-api-docs/docs/faq/error-handling
- Plain HTTP redirect URIs only for localhost testing: Intuit developer blog, https://blogs.a.intuit.com/?p=29056
- Sandbox host, rate limits (500/min per company, 10/s per company and app, 429 = wait 60 s), CDC, SyncToken: Stage 2C §3.1
  (Verified there against Intuit's reference pages, which render client-side and could not be re-read here).

### Unverified until the live sandbox run

- That `ORDERBY Id` is accepted for every entity in scope (the read uses it for stable paging).
- Exact `Fault` codes per status (failures are classified by HTTP status; codes are only recorded).
- That `x_refresh_token_expires_in` is present in token responses (handled as optional).
- Whether Intuit's portal accepts `http://127.0.0.1:3000/...` as a redirect URI (the app keeps cookies on 127.0.0.1).
- That revoking the refresh token ends the whole grant.

## Secret handling, logging and deployment isolation

- **Logs.** Intuit redirects the browser to `/connections/quickbooks/callback?code=...&state=...&realmId=...`. Redaction happens
  where log records are made, not only in the browser:
  - the frontend server never logs incoming request lines (`logging.incomingRequests: false` in `web/next.config.ts`);
  - every log record in the API, workers and scheduler passes through `ledgervigil.logredact` (a record factory installed at
    start-up): request lines lose their query strings, and `code=`, `state=`, token fields, `Bearer`/`Basic` credentials and
    JSON token fields are masked anywhere in a message or traceback; HTTP client libraries log only warnings;
  - the TLS proxy (`deploy/proxy/Caddyfile`) deletes `code`, `state`, `realmId`, `error` and `token` from logged URIs and drops
    cookies, authorization and CSRF headers from access logs;
  - the page removes the query from the address bar and history, sends `Referrer-Policy: no-referrer`, stores nothing in the
    browser, and the application loads no analytics or error-reporting script.
  Any other proxy, load balancer or hosting log in front of this stack must apply the same rule. That is an operator check
  (deploy/RUNBOOK.md, "Pre-flight").
- **State.** 32 random bytes, only its SHA-256 stored, bound to session, user, tenant and the one connection attempt; 10
  minutes; single use. Starting again supersedes an unfinished attempt; disconnecting cancels it; switching organization
  makes it unusable (a new session). Tests cover each.
- **Authorization code.** Sealed by the API with the KEK public key, bound to tenant, attempt (state hash) and realm, readable
  only by the worker role, cleared from the job on every terminal path including lease expiry.
- **Isolation.** `deploy/compose.yaml` runs the API, frontend, maintenance worker, provider worker, scheduler and migrator as
  six distinct non-root uids. The KEK private key and the client secret are mounted only into `provider-worker`; the API gets
  the public key and the API config only. At the database, the API's login (`lv_app`) cannot read credentials, OAuth states,
  job payloads or page bodies, and cannot execute any worker function (tested). The maintenance worker shares the `lv_worker`
  login but holds no key material, so the ciphertext it could reach through worker functions is useless to it.
- **Single-user Mac (local development).** `tools/provider_setup.py` writes separate `api/` and `worker/` directories, but when
  the API and the worker run as the same OS user, code execution in the API process can read `worker/`. That is a
  configuration split, not an OS boundary. Use the deployment layout (separate identities and secret mounts) for anything
  beyond a local sandbox session.

## Second security review (re-review of the fixes) and what changed

| ID | Finding | Status |
|---|---|---|
| N1 | A revocation that never succeeds left the connection in REVOKING forever | **Fixed**: errors that cannot succeed on retry (`ENVELOPE_INVALID`, `TRANSPORT_REFUSED`) are not re-queued; at most 25 revocation jobs per connection (automatic and manual together, each up to 5 calls); an administrator can then end it explicitly (`disconnect` with `{"abandon_revocation": true}`), which erases the local tokens, records outcome `ABANDONED` and an audit event, and tells the administrator to disconnect the app at Intuit |
| N2 | Any `lv_worker` holder (maintenance, scheduler) can rewrap or overwrite stored envelopes through the rotation functions; it cannot read tokens | **Open (Low)**: needs a dedicated integration database login, a role-provisioning change not made here |
| N3 | Rotation could be undone by a stale refresher; kek_id not tied to the envelope header | **Fixed**: CHECK ties the envelope header to `kek_id`; refreshed pairs are refused after the refresh lease expires; rewrap moves the lease fence (tested) |
| N4 | Revoke-on-failure might kill a healthy grant | **Partly fixed**: a lost refresh no longer revokes a refresh token equal to the stored one; a realm conflict no longer revokes (Intuit keeps one connection per app and company). Intuit's exact revocation scope is unverified |
| N5 | `_safe_revoke` failures were silent | **Fixed** where a job result is written (`grant_revocation` field) and logged by class; a process killed between exchange and record can still leave one unrecorded grant (inherent) |
| N6 | Logging gaps (repr and `name: value` forms, `makeLogRecord`, scheduler, Caddy error log) | **Fixed** in code (tests added); the Caddy global log filter is written but not validated against a Caddy binary |
| N7 | Container hardening | **Partly fixed**: read-only, `no-new-privileges`, capabilities dropped, Redis on its own internal network; open: frontend image keeps dev dependencies, Redis has no password, Caddy runs as root in its container |
| N8 | Docs did not match code | **Fixed** (secrets directory outside the repository, attempt counts, `REVOCATION_PENDING` cleared on REVOKED) |

## Key rotation, retention and deletion

- **What deletion does.** Disconnect deletes the credential row. PostgreSQL does not overwrite the old tuple immediately: the
  ciphertext can remain in table pages until vacuumed and reused, in WAL until recycled or archived, and in every backup taken
  while it existed. `DELETE` is not physical erasure, and nothing here claims it is.
- **What makes old ciphertext harmless.** (1) Revocation at Intuit: a revoked grant's tokens are useless even if decrypted.
  This is why tokens are retained until Intuit answers. (2) Destroying the key-encryption private key that sealed them: without
  it, no copy in WAL or backups can be opened.
- **Rotation procedure** (`python -m ledgervigil.provider.worker rotate-kek`, tested):
  1. Generate a new KEK (run `tools/provider_setup.py` into a new directory, or `--container` for the deployment layout).
  2. Give the worker the new private key as `LV_PROVIDER_KEK_PRIVATE` and the old one in `LV_PROVIDER_KEK_PRIVATE_PREVIOUS`
     (comma-separated, open-only). Give the API the new public key. Restart both.
  3. Run `rotate-kek` until it reports `remaining: 0` and `pending_code_envelopes: 0`. Each credential is rewrapped with a
     compare-and-set (same token version, same old KEK, no live refresh lease); no refresh is made and no token leaves the worker.
  4. Remove the old private key from the worker configuration.
  5. Keep the old private key offline and sealed (for example in the operator's password manager or HSM) only for as long as
     backups sealed under it are retained; destroy it when the last such backup expires. Record the destruction date.
- **Retention.** Backups follow `deploy/RUNBOOK.md` §Backups (proposed: 35 days). The provider replica (pages and objects) is
  append-only and retained with the tenant; it holds sandbox data only. Audit events are retained with the tenant.
- **Never** delete existing backups or financial history to remove old ciphertext; rotate and destroy the key instead.

## Live sandbox run (on the Mac, after the candidate is installed)

1. Make sure the app's interpreter has the new dependency: `.venv/bin/python -c "import cryptography"`; if it fails,
   `.venv/bin/python -m pip install cryptography==50.0.2` (or `uv pip install --python .venv/bin/python cryptography==50.0.2`).
2. Private setup (prompts with hidden input; prints only paths): `.venv/bin/python -m tools.provider_setup ~/Desktop/LedgerVigil/.provider-private`
3. Start the stack for a sandbox session with `LV_PROVIDER_SANDBOX_ENABLED=1` and `LV_PROVIDER_API_CONFIG=<dir>/api/api.env` added to
   the API environment only, and run the integration worker with `LV_PROVIDER_WORKER_CONFIG=<dir>/worker/worker.env` and
   `LV_WORKER_DATABASE_URL` (the existing worker login): `PYTHONPATH=src:. .venv/bin/python -m ledgervigil.provider.worker run-due`
   after each connect, read and disconnect (or the Celery `integration` queue).
4. In the browser at `http://127.0.0.1:3000`, as an administrator: Organization & settings → QuickBooks Online sandbox →
   Connect, sign in at Intuit, choose the sandbox company; then "Read sandbox data now"; then Disconnect.
5. Record evidence with the installer's read-only `qbo-evidence` command (connection states, job outcomes, page hashes,
   `intuit_tid`s, counts and provenance; never tokens, codes or object contents).
