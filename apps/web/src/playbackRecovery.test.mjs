/**
 * Tests for the playback recovery ladder.
 *
 * The bug these guard: a sentence whose start the browser cannot decode made the
 * transport flip to 「暂停」 and stay silent forever, because `void audio.play()`
 * swallowed the rejection and nothing retried. The ladder itself is pure, so the
 * failure mode worth pinning is arithmetic — "we nudge but forget to stop" or
 * "we nudge in the wrong direction" are both invisible to the type checker.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  MEDIA_ERROR,
  RECOVERY_OFFSETS_MS,
  describeMediaError,
  recoveryTarget,
} from "./playbackRecovery.ts";

test("the first retry is a small nudge, not a jump", () => {
  const first = recoveryTarget(215_180, 0);
  assert.equal(first, 215_210);
  // Under 50ms matters: the word the user asked to hear must still be there.
  assert.ok(first - 215_180 <= 50);
});

test("retries get progressively further but stay bounded", () => {
  const targets = RECOVERY_OFFSETS_MS.map((_, i) => recoveryTarget(1000, i));
  assert.deepEqual(targets, [1030, 1060, 1120, 1240, 1400]);
  for (let i = 1; i < targets.length; i += 1) {
    assert.ok(targets[i] > targets[i - 1], "each attempt must move forward");
  }
  // 400ms is the ceiling: beyond that we would be eating the sentence itself.
  assert.ok(targets[targets.length - 1] - 1000 <= 400);
});

test("the ladder ends instead of looping", () => {
  assert.equal(recoveryTarget(1000, RECOVERY_OFFSETS_MS.length), null);
  assert.equal(recoveryTarget(1000, 99), null);
});

test("a negative attempt is rejected rather than treated as the first nudge", () => {
  // Guarding this matters: `attempt - 1` reaches here, and -1 silently indexing
  // `undefined` would return NaN, which assigns NaN to currentTime.
  assert.equal(recoveryTarget(1000, -1), null);
});

test("recovery never moves backwards", () => {
  // Moving backwards would replay audio the user already heard, and on the file
  // that motivated this the failure window was *entered* from the front.
  for (let attempt = 0; attempt < RECOVERY_OFFSETS_MS.length; attempt += 1) {
    const target = recoveryTarget(5000, attempt);
    assert.ok(target !== null && target > 5000);
  }
});

test("every media error code has a Chinese explanation", () => {
  for (const code of [1, 2, 3, 4, 0, 99]) {
    const text = describeMediaError(code);
    assert.equal(typeof text, "string");
    assert.ok(text.length > 0);
    assert.ok(/[\u4e00-\u9fa5]/.test(text), `code ${code} should be explained in Chinese`);
  }
});

test("decode failures are named as such, not lumped with network errors", () => {
  // The distinction is what decides whether the ladder runs at all.
  assert.notEqual(
    describeMediaError(MEDIA_ERROR.DECODE),
    describeMediaError(MEDIA_ERROR.NETWORK),
  );
  assert.match(describeMediaError(MEDIA_ERROR.DECODE), /解码/);
});
