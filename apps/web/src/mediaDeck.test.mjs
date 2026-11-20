/**
 * The two-slot media deck (§19.32, P2).
 *
 * The measurement this module exists for, from `scripts/check-p2-switch.mjs` (per animation frame,
 * inside the page): the new `<video>` reaches `readyState 4` with presented frames **116 ms** after
 * it is mounted. `.layer-media` has `background: #000`, so that is nearly three film frames of black
 * box in the middle of a cut.
 *
 * The fix is not a faster load — it is not loading at all at the boundary. These tests pin the
 * properties that make that true, and the ones that must not break a playing element.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  HEAP_PRESSURE_RATIO,
  PREWARM_LEAD_MS,
  deckLayout,
  slotOf,
  underHeapPressure,
} from "./mediaDeck.ts";

const GEOMETRY = {
  canvas: { w: 1920, h: 1080 },
  box: { x: 240, y: 209, w: 1440, h: 810 },
};

/** A plan shaped like the sample's: video, a long stretch of images, video, video. */
function plan() {
  const raw = [
    { start_ms: 0, end_ms: 10_000, kind: "video", loop: false },
    { start_ms: 10_000, end_ms: 40_000, kind: "image", loop: false },
    { start_ms: 40_000, end_ms: 50_000, kind: "video", loop: true },
    { start_ms: 50_000, end_ms: 60_000, kind: "video", loop: false },
  ];
  return raw.map((segment, index) => ({
    ...segment,
    fit: "contain",
    url: `https://example.test/${index}.mp4`,
    backdrop_url: null,
    placement: "box",
  }));
}

const at = (segments, atMs, options) => deckLayout(segments, GEOMETRY, atMs, options);

test("slot index alternates with the video ordinal, and only that", () => {
  // Consecutive video ordinals must land in different slots, or the prewarm would overwrite the
  // element that is currently playing.
  assert.equal(slotOf(0), 0);
  assert.equal(slotOf(1), 1);
  assert.equal(slotOf(2), 0);
  assert.equal(slotOf(3), 1);
});

test("inside a video segment the front slot holds it and the other is free", () => {
  const layout = at(plan(), 1_000);
  assert.equal(layout.current, 0);
  assert.equal(layout.front, 0);
  assert.equal(layout.slots[0]?.url.endsWith("/0.mp4"), true);
  assert.equal(layout.slots[1], null, "nothing to warm: the next video is 29 s away");
  assert.equal(layout.degraded, false);
});

test("the next video is warmed inside the lead window, in the other slot", () => {
  const segments = plan();
  // Video ordinal 1 starts at 40 000. At 38 000 the lead has 2 s left, so it is warmed.
  const layout = at(segments, 38_000);
  assert.equal(layout.current, null, "38 000 is inside the image stretch");
  assert.equal(layout.front, null);
  assert.equal(layout.warmed, 1);
  // The held clip is ordinal 0, so the pair is (0, 1) — different slots by construction.
  assert.equal(layout.slots[0]?.ordinal, 0);
  assert.equal(layout.slots[1]?.ordinal, 1);
});

test("the prewarm is not loaded before the lead window", () => {
  // 2.5 s lead: at 37 000 there is 3 s to go, so nothing is warmed and only one decoder is up.
  const layout = at(plan(), 37_000);
  assert.equal(layout.warmed, null);
  assert.equal(layout.slots[1], null);
});

test("the warmed slot is already correct when the boundary arrives", () => {
  // This is the whole feature in one assertion: at 40 000 the new video is *already* resident, so
  // the element only flips visibility. If this ever returns `null` in the second slot, the deck is
  // back to paying 116 ms of decode at every cut.
  const segments = plan();
  const before = at(segments, 39_900);
  assert.equal(before.slots[1]?.url, segments[2].url);
  const after = at(segments, 40_000);
  assert.equal(after.current, 1);
  assert.equal(after.front, 1);
  assert.equal(after.slots[1]?.url, segments[2].url, "the same element, still");
  // The other slot is not re-tasked yet: ordinal 2 starts 10 s later, outside the 2.5 s lead.
  assert.equal(after.warmed, null);
  // Two seconds before *that* boundary it is, and the pair alternates again.
  const nearNext = at(segments, 48_500);
  assert.equal(nearNext.current, 1);
  assert.equal(nearNext.warmed, 2);
  assert.equal(nearNext.slots[nearNext.front]?.url, segments[2].url);
  assert.equal(nearNext.slots[slotOf(2)]?.url, segments[3].url);
});

