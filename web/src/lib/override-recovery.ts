import type { components } from "./api.generated";
type Proposal = components["schemas"]["Proposal"];
type Simulation = components["schemas"]["SimulationRun"];
export type OverrideScope = {
  tenantId: string;
  userId: string;
  proposalId: string;
  ledgerId: string;
};
export type OverrideAttempt = {
  key: string;
  etag: string;
  revision: number;
  simulationId: string;
  startedAt: number;
};
export type OverrideRecovery = OverrideScope & {
  schema: 1;
  reason: string;
  attempt: OverrideAttempt | null;
  blockedVersion: number | null;
};
export function canAuthorOverride(roles: string[], permissions: string[]) {
  return roles.includes("CONTROLLER") && permissions.includes("review");
}
export function overrideStorageKey(s: OverrideScope) {
  return `lv.evidence-exception.1:${s.tenantId}:${s.userId}:${s.proposalId}:${s.ledgerId}`;
}
export function validReason(reason: string) {
  const points = Array.from(reason);
  return (
    points.length >= 1 &&
    points.length <= 4000 &&
    /[^\s\u0085]/u.test(reason) &&
    !points.some(
      (c) =>
        c === "\0" ||
        (c.codePointAt(0)! >= 0xd800 && c.codePointAt(0)! <= 0xdfff),
    )
  );
}
export function readOverrideRecovery(
  raw: string | null,
  scope: OverrideScope,
): OverrideRecovery | null {
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
          "reason",
          "attempt",
          "blockedVersion",
        ]
          .sort()
          .join() ||
      v.schema !== 1 ||
      Object.entries(scope).some(([k, x]) => v[k] !== x) ||
      typeof v.reason !== "string" ||
      Array.from(v.reason).length > 4000 ||
      (v.blockedVersion !== null &&
        (!Number.isSafeInteger(v.blockedVersion) || v.blockedVersion < 1))
    )
      return null;
    const a = v.attempt;
    if (
      a !== null &&
      (!a ||
        Object.keys(a).sort().join() !==
          ["key", "etag", "revision", "simulationId", "startedAt"]
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
        !Number.isSafeInteger(a.startedAt) ||
        a.startedAt < 1 ||
        !validReason(v.reason))
    )
      return null;
    return v;
  } catch {
    return null;
  }
}
// Bound retries before the server's 24-hour cache expiry. Future clocks fail closed.
export function overrideRetryWindow(a: OverrideAttempt, now = Date.now()) {
  return now >= a.startedAt && now - a.startedAt < 23 * 60 * 60 * 1000;
}
export function overrideCandidate(
  p: Proposal,
  run: Simulation | null,
  userId: string,
  etag: string,
) {
  if (
    p.state !== "BLOCKED" ||
    p.created_by === userId ||
    p.ledger_purpose !== "SANDBOX" ||
    p.target_adapter !== "NATIVE" ||
    p.operations.length !== 1 ||
    p.operations[0]?.type !== "RecordSettlement" ||
    etag !== `"v${p.version}"` ||
    !run ||
    run.id !== p.latest_simulation_id ||
    run.proposal_id !== p.id ||
    run.ledger_id !== p.ledger_id ||
    run.revision !== p.revision ||
    run.status !== "COMPLETED" ||
    run.simulator_version !== "lv-native-recordsettlement/0.3.0" ||
    run.outcome.aggregate !== "BLOCKED_INSUFFICIENT"
  )
    return false;
  const verdicts = run.outcome.verdicts;
  if (
    !Array.isArray(verdicts) ||
    verdicts.some(
      (v) =>
        !v ||
        typeof v !== "object" ||
        Array.isArray(v) ||
        typeof v.policy_id !== "string" ||
        !["PASS", "FAIL", "NOT_APPLICABLE", "INSUFFICIENT_EVIDENCE"].includes(
          v.outcome,
        ),
    ) ||
    new Set(verdicts.map((v) => v.policy_id)).size !== verdicts.length
  )
    return false;
  const insufficient = verdicts.filter(
    (v) => v.outcome === "INSUFFICIENT_EVIDENCE",
  );
  return (
    !verdicts.some((v) => v.outcome === "FAIL") &&
    insufficient.length === 1 &&
    insufficient[0].policy_id === "P-EVD-01"
  );
}
export function overrideReceipt(
  p: Proposal | undefined,
  scope: OverrideScope,
  a: OverrideAttempt,
) {
  return (
    !!p &&
    p.id === scope.proposalId &&
    p.ledger_id === scope.ledgerId &&
    p.created_by !== scope.userId &&
    p.ledger_purpose === "SANDBOX" &&
    p.target_adapter === "NATIVE" &&
    p.state === "READY_FOR_REVIEW" &&
    p.revision === a.revision &&
    p.latest_simulation_id === a.simulationId &&
    p.version === Number(a.etag.slice(2, -1)) + 1
  );
}
// Authentication/refusal to replay cannot settle an earlier uncertain commit.
export function definitiveOverrideRefusal(status: number, code: string) {
  return (
    (status === 412 && code === "VERSION_MISMATCH") ||
    (status === 428 && code === "VERSION_REQUIRED") ||
    (status === 409 &&
      ["INVALID_STATE_TRANSITION", "PROPOSAL_STALE"].includes(code)) ||
    (status === 422 &&
      [
        "SCHEMA_INVALID",
        "CAPABILITY_UNAVAILABLE",
        "PRECONDITION_FAILED",
      ].includes(code))
  );
}
