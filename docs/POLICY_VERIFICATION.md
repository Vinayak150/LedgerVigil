# SP-1 policy verification for native RecordSettlement (`sp1-policies/1.1`)

Status: candidate, **not reviewed**. Data: SYNTHETIC. Accounting semantics: **UNREVIEWED**. A verdict is a computed check under
profile SP-1. It is not an accounting review, a certification or execution readiness (`eligible_for_execution` stays `false`).

The per-policy coverage table (inputs, applicability, implemented behaviour, owning section, permitted outcomes, reachability)
is `docs/POLICY_COVERAGE.md`, generated from the registry and checked by the test suite.

## Where verification runs

- **Stage 2A §10.1 step 6.** `engine.evaluate(bundle)` computes the simulated effects first (unchanged foundation code). For a
  COMPLETED outcome pinned to simulator `0.2.x`, it then calls `policies.verify(bundle, outcome, snapshot, operation)`.
- **Inputs.** The registry reads only:
  - the frozen bundle: ledger, operation, the pinned statement, and ledger facts rebuilt from `bundle.facts`;
  - the simulated outcome: predicted postings, account deltas and document effects.

  It reads no database, clock or network. Replay re-evaluates the stored canonical bundle, so verdicts replay exactly.
- **Validation order (§9.1).** Schema, profile, precondition and capability refusals stay typed REJECTED outcomes. They carry
  `verdicts: []`, `aggregate: null` and `verification.status: NOT_REACHED`.
- **Kernel diagnostics.** These stay under `calculation`, labelled as diagnostics. No verdict is derived from them, and a test
  asserts that changing them does not change any verdict.

## Versions and reproducibility

| Profile | Outcome format | `policy_set` | `policy_registry` | Behaviour |
|---|---|---|---|---|
| `lv-native-recordsettlement/0.1.0` (legacy) | `lv-simulation-outcome/1` | null | — | Exactly the simulation foundation: `verdicts []`, NOT_EVALUATED. Existing runs replay through this path; verdicts are never retrofitted |
| `lv-native-recordsettlement/0.2.0` (previous profile) | `lv-simulation-outcome/2` | `sp1-policies/1.1` | `sp1-policies/1.1+sha256:<SHA-256 of policies.py>` | Verified |

- **Pinning.** New bundles pin the full version set, and `snapshot.policy_set_version` = `sp1-policies/1.1`.
- **Unavailable versions.** Replay requires the pinned simulator's version set to be installed exactly. A changed or missing
  `policy_set` or `policy_registry`, or an unknown simulator, returns 409 `SIMULATION_VERSION_UNAVAILABLE`.
- **Registry identity.** Any change to any rule changes the registry content hash, so runs recorded under another registry
  refuse to replay rather than being re-judged.

## Fail-closed rules

- Every Stage 2A §6.2 policy and P-STL-01 is registered with the outcomes its contract row permits. An evaluator that returns
  any other outcome raises `PolicyContractViolation`. The simulation then fails with 500 and **nothing is recorded**.
- A required fact that is absent raises `MissingFact`, with the same result, unless the contract permits INSUFFICIENT_EVIDENCE
  for that condition (for example, a missing statement). Examples of `MissingFact`:
  - no covering period;
  - application or AR effects without the facts to judge them;
  - item payments without their posting entries.
- **Invalid amounts.** When P-CUR-01 FAILs, policies that cannot compute with the invalid amounts are listed under
  `verification.not_evaluable` and are never reported as PASS or NOT_APPLICABLE. The P-CUR-01 FAIL already blocks. An amount
  error without a P-CUR-01 FAIL fails closed.
- **P-AUT-01** is "evaluated at approval and execution, not in simulation" (§6.2). It produces no verdict here and is listed
  under `verification.deferred`; it is not counted as NOT_APPLICABLE.

## Aggregation (ADR-014, 2B aggregate enum)

- Any FAIL gives BLOCKED_FAIL.
- Otherwise, any INSUFFICIENT_EVIDENCE gives BLOCKED_INSUFFICIENT. A controller override would be required; overrides are
  **not built**, so `override` reads `NOT_AVAILABLE`.
- Otherwise the result is CLEAR, which requires at least one PASS. An all-NOT_APPLICABLE set is refused.
- `verification.counts` reports each outcome separately, so NOT_APPLICABLE never inflates passes.
- `result_statement` reads "verified under profile SP-1, policy set sp1-policies/1.1" only for CLEAR, and "blocked under …"
  otherwise. It never says "compliant".

