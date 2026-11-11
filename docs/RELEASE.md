# LedgerVigil candidate 0040

Migration head: `0040_mfa_reset`. This source is a review candidate, not an
installed or deployed release. Accounting remains UNREVIEWED.

## Current scope

Native SANDBOX RecordSettlement proposals, independent approval, execution,
withdrawal, semantic revalidation, financial reads, audited period close,
proof/export and recovery checks are implemented. Detection and the read-only
QuickBooks SANDBOX connector remain separately gated. Identity uses the isolated
issuer login; ordinary API database credentials cannot issue sessions or attest
password checks. MFA adds enrollment, challenge, recovery codes, reset cooling-off,
BOOKS privilege checks, and transactional key rotation. See MFA_LIFECYCLE.md.

Execution receipts and exact-commit test results belong to the dated review
package. Do not infer a current-tree pass from historical counts in older docs.
The previous installed source was 0029; no installation or deployment is implied.

## Reproduce

Use pinned Python 3.14 dependencies from requirements.lock and worker-requirements.lock,
Node 22.23.4 and web/package-lock.json, and a fresh PostgreSQL 18 instance.
Run scripts/ci_prepare.sh only against a disposable database, migrate to head,
then execute pytest serially with PYTHONPATH=src:.:tests. For recovery tests set
LV_PG_CLIENT=local and LV_PG_BIN to PostgreSQL 18 client tools. LV_PUBLIC_INDEX must
name the pinned UCI index; absence is a blocked input, never a passing test.

Frontend: typecheck, node --test tests/*.test.mjs scripts/*.test.mjs, check:api,
source manifest, CI formatting, and production build. Real browser/axe checks
are additional to unit tests. Deployment Compose is in deploy/compose.yaml;
the issuer-only MFA key overlay is deploy/compose.mfa.yaml. Build images locally,
validate the configuration, and rehearse backup/upgrade/rollback before install.

## Open gates

- No qualified accounting review or independent business-outcome labels.
- No installation rehearsal or native installation of this candidate.
- MFA defaults remain PROPOSED; production key rotation/reset require a
  deployment drill. No administrator lost-factor bypass is implemented.
- First-BOOKS bootstrap and provider production writes remain disabled; the
  unresolved accounting contracts must not be silently filled in.
- Live QuickBooks receipts are scoped to OAuth, realm verification and one
  read synchronization, not X-1–X-10 or write safety.
- Full browser/axe, real email, offsite recovery and measured offsite RPO/RTO
  remain open until executed and recorded.
- HTTP import upload, CDC/webhooks, broader typed financial operations, metrics
  and alerts remain unfinished implementation.
- Hosted CI and push are not implied by local checks.