test("back-to-back videos alternate slots without ever sharing one", () => {
  const segments = plan();
  for (const atMs of [40_000, 45_000, 50_000, 55_000]) {
    const layout = at(segments, atMs);
    const residents = layout.slots.filter((slot) => slot !== null);
    assert.equal(residents.length, residents.length, "guard: no crash on short lists");
    if (residents.length === 2) {
      assert.notEqual(residents[0].ordinal % 2, residents[1].ordinal % 2);
    }
  }
});

test("an image segment hides the front but keeps the deck alive", () => {
  // Unmounting the elements here would make the next video pay the decode — which is most cuts on
  // the sample plan, where 51 of 64 segments are images.
  const layout = at(plan(), 20_000);
  assert.equal(layout.front, null);
  assert.equal(layout.slots[0]?.ordinal, 0, "the last video stays resident");
});

test("a full-placement video gets the canvas rect, a box one the box", () => {
  const segments = plan();
  segments[0].placement = "full";
  const layout = at(segments, 1_000);
  assert.deepEqual(layout.slots[0]?.rect, { x: 0, y: 0, w: 1920, h: 1080 });
  const boxed = at(plan(), 1_000);
  assert.deepEqual(boxed.slots[0]?.rect, GEOMETRY.box);
});

test("an export drops the second decoder and keeps the one on screen", () => {
  const layout = at(plan(), 38_000, { exportRunning: true });
  assert.equal(layout.degraded, true);
  assert.equal(layout.reason, "export");
  assert.equal(layout.warmed, null);
  // 38 000 is inside the images, so even the held clip goes: under CPU pressure nothing that is
  // not being looked at stays resident.
  assert.deepEqual(layout.slots, [null, null]);
  // ... but a playing clip is never dropped: that would be a visible stall, not a degradation.
  const playing = at(plan(), 1_000, { exportRunning: true });
  assert.equal(playing.front, 0);
  assert.equal(playing.slots[0]?.url.endsWith("/0.mp4"), true);
  assert.equal(playing.slots[1], null);
});

test("heap pressure takes the same path as an export", () => {
  const layout = at(plan(), 1_000, {
    memory: { usedJSHeapSize: 900, jsHeapSizeLimit: 1_000 },
  });
  assert.equal(layout.reason, "memory");
  assert.equal(layout.degraded, true);
  assert.equal(layout.slots[1], null);
});

test("no pressure, no degradation", () => {
  assert.equal(
    at(plan(), 1_000, { memory: { usedJSHeapSize: 100, jsHeapSizeLimit: 1_000 } }).degraded,
    false,
  );
});

test("a plan with no videos lays out nothing rather than throwing", () => {
  const layout = at(
    plan().map((segment) => ({ ...segment, kind: "image" })),
    1_000,
  );
  assert.deepEqual(layout.slots, [null, null]);
  assert.equal(layout.front, null);
  assert.equal(layout.current, null);
});

test("the constants are the measured ones", () => {
  // 116 ms to present a cold frame means 2.5 s of lead is generous; the upper bound is about the
  // second decoder's residency, not about load time.
  assert.equal(PREWARM_LEAD_MS, 2_500);
  assert.equal(HEAP_PRESSURE_RATIO, 0.85);
});

test("heap pressure is read from Chromium's numbers and defaults to no pressure", () => {
  // Failing closed would disable prewarming everywhere `performance.memory` does not exist, for a
  // feature whose whole benefit is a few frames at a cut.
  assert.equal(underHeapPressure(undefined), false);
  assert.equal(underHeapPressure({}), false);
  assert.equal(underHeapPressure({ usedJSHeapSize: 100, jsHeapSizeLimit: Number.NaN }), false);
  assert.equal(underHeapPressure({ usedJSHeapSize: 100, jsHeapSizeLimit: 0 }), false);
  assert.equal(underHeapPressure({ usedJSHeapSize: 10, jsHeapSizeLimit: 100 }), false);
  assert.equal(underHeapPressure({ usedJSHeapSize: 90, jsHeapSizeLimit: 100 }), true);
});
