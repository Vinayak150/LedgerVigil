import { test } from "node:test";
import assert from "node:assert/strict";
import {
  executionStorageKey,
  readExecutionRecovery,
  executionRetryWindow,
  executionBody,
  canStartExecution,
  executionReceipt,
  originalExecutionReceipt,
  receiptMatchesHead,
  definitiveExecutionRefusal,
} from "../src/lib/execution-recovery.ts";
import { peerReviewBlocked } from "../src/lib/review-interlock.ts";
const s = {
  tenantId: "A",
  userId: "executor",
  proposalId: "proposal",
  ledgerId: "ledger",
};
const a = {
  key: "12345678-1234-1234-1234-123456789abc",
  etag: '"v4"',
  revision: 1,
  simulationId: "run",
  inputHash: "a".repeat(64),
  resultHash: "b".repeat(64),
  startedAt: 10000,
};
const saved = { schema: 1, ...s, attempt: a, refusal: null };
const p = {
  id: s.proposalId,
  ledger_id: s.ledgerId,
  state: "APPROVED",
  version: 4,
  revision: 1,
  latest_simulation_id: "run",
  created_by: "maker",
  submitted_by: "maker",
  editors: ["maker", "executor"],
  ledger_purpose: "SANDBOX",
  target_adapter: "NATIVE",
  operations: [{ type: "RecordSettlement" }],
  allowed_actions: ["EXECUTE"],
};
const run = {
  id: "run",
  proposal_id: p.id,
  ledger_id: p.ledger_id,
  revision: 1,
  status: "COMPLETED",
  simulator_version: "lv-native-recordsettlement/0.3.0",
  input_hash: a.inputHash,
  result_hash: a.resultHash,
  outcome: { eligible_for_execution: false, aggregate: "BLOCKED_INSUFFICIENT" },
};
const r = {
  id: "execution",
  proposal_id: p.id,
  ledger_id: p.ledger_id,
  revision: 1,
  simulation_id: "run",
  state: "RECONCILED",
  proposal_state: "EXECUTED",
  executed_by: s.userId,
  observed_version: 4,
  resulting_version: 5,
  commit_seq: "9007199254740993",
  settlement_document_id: "settlement",
  entry_id: "entry",
  accounting_status: "UNREVIEWED",
  data_label: "SANDBOX — not accounting records",
  comparison: {
    format: "lv-native-posting-match/1",
    comparison: "MATCHED",
    predicted_hash: "c".repeat(64),
    derived_hash: "c".repeat(64),
    line_count: 3,
    accounting_status: "UNREVIEWED",
    authorizes_execution: false,
  },
};
test("exact recovery survives reload and body retains the original revision", () => {
  assert.deepEqual(readExecutionRecovery(JSON.stringify(saved), s), saved);
  assert.deepEqual(executionBody(a), { revision: 1 });
  assert.equal(a.etag, '"v4"');
});
test("all verified identity/tenant/proposal/ledger scopes are separate", () => {
  for (const k of Object.keys(s)) {
    const other = { ...s, [k]: "other" };
    assert.equal(readExecutionRecovery(JSON.stringify(saved), other), null);
    assert.notEqual(executionStorageKey(s), executionStorageKey(other));
  }
});
test("corrupt or credential-bearing recovery never receives a replacement identity", () => {
  for (const v of [
    { ...saved, password: "secret" },
    { ...saved, schema: 2 },
    { ...saved, attempt: { ...a, key: "bad" } },
    { ...saved, attempt: { ...a, etag: '"v04"' } },
    { ...saved, attempt: { ...a, etag: '"v9007199254740992"' } },
    { ...saved, attempt: { ...a, inputHash: "bad" } },
    { ...saved, attempt: { ...a, startedAt: 0 } },
    { ...saved, refusal: { status: 500, code: "INTERNAL", reason: "" } },
    {
      ...saved,
      refusal: { status: 403, code: "PERMISSION_DENIED", reason: "" },
    },
    {
      ...saved,
      attempt: null,
      refusal: { status: 412, code: "VERSION_MISMATCH", reason: "" },
    },
  ])
    assert.equal(readExecutionRecovery(JSON.stringify(v), s), null);
  for (const raw of ["{", "null", "x".repeat(10001)])
    assert.equal(readExecutionRecovery(raw, s), null);
});
test("retry window fails closed on expiry and backward clock", () => {
  assert.ok(executionRetryWindow(a, a.startedAt));
  assert.ok(!executionRetryWindow(a, a.startedAt - 1));
  assert.ok(!executionRetryWindow(a, a.startedAt + 23 * 3600000));
});
test("current hint and exact pinned native target authorize the UI; historical capability remains unchanged", () => {
  assert.ok(canStartExecution(p, run, s.userId, '"v4"'));
  for (const changed of [
    { ...p, allowed_actions: [] },
    { ...p, state: "EXECUTED" },
    { ...p, created_by: s.userId },
    { ...p, submitted_by: s.userId },
    { ...p, ledger_purpose: "BOOKS" },
    { ...p, target_adapter: "QUICKBOOKS" },
    { ...p, revision: 2 },
    { ...p, operations: [] },
  ])
    assert.ok(!canStartExecution(changed, run, s.userId, '"v4"'));
  for (const changed of [
    { ...run, ledger_id: "other" },
    { ...run, id: "other" },
    { ...run, input_hash: "bad" },
    { ...run, simulator_version: "unknown" },
  ])
    assert.ok(!canStartExecution(p, changed, s.userId, '"v4"'));
  assert.ok(!canStartExecution(p, run, s.userId, '"v3"'));
});
test("receipt validates scope exact proof actor and versions, without float parsing sequence", () => {
  assert.ok(executionReceipt(r, s));
  assert.ok(originalExecutionReceipt(r, s, a));
  assert.ok(receiptMatchesHead(r, s, { ...p, state: "EXECUTED", version: 5 }));
  for (const changed of [
    { ...r, ledger_id: "other" },
    { ...r, proposal_id: "other" },
    { ...r, resulting_version: 6 },
    { ...r, state: "BUILDING" },
    { ...r, accounting_status: "REVIEWED" },
    { ...r, comparison: { ...r.comparison, derived_hash: "d".repeat(64) } },
    { ...r, comparison: { ...r.comparison, authorizes_execution: true } },
    { ...r, commit_seq: "1.0" },
  ])
    assert.ok(!executionReceipt(changed, s));
  assert.ok(
    !originalExecutionReceipt({ ...r, executed_by: "other-executor" }, s, a),
  );
  assert.ok(!originalExecutionReceipt({ ...r, simulation_id: "other" }, s, a));
});
test("readback may show another executor but never proves the original request won", () => {
  const other = { ...r, executed_by: "other-executor" };
  assert.ok(
    receiptMatchesHead(other, s, { ...p, state: "EXECUTED", version: 5 }),
  );
  assert.ok(!originalExecutionReceipt(other, s, a));
});
test("uncertain auth/infrastructure/in-progress and reused-key outcomes cannot become definite refusal", () => {
  for (const pair of [
    [500, "INTERNAL"],
    [401, "UNAUTHENTICATED"],
    [403, "PERMISSION_DENIED"],
    [409, "REQUEST_IN_PROGRESS"],
    [409, "INVALID_STATE_TRANSITION"],
    [422, "IDEMPOTENCY_KEY_REUSED"],
  ])
    assert.ok(!definitiveExecutionRefusal(...pair));
  for (const pair of [
    [412, "VERSION_MISMATCH"],
    [428, "VERSION_REQUIRED"],
    [409, "PROPOSAL_STALE"],
    [422, "CAPABILITY_UNAVAILABLE"],
  ])
    assert.ok(definitiveExecutionRefusal(...pair));
});
test("unconfirmed execution blocks both reviews; a definitively refused request does not block a newer review", () => {
  assert.ok(peerReviewBlocked(JSON.stringify(saved), "execution", s, 4));
  assert.ok(peerReviewBlocked("{", "execution", s, 4));
  assert.ok(!peerReviewBlocked(null, "execution", s, 4));
  const refused = {
    ...saved,
    refusal: { status: 409, code: "PROPOSAL_STALE", reason: "" },
  };
  assert.ok(!peerReviewBlocked(JSON.stringify(refused), "execution", s, 6));
});
