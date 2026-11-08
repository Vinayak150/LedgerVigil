import { test } from "node:test";
import assert from "node:assert/strict";
import { parseFragmentToken } from "../src/lib/account-token.ts";

test("account tokens are read only from a well-formed fragment", () => {
  const token = "A".repeat(43);
  assert.equal(parseFragmentToken(`#token=${token}`), token);
  assert.equal(parseFragmentToken(`token=${token}`), token);
  assert.equal(parseFragmentToken("#token=short"), null);
  assert.equal(
    parseFragmentToken(`#token=${token}&token=${"B".repeat(43)}`),
    token,
  );
  assert.equal(parseFragmentToken("#token=" + "A".repeat(42) + "!"), null);
  assert.equal(parseFragmentToken(""), null);
});
