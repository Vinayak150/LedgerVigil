import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { assertGroup, groupLabel, planError } from "../src/lib/public-plan.ts";
const real = JSON.parse(
  readFileSync(new URL("./fixtures/held-public-invoice.json", import.meta.url)),
);
test("real held invoice 489435 stays held with no monetary inference", () => {
  assertGroup(real);
  assert.equal(groupLabel(real), "Invoice 489435");
  assert.equal(real.amount, null);
  assert.equal(real.source_line_count, 4);
});
test("a monetary value attached to a held invoice is refused", () => {
  assert.throws(() =>
    assertGroup({ ...real, amount: { amount: "10.20", currency: "GBP" } }),
  );
});
test("a changed accounting or posting gate is refused", () => {
  for (const patch of [
    { posting_eligibility: "AVAILABLE" },
    { accounting_status: "APPROVED" },
    { status: "READY" },
  ])
    assert.throws(() => assertGroup({ ...real, ...patch }));
});
test("missing invoice identities retain their source row location", () => {
  assert.equal(
    groupLabel({
      ...real,
      source_identity: ["missing-invoice", "Year 2009-2010", 10],
    }),
    "Missing invoice · Year 2009-2010 · 10",
  );
});
test("unavailable storage is not represented as an empty dataset", () => {
  assert.match(planError(503).message, /index is unavailable/);
  assert.match(planError(503, true).message, /registration’s result/);
  assert.doesNotMatch(planError(503, true).message, /index is unavailable/);
  assert.match(planError(404).message, /unavailable in your organization/);
  assert.match(planError(401).message, /session has ended/);
});
