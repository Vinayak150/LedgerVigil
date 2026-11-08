# Identity issuer candidate

Migration 0038 separates identity credential handling from the financial API.
The following list records effective privileges intended after the migration.
The PostgreSQL role tests must validate these on the exact release checkout.

| Entry point | After 0038 |
| --- | --- |
| login_prepare and login_finish | lv_authn. Password preparation and verification are one transaction; the locked user row prevents password changes between the two. |
| switch_tenant, logout, revoke_my_sessions | lv_authn. The existing authenticated token is required. Tenant changes rotate the same user's session. |
| reauth_prepare and reauth_finish | lv_authn. Preparation, Argon2id and verdict share one transaction and throttle lock. |
| token_issue and token_consume | lv_authn. Covers email verification, resets and invitation acceptance. The issuer produces high entropy bearer values and performs Argon2id hashing. |
| member_api and member_list | lv_authn. Includes invitation creation, which supplies a token digest and provisional password hash, plus role changes and session revocation. The function still authorizes the live session, membership and recent authentication. |
| session_context | lv_authn and lv_app. Requires an existing token digest and does not issue a session. |
| audit_page | lv_app, with the audit.read permission. |
| admin_create_user, admin_grant_membership, admin_set_roles, admin_revoke_membership, admin_disable_user, admin_revoke_sessions | lv_owner only; operator path, not runtime. |
| session_authenticated_at, member_session, session_problem, session_info, audit_append, token_throttle | Internal owner routines. No runtime caller grant. |

PUBLIC, lv_worker, lv_ingest and lv_seed have no execute grant on the identity
entry points. lv_authn does not have a table grant on ledger or identity tables.
The existing identity owner and superuser remain trusted. Possession of the
issuer credential compromises issuer authority; protect its mount, process
identity and network boundary. Possession of a legitimate session remains a
separate threat from compromise of a runtime database credential.

The proof-export and accounting gates stay as in the delivered source. TOTP
cryptographic primitives are present but not wired to persistent state or
any endpoint. Enrollment, challenge, atomic replay prevention, single-use
recovery consumption, audited cooling-off, role-change invalidation,
privileged operation enforcement and UI remain release blockers.

**Proposed MFA decisions:** 30-second RFC 6238 time steps and adjacent-step
tolerance; issuer-only AES-256-GCM versioned envelopes bound to user and
enrollment; 128-bit recovery codes stored as user-bound digests. Key
distribution and rotation, recovery policy and step-up lifetime remain
PROPOSED pending integration and review.
