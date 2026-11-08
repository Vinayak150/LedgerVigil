# Simulation foundation: deterministic native RecordSettlement simulation from pinned inputs

Migration `0011_simulation_runs` and package `src/ledgervigil/simulation/`. Tests: `tests/test_simulation.py`. SANDBOX and SYNTHETIC only. Accounting status: **UNREVIEWED**.

**Not built:** no `/submit`, no proposal state change, no what-if `POST /simulations`, no approvals, no execution, no jobs. The draft API's behaviour is unchanged.

## What happens

`simulation.service.simulate(request, proposal_id, revision=None)` is the entry point the atomic-submission milestone will call. It has no public route yet (SM-C1).

1. **Boundary (READ COMMITTED).** The session must be verified, the tenant comes from the session, and the caller needs `propose`.
   - The proposal must be visible under row-level security, and must be DRAFT.
   - Absent and other-tenant ids give the same 404.
   - The revision (default: the head revision) must exist.
2. **Capture.** This runs on a second pooled `lv_app` connection, in a REPEATABLE READ, READ ONLY transaction bound to the session's tenant. It reads **one** snapshot:
   - the proposal revision;
   - the adapter's ledger facts, using the existing `kernel_adapter.loader._read` unchanged: role bindings, periods, every posting up to the sequence, the balance projection, typed payment versions with identity and posting, the payout identity's settlement, active inclusions and the payout identity's staged statement;
   - the cited statement with its record and file SHA-256;
   - every cited evidence reference;
   - the read set;
   - the installed versions.

   The capture refuses to run if the transaction is not isolated, or if it wrote anything.
3. **Pure evaluation (`engine.evaluate`).** It reads only the bundle and the installed code: no clock, no randomness, no database. It reuses the adapter and vendored kernel unchanged (`calculate_settlement`, `simulate_settlement`, `sp1-posting/1.0`).
4. **Record.** The SECURITY DEFINER function `lv.simulation_record` runs inside the boundary transaction:
   - It re-binds the session and re-checks `propose`, DRAFT and the revision.
   - It requires the bundle to name exactly this tenant, ledger, proposal, revision, content hash and a committed sequence.
   - It inserts one immutable `simulation_run` row.

   Any failure before COMMIT leaves no row. Nothing else is ever written.

**Replay.** `POST /api/v1/simulations/{id}/replay` (`read`) recomputes from the stored canonical bundle only, so it never reloads current values. It verifies the stored text against its hash and its canonical form, refuses with 409 if any pinned version differs from what is installed, and returns `{original_hash, replay_hash, identical}`.

**Reading a run.** `GET /api/v1/simulations/{id}` (`read`) returns the run: snapshot, read set, versions, pinned sources, hashes and outcome.

## Contract mapping

| Contract | Implementation |
|---|---|
| 2A §8.2 / 2B `SnapshotRef` | `ledger_id`, `seq`, `profile_version` (ledger), `posting_rule_version` `sp1-posting/1.0`, `policy_set_version` **null** (SM-G1), `provider_watermark` null, `reconciliation` NOT_REQUIRED (native). The batch and event chain heads are also pinned in the bundle |
| 2A §9.1 read set, "computed by the server" | `DOCUMENT` (each item payment, version at the snapshot) and `PERIOD` (the period covering `payout_date`, its `version`), sorted |
| 2A §9.1 validation order | Capability (native SANDBOX, one RecordSettlement v1, synchronous budget) → profile (currency, full-item amounts) → kernel and adapter preconditions. Each failure is a REJECTED run carrying the typed problem, with no postings and no verdicts |
| 2A §19.3 preconditions | DUPLICATE_SETTLEMENT (payout identity used), ITEM_ALREADY_SETTLED (SETTLED version or active inclusion), PERIOD_CLOSED: `PRECONDITION_FAILED` with the contracted reason. Totals, processor and other kernel or adapter preconditions: `PRECONDITION_FAILED` with the kernel or adapter code, flagged `reason_named_by_contract: false` (SM-G2). PARTIAL_ITEM_SETTLEMENT gives `UNSUPPORTED_BY_PROFILE`; a wrong currency gives `CURRENCY_MISMATCH` |
| 2A §10.1 steps 3–5 | Predicted postings (Dr BANK net, Dr FEE fee when above zero, Cr CLR gross); `AccountDelta` for every bound account (before, after and delta as SignedMoney); `DocumentEffect` (settlement CREATE; each payment UPDATE_IN_PLACE, a new version from v to v+1, RECEIVED to SETTLED, open amount unchanged) |
| 2A §10.1 step 6, §6.2, ADR-014 | **Simulator 0.1.0 (this foundation): not evaluated.** `verdicts: []`, `aggregate: null`, `verification.status: NOT_EVALUATED`. Simulator 0.2.0 adds SP-1 verification: see `docs/POLICY_VERIFICATION.md`. The kernel's own settlement-slice checks are returned under `calculation.kernel_diagnostics`, labelled as diagnostics, not verdicts |
| 2A §10.2 determinism | Canonical JSON (sorted keys, ASCII, no floats); money and dates as exact strings; the transaction timestamp is excluded from the bundle. The complete evaluation uses precision 34 and ROUND_HALF_UP inside an isolated Decimal context; the same committed state gives the same `input_hash` and `result_hash` |
| 2B §7.3 | Synchronous runs are COMPLETED or REJECTED and immutable (trigger plus hash CHECKs) |
| 2B Family 9 replay | Stored inputs and versions; 409 if a pinned version is not installed |
| 2B Family 9 / CT-10 "simulation writes only simulation rows" | The only write is `simulation_run`. Financial, ingestion, identity and proposal tables, counters, events and balances are unchanged (tested) |

