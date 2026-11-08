# Authenticated public source-plan registry (SR-1 PROPOSED)

POST /api/v1/source-plan-registrations accepts only {ledger_id,manifest_sha256}
with a verified session, source.import permission, CSRF/Origin and Idempotency-Key.
The current catalog contains one pinned, previously verified UCI admission-plan
manifest. Client-supplied counts, provenance, creator and other fields are refused.
The target must be a visible native GBP SANDBOX ledger. Missing/foreign ledger ids
are indistinguishable. Administrators/controllers do not inherit source.import.

GET /api/v1/source-plan-registrations/{id} requires read permission and a verified
tenant context. It returns immutable historical metadata: creator, UTC timestamp,
manifest hash, public-observation classification, source/invoice/hold counts and
posting UNAVAILABLE. It runs read-only and supports the established representation
ETag. It does not imply that raw files are online or currently executable evidence.

This is an additive API proposal SR-1. It is not the contracted POST /imports
multipart upload/IMPORT_PARSE job, a generic file importer, raw-row browser, native
staged_document, provenance approval or posting capability. Those require further
work and must not be inferred from successful registration. Accepted ADRs, DATA-1,
DATA-2, RC-1 and accounting UNREVIEWED statuses retain their status.

Migration 0016 adds one immutable FORCE-RLS tenant table and a narrow registration
SECURITY DEFINER wrapper. Runtime receives SELECT and wrapper EXECUTE only. Session,
user, membership and source.import are checked in the wrapper, with identity locks
held through commit. A deferred validator requires the matching audit/cache record
before a new registration can commit. Existing cache infrastructure scopes keys to
tenant/principal/method/route for 24 hours. Same-key changed payload is refused;
new-key requests for the same ledger/manifest reuse the existing immutable row.
Successful requests are cached. Refusal caching and generic upload lifecycle are
not implemented in this bounded proposal. All financial tables/counters stay out
of its write privileges.

Public catalog metadata is pinned in code/migration to manifest
3c7dbcb169405d9d0f8abcff095c9f30df46ca6b1b38fb31f894ed1657ea0523:
1,067,371 rows; 53,628 invoice groups; 12,696 CANDIDATE_REVIEW; 40,932 HOLD.
The HTTP demonstration compares these values with the actual sealed plan manifest.
Registration is deliberately restricted to that catalog entry; future catalog
publication needs independent artifact verification and deliberate versioning.

The separate SR-2 PROPOSED API now provides tenant-authorized paged group and raw-row
inspection linked to this registration. See [PUBLIC_PLAN_INSPECTION.md](PUBLIC_PLAN_INSPECTION.md)
for its derived public-only index, publication checks and trust limits. The frontend
selection and hold-inspection screen remains the next slice. Neither API authorizes
financial source admission.

Concurrent requests using the same session/key try the idempotency lock before
serializing session activity. They receive REQUEST_IN_PROGRESS while the first
request is blocked. The wrapper then touches/revalidates the session and locks
identity/permission rows in the established workflow order. A disabled session
between HTTP boundary and SQL is refused as 401.
