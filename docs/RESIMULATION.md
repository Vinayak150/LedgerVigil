# Native dependency freshness and re-simulation

This SANDBOX milestone adds maker-only recovery of unchanged RecordSettlement
content from STALE. It does not implement editing STALE content, human review,
approval, overrides, execution, providers, AI or source-provenance admission.
Accounting remains UNREVIEWED; synthetic evidence remains insufficient.

## Routes and authorization

- POST `/api/v1/proposals/{id}/resimulate`: accepted Stage 2B Family 8 route;
  requires verified `propose`, maker ownership, If-Match and Idempotency-Key.
  STALE only. Copies the exact previous content, citations and content hash into
  a new immutable revision, pins a new run and reserves its cited statements.
- POST `/api/v1/proposals/{id}/revalidate`: **RC-1 PROPOSED additive contract
  extension**, not named by the accepted base contract. Explicit maker action,
  same authorization/headers, empty body. Checks BLOCKED/READY_FOR_REVIEW heads.
  FRESH records a receipt without advancing head/version. STALE atomically
  advances resource version, records a transition and releases every claim.
- GET `/api/v1/proposals/{id}/simulations`: Family 9 read permission, immutable
  history ordered by revision descending then UUID descending. Signed keyset
  cursor is bound to tenant/proposal/order; default 50, maximum 200.

The maker-only, unchanged-content re-simulation restriction is a conservative
implementation boundary. It does not resolve broader editor/reviewer semantics.
Accepted Stage/ADR files and existing proposed decisions have not been modified.
Do not treat this local implementation as approval of RC-1 or accounting rules.

## Freshness scope

Requires native simulator 0.3.0 with complete account dependencies v1. Compares
captured account/document/period versions, closed period, dependency completeness,
role bindings, ledger profile/currency, rejected statement-conflict ranges,
statement payment-identity ranges, newly used payout/payment inclusion, and
DOCUMENT_VERSION evidence current versions. Immutable statement/source facts are
not rewritten. Identical statement reimports do not invalidate a proposal.

Unrelated sequence advances alone do not invalidate (ADR-009). This is a
point-in-time comparison, not an execution eligibility oracle. Same-key retries
return the original receipt even if facts subsequently change. A new explicit
check requires a new key. Background maintenance still only consumes period-close
jobs; broad automatic invalidation coverage is not claimed.

## Atomicity and persistence

Migration 0015_resimulation adds one immutable FORCE-RLS table,
`proposal_freshness_check`, and replaces bounded workflow/commit validators. The
runtime login receives SELECT on receipts and EXECUTE on two workflow wrappers;
no direct mutation grants. Deferred validators require matching cache, audit,
head, report and complete reservation release before success can commit.

Re-simulation creates revision+1/resource version+1; it retains original revisions,
snapshot, read set, verdicts and replay hashes. Rejected capture, permission loss,
closed periods, conflicting claims or injected failure roll back new artifacts.
There is no automatic retry with a new key. Existing in-flight/success/refusal
idempotency behavior is reused, including original If-Match and empty body.

Financial facts, balances, financial events and sequence are unchanged by these
workflows. A developer configuration change in the demonstration is explicitly
outside the HTTP workflow and is not advertised as a public editing feature.

## Frontend

Server-provided actions expose Check dependencies / Re-simulate saved content.
Confirmation explains effect and unchanged-content boundary; fresh receipts can
legitimately retain the same version. Receipt and latest head are displayed
separately; cached responses never replace current state. Browser retry metadata
remains scoped by user/tenant/proposal, with the original key/version preserved.

Recorded simulation history is paginated and each selected run is independently
verified against proposal/ledger/revision. Historical runs are labeled separately
from current content; replay still reads only frozen inputs. No current freshness
is inferred from a historical verdict. Browser verification used disposable data,
including desktop, 390px mobile and forward/reverse dialog focus wrapping.

## Verification and limits

See the dated review package for raw logs. One full run reported 783 passed and
one obsolete route-inventory expectation failed. After updating that expectation,
26 targeted checks passed: all 24 new cases plus route inventory and committed
OpenAPI parity. These are separate runs, not a claimed single 786-test full run.
Three deliberate safeguard removals each caused an assertion failure. Native
restored-backup upgrade preserved all 51 existing tables/columns/rows. A public
HTTP demo preserved financial fingerprints and replayed old/new runs identically.

No empirical model accuracy or production readiness is established by these
synthetic tests. Source admission, accounting oracle review and accepted review/
approval semantics remain prerequisites for later financial execution work.
