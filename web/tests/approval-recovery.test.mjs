import { test } from "node:test";
import assert from "node:assert/strict";
import {
  approvalStorageKey,
  readApprovalRecovery,
  validApprovalComment,
  approvalRetryWindow,
  canStartApproval,
  approvalRequestBody,
  approvalReceipt,
  definitiveApprovalRefusal,
} from "../src/lib/approval-recovery.ts";
const scope = {
  tenantId: "A",
  userId: "reviewer-A",
  proposalId: "proposal-A",
  ledgerId: "ledger-A",
};
const attempt = {
  key: "12345678-1234-1234-1234-123456789abc",
  etag: '"v3"',
  revision: 1,
  simulationId: "run-A",
  inputHash: "a".repeat(64),
  resultHash: "b".repeat(64),
  startedAt: 10000,
};
const saved = {
  schema: 1,
  ...scope,
  comment: " Exact ₹ <img src=x>\nWorkflow decision only. ",
  attempt,
  blockedVersion: null,
};
const p = {
  id: scope.proposalId,
  ledger_id: scope.ledgerId,
  state: "READY_FOR_REVIEW",
  version: 3,
  revision: 1,
  latest_simulation_id: "run-A",
  created_by: "maker-A",
  editors: ["maker-A"],
  ledger_purpose: "SANDBOX",
  target_adapter: "NATIVE",
  operations: [{ type: "RecordSettlement" }],
  allowed_actions: ["APPROVE"],
};
const run = {
  id: "run-A",
  proposal_id: p.id,
  ledger_id: p.ledger_id,
  revision: 1,
  status: "COMPLETED",
  simulator_version: "lv-native-recordsettlement/0.3.0",
  input_hash: attempt.inputHash,
  result_hash: attempt.resultHash,
  outcome: { aggregate: "BLOCKED_INSUFFICIENT" },
};
test("reload preserves exact optional comment and original capture/key/version", () =>
  assert.deepEqual(readApprovalRecovery(JSON.stringify(saved), scope), saved));