## What end-to-end runs show today

- **Baseline.** Every SYNTHETIC draft is **BLOCKED_INSUFFICIENT**: P-EVD-01 is INSUFFICIENT because the statement provenance is
  SYNTHETIC, not SOURCE.
- **No end-to-end CLEAR.** CLEAR is not reachable end to end, and existing evidence is not relabeled to manufacture one. The
  PASS branch of P-EVD-01 and the CLEAR aggregate are exercised only in an isolated, in-memory test, which also asserts that the
  stored provenance is still SYNTHETIC.
- **Reachable end to end:**
  - P-STL-01 FAIL (fee, net or items disagree with the statement);
  - P-FEE-01 FAIL;
  - P-CLR-02 FAIL (clearing would end in credit);
  - FAIL taking precedence over INSUFFICIENT.
- **Isolated only**, because an earlier typed precondition refuses the same facts first (each test asserts that masking):
  - P-ACC-01, P-PER-01, P-DUP-01 and P-CUR-01 FAILs;
  - P-REV-01 FAIL;
  - P-CLR-01 FAIL;
  - P-SNP-01 PASS and INSUFFICIENT.

## Proposed choices (need review; not decided by contract)

| Id | Choice |
|---|---|
| PV-C1 | Explanation codes are proposed names. The three 2B examples are reused where they fit: ENTRY_BALANCED, CLEARING_NOT_RELIEVED_FOR_SETTLED_ITEMS, and (as SETTLEMENT_ENTRY_TOUCHES_REVENUE) the revenue case |
| PV-C2 | "The statement document" for P-STL-01, P-FEE-01 and P-CLR-01 is the staged statement the draft cites (`statement_document_id`, pinned in `pinned_sources.statement`). This follows PG-2 / PC-4 |
| PV-C3 | Statement items are matched to proposed items by (payment `processor_item_id`, kind, amount) as a multiset. Payout identity (processor, payout, date) disagreement is recorded as a fact (`identity_matches`), not a FAIL; §19.3 names gross, fee, net and items only |
| PV-C4 | P-EVD-01 for RecordSettlement requires the settlement statement (§9.2) with SOURCE provenance. Other cited evidence is recorded but not required |
| PV-C5 | A RecordSettlement entry is a "settlement entry" for P-REV-01: touching revenue on either side is FAIL. The invoice-source PASS and manual-adjustment INSUFFICIENT branches belong to operations not simulated |
| PV-C6 | P-DUP-01 PASS needs frozen facts showing no active payout settlement, no active inclusion and RECEIVED status for every item. The "same customer, amount and date without a source reference" INSUFFICIENT branch does not apply: settlements always carry a source reference |
| PV-C7 | Registry identifier = policy-set semver + SHA-256 of `policies.py` source (ADR-014 "semantic version plus content hash") |
| PV-C8 | Policy versions are each "1.0" (unchanged from the §6.2 rows); P-STL-01 is "1.0" within set 1.1 |
| PV-C9 | `verification.status` values EVALUATED and NOT_REACHED, and the `not_evaluable` and `deferred` lists, are proposed fields inside the stored outcome |

## Gaps (explicit; not built)

| Id | Gap |
|---|---|
| PV-G1 | The contracted `verdict` table is not created. Verdicts live inside the immutable, hashed simulation outcome. No migration is added in this milestone |
| PV-G2 | INSUFFICIENT_EVIDENCE overrides (controller, reason, evidence) are not built. BLOCKED_INSUFFICIENT cannot be cleared |
| PV-G3 | P-AUT-01 is deferred to approval and execution (not built) |
| PV-G4 | P-APP-01 and P-SUB-01 have no application or subledger post-state facts. They are NOT_APPLICABLE for RecordSettlement and fail closed otherwise |
| PV-G5 | Account-version read sets (SM-G4) and statement reservations remain **submission dependencies**, unchanged |
| PV-G6 | No SOURCE-provenance statement exists in this system (all data SYNTHETIC), so CLEAR is unreachable end to end |
| PV-G7 | The 409 code `SIMULATION_VERSION_UNAVAILABLE` remains unnamed by the contract (SM-G3) |

Native integration uses simulator `0.2.1` for new captures with the same policy registry and outcome format. It pins the full Decimal context; `0.1.0` and `0.2.0` remain replayable. See SIMULATION.md. All PV-C choices remain PROPOSED; integration is not their approval.
