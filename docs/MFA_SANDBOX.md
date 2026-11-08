# Authenticator verification in the SANDBOX

Migration `0039_mfa` adds issuer-owned TOTP enrollment, session challenge,
single-use recovery codes and recovery code replacement. It does not enable
BOOKS ledgers, QuickBooks writes, or production provider connections.

In the issuer process only, set `LV_MFA_KEY_FILE` to the path of a private file
containing a generated 32-byte key encoded as 64 hexadecimal characters. The
owner should generate the file outside the repository with mode 0600, without
printing it. With Compose use `-f deploy/compose.yaml -f deploy/compose.mfa.yaml`;
the bind-mounted file must be readable only by issuer UID 10007. If the file is
missing or invalid, authenticator actions refuse rather than using a fallback
key. Do not replace a key in place while any factors are enrolled.

Open **Organization & settings → Authenticator app**, confirm your password,
enter the displayed key in an authenticator, and verify a six-digit code.
Save the one-time recovery codes privately. In a subsequent session you can
verify with your authenticator or use a recovery code. A tenant switch, new
login, password reset or role change does not inherit the previous session's
verification. A password reset does not reset the enrolled authenticator.

Pending setup is session-bound and expires after ten minutes. TOTP steps and
recovery-code use commit atomically under the account lock. Only the issuer
can invoke the MFA database functions; the API, worker, ingest and seed roles
cannot read the factor state. Security audit events contain outcomes and
attribution, never a setup key, code, or digest.

Proposed SANDBOX defaults: a 30-second TOTP step with one adjacent step on
either side; ten recovery codes; ten attempts per account and forty per
source in fifteen minutes. Secret rotation, recovery/reset approval and
cooling-off, and the lifetime of proof for privileged production actions
need a recorded policy. Privileged disable/reset and production-action
enforcement remain open. This SANDBOX implementation is not evidence that
the ADR-027 production gate is satisfied.

For rollback, downgrade refuses to discard enrolled factors or attempt
history. Keep the existing database backup and the key in independently
managed private storage. A production installer and key-rotation drill
are still required.
