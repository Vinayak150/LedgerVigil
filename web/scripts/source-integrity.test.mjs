import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { checkSourceText } from "../src/lib/source-integrity.ts";
const hash = (text) => createHash("sha256").update(text, "utf8").digest("hex");
test("source digest agrees with an independent UTF-8 hash including Unicode and literal markup", async () => {
  for (const text of [
    '{"amount":"9999999999999999.99"}',
    "मराठी ₹\n<script>literal only</script>",
    "a\r\nb",
    "e\u0301",
  ])
    assert.equal(
      await checkSourceText(text, hash(text), Buffer.byteLength(text, "utf8")),
      "MATCH",
    );
});
test("changed content, incorrect byte length, Unicode normalization and newline changes never pass", async () => {
  assert.equal(
    await checkSourceText("changed", hash("original"), 7),
    "MISMATCH",
  );
  assert.equal(await checkSourceText("₹", hash("₹"), 1), "MISMATCH");
  assert.equal(
    await checkSourceText("é", hash("e\u0301"), Buffer.byteLength("é")),
    "MISMATCH",
  );
  assert.equal(await checkSourceText("a\nb", hash("a\r\nb"), 3), "MISMATCH");
});
test("missing or malformed integrity metadata remains unavailable", async () => {
  for (const expected of [null, undefined, "not-a-hash"])
    assert.equal(await checkSourceText("text", expected, 4), "UNAVAILABLE");
  assert.equal(await checkSourceText("text", hash("text"), -1), "UNAVAILABLE");
});
