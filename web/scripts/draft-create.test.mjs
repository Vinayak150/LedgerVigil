import { test } from "node:test";
import assert from "node:assert/strict";
import { createBody, evidenceKey } from "../src/lib/draft-create.ts";
const ledger = { id: "ledger-a", purpose: "SANDBOX", source: "NATIVE" };
const statement = {
  id: "statement",
  ledger_id: "ledger-a",
  processor_ref: "processor-1",
  payout_id: "payout-1",
  payout_date: "2026-10-14",
  gross: { amount: "9999999999999999.99", currency: "INR" },
  fee: { amount: "0.01", currency: "INR" },
  net: { amount: "9999999999999999.98", currency: "INR" },
};
const payment = {
  id: "payment",
  ledger_id: "ledger-a",
  type: "PAYMENT",
  status: "SETTLED",
  amount: { amount: "9999999999999999.99", currency: "INR" },
};
test("source selection yields exact full amounts, immutable request snapshot, and kind/id evidence only", () => {
  const ref = {
    kind: "SOURCE_RECORD",
    id: "source",
    sha256: "readonly",
    provenance: "SYNTHETIC",
  };
  const body = createBody(
    ledger,
    statement,
    [payment],
    [ref, ref],
    "Review source",
  );
  assert.equal(body.operations[0].gross.amount, "9999999999999999.99");
  assert.equal(
    body.operations[0].items[0].amount.amount,
    "9999999999999999.99",
  );
  assert.deepEqual(body.evidence_refs, [
    { kind: "SOURCE_RECORD", id: "source" },
  ]);
  body.operations[0].gross.amount = "0";
  assert.equal(statement.gross.amount, "9999999999999999.99");
  body.operations[0].items[0].amount.amount = "0";
  assert.equal(payment.amount.amount, "9999999999999999.99");
  assert.equal(evidenceKey(ref), "SOURCE_RECORD:source");
});
test("cross-ledger, duplicate, missing amounts and non-native/non-sandbox inputs never form a request", () => {
  for (const [l, s, p] of [
    [{ ...ledger, purpose: "BOOKS" }, statement, [payment]],
    [{ ...ledger, source: "QUICKBOOKS" }, statement, [payment]],
    [ledger, { ...statement, ledger_id: "other" }, [payment]],
    [ledger, statement, [{ ...payment, ledger_id: "other" }]],
    [ledger, statement, [payment, payment]],
    [ledger, statement, []],
    [ledger, statement, [{ ...payment, amount: null }]],
    [
      ledger,
      statement,
      [{ ...payment, amount: { amount: "1.00", currency: "USD" } }],
    ],
  ])
    assert.throws(() => createBody(l, s, p, [], ""));
});
test("settled status is preserved as a warning, not silently redefined as draft ineligibility", () => {
  const body = createBody(ledger, statement, [payment], [], "");
  assert.equal(body.operations[0].items[0].document_id, payment.id);
  assert.equal(payment.status, "SETTLED");
});
