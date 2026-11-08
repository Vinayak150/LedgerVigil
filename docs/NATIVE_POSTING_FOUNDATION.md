# Native posting foundation — bounded seed path

2 November 2026. Revision `0002_posting` adds an atomic developer-only SANDBOX manual-journal seed operation. It uses Stage 2A §19.2's accepted synthetic seeding exception. It does not implement an approved proposal, a BOOKS bootstrap, or a general provider adapter.

## Critical decisions and why

1. **Use a real connected schema.** Eleven new tables connect service principal, counter, batch, document identity/version/current pointer, entry/lines, event and account/customer projections. Journal sources reference exact document version and sequence through composite tenant/ledger foreign keys. No unbacked approval or operation UUID is stored; EXECUTION and BOOKS write capabilities are absent until their actual referenced structures and authorization exist.
2. **Use a separate seed credential.** `lv_seed` is a restricted login, not a member of `lv_owner` or `lv_app`. It has SELECT plus EXECUTE on one seed function, with no direct financial DML. The existing API login cannot execute that function. An active tenant-scoped DEVELOPER_SEED principal must map to the actual session login. The caller cannot supply an actor. The owner is non-superuser/non-BYPASSRLS; FORCE RLS still applies inside the function.
3. **Retain NUMERIC(20,4), prevent pre-validation rounding.** All amount inputs are JSON strings. The database checks lexical form, integer-digit bound, positive value and registered currency scale on raw text before the NUMERIC(20,4) cast. API and seed roles cannot bypass this through direct INSERT. Both-sided excess precision that would round into a balanced entry is explicitly tested. Administrative roles remain trusted; a DBA can bypass controls through DDL.
4. **Commit together or not at all.** The function locks the per-ledger counter, writes a single batch, a new MANUAL_JOURNAL document at version 1, one MANUAL_ADJUSTMENT entry and its lines, the event and projections, then updates the counter. Deferred constraints reject unbalanced or empty/incomplete financial commits, document/posting divergence, projection mismatch and counter/head mismatch. Python returns a success result only after transaction commit succeeds.
5. **Protect mutable prerequisites while posting.** Seed calls share-lock the principal, ledger, role bindings, period and accounts; counter locking serializes financial commits per ledger. Entry and line triggers independently check the open period and active account. After postings exist, ledger interpretation, account financial classification and role rebinding are refused pending a future audited transition. Existing account deactivation semantics and role-binding FKs remain in force.
6. **Preserve immutable history.** No app/seed UPDATE, DELETE or TRUNCATE grants exist on financial tables. BEFORE UPDATE/DELETE guards also reject ordinary owner edits. Inserts cannot append to an earlier transaction's batch. A privileged owner can still disable triggers/drop/truncate objects: this is not protection from a database administrator.
7. **Make retries observable.** A unique fixture source reference in a ledger identifies one body. An identical semantic JSON body returns the stored IDs/sequence. A different body under that reference is rejected. Detection runs under the ledger lock. This is local seed idempotency, not the unimplemented HTTP idempotency or provider replay contract.
8. **Replay separately.** Python reconstructs debit/credit totals from stored event lines, verifies sequence/identity/chain consistency and compares them with database projections. Events use a constrained canonical JSON envelope (ASCII keys, string values, nulls, arrays/objects); monetary values are strings. This is not a general RFC 8785 implementation or a sealed proof-pack format. The chain authenticates the defined event content, not every physical database column, and is not externally anchored.

## Supported input and limits

The only write function is `lv.seed_manual_journal(ledger_id, request_jsonb)`. Tenant comes from transaction-local trusted tool context; actor comes from the session login. Required request keys: `source_ref`, ISO `effective_date`, `description`, `lines`. Each line has an account UUID, D/C side and exact amount string, with optional party UUID. Unknown keys are rejected. The bounded developer interface allows 2–1,000 lines, 256 KiB of parsed request text, 512 description characters and a 128-character ASCII fixture source reference.

Every ledger must already have all seven valid SP-1 account role bindings. Only NATIVE SANDBOX ledgers can post. BOOKS and PROVIDER_CONNECTED are rejected in the function; a separate batch trigger independently enforces the ledger/principal boundary. All created facts are SYNTHETIC. No upload endpoint or source-ingestion path calls this function.

The first financial enum values are intentionally limited to SEED, MANUAL_JOURNAL, MANUAL_ADJUSTMENT and SYNTHETIC. Future kinds must arrive with their complete contracts/migrations; enum presence cannot imply support. Document version/current-pointer structures are present, but only initial version creation is enabled. No supersession, annotation, void or reversal operation is exposed.

## Requirement-to-evidence map

