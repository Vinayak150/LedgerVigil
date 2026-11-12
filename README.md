# LedgerVigil

LedgerVigil is an auditable financial-change workflow: inspect source evidence,
prepare a typed proposal, simulate it deterministically, obtain an independent
review, then record the native settlement and its proof in one transaction.
Amounts use exact decimal inputs; PostgreSQL owns the state transitions,
tenant isolation, immutable history and concurrency controls.

**Current source: `0040_mfa_reset`.** This is an integration candidate. The owner's
installed application remains at `0029_account_patch`. It has not been deployed.
Accounting is **UNREVIEWED**; business effectiveness is **NOT ESTABLISHED**.
See [release scope and open gates](docs/RELEASE.md) before running or assessing it.
Historical test counts in evidence folders describe their recorded source trees.

## Implemented scope

| Area | Behavior |
| --- | --- |
| Identity | Separate identity issuer and database login; password verification, sessions, CSRF, revocation, invitations, reset, membership administration and audit log |
| MFA | Encrypted TOTP enrollment, replay refusal, single-use recovery codes, session step-up, 24-hour reset cooling-off and transactional key rotation |
| Native workflow | SANDBOX RecordSettlement drafts/revisions, deterministic simulation, independent exceptions/review, approval/rejection, withdrawal, semantic revalidation and atomic execution |
| Evidence | Source inspection, exact-byte integrity checks, financial reads, audited period close, durable export jobs, chained proof packs and recovery verification |
| Detection | Gated deterministic advisory findings and review UI; no autonomous posting, no established business-accuracy claim |
| QuickBooks | Separately gated read-only development-sandbox OAuth and replica; no QuickBooks write path or production connection |

## Run and verify

Use an isolated checkout with Python 3.14, Node 22 and PostgreSQL 18. Follow
[reproduction instructions](docs/RELEASE.md), [deployment-validation setup](deploy/README.md)
and the [operations runbook](deploy/RUNBOOK.md). Development provisioning must
use a disposable synthetic database, never an existing database you care about.
Pinned dependencies are in `requirements.lock`, `worker-requirements.lock` and
`web/package-lock.json`. Native installation requires a backup and a successful
rehearsal on a copy first; there is no permission to reset the installed database.

Generate private configuration with the operator tools. Never commit environment
files, credentials, provider keys, dumps, backups, or MFA/recovery secrets.
The issuer-only MFA key mount is in `deploy/compose.mfa.yaml`. See
[MFA lifecycle](docs/MFA_LIFECYCLE.md), [issuer boundary](docs/IDENTITY_ISSUER.md),
[QuickBooks sandbox setup](docs/QUICKBOOKS_SANDBOX.md) and
[restore interlock](docs/RESTORE_INTERLOCK.md).

Detection stays off unless explicitly enabled for a separate validation session.
Provider writes, BOOKS writes and production gates remain closed. The connector
has a separately recorded live OAuth/realm/read receipt on the earlier 0039
workspace; that evidence does not attest a later candidate or X-1 through X-10.

## Release limits

Refunds, voids, reversals and BootstrapLedger require their owning accounting
contracts before implementation. HTTP import upload, CDC/webhooks, broader typed
operations, and operational metrics/alerts are not complete. Final independent
security review, native installation/rollback, real email delivery, hosted CI,
qualified accounting review, independent evaluation labels and offsite recovery
remain release gates. Local Git anchors are neither signatures nor offsite storage.
Policy defaults and unresolved decisions retain their recorded PROPOSED status.

The dated review package provides the exact commit, manifest, executed checks,
failed/setup attempts and completion matrix. A green test suite is not a claim
that the full product is production-ready. Licensing and third-party attribution
are recorded in `NOTICE.md`; do not infer an open-source license from publication.
