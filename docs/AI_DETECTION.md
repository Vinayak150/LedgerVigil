# First detection slice: statement-to-ledger settlement reconciliation

Status: **implemented candidate, disabled by default** (`LV_DETECTION_ENABLED=1` mounts it). Every product choice below that the
accepted documents do not make is labelled **PROPOSED**; none is approved by this implementation. Accounting status: UNREVIEWED.
All data is SYNTHETIC.

## 1. What "AI detection" means here (recovered from the owning documents)

| Source (accepted copy 20261102-132232-claude) | What it says |
|---|---|
| Stage 1 Part A, Part C v1 scope | "The AI proposes; deterministic code decides." The AI is an *investigator that drafts typed proposals for fixture discrepancies, evaluated against a rules-only baseline*; success criterion 2: the AI must measurably beat a rules-only baseline "or the write-up says plainly that it does not". |
| Stage 1 H-6 | Intuit already ships anomaly detection; LedgerVigil's value is source-linked relationships, not generic anomaly scores. |
| Stage 1 H-8, M-6; ADR-017 | Frozen evidence sets, stable ids, imported text is data not instructions, schema-validated output, any cited id outside the evidence set rejects the output, labels from humans or a deterministic generator, model output only in run tables, a rules-only baseline on every evaluation. |
| Stage 2A §5.3 | The failure to catch: a payout that the settlement statement shows as paid out, with its payment still sitting in Processor Clearing. |
| Stage 2B Family 7, Workflow 2, §7.2 | `POST /investigations` → findings labelled `HYPOTHESIS`; the canonical question is "Why is clearing not zero?" for STL-3001 / PMT-2001. |
| Stage 2C §9.3; end of document | A recorded discrepancy finding carries both figures and is never auto-corrected; "The AI architecture stage has not been started." |

So "AI detection" is **not** a fraud classifier and not an autonomous agent: it is an evidence-backed investigator for concrete
statement/ledger discrepancies, measured against a rules-only baseline. The rules-only baseline is required in any case, and the
current data supports nothing more (§4), so this slice implements that baseline end to end, with the interfaces a later model
investigator needs, and stops there.

## 2. The workflow (PROPOSED DET-1)

**Detects, for whom.** For an analyst (investigator, `propose`) and a controller (reviewer, `review`): every staged synthetic
processor settlement statement of a native SANDBOX ledger is compared with authoritative ledger state, and exact differences
become persisted findings that a human confirms or dismisses.

**Inputs and evidence.** `lv.detection_inputs` builds one frozen evidence set per statement from committed rows: the staged
statement (ids, financial fingerprint, source record hash, source file SHA-256), conflicting import attempts, each statement item
with its identity links (`processor_ref` + `processor_item_id`, statement namespace only) and the linked payment's current
version, amount, status and settlement link, and the settlement recorded for the payout identity with its active inclusions.
Its SHA-256 is the finding's input fingerprint.

**Deterministic baseline** (`ledgervigil.detection.detector`, exact kernel `Money` arithmetic):

| Kind | Class | Condition |
|---|---|---|
| SETTLEMENT_NOT_RECORDED | DISCREPANCY | No settlement for the statement's payout identity. Reports statement gross/fee/net and the RECEIVED linked payments still in clearing with their exact total. |
| SETTLEMENT_AMOUNTS_DIFFER | DISCREPANCY | Settlement recorded; gross, fee or net differ (each field: statement, ledger, statement − ledger). |
| SETTLEMENT_ITEMS_DIFFER | DISCREPANCY | Settlement recorded; resolved statement payments ≠ actively included payments (both directions listed). |
| STATEMENT_ITEM_SETTLED_ELSEWHERE | DISCREPANCY | A linked payment is in another payout's settlement. |
| STATEMENT_ITEM_AMOUNT_DIFFERS | DISCREPANCY | A linked payment's amount differs from the statement line (exact decimal, not text). |
| STATEMENT_ITEM_UNRESOLVED | EVIDENCE_GAP | No identity link (NO_IDENTITY_LINK), the id exists only under another processor (OTHER_NAMESPACE_ONLY), or several links. Equal amounts never link. |

