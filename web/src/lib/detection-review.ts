// Pure helpers for the detection review UI (PROPOSED DET-1..DET-11). No network, no storage writes; unit-tested.
export type ReviewAction = "CONFIRMED" | "DISMISSED" | "REOPENED";
export const DISMISS_REASONS = [
  ["EXPECTED_TIMING", "Expected timing difference"],
  ["STATEMENT_ERROR", "Error in the statement"],
  ["KNOWN_EXCEPTION", "Known exception"],
  ["NOT_A_DISCREPANCY", "Not a discrepancy"],
  ["INPUTS_CHANGED", "Inputs changed since detection"],
] as const;
export const KIND_LABELS: Record<string, string> = {
  SETTLEMENT_NOT_RECORDED: "Settlement not recorded",
  SETTLEMENT_AMOUNTS_DIFFER: "Settlement amounts differ",
  SETTLEMENT_ITEMS_DIFFER: "Settlement items differ",
  STATEMENT_ITEM_SETTLED_ELSEWHERE: "Payment settled in another payout",
  STATEMENT_ITEM_AMOUNT_DIFFERS: "Payment amount differs",
  STATEMENT_ITEM_UNRESOLVED: "Statement line not identified",
  PAYMENT_CLAIMED_BY_OTHER_PAYOUT: "Potential overlapping claim",
  PAYOUT_DATE_DIFFERS: "Date difference",
};
export const CLASS_LABELS: Record<string, string> = {
  DISCREPANCY: "Discrepancy",
  EVIDENCE_GAP: "Evidence gap",
  POTENTIAL_CONFLICT: "Potential conflict",
};
export const EXCLUDED_REASONS: Record<string, string> = {
  OTHER_PAYOUT_RECORDED: "the other payout is already recorded",
  OTHER_STATEMENT_AMBIGUOUS: "the other payout has a conflicting second file",
  OTHER_STATEMENT_UNSUPPORTED:
    "the other statement has unsupported (refund) lines",
};
export function kindLabel(kind: string) {
  return KIND_LABELS[kind] ?? kind.replaceAll("_", " ").toLowerCase();
}
// Signed calendar days, statement minus ledger, exactly as the finding states them. No business-day or timezone logic.
export function dayDifferenceText(days: number) {
  if (!Number.isSafeInteger(days)) return "Difference not available";
  if (days === 0) return "Same calendar date";
  const n = Math.abs(days),
    unit = n === 1 ? "calendar day" : "calendar days";
  return days < 0
    ? `The statement date is ${n} ${unit} earlier than the recorded date`
    : `The statement date is ${n} ${unit} later than the recorded date`;
}
// Actions the API accepts for the current review status (lv.detection_dispose rules). Confirm needs CURRENT inputs.
export function availableActions(
  status: string,
  freshness: string,
  canReview: boolean,
): {
  action: ReviewAction;
  reason: string | null;
  enabled: boolean;
  why?: string;
}[] {
  if (!canReview) return [];
  if (status === "OPEN")
    return [
      {
        action: "CONFIRMED",
        reason: "VERIFIED_AGAINST_EVIDENCE",
        enabled: freshness === "CURRENT",
        why:
          freshness === "CURRENT"
            ? undefined
            : "Inputs changed since detection: run detection again, or dismiss as inputs changed.",
      },
      { action: "DISMISSED", reason: null, enabled: true },
    ];
  if (status === "CONFIRMED" || status === "DISMISSED")
    return [{ action: "REOPENED", reason: "REVIEW_CORRECTION", enabled: true }];
  return [];
}
export function validNote(note: string) {
  if (!note) return true;
  const points = Array.from(note);
  return (
    points.length <= 2000 &&
    /\S/u.test(note) &&
    !points.some(
      (c) =>
        c === "\0" ||
        (c.codePointAt(0)! >= 0xd800 && c.codePointAt(0)! <= 0xdfff),
    )
  );
}
// A pending review decision survives a reload so the SAME request (same Idempotency-Key, If-Match and body) can be resent.
// The scope binds it to one tenant, user and finding: it is ignored anywhere else. No secret is stored.
export type ReviewScope = {
  tenantId: string;
  userId: string;
  findingId: string;
};
export type PendingReview = ReviewScope & {
  schema: 1;
  key: string;
  etag: string;
  action: ReviewAction;
  reason: string;
  note: string;
  startedAt: number;
};
export const reviewRetryWindowMs = 23 * 60 * 60 * 1000; // the server keeps idempotency records for 24 hours
export function reviewStorageKey(s: ReviewScope) {
  return `lv.detection-review.1:${s.tenantId}:${s.userId}:${s.findingId}`;
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function readPendingReview(
  raw: string | null,
  scope: ReviewScope,
  now = Date.now(),
): PendingReview | null {
  if (!raw || raw.length > 10000) return null;
  try {
    const v = JSON.parse(raw);
    const keys = [
      "schema",
      "tenantId",
      "userId",
      "findingId",
      "key",
      "etag",
      "action",
      "reason",
      "note",
      "startedAt",
    ];
    if (
      !v ||
      Object.keys(v).sort().join() !== keys.sort().join() ||
      v.schema !== 1 ||
      v.tenantId !== scope.tenantId ||
      v.userId !== scope.userId ||
      v.findingId !== scope.findingId ||
      typeof v.key !== "string" ||
      !UUID.test(v.key) ||
      typeof v.etag !== "string" ||
      !/^v[1-9][0-9]{0,8}$/.test(v.etag) ||
      !["CONFIRMED", "DISMISSED", "REOPENED"].includes(v.action) ||
      typeof v.reason !== "string" ||
      typeof v.note !== "string" ||
      !validNote(v.note) ||
      !Number.isSafeInteger(v.startedAt) ||
      now - v.startedAt > reviewRetryWindowMs ||
      v.startedAt > now + 60000
    )
      return null;
    return v as PendingReview;
  } catch {
    return null;
  }
}
export function reviewBody(p: PendingReview) {
  return {
    action: p.action,
    reason_code: p.reason,
    ...(p.note ? { note: p.note } : {}),
  };
}
// Problem codes from the dispositions endpoint, in words for the reviewer.
export function reviewProblem(status: number, code: string | undefined) {
  if (status === 409 && code === "FINDING_STALE")
    return "Inputs changed since detection. Run detection again, or dismiss this finding as inputs changed.";
  if (status === 412)
    return "Someone else updated this review. The latest decision has been loaded; check it before deciding.";
  if (status === 409 && code === "INVALID_STATE_TRANSITION")
    return "This decision is no longer possible from the current review status. The latest state has been loaded.";
  if (status === 409 && code === "REQUEST_IN_PROGRESS")
    return "The same decision is still being processed. Wait a moment and resend it.";
  if (status === 403)
    return "Your role cannot record review decisions in this organization.";
  if (status === 401)
    return "Your session has ended. Refresh your session; the pending decision is kept.";
  if (status === 422)
    return "The decision or note is not valid. Check the reason and the note.";
  if (status === 404)
    return "This finding is not available in your organization.";
  return "The decision could not be confirmed. Resend the same decision to find out whether it was recorded.";
}
