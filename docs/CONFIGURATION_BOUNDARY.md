# Runtime configuration authority (RG-05 candidate)

This bounded migration follows the accepted Stage 2B Family 4 chart-of-accounts PATCH contract and Stage 2A administrator/audit requirements. It does not amend an ADR or approve a proposed decision. Native SANDBOX only; accounting remains UNREVIEWED.

## Authority and atomicity

Migration `0023_configuration_boundary` removes direct INSERT, UPDATE, DELETE and TRUNCATE privileges from `lv_app` on `ledger`, `account`, `account_role_binding`, `party` and `fiscal_period`. Existing privileged writers retain only their existing grants and function authority. Period close still uses the authenticated atomic writer from 0009. A runtime connection with a tenant GUC cannot rename an account or add a period directly.

`PATCH /api/v1/accounts/{account_id}` requires the verified session, CSRF/Origin boundary, `administer`, and the current `If-Match: "vN"`. The body is exactly `{name}` or `{active: false}`. A read of the account list now includes its integer `version`; the list's representation ETag is not an account version token. Account names must contain a non-whitespace character, have at most 120 Unicode code points, and contain no NUL or unpaired surrogate. Other properties, duplicate JSON keys and unknown query parameters are refused. The administrator still has no propose/review/execute capability.

The SQL writer independently verifies permission, session, tenant, version, body shape and native SANDBOX eligibility. Identity rows are locked and checked again. It locks ledger then account, matching the financial ledger fence. Posting wins first: a stale account token receives 412. Configuration wins first: dependent approved settlement execution detects the changed dependency and becomes STALE, without posting. Existing invalidation semantics are preserved; RG-03 is not changed.

Nonzero account balance or any role binding prevents deactivation (409 ACCOUNT_IN_USE). Reactivation is not supported by this contract. A successful edit increments the existing database-owned account version, writes an immutable `configuration_change` with exact before/after configuration, and appends the chained identity audit event in the same transaction. Deferred checks require the corresponding account and audit artifacts. Runtime name/active changes require their history artifact; removing history or audit from the writer causes the whole transaction to roll back. No financial commit, ledger sequence, journal line, document or balance changes during an edit.

PATCH has no new idempotency-key semantics: the accepted contract uses version guarding. On an uncertain response, read the current account version/name before deciding what to do; blind replay with the old version is refused. Same-value edits still increment the account version, consistent with the existing dependency-version contract.

## Scope and remaining limitations

This closes the reported direct runtime configuration DML paths, and provides the accepted audited account-edit route. It does not implement all configuration onboarding: public ledger/account creation, party creation and role-binding replacement routes remain unavailable. Existing private operator provisioning is retained for synthetic local setup; it already used `lv_migrator`/`lv_owner` and is not a runtime endpoint. Tests now provision configuration explicitly with that operator instead of granting runtime authority. Runtime permission tests continue using the actual `lv_app` login.

The private owner/migrator is a trusted maintenance principal. The account history trigger enforces the runtime login boundary; it does not claim to stop a privileged operator, superuser, disabled triggers or tampering with function definitions. Administrative access and those credentials must remain separate from runtime. Q-16 (database session issuance trusts the API password check) is unchanged; this milestone is not proof against complete compromise of the runtime database credential. MFA/email/reset/production gates remain unchanged. Configuration history has no public history API in this slice. RG-02/RG-04/RG-06/RG-07/RG-08 remain open.

## Migration and rollback

Upgrade adds one empty table, its FORCE RLS policy, private helpers, one runtime-callable authenticated writer and deferred guards, and revokes five tables' direct DML. It does not rewrite any earlier migration or financial SQL. A populated upgrade preserves every preexisting row and ledger counter.

A disposable empty-history downgrade restores predecessor grants. Any configuration history, from any tenant, blocks downgrade using a validating CHECK constraint; the migration tool requires explicit disposable-database authorization. Do not downgrade a development or production database to undo an edit. A correction is a new audited version-guarded edit.

## Verification

`tests/test_configuration_boundary.py` covers five direct privilege denials, roles, cross-tenant masking, schema/encoding, missing/stale versions, revocation after the HTTP boundary, immutable private history, zero/nonzero/bound deactivation, SQL literal names, database-level validation, six competing editors, both edit/execution lock orderings, missing atomic artifacts, upgrade preservation and downgrade refusal. Existing foundation/read/period/proposal/execution tests retain their actual runtime roles while setup uses `tests/configuration_support.py`.

The review package records native Mac PostgreSQL results and a separate real-loopback HTTP rehearsal. These are operational correctness checks on labelled synthetic data, not real-world accounting accuracy or external-provider certification.

## Account edit concurrency correction (0027)

Account edits now lock user, membership and ordered roles, then the session, before revalidation and idle-session touch. Revocation-first edits are refused; an edit that locks first commits its configuration history/audit before revocation proceeds. Ledger/account lock order, authorization and CAS rules are preserved. Migration0027 replaces only edit_account and restores its prior definition on downgrade; previous migrations remain unchanged. Other inherited workflow writers are outside this bounded correction.

## Six workflow writers (0028)

The follow-up correction applies the same identity -> ordered role -> session locking, revalidation and touch order to comments, approval, rejection, native execution, evidence overrides and source-plan registration. Their existing idempotency-key locks remain nonblocking. Migration0028 replaces only those six functions with exact predecessor restoration on downgrade. Business logic after the identity gate, grants, ownership, financial rules and HTTP contracts are unchanged. The previous0027 paragraph describes its original account-only milestone; these six followups are now included in0028.

## Validation and unchanged saves (0029)

SCHEMA_INVALID 422 responses include errors[] with RFC6901 field pointers. Strict parser errors retain their root pointer; malformed surrogate field names use a safe root pointer. Unknown fields are rejected; version/permission/tenant guard precedence remains unchanged. OpenAPI409 now names INVALID_STATE_TRANSITION with reason ACCOUNT_IN_USE.

An exact unchanged name or repeated inactive request returns the current Account and ETag after all existing guards succeed. It writes CONFIGURATION_ACCOUNT_UNCHANGED with a unique no-op id; omitted audit rolls the request back. It changes no account version, configuration history, financial record or proposal dependency. Unicode/whitespace are preserved without normalization. Concurrent real edits retain the original CAS rule; a rename that wins makes an old token stale.

This resolves the contract-silent unchanged-save behavior as an implementation choice under the user's request to address F3. It does not approve an ADR or any other proposed contract decision. Errors and no-op behavior are documented here; accounting remains UNREVIEWED.
