import type { components } from "./api.generated";
import type { ApprovalScope, ApprovalAttempt } from "./approval-recovery";
type Proposal = components["schemas"]["Proposal"];
type Simulation = components["schemas"]["SimulationRun"];
const hash = (v: unknown): v is string =>
  typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
export function canStartReview(
  p: Proposal,
  run: Simulation | null,
  userId: string,
  etag: string,
  action: "APPROVE" | "REJECT",
) {
  return (
    p.allowed_actions.includes(action) &&
    p.state === "READY_FOR_REVIEW" &&
    p.created_by !== userId &&
    !p.editors.includes(userId) &&
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
    hash(run.result_hash) &&
    ["CLEAR", "BLOCKED_INSUFFICIENT"].includes(String(run.outcome.aggregate))
  );
}
export function reviewReceipt(
  p: Proposal | undefined,
  s: ApprovalScope,
  a: ApprovalAttempt,
  state: "APPROVED" | "REJECTED",
) {
  return (
    !!p &&
    p.id === s.proposalId &&
    p.ledger_id === s.ledgerId &&
    p.state === state &&
    p.revision === a.revision &&
    p.latest_simulation_id === a.simulationId &&
    p.version === Number(a.etag.slice(2, -1)) + 1 &&
    p.created_by !== s.userId &&
    !p.editors.includes(s.userId) &&
    p.ledger_purpose === "SANDBOX" &&
    p.target_adapter === "NATIVE"
  );
}
