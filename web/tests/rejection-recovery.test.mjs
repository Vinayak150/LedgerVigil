import { test } from "node:test";
import assert from "node:assert/strict";
import {
  rejectionStorageKey,
  readRejectionRecovery,
  validRejectionComment,
  canStartRejection,
  rejectionRequestBody,
  rejectionReceipt,
  rejectionRetryWindow,
} from "../src/lib/rejection-recovery.ts";
import {
  approvalStorageKey,
  readApprovalRecovery,
} from "../src/lib/approval-recovery.ts";
import { peerReviewBlocked } from "../src/lib/review-interlock.ts";
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
  decision: "REJECT",
  ...scope,
  comment: " Exact ₹ <img src=x>\nMissing bank confirmation. ",
  attempt,
  blockedVersion: null,
};
const p = {
  id: scope.proposalId,
  ledger_id: scope.ledgerId,
  created_by: "maker",
  editors: ["maker", "editor"],
  state: "READY_FOR_REVIEW",
  revision: 1,
  version: 3,
  ledger_purpose: "SANDBOX",
  target_adapter: "NATIVE",
  operations: [{ type: "RecordSettlement" }],
  latest_simulation_id: attempt.simulationId,
  allowed_actions: ["REJECT"],
};
const run = {
  id: attempt.simulationId,
  proposal_id: p.id,
  ledger_id: p.ledger_id,
  revision: 1,
  status: "COMPLETED",
  simulator_version: "lv-native-recordsettlement/0.3.0",
  input_hash: attempt.inputHash,
  result_hash: attempt.resultHash,
  outcome: { aggregate: "BLOCKED_INSUFFICIENT" },
};
test("approval and rejection use separate scoped namespaces and rejection carries an explicit decision", () => {
  assert.notEqual(rejectionStorageKey(scope), approvalStorageKey(scope));
  assert.deepEqual(readRejectionRecovery(JSON.stringify(saved), scope), saved);
  for (const k of Object.keys(scope))
    assert.equal(
      readRejectionRecovery(JSON.stringify(saved), { ...scope, [k]: "other" }),
      null,
    );
  const { decision, ...approval } = saved;
  assert.equal(readRejectionRecovery(JSON.stringify(approval), scope), null);
  assert.equal(readApprovalRecovery(JSON.stringify(saved), scope), null);
});
test("required reason matches server Unicode whitespace rules and preserves exact text", () => {
  for (const s of [
    "",
    " \n\t",
    "\u001c\u0085",
    "\u00a0\u3000",
    "\u2000\u202f",
    "\0",
    "\ud800",
    "x".repeat(4001),
  ])
    assert.equal(validRejectionComment(s), false, JSON.stringify(s));
  for (const s of [saved.comment, "😀".repeat(4000), "\ufeff"])
    assert.equal(validRejectionComment(s), true);
  assert.deepEqual(rejectionRequestBody(saved), {
    revision: 1,
    comment: saved.comment,
  });
});
test("empty draft reason may recover but an empty original attempt never recovers or sends", () => {
  assert.ok(
    readRejectionRecovery(
      JSON.stringify({ ...saved, comment: "", attempt: null }),
      scope,
    ),
  );
  assert.equal(
    readRejectionRecovery(JSON.stringify({ ...saved, comment: "" }), scope),
    null,
  );
  assert.throws(() => rejectionRequestBody({ ...saved, comment: "" }));
  assert.throws(() => rejectionRequestBody({ ...saved, decision: "APPROVE" }));
});
test("corrupt or widened recovery records fail closed", () => {
  for (const raw of [
    "{",
    "null",
    JSON.stringify({ ...saved, extra: true }),
    JSON.stringify({ ...saved, decision: "APPROVE" }),
    JSON.stringify({ ...saved, attempt: { ...attempt, extra: true } }),
    JSON.stringify({ ...saved, attempt: { ...attempt, etag: "v3" } }),
    JSON.stringify({ ...saved, attempt: { ...attempt, inputHash: "bad" } }),
  ])
    assert.equal(readRejectionRecovery(raw, scope), null);
});
test("REJECT hint is mandatory and independent from APPROVE hint", () => {
  assert.ok(canStartRejection(p, run, scope.userId, '"v3"'));
  assert.equal(
    canStartRejection(
      { ...p, allowed_actions: ["APPROVE"] },
      run,
      scope.userId,
      '"v3"',
    ),
    false,
  );
  for (const actor of ["maker", "editor"])
    assert.equal(canStartRejection(p, run, actor, '"v3"'), false);
});
test("rejection target refuses changed head, run, scope, profile and failed policy result", () => {
  for (const x of [
    { state: "APPROVED" },
    { state: "REJECTED" },
    { target_adapter: "QUICKBOOKS" },
    { ledger_purpose: "BOOKS" },
    { revision: 2 },
    { latest_simulation_id: "other" },
  ])
    assert.equal(
      canStartRejection({ ...p, ...x }, run, scope.userId, '"v3"'),
      false,
    );
  for (const x of [
    { id: "other" },
    { ledger_id: "other" },
    { revision: 2 },
    { status: "REJECTED" },
    { simulator_version: "old" },
    { result_hash: "bad" },
    { outcome: { aggregate: "BLOCKED_FAIL" } },
  ])
    assert.equal(
      canStartRejection(p, { ...run, ...x }, scope.userId, '"v3"'),
      false,
    );
  assert.equal(canStartRejection(p, run, scope.userId, '"v4"'), false);
});
test("only an original REJECTED receipt confirms rejection; approval never substitutes for it", () => {
  const result = { ...p, state: "REJECTED", version: 4 };
  assert.ok(rejectionReceipt(result, scope, attempt));
  for (const x of [
    { state: "APPROVED" },
    { state: "STALE" },
    { version: 5 },
    { revision: 2 },
    { id: "other" },
    { ledger_id: "other" },
    { latest_simulation_id: "other" },
    { created_by: scope.userId },
    { editors: [scope.userId] },
  ])
    assert.equal(rejectionReceipt({ ...result, ...x }, scope, attempt), false);
});
test("original retry window refuses expiry and backward clock", () => {
  assert.ok(rejectionRetryWindow(attempt, 10001));
  assert.equal(rejectionRetryWindow(attempt, 9999), false);
  assert.equal(rejectionRetryWindow(attempt, 10000 + 23 * 3600000), false);
});
test("a peer pending/corrupt/blocked record prevents new opposite decisions", () => {
  const { decision, ...approval } = saved;
  for (const [peer, record] of [
    ["rejection", saved],
    ["approval", approval],
  ]) {
    assert.ok(peerReviewBlocked(JSON.stringify(record), peer, scope, 3));
    assert.ok(peerReviewBlocked("{", peer, scope, 3));
    assert.ok(
      peerReviewBlocked(
        JSON.stringify({ ...record, attempt: null, blockedVersion: 3 }),
        peer,
        scope,
        3,
      ),
    );
    assert.equal(
      peerReviewBlocked(
        JSON.stringify({ ...record, attempt: null, blockedVersion: 2 }),
        peer,
        scope,
        3,
      ),
      false,
    );
    assert.equal(
      peerReviewBlocked(
        JSON.stringify({ ...record, attempt: null }),
        peer,
        scope,
        3,
      ),
      false,
    );
    assert.equal(peerReviewBlocked(null, peer, scope, 3), false);
  }
});