## Implementation choices awaiting confirmation

| ID | Choice | Reason |
|---|---|---|
| SM-C1 | Simulation creation is an internal service with no public route | The user scope excludes `/submit`. `POST /simulations` (what-if) takes raw operations, not a draft revision |
| SM-C2 | Any existing revision of a DRAFT proposal can be simulated; several runs per revision are allowed | 2B "one per revision" is a submission rule. A draft edit during simulation still records the run against the captured revision (tested) |
| SM-C3 | REJECTED runs are recorded with their problem | 2B §7.3 "created as COMPLETED or REJECTED (pre-simulation rejection with its problem)" |
| SM-C4 | Integrity failures (captured facts inconsistent) record nothing and return 500 | They are not outcomes and must not be replayed as results |
| SM-C5 | Each item payment becomes `UPDATE_IN_PLACE` with `version_before`/`version_after` | 2A §10.1: "UPDATE_IN_PLACE adds a new version of an existing document"; ADR-008 amendment |
| SM-C6 | Synchronous limits: at most 200 items and 20,000 posted entries, otherwise `CAPABILITY_UNAVAILABLE` | 2B: above 200 documents needs a SIMULATION job, which is not built; R-08 |
| SM-C7 | The bundle embeds the full captured facts, including the staged statement's file bytes (base64) | Replay must never reload; the adapter's evidence observations re-parse those bytes |
| SM-C8 | The simulator version is `lv-native-recordsettlement/0.1.0` | Foundation version, recorded per 2A §10.2 |

## Gaps (stated, not resolved)

| ID | Gap | Current behaviour |
|---|---|---|
| SM-G1 | No policy set (`sp1-policies/1.x`) or ADR-014 registry is installed. **Addressed for new runs by simulator 0.2.0 (`docs/POLICY_VERIFICATION.md`); 0.1.0 runs keep this status** | No verdicts, aggregate or verification. `policy_set_version` is null. **Submission needs this decided** (READY_FOR_REVIEW versus BLOCKED depends on verdicts) |
| SM-G2 | The §4.2 reason list names DUPLICATE_SETTLEMENT, ITEM_ALREADY_SETTLED and PERIOD_CLOSED, but not settlement arithmetic (I-09), processor mismatch, no period for the date, incomplete configuration or an item that is not a typed payment | The kernel or adapter code is the reason, flagged `reason_named_by_contract: false` |
| SM-G3 | Replay's 409 for a missing version has no code in §4.2 | `SIMULATION_VERSION_UNAVAILABLE` (class CONFLICT) |
| SM-G4 | `ACCOUNT` read-set entries need a version; `lv.account` has no version column, and the runtime role can update accounts | Account facts (role, type, active) are pinned in the bundle but are not read-set entries |
| SM-G5 | The staged statement is not a `DOCUMENT` (P-STL-01 compares a posted statement, path B), and no `STAGED_DOCUMENT_RESERVED` reservation exists | The statement is pinned by id with its record and file hashes. Its equality with the operation is reported by the adapter's evidence observations, not as a verdict |
| SM-G6 | Multi-operation drafts need a sequential overlay | REJECTED, `CAPABILITY_UNAVAILABLE` / MULTI_OPERATION_SIMULATION |
| SM-G7 | PG-1 (JSON operations versus normalised `proposal_operation` and `proposal_read_set` tables) is unresolved | The read set lives in the immutable bundle, not a `proposal_read_set` table |
| SM-G8 | `ledger_snapshot` (2A §8.2 balances hash) is not created | The snapshot is identified by sequence plus batch and event chain heads in the bundle |
| SM-G9 | Revalidation (2A §10.3) and staleness | Not built. The staleness consumer is still absent; nothing marks proposals STALE |

## Native integration correction: simulator 0.2.1

New captures pin `lv-native-recordsettlement/0.2.1`. The complete adapter and policy evaluation runs inside the kernel's fixed Decimal context (precision 34, ROUND_HALF_UP and its declared traps). This covers direct Decimal projection and policy sums as well as Money methods. Caller settings and flags are restored on success or failure. Independent native tests reproduced three failures under low caller precision before this correction and pass afterwards. Existing 0.1.0 and 0.2.0 profiles remain installed; the original packaged frozen outcomes are checked byte-for-byte. No policy rule or provenance is changed.

## Account dependencies — simulator 0.3.0

Migration 0012 adds managed account dependency versions covering configuration, bindings and balance projection changes. New captures pin ACCOUNT entries from the same snapshot as DOCUMENT and PERIOD entries. See ACCOUNT_READ_SETS.md. The SM-G4 capture gap is addressed for new 0.3.0 runs; action-time revalidation and submission remain pending. Older profiles preserve their original incomplete read-set shape for historical replay and cannot authorize future actions.
