import { canStartReview, reviewReceipt } from "./review-target.ts";
import type { components } from "./api.generated";
type Proposal = components["schemas"]["Proposal"];
type Simulation = components["schemas"]["SimulationRun"];
export type ApprovalScope = {
  tenantId: string;
  userId: string;
  proposalId: string;
  ledgerId: string;
};
export type ApprovalAttempt = {
  key: string;
  etag: string;
  revision: number;
  simulationId: string;
  inputHash: string;
  resultHash: string;
  startedAt: number;
};
export type ApprovalRecovery = ApprovalScope & {
  schema: 1;
  comment: string;
  attempt: ApprovalAttempt | null;
  blockedVersion: number | null;
};
const hash = (v: unknown): v is string =>
  typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
export function approvalStorageKey(s: ApprovalScope) {
  return `lv.approval.1:${s.tenantId}:${s.userId}:${s.proposalId}:${s.ledgerId}`;
}
export function validApprovalComment(value: string) {
  const points = Array.from(value);
  return (
    points.length <= 4000 &&
    !points.some(
      (c) =>
        c === "\0" ||
        (c.codePointAt(0)! >= 0xd800 && c.codePointAt(0)! <= 0xdfff),
    )
  );
}
export function readApprovalRecovery(
  raw: string | null,
  scope: ApprovalScope,
): ApprovalRecovery | null {
  if (!raw || raw.length > 50000) return null;
  try {
    const v = JSON.parse(raw);
    if (
      !v ||
      Object.keys(v).sort().join() !==
        [
          "schema",
          "tenantId",
          "userId",
          "proposalId",
          "ledgerId",
          "comment",
          "attempt",
          "blockedVersion",
        ]
          .sort()
          .join() ||
      v.schema !== 1 ||
      Object.entries(scope).some(([k, x]) => v[k] !== x) ||
      typeof v.comment !== "string" ||
      !validApprovalComment(v.comment) ||
      (v.blockedVersion !== null &&
        (!Number.isSafeInteger(v.blockedVersion) || v.blockedVersion < 1))
    )
      return null;
    const a = v.attempt;
    if (
      a !== null &&
      (!a ||
        Object.keys(a).sort().join() !==
          [
            "key",
            "etag",
            "revision",
            "simulationId",
            "inputHash",
            "resultHash",
            "startedAt",
          ]
            .sort()
            .join() ||
        typeof a.key !== "string" ||
        !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(
          a.key,
        ) ||
        typeof a.etag !== "string" ||
        !/^"v[1-9]\d*"$/.test(a.etag) ||
        !Number.isSafeInteger(Number(a.etag.slice(2, -1))) ||
        !Number.isSafeInteger(a.revision) ||
        a.revision < 1 ||
        typeof a.simulationId !== "string" ||
        !a.simulationId ||
        !hash(a.inputHash) ||
        !hash(a.resultHash) ||
        !Number.isSafeInteger(a.startedAt) ||
        a.startedAt < 1)
    )
      return null;
    return v;
  } catch {
    return null;
  }
}
export function approvalRetryWindow(a: ApprovalAttempt, now = Date.now()) {
  return now >= a.startedAt && now - a.startedAt < 23 * 60 * 60 * 1000;
}
export function canStartApproval(
  p: Proposal,
  run: Simulation | null,
  userId: string,
  etag: string,
) {
  return canStartReview(p, run, userId, etag, "APPROVE");
}
export function approvalRequestBody(r: ApprovalRecovery) {
  if (!r.attempt) throw Error("No original approval attempt");
  return {
    revision: r.attempt.revision,
    ...(r.comment !== "" ? { comment: r.comment } : {}),
  };
}
export function approvalReceipt(
  p: Proposal | undefined,
  s: ApprovalScope,
  a: ApprovalAttempt,
) {
  return reviewReceipt(p, s, a, "APPROVED");
}
export function definitiveApprovalRefusal(status: number, code: string) {
  return (
    (status === 412 && code === "VERSION_MISMATCH") ||
    (status === 428 && code === "VERSION_REQUIRED") ||
    (status === 409 &&
      [
        "PROPOSAL_STALE",
        "ALREADY_DECIDED",
        "INVALID_STATE_TRANSITION",
      ].includes(code)) ||
    (status === 422 &&
      ["SCHEMA_INVALID", "CAPABILITY_UNAVAILABLE"].includes(code))
  );
}
