import { test } from "node:test";
import assert from "node:assert/strict";
import {
  replaceReferences,
  patchContent,
} from "../src/lib/draft-references.ts";
import { editableContent } from "../src/lib/draft-edit.ts";
const amount = { amount: "9999999999999999.99", currency: "INR" };
const op = {
  type: "RecordSettlement",
  schema_version: 1,
  processor_ref: "original",
  payout_id: "original",
  payout_date: "2026-10-14",
  statement_document_id: "statement-old",
  gross: { ...amount },
  fee: { amount: "0.01", currency: "INR" },
  net: { amount: "9999999999999999.98", currency: "INR" },
  items: [
    { document_id: "payment-original", kind: "PAYMENT", amount: { ...amount } },
  ],
};
const content = {
  operations: [structuredClone(op), structuredClone(op)],
  rationale: "Original rationale",
  evidence_refs: [{ kind: "SOURCE_RECORD", id: "source-original" }],
};
const statement = {
  id: "statement-new",
  ledger_id: "ledger-a",
  processor_ref: "new-processor",
  payout_id: "new-payout",
  payout_date: "2026-10-15",
  gross: { amount: "50000.00", currency: "INR" },
  fee: { amount: "1000.00", currency: "INR" },
  net: { amount: "49000.00", currency: "INR" },
};
const item = {
  document_id: "payment-new",
  kind: "PAYMENT",
  amount: { ...amount },
};
test("reference replacement preserves all financial inputs until statement adoption is explicitly chosen", () => {
  const next = replaceReferences(
    content,
    0,
    "ledger-a",
    [item],
    [],
    statement,
    false,
  );
  assert.equal(next.operations[0].statement_document_id, "statement-new");
  assert.deepEqual(next.operations[0].gross, amount);
  assert.equal(next.operations[0].processor_ref, "original");
  assert.equal(next.rationale, content.rationale);
  assert.deepEqual(next.operations[1], content.operations[1]);
  assert.equal(content.operations[0].statement_document_id, "statement-old");
  next.operations[0].items[0].amount.amount = "0";
  assert.equal(item.amount.amount, "9999999999999999.99");
});
test("explicit statement adoption copies exact source values and changes only the selected operation", () => {
  const next = replaceReferences(
    content,
    1,
    "ledger-a",
    [item],
    [],
    statement,
    true,
  );
  assert.deepEqual(next.operations[0], content.operations[0]);
  assert.equal(next.operations[1].gross.amount, "50000.00");
  assert.equal(next.operations[1].fee.amount, "1000.00");
  assert.equal(next.operations[1].processor_ref, "new-processor");
  next.operations[1].net.amount = "0";
  assert.equal(statement.net.amount, "49000.00");
});
test("patch construction strips read-only evidence metadata and clones the attempted content", () => {
  const editable = editableContent({
    ...content,
    evidence_refs: [
      {
        kind: "SOURCE_RECORD",
        id: "source-original",
        sha256: "readonly",
        provenance: "SYNTHETIC",
      },
    ],
  });
  assert.deepEqual(editable.evidence_refs, [
    { kind: "SOURCE_RECORD", id: "source-original" },
  ]);
  const patch = patchContent({
    ...editable,
    evidence_refs: [{ kind: "SOURCE_RECORD", id: "s", sha256: "readonly" }],
  });
  assert.deepEqual(patch.evidence_refs, [{ kind: "SOURCE_RECORD", id: "s" }]);
  patch.operations[0].fee.amount = "0";
  assert.equal(editable.operations[0].fee.amount, "0.01");
});
test("empty/duplicate items, cross-ledger or cross-currency statements and unknown evidence are refused locally", () => {
  assert.throws(() =>
    replaceReferences(content, 0, "ledger-a", [], [], null, false),
  );
  assert.throws(() =>
    replaceReferences(content, 0, "ledger-a", [item, item], [], null, false),
  );
  assert.throws(() =>
    replaceReferences(content, 0, "other-ledger", [item], [], statement, true),
  );
  assert.throws(() =>
    replaceReferences(
      content,
      0,
      "ledger-a",
      [{ ...item, amount: { amount: "1.00", currency: "USD" } }],
      [],
      null,
      false,
    ),
  );
  assert.throws(() =>
    patchContent({ ...content, evidence_refs: [{ kind: "UNKNOWN", id: "s" }] }),
  );
  assert.throws(() =>
    patchContent({
      ...content,
      evidence_refs: [
        { kind: "SOURCE_RECORD", id: "s" },
        { kind: "SOURCE_RECORD", id: "s" },
      ],
    }),
  );
});
test("removing an item never automatically prunes evidence used by another operation", () => {
  const next = replaceReferences(
    content,
    0,
    "ledger-a",
    [item],
    content.evidence_refs,
    null,
    false,
  );
  assert.deepEqual(next.evidence_refs, content.evidence_refs);
  assert.deepEqual(next.operations[1], content.operations[1]);
});
