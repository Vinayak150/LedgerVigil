import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { checkProofDownload, PROOF_LIMIT } from "../src/lib/execution-proof.ts";
const text = readFileSync(
    new URL("./fixtures/execution-proof.json", import.meta.url),
    "utf8",
  ),
  b = JSON.parse(text),
  s = b.scope,
  target = {
    tenantId: s.tenant_id,
    ledgerId: s.ledger_id,
    proposalId: s.proposal_id,
    revision: s.revision,
    executionId: s.execution_id,
    simulationId: s.simulation_id,
    commitSeq: s.commit_seq,
  };
const sha = (t) => createHash("sha256").update(t).digest("hex");
const canonical = (v) =>
  typeof v === "string"
    ? JSON.stringify(v).replace(
        /[\u007f-\uffff]/g,
        (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
      )
    : Array.isArray(v)
      ? "[" + v.map(canonical).join(",") + "]"
      : v && typeof v === "object"
        ? "{" +
          Object.keys(v)
            .sort()
            .map((k) => canonical(k) + ":" + canonical(v[k]))
            .join(",") +
          "}"
        : JSON.stringify(v);
test("real native proof with Unicode revision retains exact downloadable bytes", async () => {
  assert.match(b.artifacts["revision.json"].rationale, /₹ café 🚀/);
  const r = await checkProofDownload(text, sha(text), target);
  assert.equal(r.digest, sha(text));
  assert.equal(new TextDecoder().decode(r.bytes), text);
  assert.equal(r.filename, `ledgervigil-execution-${target.executionId}.json`);
});
test("every receipt scope field is required before download", async () => {
  for (const k of Object.keys(target))
    await assert.rejects(
      checkProofDownload(text, sha(text), {
        ...target,
        [k]: k === "revision" ? 2 : "other",
      }),
    );
});
test("missing or altered digest, incomplete and noncanonical JSON are rejected", async () => {
  for (const digest of [null, "", "0".repeat(64), "x".repeat(64)])
    await assert.rejects(checkProofDownload(text, digest, target));
  for (const raw of [
    "{",
    "{}",
    JSON.stringify(b, null, 2),
    text.replace('"format":', '"format":"forged","format":'),
  ])
    await assert.rejects(checkProofDownload(raw, sha(raw), target));
});
test("artifact tampering fails even if response byte digest is recomputed", async () => {
  const value = structuredClone(b);
  value.artifacts["journal.json"].lines[0].amount = "1.0000";
  const raw = canonical(value) + "\n";
  await assert.rejects(checkProofDownload(raw, sha(raw), target), /manifest/);
});
test("missing artifact or changed scope fails with a newly computed HTTP digest", async () => {
  for (const mutate of [
    (v) => delete v.artifacts["approval.json"],
    (v) => (v.scope.tenant_id = "other"),
  ]) {
    const value = structuredClone(b);
    mutate(value);
    const raw = canonical(value) + "\n";
    await assert.rejects(checkProofDownload(raw, sha(raw), target));
  }
});
test("unsigned UNREVIEWED claims cannot be upgraded by a rewritten response", async () => {
  const value = structuredClone(b);
  value.claims.signature = "VERIFIED";
  const raw = canonical(value) + "\n";
  await assert.rejects(checkProofDownload(raw, sha(raw), target), /claims/);
});
test("oversized payloads are refused before creating a file", async () => {
  await assert.rejects(
    checkProofDownload("x".repeat(PROOF_LIMIT + 1), "a".repeat(64), target),
  );
});
