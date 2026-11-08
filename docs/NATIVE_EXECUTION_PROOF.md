# Native execution integrity bundle, version 1

## Scope

GET /api/v1/executions/{execution_id}/proof exports an already committed native SANDBOX RecordSettlement. It is a read-only authenticated route with the existing read permission, tenant RLS and ordinary absent/inaccessible 404 masking. It does not dispatch the completion outbox, create a financial effect, alter decisions, re-run a simulation or approve accounting. Migration head stays 0021_native_execution; financial writers, grants and all migrations remain byte-identical.

The receipt UI offers Download integrity bundle. The client requires exact byte SHA-256, canonical format, the current verified tenant and receipt identity, every artifact's hash/length and the manifest hash before creating its JSON file. Busy/disabled controls and request abortion on unmount preserve existing session/tenant boundaries. Failed integrity or service checks leave the receipt intact and do not create an accepted export. The response is no-store and nosniff, with an attachment filename constructed only from the parsed execution UUID.

## Format and deterministic boundaries

Format: ledgervigil.native-execution-proof/1. The UTF-8 file is canonical sorted-key compact JSON with non-ASCII escaped, followed by one newline. Money and ledger/entry sequences remain exact strings. JSON floats, duplicate keys, noncanonical encodings, oversized/deep inputs, unsupported fields and malformed versions are refused by the offline verifier. It uses only Python's standard library.

The envelope has format, scope, claims, manifest, manifest_sha256 and artifacts. Scope binds tenant, ledger, proposal, exact revision, execution, simulation, commit sequence, currency and exponent. Each fixed-name artifact has the byte length and SHA-256 of its canonical JSON. The manifest hash covers format, scope, claims and the ordered manifest. X-Proof-SHA256 and the strong proof ETag describe the complete file bytes including the newline. Conditional validation occurs after authentication and read checks.

The eight artifacts are:

| Name | Immutable facts included |
| --- | --- |
| revision.json | Exact approved revision, operations, evidence references, rationale, author and content hash |
| approval.json | Approval, recorded freshness check, reviewed grant timestamp and any matching evidence exception; original verdict remains separate |
| simulation.json | Exact stored canonical input/outcome and hashes, scope and pinned simulator version |
| execution.json | Immutable receipt, approval link, execution check, transition, terminal proposal identity and editor ids through this revision |
| documents.json | New settlement/payment versions and the preceding payment versions, financial fingerprints and original requests |
| journal.json | The committed journal entry and exact ordered lines |
| inclusions.json | Immutable settlement/payment inclusion rows |
| history.json | Both commit and event chains from sequence 1 through this execution's commit sequence |

The export intentionally includes earlier activity in this same ledger to validate the prefix. It does not export other tenants or other ledgers. Raw source files, the identity/session/password tables, session tokens, CSRF tokens, idempotency cache and global identity audit chain are not queried or copied. Canonical historical events can contain internal user/session/request identifiers; these are historical correlation ids, not authentication credentials. User-authored financial descriptions/reasons/rationale remain part of their original records and may be sensitive organization data. Exported files are not revocable when an account later loses access; revocation controls subsequent downloads.

There is no export-time timestamp or live balance/projection in the file. All included facts are immutable or terminal proposal identity; later commits after the recorded sequence do not alter the bundle. Existing session bookkeeping can occur before the transaction switches to READ ONLY, as in the owning read boundary. Every subsequent export query executes read-only. No new database privilege or SECURITY DEFINER function is introduced.

Version 1 refuses histories beyond 2,000 commits and complete files beyond 16 MiB. Source materialization has conservative component budgets (4 MiB each for chain text, simulation text and document JSON) and bounded row limits; a smaller output can therefore also be refused. The response is 422 CAPABILITY_UNAVAILABLE / PROOF_EXPORT_LIMIT, never a partial bundle. It is not a general archival/large-ledger export.

## Independent checks

