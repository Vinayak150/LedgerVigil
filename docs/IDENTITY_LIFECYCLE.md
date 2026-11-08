# Identity lifecycle (migration 0036)

**Owning contract:** ADR-027 (accepted): email verification before first sign-in, single-use time-limited reset links that
revoke all sessions, rate limits per account and per source on sign-in, reset and verification, user and administrator
session revocation, audit of every security event. Stage 2B §5.1–5.2 and Family 1 (accepted): `GET/POST/PATCH/DELETE
/members`, `SEGREGATION_OF_DUTIES` on self-grant, 409 on removing the last administrator, sessions rotated on privilege change,
24-hour cooling-off after a review/execute grant (2A §12.2).

**Status:** implemented and tested in Claude's Linux workspace (`tests/test_identity_lifecycle.py`, 11 tests; disposable
PostgreSQL 18.6). Not installed on the Mac. Real mail delivery has **not** been verified: tests use the development capture
adapter. MFA is **not implemented** (below).

## What exists

| Endpoint | Rule |
|---|---|
| `POST /auth/email-verification` `{email}` | Always 202 (or 429). A link is sent only to an unverified, enabled account |
| `POST /auth/email-verification/confirm` `{token}` | Single use, 24 hours; every refusal is the same 400 `TOKEN_INVALID` |
| `POST /auth/password-reset` `{email}` | Always 202 (or 429). Link valid 30 minutes; a new request supersedes the old link |
| `POST /auth/password-reset/confirm` `{token, new_password}` | Single use; sets the password, verifies the address, **ends every session** of the user |
| `POST /auth/invitation/accept` `{token, password}` | Single use, 7 days; the membership must still be active |
| `POST /auth/reauthenticate` `{password}` | Records a password confirmation for this session; throttled like sign-in |
| `POST /auth/sessions/revoke-all` | Ends every session of the user, including this one |
| `GET /members` | `read` (2B Family 1) |
| `POST /members` `{email, display_name?, roles[]}` | `administer`, Idempotency-Key, password confirmed within 15 minutes; creates an unverified user and sends an invitation, or adds an existing user |
| `PATCH /members/{user_id}` `{roles[]}` | as above plus `If-Match` (412/428); no self-grant of review, execute or ledger.bootstrap (403 `SEGREGATION_OF_DUTIES`); last administrator kept (409); the member's sessions in this organization end; new grants start the 24-hour cooling-off that approval and execution already enforce |
| `DELETE /members/{user_id}` | as above; membership REVOKED, roles removed, sessions and pending invitations end; history kept |
| `POST /members/{user_id}/sessions/revoke` | `administer`; ends that member's sessions in this organization |
| `GET /audit-events?before=&limit=` | `audit.read`; this organization's hash chain newest first, keyset-paged; every item carries `canonical_content`, `prev_hash` and `hash` so a reader can verify it; each read is itself audited |

Enumeration: public answers are identical whether the address exists; delivery happens after the response (background task);
throttling (3 per address key per hour, 20 per source per hour; 20 refused token uses per source per hour) counts unknown
addresses exactly like known ones. Tokens: 256 random bits, stored as SHA-256 only, carried in the link **fragment**
(`/auth/reset#token=...`) so they never reach server, proxy or analytics logs; the page reads the fragment once, removes it
from the address bar and history, and posts it. Concurrent uses of one token succeed exactly once (row lock; tested with four
parallel requests).

Mail adapters (`LV_MAIL_ADAPTER`): `disabled` (default: requests are recorded, nothing is sent), `capture` (development and
tests only, private directory), `smtp` (STARTTLS required; **never exercised against a real mail service here**).
`LV_PUBLIC_ORIGIN` sets the link origin (HTTPS, or loopback HTTP in development).

The administrator still has no financial permission (ADR-015); member administration cannot grant the actor review, execute
or ledger.bootstrap, and there is no emergency bypass in the UI or API.

## Not done, and why

- **MFA (TOTP) — NOT IMPLEMENTED.** ADR-027 makes it mandatory for review, execute, ledger.bootstrap and administer before
  BOOKS data or any production provider. Until it ships the deployment stays SANDBOX-only, as ADR-027 states. This is a
  release blocker for real data.
- **Q-16 — database trusts the API's password verdict.** Argon2 runs in the API process; `login_finish` and `reauth_finish`
  accept `password_ok` from `lv_app` (bounded: pending attempt, same source, under 2 minutes). A stolen `lv_app` credential
  could therefore mint sessions for verified members. The defensible boundary is a separate authentication service with its
  own database login that alone may call the session-issuing functions; that needs an architecture decision and is **not
  implemented**. It is listed as a production blocker.
- **Per-tenant cooling-off configuration** (2A §12.2 "configurable per tenant"): the 24-hour value is fixed.
- **Real email delivery** has not been verified; the SMTP adapter is untested against a provider.
