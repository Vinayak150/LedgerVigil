import { test } from "node:test";
import assert from "node:assert/strict";
import {
  availableActions,
  dayDifferenceText,
  kindLabel,
  readPendingReview,
  reviewBody,
  reviewProblem,
  reviewStorageKey,
  reviewRetryWindowMs,
  validNote,
} from "../src/lib/detection-review.ts";

const scope = { tenantId: "t1", userId: "u1", findingId: "f1" };
const pending = {
  schema: 1,
  ...scope,
  key: "0f8e3a6c-1b2d-4c5e-8f90-123456789abc",
  etag: "v2",
  action: "DISMISSED",
  reason: "EXPECTED_TIMING",
  note: "",
  startedAt: 1_000_000,
};

test("labels use the agreed wording and never fraud or error language", () => {
  assert.equal(
    kindLabel("PAYMENT_CLAIMED_BY_OTHER_PAYOUT"),
    "Potential overlapping claim",
  );
  assert.equal(kindLabel("PAYOUT_DATE_DIFFERS"), "Date difference");
});

test("day difference text is exact and signed, statement minus recorded", () => {
  assert.equal(
    dayDifferenceText(-2),
    "The statement date is 2 calendar days earlier than the recorded date",
  );
  assert.equal(
    dayDifferenceText(1),
    "The statement date is 1 calendar day later than the recorded date",
  );
  assert.equal(dayDifferenceText(0), "Same calendar date");
  assert.equal(dayDifferenceText(Number.NaN), "Difference not available");
});

test("actions follow the API: confirm only on current inputs, reopen only after a decision, none without permission", () => {
  assert.deepEqual(
    availableActions("OPEN", "CURRENT", true).map((a) => [a.action, a.enabled]),
    [
      ["CONFIRMED", true],
      ["DISMISSED", true],
    ],
  );
  const stale = availableActions("OPEN", "STALE", true);
  assert.equal(stale[0].enabled, false);
  assert.match(stale[0].why, /Inputs changed/);
  assert.deepEqual(
    availableActions("DISMISSED", "STALE", true).map((a) => [
      a.action,
      a.reason,
    ]),
    [["REOPENED", "REVIEW_CORRECTION"]],
  );
  assert.deepEqual(availableActions("OPEN", "CURRENT", false), []);
});

test("pending decision is bound to tenant, user and finding, expires, and resends the same body", () => {
  const raw = JSON.stringify(pending);
  assert.deepEqual(readPendingReview(raw, scope, 1_000_000 + 1000), pending);
  assert.equal(
    readPendingReview(raw, { ...scope, tenantId: "t2" }, 1_000_001),
    null,
  );
  assert.equal(
    readPendingReview(raw, { ...scope, userId: "u2" }, 1_000_001),
    null,
  );
  assert.equal(
    readPendingReview(raw, scope, 1_000_000 + reviewRetryWindowMs + 1),
    null,
  );
  assert.equal(
    readPendingReview(
      JSON.stringify({ ...pending, extra: 1 }),
      scope,
      1_000_001,
    ),
    null,
  );
  assert.equal(
    readPendingReview(
      JSON.stringify({ ...pending, key: "not-a-uuid" }),
      scope,
      1_000_001,
    ),
    null,
  );
  assert.equal(readPendingReview("{", scope), null);
  assert.deepEqual(reviewBody(pending), {
    action: "DISMISSED",
    reason_code: "EXPECTED_TIMING",
  });
  assert.deepEqual(reviewBody({ ...pending, note: "x" }), {
    action: "DISMISSED",
    reason_code: "EXPECTED_TIMING",
    note: "x",
  });
  assert.equal(reviewStorageKey(scope), "lv.detection-review.1:t1:u1:f1");
});

test("notes and problems", () => {
  assert.equal(validNote(""), true);
  assert.equal(validNote("   "), false);
  assert.equal(validNote("a".repeat(2001)), false);
  assert.match(reviewProblem(409, "FINDING_STALE"), /Inputs changed/);
  assert.match(reviewProblem(412, "VERSION_MISMATCH"), /Someone else updated/);
});