The verifier first checks exact file bytes (when a separate expected digest is supplied), canonical envelope, manifest and strict scope. It then independently checks revision and simulation hashes, captured revision/head, matching approval/exception, reviewer/maker/editor links and the recorded review waiting interval. It validates the execution/check/transition/version relationship; both chain prefixes for gaps, scope, hashes and balanced entries; and the captured heads immediately before execution.

For the target settlement it independently derives gross/fee/net and journal account roles using integer minor units. It compares the approved operation, actual original request, new financial fields, payment before/after versions, immutable inclusions, original payment postings, actual journal lines, predicted postings and the E-7 comparison hash. A balanced entry on the wrong account is not sufficient. Zero fee or zero net legitimately produces two lines; zero included payments are refused.

The result is INTERNAL_CONSISTENCY_VERIFIED, with accounting UNREVIEWED, authenticity NOT_ESTABLISHED and external_anchor NONE. It is not an independent accounting oracle or a replay/certification of every historical business policy. It does not fully reconstruct historical role grants, current authorization, every earlier revision's content, raw source authenticity, identity audit history or production durability. Historical simulator capability metadata and original policy verdicts stay unchanged.

## Exact trust boundary

Hashes detect accidental alteration and the independent checks reject inconsistent or incomplete records even after a manifest is rebuilt. An attacker able to rewrite an entire unsigned package into a different internally consistent package can recompute all embedded hashes. The verifier cannot establish origin/authenticity from such self-contained hashes. It does not establish that an unanchored database chain was never replaced.

An expected SHA-256 supplied through a separately trusted record can pin exact bytes. A digest shown by the same download response is useful for transfer comparison but is not an independent signature or external trust anchor. The UI, output claims and CLI preserve this distinction. No signing key or trust service is invented.

## Offline use

From the owning application source:

```sh
python3 -S tools/verify_execution_proof.py /path/to/download.json
python3 -S tools/verify_execution_proof.py /path/to/download.json --expected-sha256 EXPECTED_64_HEX_DIGEST
```

No database, network, application credentials or third-party Python packages are needed. Exit 0 means the bounded checks passed; exit 1 gives a short refusal reason without echoing record contents. Preserve the downloaded bytes; reformatting JSON changes the file and is refused. The actual browser download was checked with macOS system Python 3.9.6 as well as the native 3.14 application environment.

## Evidence and preserved decisions

The dated review includes real native PostgreSQL tests, a real browser-saved file, its independent offline verification, fault-response tests, exact source hashes and read-only fingerprints. Synthetic data and explicitly aged test grants remain labeled. No settlement/fraud accuracy, customer outcome or independent accounting review is inferred from software checks.

The tampering tests cover manifest changes, missing artifacts, tenant/revision/sequence substitution, approvals/exceptions, simulation bytes, document fingerprints, wrong or over-precise money, balanced wrong-account postings, original requests, inclusions, freshness, transitions/actors, bool versions, original payment sequence, false signature claims, duplicate JSON keys and unknown fields. Other tests cover later ledger activity, live permission/revocation, source limits, an intentionally broken reader result, READ ONLY refusal of a permitted definer write, zero-fee/net and an edited revision with the accepted independent editor-executor rule.

PX-1 PROPOSED: versioned bounded unsigned integrity envelope and whitelist. PX-2 PROPOSED: exact-byte digest/manifest conventions and standard-library verifier. PX-3 PROPOSED: explicit trust/omission claims separate from completion publication and independent accounting review. These implementation records do not approve prior ADR amendments or change NE/EU/D/PA/data decisions.

## Remaining work

Complete the application recovery story for database plus external public-data assets, maintenance state/configuration and execution evidence, extending the existing same-host database drill rather than rebuilding it. Preserve private backups and guarded disposable restores. Offsite/separate-host recovery, production identity/MFA controls, independent accounting review and independently labeled business evaluation remain distinct release gates. The execution completion-event consumer remains unimplemented.
