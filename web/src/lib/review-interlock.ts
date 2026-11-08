import {
  readApprovalRecovery,
  type ApprovalScope,
} from "./approval-recovery.ts";
import { readExecutionRecovery } from "./execution-recovery.ts";
import { readRejectionRecovery } from "./rejection-recovery.ts";
export function peerReviewBlocked(
  raw: string | null,
  peer: "approval" | "rejection" | "execution",
  scope: ApprovalScope,
  version: number,
) {
  if (raw === null) return false;
  if (peer === "execution") {
    const saved = readExecutionRecovery(raw, scope);
    return !saved || (!!saved.attempt && !saved.refusal);
  }
  const saved =
    peer === "approval"
      ? readApprovalRecovery(raw, scope)
      : readRejectionRecovery(raw, scope);
  return !saved || !!saved.attempt || saved.blockedVersion === version;
}
