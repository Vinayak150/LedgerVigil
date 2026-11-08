# Account dependencies for native settlement simulation

Owning contracts: Stage 2A §9.1 server-computed read sets, §10.2 deterministic simulation and §10.3 action-time revalidation; Stage 2B ReadSetEntry ACCOUNT. This is a prerequisite, not submission or an approval mechanism.

Migration 0012_account_versions adds a positive integer version to lv.account. Existing accounts start at version 1 at this cutover; no historical counter is invented. The migration changes no pre-existing field or financial posting, balance, ledger counter, event or stored simulation.

The database advances the counter on every accepted account UPDATE, including same-value updates, and refuses a supplied version change or an initial version other than 1. Increment overflow aborts the transaction. Role-binding inserts/updates/deletes touch the old and new accounts once each in sorted identifier order. Account-balance projection inserts/updates/deletes touch their account. These are invoker-rights triggers under the existing FORCE RLS and privileges; they grant no access or new write path. Metadata increments roll back with their configuration or financial transaction. Idempotent financial replays write no projection and advance no version.

Balance invalidation matters: a settlement policy depends on the resulting clearing balance, so other legitimate postings can invalidate the inputs even when the settlement's payment documents have not changed. A configuration-only counter would miss that change.

Simulator lv-native-recordsettlement/0.3.0 pins account_read_set=lv-account-dependencies/1. Its READ ONLY, REPEATABLE READ capture includes every bound account's version alongside the payment DOCUMENT versions and payout PERIOD version. It uses the same transaction and snapshot as configuration, bindings and balances. The read set is sorted and computed by the server; it is not accepted from a request. All seven SP-1 bound accounts enter the adapter snapshot, including accounts whose balance delta may be zero.

Original 0.1.0, 0.2.0 and 0.2.1 profiles remain installed and preserve their original capture/read-set shape and pure replay. Frozen bundles remain unchanged. New captures use 0.3.0. Old runs lack the account dependency marker and **must not authorize a future submission, approval or execution**. All current runs still say eligible_for_execution=false. No action-time stale transition or consumer is introduced here.

Implementation choices (engineering defaults; not acceptance of prior proposed ADR/policy choices):

- AV-C1: conservatively invalidate on every account UPDATE, even a same-value write. An unnecessary revalidation is preferred to a missed dependency.
- AV-C2: configuration, bindings and balance projections share the account's dependency counter; it is not a per-commit number. Several projection rows can advance it several times in one commit.
- AV-C3: capture all bound accounts used by the current adapter, rather than only nonzero predicted posting lines.
- AV-C4: backfill one marks the new dependency-version epoch, not reconstruction of historical account edits.

Remaining gates: statement reservations occur only with submission under Stage 2B §7.1. They belong in the atomic submission milestone, together with lifecycle CAS, persisted read sets/snapshot/verdicts, idempotency, audited transitions and rollback. The current DRAFT-only database contract has not been loosened. Raw database configuration rights and their future authenticated/audited administration routes still need the existing production hardening work. SM/PV and all earlier proposed decisions retain their status; accounting remains UNREVIEWED.

The current adapter also scans the full ledger posting/projection history. Account counters are a necessary dependency check, not a complete action-time authorization check: a future action must also lock and compare the captured ledger sequence (or narrow the adapter's query dependencies with a separately reviewed range-invalidation design). For example, a posting involving newly added, unbound accounts can move that global snapshot without changing the previously bound accounts. No approval/execution path exists here, so no current action relies on version checks alone. Carry this requirement into atomic submission and later revalidation tests.