| Requirement | Evidence in the actual PostgreSQL suite |
|---|---|
| Exact received money (§3) | Invalid numbers, signs, NaN/Infinity, exponent syntax, whitespace, excess digits and excess scale rejected without residue; exact minimum/maximum round trips; registered exponents 0/2/3/4 |
| Balanced positive journals (§4, I-01/02) | Unbalanced function returns are rejected at actual COMMIT; one-line/zero/nonpositive requests rejected; committed line-to-document equality verified |
| Tenant, ledger, currency and party scope (§11–12) | Exact composite source-version FK, cross-tenant/ledger account and party rejection, missing context, scoped reads of posted tables, AR party requirement |
| Immutable history (I-10) | Ordinary owner UPDATE/DELETE rejected on all six history tables; app/seed financial writes denied; late append to prior commit refused |
| Open period (I-12) | Close-first blocks posting then causes rejection; post-first blocks close until posting commits; later posts rejected |
| Gapless sequence and entry number (I-13) | Failed commit and client rollback consume no number; concurrent same-ledger posts produce 1/2; another ledger proceeds while first is pending |
| Idempotent seed | Sequential and simultaneous duplicate calls converge to one stored effect; changed body rejected |
| Atomic projections/history | Deliberately corrupting a projection in a disposable test DB makes the whole commit fail; event replay matches current balances |
| Seed authority (§19.2) | Actual restricted seed login; API cannot invoke it; unregistered/revoked principal fails; BOOKS/provider ledger and direct forged BOOKS batch refused |
| Migration lifecycle | Fresh head upgrade → base downgrade → head upgrade passes; previous revision 0001 remains byte-for-byte unchanged |

Final suite: **135 passed**, including 66 prior cases and 69 new posting cases. The one upstream Starlette/TestClient transport deprecation warning remains. During initial disposable-database runs, two PL/pgSQL variable-qualification errors were found and corrected before applying revision 0002 to development. Failed-run evidence is retained.

Eight deliberately disabled safeguards were detected by targeted tests: four previous configuration guards plus balance, exact-input validation, history immutability and projection validation. These are bounded fault experiments, not exhaustive mutation coverage. Privilege changes used to inject projection corruption occur only in disposable test databases and are revoked in cleanup.

## Practical demonstration

`tools/demo_seed.py` creates a NEW synthetic tenant, a NATIVE SANDBOX ledger, the complete chart, a customer and an open October 2026 period. Three manual journals represent invoice-like, payment-like and settlement-like effects. They produce Bank 49,000 Dr, Fees 1,000 Dr, Revenue 50,000 Cr, AR zero and Clearing zero in INR. Replay matches account projections and an identical retry leaves exactly three entries and sequence 3.

These are manual fixture effects, not implemented Invoice/Payment/Settlement document operations, payment applications, policy decisions or an approval journey. A balanced wrong-sale fixture is not rejected by this seed path merely because its business interpretation is wrong: the policy layer must make that distinction in the future journey. Accounting oracle status remains UNREVIEWED.

## Remaining work and practical limits

- Typed financial document lifecycles and projections, real source/evidence references, version compare-and-swap, supersession, annotation and exact reversals are not yet implemented. No production/BOOKS financial path exists.
- Authentication, user permissions, maker-checker, approvals, proposal/read-set revalidation, native execution idempotency and outbox/worker integration remain missing. RLS is not user authentication; a trusted tool credential can select tenant context where it has a registered principal.
- Configuration and period-close commands still need their authenticated/audited commit-service lifecycle before public exposure. Raw configuration changes used in tests are not an implemented PERIOD_CLOSE event workflow.
- Seed projection verification recomputes ledger aggregates at commit. This intentionally favors correctness evidence for small fixtures; it is O(history) and has no production/performance claim. Large datasets, checkpoints and query plans remain future work.
- The canonical event format is local and constrained. Proof-pack schema, public contract generation, external anchoring and independent accounting review are not established.
- The existing pure accounting kernel is not wired into an approved proposal workflow. This checkpoint independently exercises persistence, not policy correctness or AI quality.
- The original master scope remains missing. PA-011-1 and D-1–D-11 remain PROPOSED. No provider experiment or QuickBooks write occurred.

Next bounded milestone: authenticated native identity and maker-checker contracts, followed by the typed source→proposal→simulation→verification→approval→execution path using this persistence foundation. Reuse the kernel and add the real document-operation references; do not expose the seed path as financial execution.

## Sources

Preserved Stage 2A §§3–4, 8, 11–12, 19.1–19.2: SHA-256 `edb99392a9b024407a4cbdf463fa9be0d120c59f2d94d93e2dcbb04cf4c22db0`.
Preserved ADR record: SHA-256 `6217b047723e791a03d64a34e61e882e865794f4fbde95ad575a72fa50f0f491`.

Primary technical references: [PostgreSQL numeric coercion](https://www.postgresql.org/docs/current/datatype-numeric.html), [row locking](https://www.postgresql.org/docs/current/explicit-locking.html), [SECURITY DEFINER function precautions](https://www.postgresql.org/docs/current/sql-createfunction.html).
