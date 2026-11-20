/**
 * Original ↔ output time (P0-1, §19.31.5).
 *
 * The bug this pins is silent: the video preview read the playhead — an **original**-timeline
 * position — as if it were **output** time. On a project with 1.2 s of deletions the still
 * showed a frame from 1.2 s later than the playhead, which looks like a slightly wrong
 * frame, not like an error. And it never showed on the sample project, which has no
 * deletions at all.
 *
 * So the assertions below are about *direction* and *offsets*, not just round-trips: a
 * round-trip passes even when both directions are swapped.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { timeMapFrom, toOriginal, toOutput, toOutputClamped } from "./timeMap.ts";

/** One 1.2 s cut at 10 s, like the three real projects the bug was measured on. */
const CUT_1_2S = [
  { orig_start: 0, orig_end: 10_000, new_start: 0 },
  { orig_start: 11_200, orig_end: 60_000, new_start: 10_000 },
];

test("no keeps maps identically", () => {
  const map = timeMapFrom(null);
  assert.equal(toOutput(map, 12_345), 12_345);
  assert.equal(toOutputClamped(map, 12_345), 12_345);
});

test("with no deletions the two clocks agree", () => {
  const map = timeMapFrom([{ orig_start: 0, orig_end: 60_000, new_start: 0 }]);
  for (const ms of [0, 1, 30_000, 59_999]) {
    assert.equal(toOutput(map, ms), ms);
  }
});

test("an instant after a cut is pulled back by the amount cut before it", () => {
  const map = timeMapFrom(CUT_1_2S);
  // 20 s on the original timeline is 20 − 1.2 = 18.8 s into the film. Forgetting this is
  // exactly the bug: the preview rendered 20 s of output, i.e. 1.2 s of content too late.
  assert.equal(toOutput(map, 20_000), 18_800);
  // 59 999 is the last millisecond of the film; the whole 1.2 s is gone before it.
  assert.equal(toOutput(map, 59_999), 58_799);
});

test("an instant inside a deletion has no output position", () => {
  const map = timeMapFrom(CUT_1_2S);
  assert.equal(toOutput(map, 10_000), null);
  assert.equal(toOutput(map, 10_500), null);
  assert.equal(toOutput(map, 11_199), null);
});

test("the clamped form snaps forward to the next surviving instant", () => {
  const map = timeMapFrom(CUT_1_2S);
  // The playhead can legitimately sit inside a cut (a seek that landed there), and the
  // panel must still draw something — the same place the audio would resume.
  assert.equal(toOutputClamped(map, 10_000), 10_000);
  assert.equal(toOutputClamped(map, 10_500), 10_000);
  assert.equal(toOutputClamped(map, 11_199), 10_000);
});

test("a keep boundary is half-open, not closed", () => {
  const map = timeMapFrom([
    { orig_start: 0, orig_end: 10_000, new_start: 0 },
    { orig_start: 10_000, orig_end: 20_000, new_start: 10_000 },
  ]);
  // The instant 10 000 belongs to the second keep, which starts at output 10 000 — the same
  // number here, but it must not be reported as "inside the first keep" either.
  assert.equal(toOutput(map, 10_000), 10_000);
  assert.equal(toOutput(map, 9_999), 9_999);
});

test("output maps back to the original instant", () => {
  const map = timeMapFrom(CUT_1_2S);
  assert.equal(toOriginal(map, 18_800), 20_000);
  assert.equal(toOriginal(map, 0), 0);
  // Past the end of the film: there is no original instant for it.
  assert.equal(toOriginal(map, 58_800), null);
});

test("several cuts accumulate", () => {
  const map = timeMapFrom([
    { orig_start: 0, orig_end: 5_000, new_start: 0 },
    { orig_start: 6_000, orig_end: 10_000, new_start: 5_000 },
    { orig_start: 12_000, orig_end: 20_000, new_start: 9_000 },
  ]);
  // Two cuts before the last instant of the film: 1.0 s at 5 s and 2.0 s at 10 s.
  // 19 999 rather than 20 000 because the keeps are half-open — 20 000 is the film's end.
  assert.equal(toOutput(map, 19_999), 16_999);
  assert.equal(toOutput(map, 12_000), 9_000);
  assert.equal(toOutput(map, 11_000), null);
  assert.equal(toOutputClamped(map, 11_000), 9_000);
});

test("unsorted keeps are sorted before use", () => {
  const map = timeMapFrom([CUT_1_2S[1], CUT_1_2S[0]]);
  assert.equal(toOutput(map, 20_000), 18_800);
});

test("a playhead past the last keep clamps to the film's end", () => {
  const map = timeMapFrom(CUT_1_2S);
  // 60 000 is the original duration and therefore outside every keep; the honest answer is
  // the end of the film, not "no preview".
  assert.equal(toOutputClamped(map, 60_000), 58_800);
  assert.equal(toOutputClamped(map, 99_999), 58_800);
});
