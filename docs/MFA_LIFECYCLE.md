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

Key rotation and external recovery remain separate work. Migration downgrade
refuses to discard reset history. Tests use synthetic BOOKS metadata and a
test-owner clock adjustment; there is no API to shorten the waiting period.
