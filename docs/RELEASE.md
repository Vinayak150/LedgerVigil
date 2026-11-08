# Release candidate 0037 — clean-clone setup, verification and decision

**Candidate head:** `0037_restore_interlock`. **Decision:** ready for local and sandbox use, and for a deployment-validation
exercise on SANDBOX ledgers and the QuickBooks sandbox only. **Not** ready for production, real BOOKS data or production
QuickBooks: named blockers below. Accounting is UNREVIEWED. Nothing here has been installed on the owner's Mac or deployed.

## Clean clone (development and tests)

Supported runtimes: Python 3.14, Node 22.23.4, PostgreSQL 18, Redis 7.4 (pinned dependencies in `requirements.lock`,
`worker-requirements.lock`, `web/package-lock.json`).

```sh
git clone <repository> ledgervigil && cd ledgervigil
python3.14 -m venv .venv
.venv/bin/python -m pip install --no-deps -r requirements.lock -r worker-requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
# A disposable PostgreSQL 18 listening on 127.0.0.1:55432 (for example: docker compose up -d db, using compose.yaml,
# after scripts/setup has written .env), then:
POSTGRES_PASSWORD=<that server's superuser password> PATH=.venv/bin:$PATH sh scripts/ci_prepare.sh
PYTHONPATH=src:. .venv/bin/python -m tools.dev_db migrate
PYTHONPATH=src:.:tests .venv/bin/python -m pytest -q tests        # serial: tests share one server
cd web && npm ci && npx tsc --noEmit && node --test tests/*.test.mjs scripts/*.test.mjs && node scripts/check-api.mjs && npx next build
```

Optional test inputs and their effect:
- `LV_PG_CLIENT=local LV_PG_BIN=<PostgreSQL 18 bin>` — recovery drills use local client tools instead of the Mac Docker path.
- `LV_TEST_REDIS_URL` — a disposable Redis for the real-broker tests (otherwise a local `redis-server`, then Docker).
- `LV_PUBLIC_INDEX` — the pinned public UCI index; without it `tests/test_plan_inspection.py` cannot run and must be
  reported as not run (CI does exactly that).

CI (`.github/workflows/ci.yml`): secret scan of tracked files and history, the full backend suite against PostgreSQL 18 and
Redis services, Python and npm dependency audits, frontend typecheck, unit tests, contract parity, delivered-source
manifest, formatting and production build. **CI has not run yet**: the repository was not pushed from this workspace.

## Deployment artifacts

`deploy/` (compose, Dockerfiles, Caddy TLS proxy, secret layout, runbook). Not built or run: no container runtime was
available where they were prepared. See `deploy/README.md` and `deploy/RUNBOOK.md` (backups, restore, key rotation,
incidents, rollback).

## Production blockers (named, unmet)

1. **MFA (TOTP)** for review, execute, ledger.bootstrap and administer — not implemented (ADR-027 gate).
2. **Q-16** — the database trusts the API's password verdict; a stolen `lv_app` credential can mint sessions. Needs a separate
   authentication service and login (architecture decision).
3. **Accounting review** — no qualified accounting review of any posting rule, oracle or evaluation label.
4. **Native installation** — the Mac still runs `0029_account_patch`; no install receipts.
5. **Live QuickBooks sandbox** — never run (Intuit unreachable from the workspace and the VM; no sandbox app configured).
   Experiments X-1..X-10 not run; provider writes stay disabled.
6. **Offsite backup and measured RPO/RTO** — no authorized offsite destination; only same-host drills measured.
7. **Real email delivery** — the SMTP adapter is untested against a mail service.
8. **Deployment images** — written, never built or run.
9. Remaining Low security items: shared `lv_worker` login for maintenance and provider workers (N2); Redis without a
   password; frontend image with dev dependencies; Caddy filters unvalidated against a Caddy binary.

Everything else that the completion matrix marks NOT IMPLEMENTED (for example CDC/webhooks, provider writes, typed operations
beyond RecordSettlement, metrics/alerts, HTTP import upload) is outside what this candidate claims.
