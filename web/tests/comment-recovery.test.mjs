import { test } from "node:test";
import assert from "node:assert/strict";
import {
  commentStorageKey,
  readCommentRecovery,
} from "../src/lib/comment-recovery.ts";
const scope = {
  tenantId: "tenant-A",
  userId: "analyst-A",
  proposalId: "proposal-A",
};
const draft = {
  schema: 1,
  ...scope,
  body: "Exact text ₹ <img src=x>\nNo decision.",
  request: { key: "12345678-1234-1234-1234-123456789abc", version: 3 },
  blockedVersion: null,
};
test("an uncertain comment retains its exact body, original version and key", () => {
  assert.deepEqual(readCommentRecovery(JSON.stringify(draft), scope), draft);
});
test("another tenant, author or proposal cannot load a saved request", () => {
  for (const changed of [
    { tenantId: "tenant-B" },
    { userId: "controller-B" },
    { proposalId: "proposal-B" },
  ])
    assert.equal(
      readCommentRecovery(JSON.stringify(draft), { ...scope, ...changed }),
      null,
    );
});
test("malformed, oversized and invalid version metadata is rejected", () => {
  for (const raw of [
    "bad",
    JSON.stringify({ ...draft, schema: 2 }),
    JSON.stringify({ ...draft, body: "x".repeat(4001) }),
    JSON.stringify({ ...draft, request: { key: "bad", version: 0 } }),
    "x".repeat(50001),
  ])
    assert.equal(readCommentRecovery(raw, scope), null);
});
test("definitively stale drafts retain text without retaining the rejected request", () => {
  const stale = { ...draft, request: null, blockedVersion: 3 };
  assert.deepEqual(readCommentRecovery(JSON.stringify(stale), scope), stale);
});
test("storage scopes are distinct for tenant, user and proposal", () => {
  const keys = [
    scope,
    { ...scope, tenantId: "tenant-B" },
    { ...scope, userId: "controller-B" },
    { ...scope, proposalId: "proposal-B" },
  ].map(commentStorageKey);
  assert.equal(new Set(keys).size, 4);
});