test("tenant, actor, proposal and ledger scopes cannot share recovery", () => {
  for (const k of Object.keys(scope)) {
    const changed = { ...scope, [k]: "other" };
    assert.equal(readApprovalRecovery(JSON.stringify(saved), changed), null);
    assert.notEqual(approvalStorageKey(scope), approvalStorageKey(changed));
  }
});
test("body uses original revision and exact text; empty optional comment is omitted", () => {
  assert.deepEqual(approvalRequestBody(saved), {
    revision: 1,
    comment: saved.comment,
  });
  assert.deepEqual(approvalRequestBody({ ...saved, comment: "" }), {
    revision: 1,
  });
  assert.throws(() => approvalRequestBody({ ...saved, attempt: null }));
});
test("malformed or credential-bearing recovery is rejected rather than given a new key", () => {
  for (const value of [
    { ...saved, schema: 2 },
    { ...saved, password: "forbidden" },
    { ...saved, attempt: { ...attempt, key: "bad" } },
    { ...saved, attempt: { ...attempt, etag: '"v03"' } },
    { ...saved, attempt: { ...attempt, etag: '"v9007199254740992"' } },
    { ...saved, attempt: { ...attempt, inputHash: "invalid" } },
    { ...saved, attempt: { ...attempt, resultHash: "g".repeat(64) } },
    { ...saved, attempt: { ...attempt, revision: 0 } },
    { ...saved, attempt: { ...attempt, startedAt: 0 } },
    { ...saved, blockedVersion: -1 },
    { ...saved, comment: "\ud800" },
  ])
    assert.equal(readApprovalRecovery(JSON.stringify(value), scope), null);
  for (const raw of ["bad", "null", "x".repeat(50001)])
    assert.equal(readApprovalRecovery(raw, scope), null);
});
test("comment stays optional and Unicode limits reject NUL and broken surrogates", () => {
  for (const value of ["", "  ", "🙂".repeat(4000), saved.comment])
    assert.ok(validApprovalComment(value));
  for (const value of ["🙂".repeat(4001), "\ud800", "\udfff", "text\0"])
    assert.equal(validApprovalComment(value), false);
});
test("retry stops at 23 hours and on clock rollback", () => {
  assert.ok(approvalRetryWindow(attempt, 10000));
  assert.ok(approvalRetryWindow(attempt, 10000 + 23 * 3600000 - 1));
  assert.equal(approvalRetryWindow(attempt, 10000 + 23 * 3600000), false);
  assert.equal(approvalRetryWindow(attempt, 9999), false);
});
test("server APPROVE hint is mandatory; maker and editor exclusions add a defensive gate", () => {
  assert.ok(canStartApproval(p, run, scope.userId, '"v3"'));
  assert.equal(
    canStartApproval({ ...p, allowed_actions: [] }, run, scope.userId, '"v3"'),
    false,
  );
  assert.equal(canStartApproval(p, run, "maker-A", '"v3"'), false);
  assert.equal(
    canStartApproval(
      { ...p, editors: [scope.userId] },
      run,
      scope.userId,
      '"v3"',
    ),
    false,
  );
});
test("pinned review target rejects wrong state, scope, profile, hashes and token", () => {
  for (const changed of [
    { state: "APPROVED" },
    { state: "STALE" },
    { target_adapter: "QUICKBOOKS" },
    { ledger_purpose: "BOOKS" },
    { operations: [{ type: "Other" }] },
    { latest_simulation_id: "other" },
  ])
    assert.equal(
      canStartApproval({ ...p, ...changed }, run, scope.userId, '"v3"'),
      false,
    );
  for (const changed of [
    { id: "other" },
    { revision: 2 },
    { proposal_id: "other" },
    { ledger_id: "other" },
    { status: "REJECTED" },
    { simulator_version: "legacy" },
    { input_hash: "not-a-hash" },
    { outcome: { aggregate: "BLOCKED_FAIL" } },
  ])
    assert.equal(
      canStartApproval(p, { ...run, ...changed }, scope.userId, '"v3"'),
      false,
    );
  assert.equal(canStartApproval(p, run, scope.userId, '"v999"'), false);
});
test("only the original independent approval receipt confirms an attempt", () => {
  const result = { ...p, state: "APPROVED", version: 4 };
  assert.ok(approvalReceipt(result, scope, attempt));
  for (const changed of [
    { state: "STALE" },
    { state: "READY_FOR_REVIEW" },
    { version: 5 },
    { revision: 2 },
    { id: "other" },
    { ledger_id: "other" },
    { latest_simulation_id: "new-run" },
    { created_by: scope.userId },
    { editors: [scope.userId] },
  ])
    assert.equal(
      approvalReceipt({ ...result, ...changed }, scope, attempt),
      false,
    );
  assert.equal(approvalReceipt(undefined, scope, attempt), false);
});
test("permission failures and unknown responses retain pending identity; typed refusals resolve it", () => {
  for (const [status, code] of [
    [401, "UNAUTHENTICATED"],
    [403, "SEGREGATION_OF_DUTIES"],
    [404, "NOT_FOUND"],
    [409, "REQUEST_IN_PROGRESS"],
    [422, "IDEMPOTENCY_KEY_REUSED"],
    [500, "INTERNAL"],
    [200, ""],
    [412, "UNKNOWN"],
  ])
    assert.equal(definitiveApprovalRefusal(status, code), false);
  for (const [status, code] of [
    [412, "VERSION_MISMATCH"],
    [409, "PROPOSAL_STALE"],
    [409, "ALREADY_DECIDED"],
    [409, "INVALID_STATE_TRANSITION"],
    [422, "CAPABILITY_UNAVAILABLE"],
  ])
    assert.ok(definitiveApprovalRefusal(status, code));
});
