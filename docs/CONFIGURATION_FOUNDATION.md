# Configuration foundation — critical review and traceability

This is an implementation checkpoint, not a new accepted ADR. It implements prerequisites from the preserved Stage 2A and ADR sources. PA-011-1 and D-1–D-11 remain PROPOSED. No candidate recovery schema or state transition was adopted.

## Why this boundary

A journal is not an isolated pair of debit/credit rows. Stage 2A §4 ties it to exact immutable document versions, a commit sequence, a posting rule and, when executed, an approved operation. Sections 8 and 19.1 require history/projection atomicity and version sequencing. Creating those columns without their real references would produce a misleading persistence demonstration. This migration therefore stops before any financial posting path and supplies the actual configuration dependencies first.

Dependency sequence:

1. Global currencies and tenant identity.
2. Ledger, exact currency/exponent reference, immutable purpose.
3. Accounts and tenant/ledger-scoped role bindings; customers and monthly periods.
4. Next: principal/operation authority, immutable document/version/source references, counter/batch/event structures, postings and projections in one connected migration/service design.
5. Then: seed/approval/execution paths, evidence export and the complete native journey.

The future design must distinguish approved proposal execution from tightly fenced SANDBOX seeding. No implementation may substitute an unaudited administrative insert for an application workflow.

## Requirement → implementation → evidence

| Requirement | Implementation | Actual evidence |
|---|---|---|
| Stage 2A §11.1, §12.1: scoped keys/RLS | Six scoped tables; tenant-leading PKs/uniques; composite ledger references; ENABLE/FORCE RLS | Populated cross-tenant reads on every scoped table; cross-tenant write rejection; cross-ledger parent/binding rejection; owner also subject to RLS |
| §12.1: separate owner/application roles | NOLOGIN owner; separate non-superuser migrator and application LOGINs; app cannot assume owner | Actual lv_app connection; DDL, policy, metadata, delete/truncate and global-catalog mutations rejected |
| §12.1: transaction-local tenant | Internal helper refuses an already active transaction; SET LOCAL via parameterized set_config | Reused connection across tenants, commit and rollback; missing/malformed context; nested-switch rejection |
| §2.2: role semantics | Account type enum, generated normal side; copied attributes constrained by composite FK and role CHECK | All seven role shapes, ten normal-side cases, invalid/forged bindings and subsequent account drift |
| §3.1: registered exponent | Global read-only catalog; ledger/account composite references | Canonical INR/USD/GBP; forged exponent/currency rejected |
| §2.1/11.2: monthly periods | Finite month boundaries, tenant/ledger GiST exclusion, immutable dates, no reopen | Invalid ranges, adjacency, overlap, no reopen; observed blocking and rejection of concurrent conflicting insert; another ledger proceeds |
| §19.2 / ADR-024: immutable purpose | Guard on purpose and scope/identity updates | BOOKS↔SANDBOX refused; ordinary rename succeeds |
| ADR-020: migration authority | Alembic revision 0001, restricted owner, separately provisioned prerequisites | Development upgrade; fresh DB upgrade→downgrade→upgrade; protected development downgrade |

## Review findings and handling

- **No silent rounding claim.** Existing probe demonstrates NUMERIC(20,4) coercion can erase excess input precision before a CHECK sees it. No money column/write path is introduced here. The future migration must validate raw amounts before casting and prevent direct writes bypassing that boundary; it must not weaken the accepted exact-money requirement.
- **No false isolation claim.** RLS filters with trusted application context; it is not authentication. The application credential can deliberately set another UUID. Server-side authentication, role permissions, maker-checker and audit linkage must exist before exposing configuration or financial endpoints.
- **No vacuous empty-table proof.** Tenant-read tests populate all six scoped tables for two tenants before checking visibility.
- **No fake concurrency proof.** The exclusion test verifies `pg_blocking_pids` names the competing writer, releases the first transaction, then expects a database exclusion error. Another ledger's insert succeeds while the first remains uncommitted. This establishes period-configuration concurrency, not closed-period versus journal-posting behavior.
- **No source-of-truth drift in role bindings.** Copied account characteristics are foreign-keyed to the live account. Deactivation/classification changes cannot invalidate a bound role silently.
- **No mutation of accepted records.** Previous application source was archived before edits. Prior document/review packages are checked against hashes and retained. New implementation notes do not change approval status.
- **First-run failure corrected in the test, not the guard.** The forged-copy test originally violated role shape before reaching its intended FK. The new input has a valid role shape but falsifies the referenced account type. Baseline is retained as `tests-initial.txt`; the corrected suite passes.
- **Guard sensitivity checked.** Removing FORCE RLS for the owner, the purpose trigger, the role-binding FK or the period exclusion causes its targeted invariant test to fail. These are four bounded experiments, not exhaustive mutation coverage.

## Deliberate limits

Configuration tables are not a financial ledger. There are no document/version/principal/approval rows, commit counters, postings, immutable financial history, BOOKS seed guard, operational setup-readiness gate, audit chain, authenticated API or UI here. Configuration deletion is unsupported in this first revision; scoped identities and period dates cannot be rewritten. Later lifecycle behavior requires an explicit migration and appropriate audit service.

Longer reporting-parent cycles, limits on role rebinding once financial history exists, complete module import boundaries and full account configuration audit are not implemented. These must be settled before those configuration actions are publicly exposed. Merely having the seven role-binding shapes tested does not mean every ledger has all required bindings.

The original full master scope (modules A–M, seven workspaces) remains a separate traceability gap; this change infers no missing modules. Accounting oracles remain UNREVIEWED; QuickBooks capabilities remain unvalidated.

## Sources

- Preserved `docs/incoming/LedgerVigil-Stage-2A-Financial-Domain-and-Database.md`, SHA-256 `edb99392a9b024407a4cbdf463fa9be0d120c59f2d94d93e2dcbb04cf4c22db0`.
- Preserved `docs/incoming/LedgerVigil-ADR.md`, SHA-256 `6217b047723e791a03d64a34e61e882e865794f4fbde95ad575a72fa50f0f491`.
- [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html): owner/FORCE behavior, privileged bypass and FK limitations.
- [PostgreSQL range constraints](https://www.postgresql.org/docs/current/rangetypes.html): exclusion constraints for overlap prevention.
- [Alembic migration environment](https://alembic.sqlalchemy.org/en/latest/tutorial.html): migration ownership and project environment.
