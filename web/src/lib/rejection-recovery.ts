import type { components } from "./api.generated";
import {
  readApprovalRecovery,
  validApprovalComment,
  approvalRetryWindow,
  definitiveApprovalRefusal,
  type ApprovalRecovery,
  type ApprovalScope,
  type ApprovalAttempt,
} from "./approval-recovery.ts";
import { canStartReview, reviewReceipt } from "./review-target.ts";
export type RejectionRecovery = ApprovalRecovery & { decision: "REJECT" };
export type RejectionScope = ApprovalScope;
export type RejectionAttempt = ApprovalAttempt;
export const rejectionRetryWindow = approvalRetryWindow;
export const definitiveRejectionRefusal = definitiveApprovalRefusal;
export function rejectionStorageKey(s: RejectionScope) {
  return `lv.rejection.1:${s.tenantId}:${s.userId}:${s.proposalId}:${s.ledgerId}`;
}
// Match the owning Python/PostgreSQL whitespace inventory; JS trim differs for U+0085 and U+001C–001F.
const blank =
  /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;
export function validRejectionComment(value: string) {
  return validApprovalComment(value) && !blank.test(value);
}
export function readRejectionRecovery(
  raw: string | null,
  scope: RejectionScope,
): RejectionRecovery | null {
  if (!raw || raw.length > 50000) return null;
  try {
    const value = JSON.parse(raw);
    if (value?.decision !== "REJECT") return null;
    const { decision, ...original } = value;
    const saved = readApprovalRecovery(JSON.stringify(original), scope);
    if (!saved || (saved.attempt && !validRejectionComment(saved.comment)))
      return null;
    return { ...saved, decision };
  } catch {
    return null;
  }
}
export function canStartRejection(
  p: components["schemas"]["Proposal"],
  run: components["schemas"]["SimulationRun"] | null,
  userId: string,
  etag: string,
) {
  return canStartReview(p, run, userId, etag, "REJECT");
}
export function rejectionRequestBody(r: RejectionRecovery) {
  if (
    r.decision !== "REJECT" ||
    !r.attempt ||
    !validRejectionComment(r.comment)
  )
    throw Error("No valid original rejection request");
  return { revision: r.attempt.revision, comment: r.comment };
}
export function rejectionReceipt(
  p: components["schemas"]["Proposal"] | undefined,
  scope: RejectionScope,
  attempt: RejectionAttempt,
) {
  return reviewReceipt(p, scope, attempt, "REJECTED");
}