**Abstention** (no findings for that statement, recorded on the run): `AMBIGUOUS_STATEMENT` (another file claimed the payout with
different content), `REFUND_ITEMS_NOT_SUPPORTED` (no native refund model), `SOURCE_INTEGRITY_FAILED` (stored bytes no longer hash
or re-parse to the staged statement; same rule as the adapter's E-SRC-01).

**What a model would contribute — nothing yet.** `interpretation` is always `NOT_AVAILABLE`; the column is constrained NULL.
A future investigator (ADR-017 gateway: local Ollama, optional hosted, recorded replay) could add a *hypothesis* about cause
(timing, mis-keyed fee, wrong payout) and draft a `RecordSettlement` for a human to promote. It must first beat this baseline on
labelled data that does not exist yet (§5).

**How a human verifies or dismisses.** `GET /detection-findings/{id}` shows observed facts (statement OBSERVATION), recorded facts
(AUTHORITATIVE at the run's snapshot seq), the computed difference, every cited id/version/hash, and live freshness. The reviewer
checks the cited statement and ledger records, then posts a disposition with `If-Match`:

| Action | Reason codes | Rule |
|---|---|---|
| CONFIRMED | VERIFIED_AGAINST_EVIDENCE | Only from OPEN and only while CURRENT (stale → 409 FINDING_STALE) |
| DISMISSED | EXPECTED_TIMING, STATEMENT_ERROR, KNOWN_EXCEPTION, NOT_A_DISCREPANCY, INPUTS_CHANGED | Only from OPEN; allowed when STALE |
| REOPENED | REVIEW_CORRECTION | From CONFIRMED or DISMISSED back to OPEN |

A disposition records the decision only. It never changes a balance, proposal, approval or execution; a correction still goes
through the normal proposal lifecycle.

## 3. PROPOSED decisions (not approved)

| ID | Proposal | Why it is needed |
|---|---|---|
| DET-1 | First slice = deterministic statement/settlement reconciliation; no model | §1, §4 |
| DET-2 | Run needs `propose` (Family 7 precedent); read needs `read`; disposition needs `propose` or `review` | Family 7 defines investigations, not reviewer dispositions |
| DET-3 | Disposition vocabulary and transitions in §2 | Not defined anywhere |
| DET-4 | Finding identity = (ledger, detector, kind, staged statement, input fingerprint); reruns on identical inputs reuse it | Duplicate prevention |
| DET-5 | Freshness recomputed on read; CONFIRMED only while CURRENT; prior findings kept | Staleness semantics |
| DET-6 | Feature gate `LV_DETECTION_ENABLED` (default off); routes absent from the default contract (`api/openapi.json`); enabled contract in `api/openapi.detection.json` | Keeps the shared contract unchanged until a decision |
| DET-7 | Synchronous run, ≤ 200 statements, ledger counter `FOR SHARE` for the run | Family 7 uses jobs for model latency; this baseline needs none |
| DET-9 | Detector 1.1.0 rule PAYMENT_CLAIMED_BY_OTHER_PAYOUT (class POTENTIAL_CONFLICT), §8 | Overlapping claims were a documented gap |
| DET-10 | Detector 1.1.0 rule PAYOUT_DATE_DIFFERS (class DISCREPANCY), calendar dates, no tolerance, §8 | Payout dates were a documented gap |
| DET-8 | Routes `/ledgers/{id}/detection-runs`, `/detection-runs/{id}`, `/ledgers/{id}/detection-findings`, `/detection-findings/{id}`, `/detection-findings/{id}/dispositions` | Family 7 `/investigations` is reserved for the model investigator |

## 4. Method choice (primary sources)

| Method | Fit for this data | Decision |
|---|---|---|
| Exact identity reconciliation (this slice) | Statements and payments share a persisted key `(processor_ref, processor_item_id)`; Stage 2A §19.3 forbids inferring identity from amounts, dates or text | **Chosen.** Explainable, exact, reproducible |
| Probabilistic record linkage — Fellegi & Sunter, "A Theory for Record Linkage", *JASA* 64(328):1183–1210, 1969 | Designed for files *without* a reliable common key: weighs field agreement patterns (m/u probabilities) into link / possible link / non-link | Not needed while the key exists; would reintroduce amount/date similarity matching the rules forbid. Candidate only for future real files with missing ids |
| Unsupervised anomaly scoring — Liu, Ting & Zhou, "Isolation Forest", *Proc. IEEE ICDM 2008*, 413–422 | Ranks unusual points; produces a score, not a cited discrepancy; no labels exist to calibrate a threshold | Rejected: a score would read as a probability we cannot justify (Stage 1 H-6, ADR-017) |
| LLM investigator via structured output — Ollama `format` with a JSON schema ([docs](https://docs.ollama.com/capabilities/structured-outputs)); the docs do not guarantee conformance and validate after generation | ADR-017's intended role; O-1 (hardware) is open, no labelled real data, no authorised paid calls | Deferred. The slot (`interpretation`), the untrusted-output validator (`lv.detection_record` vs `lv.detection_expected`) and the evaluation harness are ready for it |

## 5. Evaluation (tools/evaluate_detection.py)

Frozen case file `evaluation/detection/cases-1.0.0.json` (91 cases, 13 classes, SHA-256 in `evaluation/detection/MANIFEST.json`),
labels from the deterministic generator only (injected perturbations), dev split (26) used only to build the harness, held-out
split (65) scored once; leakage check over payout ids, item identities and scenario hashes. Includes difficult cases: amounts
written with fewer decimals, an equal-amount id under another processor, swapped item amounts, refunds, conflicting files, and
a payout-date difference v1 does not compare (a known blind spot, labelled as a real discrepancy so it counts as a miss).
A naive equal-amount matcher is scored alongside as a contrast. Cost is 0 (no model call); latency is measured per statement.

**Effectiveness on real processor data: NOT YET ESTABLISHED.** Synthetic results show the specification is implemented; they
were designed by the same author as the detector and cannot show usefulness. To validate a model (or this baseline) the project
needs: a set of real or realistically anonymised processor statements with ledger exports, labelled by an accountant (not by
either system) for discrepancy type and cause, split by processor and period, with the rules-only baseline scored first.

## 6. Security and integrity properties

Verified sessions and permissions (identity-first lock order of 0027/0028, revalidated after locks); server-derived tenant and
actor; FORCE RLS on all five tables, lv_app SELECT only, writes only through `lv_owner` SECURITY DEFINER functions with a fixed
search path; 404 across tenants; idempotency keys and replay; `If-Match` on dispositions; append-only runs, findings and
dispositions; review pointer advances only by CAS; deferred constraint triggers refuse a run, finding or disposition without its
audit, idempotency record or database-verified computation; populated downgrade refused for any tenant. Detector output is
untrusted: the database recomputes the evidence fingerprint and the expected findings and stores a REJECTED run (502) on any
difference. Imported memo text and reviewer notes are never interpreted. Detection and disposition change no financial table or
ledger counter (verified by fingerprints in tests).

## 7. Limitations

- Since 1.1.0 (§8) payout dates are compared and overlapping claims are reported. Posting periods, ingestion times,
  party/customer and refunds are still not compared (refund statements are abstained on).
- `SOURCE_INTEGRITY_FAILED` is decided in Python (re-parsing bytes); the database cannot re-parse, so a faulty detector could
  abstain on a statement it should report. The abstention is visible on the run and in evaluation.
- A ledger with no commit yet has no counter row to lock; a first commit racing a run is caught by the fingerprint re-check and
  rejects the run rather than storing mixed state.
- No reviewer UI in this slice (contract and frontend handoff only); no scheduled detection; no notification.
- Not a fraud detector; no probability or confidence is produced; findings are not accounting conclusions.

## 8. Detector 1.1.0 (migration 0033; PROPOSED DET-9, DET-10)

Still deterministic rules over a database-built evidence set: **no statistical model and no LLM**, no score, probability or
confidence. The database oracle (`lv.detection_expected`) recomputes both new rules independently of the Python detector,
and a run is rejected (502, nothing recorded) unless they agree exactly.

**Evidence set v2** (`lv.detection_inputs_v2`, schema `lv-detection-inputs/2`): v1 unchanged, plus `other_claims`. These are
every other staged statement in the same tenant, ledger and processor, for a different payout id, that lists one of this
statement's PAYMENT item identities. Each claim carries its staged id, payout id, line, amount and source-record hash, and
whether that statement is ambiguous (a conflicting second file), unsupported (refund lines) or already recorded as a
settlement. Any change to those facts changes this statement's fingerprint, so the finding reads STALE (DET-5).

**PAYMENT_CLAIMED_BY_OTHER_PAYOUT** (class `POTENTIAL_CONFLICT`). Raised when all of these hold:

- this payout has no recorded settlement;
- a statement line resolves by exact identity to one RECEIVED, unsettled payment (one link: `processor_ref` +
  `processor_item_id` in the statement's namespace);
- another staged statement for a different, also unrecorded payout lists that same identity.

The finding cites both claims: this line, and the other statement's id, payout, line and source-record hash (also as
`OTHER_PAYOUT_CLAIM` evidence). It says "not a conclusion about cause"; it is not fraud and nothing is corrected.

| Situation | Result |
|---|---|
| Same payout imported again (same content) | Ingestion DUPLICATE: one statement, no overlap |
| Second file for the same payout, different content | AMBIGUOUS_STATEMENT abstention (1.0.0 rule) |
| Two distinct unrecorded payouts list one payment | One overlap finding on each, each citing the other |
| Other payout recorded and includes the payment | Payment SETTLED: STATEMENT_ITEM_SETTLED_ELSEWHERE (1.0.0), no overlap |
| Other payout recorded without the payment; other statement ambiguous or with refund lines | Listed under `excluded_claims` of an item that also has a valid claim; never raises the finding alone |
| Same item id under another processor, ledger or tenant; equal amounts with other ids; unidentified item | Not the same payment: no overlap (unidentified items stay STATEMENT_ITEM_UNRESOLVED) |

**PAYOUT_DATE_DIFFERS** (class `DISCREPANCY`). Compares the statement's payout date with the recorded settlement's payout date.
They have the same documented meaning: SOURCE_READS.md maps `StagedDocument.payout_date` to `RecordSettlement.payout_date`.
Both are calendar dates, stored NOT NULL. The finding reports both dates and the signed calendar-day difference (statement
minus ledger). There is no timezone conversion, business-day tolerance or cutoff rule, because the accepted documents define
none. A difference can be legitimate (processor value date versus booking), so it is surfaced, not judged. Ingestion
timestamps and journal posting dates have different meanings and are never compared. A statement without a date is
rejected at ingestion, so there is never a missing date to compare.

**Versions and history.** 1.0.0 findings keep their v1 fingerprint, so the upgrade alone makes nothing stale. A 1.1.0 run
creates new findings (version and fingerprint differ) with a new OPEN review; no confirmation is carried forward, and old
findings keep their dispositions. Downgrading 0033 is refused while any 1.1.0 run exists in any tenant.

**Concurrency.** The lock order is unchanged: ledger row FOR KEY SHARE, then counter FOR SHARE. A settlement posting waits
for an in-progress run, and the run waits for an uncommitted posting. Statement ingestion takes neither lock. If another
statement is ingested during a run, the database's fingerprint re-check rejects that run (502, nothing recorded), and a
rerun sees the new claim.

**Limits.** Items whose only other claims are excluded are not reported. A payment claimed by a payout whose statement has
not been ingested is invisible. Business effectiveness is NOT ESTABLISHED (synthetic, author-labelled evaluation only).
