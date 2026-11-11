# MFA lifecycle and privileged BOOKS access (0040)

The isolated candidate adds self-service authenticator removal, with no
administrator override or emergency bypass. The user must reconfirm the password
and supply a fresh authenticator code or unused recovery code to request removal.
Completion requires another fresh password confirmation and factor proof after
a mandatory 24-hour wait. Cancellation also requires password and factor proof.
Completion revokes every session and removes the current factor; the audit and
attempt history remain. Removal does not erase ciphertext from WAL or backups.
Losing both the authenticator and all recovery codes does not authorize a bypass.

For a tenant containing BOOKS ledger metadata, the shared database permission
check refuses review, execute, ledger.bootstrap and administer unless the current,
valid session has a verified factor within fifteen minutes. A pending removal
also refuses these permissions. The HTTP permission boundary returns MFA_REQUIRED;
a direct call through the normal database login is refused too. Role changes and
tenant switches revoke or rotate sessions and do not inherit factor verification.

The fifteen-minute lifetime, 24-hour wait and self-service removal policy are
PROPOSED implementation defaults awaiting the ADR process. This migration does
not enable BOOKS financial writes, first-BOOKS bootstrap, production provider
connections or accounting approval. First-BOOKS creation remains unavailable
under the unresolved bootstrap contract; a future implementation must enforce
step-up before creation, not only once a BOOKS ledger already exists.

External recovery and the operator-run deployment rotation drill remain separate work. Migration downgrade
refuses to discard reset history. Tests use synthetic BOOKS metadata and a
test-owner clock adjustment; there is no API to shorten the waiting period.

## Encryption-key rotation

Only an operator holding the restricted migrator login and the old and new
private encryption-key files can run:

    python -m tools.mfa_rotate --old-key-file /private/old-key --new-key-file /private/new-key

The operator supplies LV_MIGRATOR_DATABASE_URL_FILE privately. Generate the new
32-byte hexadecimal key outside the repository; do not print it. Before rotating,
configure the issuer with the new primary LV_MFA_KEY_FILE and the previous key
under LV_MFA_PREVIOUS_KEY_FILES (comma-separated private paths, at most three).
Restart/reconfigure only the issuer through the deployment runbook. Both generations
remain readable during transition, and new enrollments use the new primary.

The tool takes a nonwaiting audit-table lock before the factor-table lock,
rotates all envelopes in one transaction, and records aggregate counts only.
A busy issuer, wrong key or failed update rolls the transaction back. Retry
after the operator has quiesced traffic. A rerun recognizes envelopes already
encrypted with the new key. TOTP counters, recovery-code hashes and reset state
are preserved. Remove the old key from the active ring only after every factor
is current and a challenge succeeds. Retain the old key privately for the
backup-retention window: database backups may still contain old envelopes.

The tools do not delete ciphertext in backups or WAL and do not establish an
offsite backup policy. This protocol requires an operator-run deployment drill
before production use.
