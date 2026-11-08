import { test } from "node:test";
import assert from "node:assert/strict";
import {
  canAuthorOverride,
  overrideStorageKey,
  readOverrideRecovery,
  validReason,
  overrideRetryWindow,
  overrideCandidate,
  overrideReceipt,
  definitiveOverrideRefusal,
} from "../src/lib/override-recovery.ts";
const scope = {
  tenantId: "A",
  userId: "controller-A",
  proposalId: "proposal-A",
  ledgerId: "ledger-A",
};
const a = {
  key: "12345678-1234-1234-1234-123456789abc",
  etag: '"v2"',
  revision: 1,
  simulationId: "run-A",
  startedAt: 10000,
};
const saved = {
  schema: 1,
  ...scope,
  reason: " Exact ₹ <img src=x>\nSYNTHETIC only. ",
  attempt: a,
  blockedVersion: null,
};
const proposal = {
  id: scope.proposalId,
  ledger_id: scope.ledgerId,
  created_by: "maker-A",
  editors: ["maker-A"],
  ledger_purpose: "SANDBOX",
  target_adapter: "NATIVE",
  state: "BLOCKED",
  version: 2,
  revision: 1,
  latest_simulation_id: "run-A",
  operations: [{ type: "RecordSettlement" }],
};
const run = {
  id: "run-A",
  proposal_id: scope.proposalId,
  ledger_id: scope.ledgerId,
  revision: 1,
  status: "COMPLETED",
  simulator_version: "lv-native-recordsettlement/0.3.0",
  outcome: {
    aggregate: "BLOCKED_INSUFFICIENT",
    verdicts: [
      { policy_id: "P-EVD-01", outcome: "INSUFFICIENT_EVIDENCE" },
      { policy_id: "P-FEE-01", outcome: "PASS" },
    ],
  },
};
test("uncertain action reload preserves exact Unicode reason, original key, version and simulation", () => {
  assert.deepEqual(readOverrideRecovery(JSON.stringify(saved), scope), saved);
});
test("recovery cannot cross tenant, actor, proposal or ledger", () => {
  for (const k of Object.keys(scope)) {
    const other = { ...scope, [k]: "other" };
    assert.equal(readOverrideRecovery(JSON.stringify(saved), other), null);
    assert.notEqual(overrideStorageKey(other), overrideStorageKey(scope));
  }
});
test("malformed recovery never supplies a replacement request", () => {
  for (const data of [
    { ...saved, schema: 2 },
    { ...saved, password: "secret" },
    { ...saved, attempt: { ...a, key: "invalid" } },
    { ...saved, attempt: { ...a, etag: '"v02"' } },
    { ...saved, attempt: { ...a, etag: '"v9007199254740992"' } },
    { ...saved, attempt: { ...a, startedAt: 0 } },
    { ...saved, reason: "x".repeat(4001) },
    { ...saved, reason: "\ud800" },
  ])
    assert.equal(readOverrideRecovery(JSON.stringify(data), scope), null);
  for (const raw of ["bad", "null", "x".repeat(50001)])
    assert.equal(readOverrideRecovery(raw, scope), null);
});
test("Unicode limits are code points and reject malformed, NUL and whitespace reasons", () => {
  assert.ok(validReason("🙂".repeat(4000)));
  for (const reason of [
    "🙂".repeat(4001),
    "",
    "\n\t \u0085",
    "\ud800",
    "\udfff",
    "text\0",
  ])
    assert.equal(validReason(reason), false);
  assert.ok(validReason(saved.reason));
});
test("23-hour retry bound and clock rollback refuse sending", () => {
  assert.ok(overrideRetryWindow(a, 10000));
  assert.ok(overrideRetryWindow(a, 10000 + 23 * 3600000 - 1));
  assert.equal(overrideRetryWindow(a, 10000 + 23 * 3600000), false);
  assert.equal(overrideRetryWindow(a, 9999), false);
});
test("candidate is narrow, maker excluded and captured run must match", () => {
  assert.ok(overrideCandidate(proposal, run, scope.userId, '"v2"'));
  assert.equal(overrideCandidate(proposal, run, "maker-A", '"v2"'), false);
  for (const change of [
    { state: "READY_FOR_REVIEW" },
    { target_adapter: "QUICKBOOKS" },
    { ledger_purpose: "BOOKS" },
    {
      operations: [{ type: "RecordSettlement" }, { type: "RecordSettlement" }],
    },
    { latest_simulation_id: "other" },
  ])
    assert.equal(
      overrideCandidate({ ...proposal, ...change }, run, scope.userId, '"v2"'),
      false,
    );
  for (const change of [
    { ledger_id: "other" },
    { proposal_id: "other" },
    { revision: 2 },
    { status: "REJECTED" },
    { simulator_version: "legacy" },
  ])
    assert.equal(
      overrideCandidate(proposal, { ...run, ...change }, scope.userId, '"v2"'),
      false,
    );
  assert.equal(overrideCandidate(proposal, run, scope.userId, '"v99"'), false);
});
test("FAIL, multiple insufficiencies and legacy aggregates never offer action", () => {
  for (const outcome of [
    { ...run.outcome, aggregate: "BLOCKED_FAIL" },
    {
      ...run.outcome,
      verdicts: [
        ...run.outcome.verdicts,
        { policy_id: "P-FEE-01", outcome: "FAIL" },
      ],
    },
    {
      ...run.outcome,
      verdicts: [
        ...run.outcome.verdicts,
        { policy_id: "P-OTHER", outcome: "INSUFFICIENT_EVIDENCE" },
      ],
    },
    { ...run.outcome, verdicts: [] },
    { ...run.outcome, verdicts: [null] },
  ])
    assert.equal(
      overrideCandidate(proposal, { ...run, outcome }, scope.userId, '"v2"'),
      false,
    );
});
test("only original revision/run/version receipt can confirm the attempt", () => {
  const receipt = { ...proposal, state: "READY_FOR_REVIEW", version: 3 };
  assert.ok(overrideReceipt(receipt, scope, a));
  for (const change of [
    { state: "WITHDRAWN" },
    { version: 4 },
    { revision: 2 },
    { latest_simulation_id: "new-run" },
    { id: "other" },
    { ledger_id: "other" },
    { created_by: scope.userId },
    { target_adapter: "QUICKBOOKS" },
  ])
    assert.equal(overrideReceipt({ ...receipt, ...change }, scope, a), false);
});
test("session failures and uncertain/malformed outcomes retain pending request", () => {
  for (const [status, code] of [
    [401, "UNAUTHENTICATED"],
    [403, "PERMISSION_DENIED"],
    [404, "NOT_FOUND"],
    [409, "REQUEST_IN_PROGRESS"],
    [422, "IDEMPOTENCY_KEY_REUSED"],
    [500, "INTERNAL_ERROR"],
    [200, ""],
    [412, "UNKNOWN"],
  ])
    assert.equal(definitiveOverrideRefusal(status, code), false);
  for (const [status, code] of [
    [412, "VERSION_MISMATCH"],
    [409, "PROPOSAL_STALE"],
    [422, "PRECONDITION_FAILED"],
    [422, "CAPABILITY_UNAVAILABLE"],
  ])
    assert.ok(definitiveOverrideRefusal(status, code));
});

test("verified role and review permission are both required; administrator has no implicit access", () => {
  assert.ok(canAuthorOverride(["CONTROLLER"], ["read", "review"]));
  for (const [roles, permissions] of [
    [["ADMINISTRATOR"], ["read", "administer"]],
    [["ANALYST"], ["read", "propose"]],
    [["CONTROLLER"], ["read"]],
    [["AUTOMATION_DEVELOPER"], ["read", "review"]],
  ])
    assert.equal(canAuthorOverride(roles, permissions), false);
});
