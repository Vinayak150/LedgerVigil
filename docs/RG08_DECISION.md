# RG-08 implementation decision

On 2026-11-06 the user explicitly approved requiring an evidence-exception grantor independent of maker, submitter and all editors, using existing self-grant and 24-hour cooling protections. This resolves RG-08's previously unspecified policy for native SANDBOX exceptions. The existing reviewer may grant an exception and approve if otherwise independent of contributors; this decision does not require a different exception-grantor and approver.

The same user reply approved RG-03 semantic revalidation as a separate milestone. No RG-03 staleness behavior is changed by 0030. Accepted historical ADR text and unrelated proposed decisions are preserved.

Backend enforcement is authoritative. The web helper also excludes known editors. A recent or self-granted controller can still see the exception form because grant provenance is private server data; submitting receives a structured 403 with reason.
