import type { components } from "./api.generated";
import type { ApprovalScope, ApprovalAttempt } from "./approval-recovery";
type Proposal = components["schemas"]["Proposal"];
type Simulation = components["schemas"]["SimulationRun"];
export type ExecutionReceipt = components["schemas"]["NativeExecution"];
export type ExecutionScope = ApprovalScope;
export type ExecutionAttempt = ApprovalAttempt;
export type ExecutionRecovery = ExecutionScope & {
  schema: 1;
  attempt: ExecutionAttempt | null;
  refusal: { status: number; code: string; reason: string } | null;
};
const hash = (v: unknown): v is string =>
  typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const exact = (v: object, keys: string[]) =>
  Object.keys(v).sort().join() === keys.sort().join();
export function executionStorageKey(s: ExecutionScope) {
  return `lv.execution.1:${s.tenantId}:${s.userId}:${s.proposalId}:${s.ledgerId}`;
}
export function readExecutionRecovery(
  raw: string | null,
  scope: ExecutionScope,
): ExecutionRecovery | null {
  if (!raw || raw.length > 10000) return null;
  try {
    const v = JSON.parse(raw);
    if (
      !v ||
      !exact(v, [
        "schema",
        "tenantId",
        "userId",
        "proposalId",
        "ledgerId",
        "attempt",
        "refusal",
      ]) ||
      v.schema !== 1 ||
      Object.entries(scope).some(([k, x]) => v[k] !== x)
    )
      return null;
    const a = v.attempt;
    if (
      a !== null &&
      (!a ||
        !exact(a, [
          "key",
          "etag",
          "revision",
          "simulationId",
          "inputHash",
          "resultHash",
          "startedAt",
        ]) ||
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
    const f = v.refusal;
    if (
      f !== null &&
      (!a ||
        !f ||
        !exact(f, ["status", "code", "reason"]) ||
        !definitiveExecutionRefusal(f.status, f.code) ||
        typeof f.reason !== "string" ||
        !/^[A-Z0-9_]{0,100}$/.test(f.reason))
    )
      return null;
    return v;
  } catch {
    return null;
  }
}
export function executionRetryWindow(a: ExecutionAttempt, now = Date.now()) {
  return now >= a.startedAt && now - a.startedAt < 23 * 60 * 60 * 1000;
}
export function executionBody(a: ExecutionAttempt) {
  return { revision: a.revision };
}
export function canStartExecution(
  p: Proposal,
  run: Simulation | null,
  userId: string,
  etag: string,
) {
  return (
    p.allowed_actions.includes("EXECUTE") &&
    p.state === "APPROVED" &&
    p.created_by !== userId &&
    p.submitted_by !== userId &&
    p.ledger_purpose === "SANDBOX" &&
    p.target_adapter === "NATIVE" &&
    p.operations.length === 1 &&
    p.operations[0]?.type === "RecordSettlement" &&
    etag === `"v${p.version}"` &&
    !!run &&
    run.id === p.latest_simulation_id &&
    run.proposal_id === p.id &&
    run.ledger_id === p.ledger_id &&
    run.revision === p.revision &&
    run.status === "COMPLETED" &&
    run.simulator_version === "lv-native-recordsettlement/0.3.0" &&
    hash(run.input_hash) &&
    hash(run.result_hash)
  );
}
export function executionReceipt(
  value: unknown,
  s: ExecutionScope,
): value is ExecutionReceipt {
  if (!value || typeof value !== "object") return false;
  const r = value as ExecutionReceipt,
    c = r.comparison;
  return (
    r.proposal_id === s.proposalId &&
    r.ledger_id === s.ledgerId &&
    typeof r.id === "string" &&
    !!r.id &&
    typeof r.executed_by === "string" &&
    !!r.executed_by &&
    typeof r.entry_id === "string" &&
    !!r.entry_id &&
    typeof r.settlement_document_id === "string" &&
    !!r.settlement_document_id &&
    Number.isSafeInteger(r.revision) &&
    r.revision > 0 &&
    typeof r.simulation_id === "string" &&
    !!r.simulation_id &&
    r.state === "RECONCILED" &&
    r.proposal_state === "EXECUTED" &&
    Number.isSafeInteger(r.observed_version) &&
    r.observed_version > 0 &&
    r.resulting_version === r.observed_version + 1 &&
    typeof r.commit_seq === "string" &&
    /^[1-9]\d*$/.test(r.commit_seq) &&
    r.accounting_status === "UNREVIEWED" &&
    r.data_label === "SANDBOX — not accounting records" &&
    !!c &&
    c.format === "lv-native-posting-match/1" &&
    c.comparison === "MATCHED" &&
    hash(c.predicted_hash) &&
    c.predicted_hash === c.derived_hash &&
    [2, 3].includes(c.line_count as number) &&
    c.accounting_status === "UNREVIEWED" &&
    c.authorizes_execution === false
  );
}
export function originalExecutionReceipt(
  r: unknown,
  s: ExecutionScope,
  a: ExecutionAttempt,
) {
  return (
    executionReceipt(r, s) &&
    r.revision === a.revision &&
    r.simulation_id === a.simulationId &&
    r.executed_by === s.userId &&
    r.observed_version === Number(a.etag.slice(2, -1))
  );
}
export function receiptMatchesHead(r: unknown, s: ExecutionScope, p: Proposal) {
  return (
    executionReceipt(r, s) &&
    p.id === s.proposalId &&
    p.ledger_id === s.ledgerId &&
    p.state === "EXECUTED" &&
    p.revision === r.revision &&
    p.latest_simulation_id === r.simulation_id &&
    p.version === r.resulting_version
  );
}
export function definitiveExecutionRefusal(status: number, code: string) {
  return (
    (status === 412 && code === "VERSION_MISMATCH") ||
    (status === 428 && code === "VERSION_REQUIRED") ||
    (status === 409 && code === "PROPOSAL_STALE") ||
    (status === 422 &&
      ["SCHEMA_INVALID", "CAPABILITY_UNAVAILABLE"].includes(code))
  );
}
