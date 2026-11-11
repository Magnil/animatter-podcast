/**
 * Zoom/scroll arithmetic for the waveform.
 *
 * Pure functions on purpose: zoom bugs are arithmetic bugs. An off-by-one in the
 * clamp lets the window drift past the end and the canvas goes blank; an anchor
 * that uses the wrong edge makes the waveform appear to slide sideways instead
 * of zooming where the pointer is. Both are cheap to pin down here and
 * miserable to debug inside a canvas draw call.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  followView,
  MIN_VISIBLE_MS,
  clampView,
  hitEdge,
  initialView,
  shouldRememberView,
  isZoomed,
  msToX,
  panTo,
  scrollFraction,
  xToMs,
  zoomAt,
} from "./waveView.ts";

// --- playhead follow (shared by both timeline tracks) -----------------

test("followView leaves a window that already contains the playhead alone", () => {
  const view = { from: 10_000, to: 20_000 };
  assert.deepEqual(followView(view, 60_000, 15_000), view);
});

test("followView puts a playhead that ran off the right edge 30% from the left", () => {
  const view = { from: 10_000, to: 20_000 };
  const next = followView(view, 60_000, 25_000);
  assert.equal(next.to - next.from, 10_000);
  assert.equal(next.from, 25_000 - 3_000);
});

test("followView clamps at both ends of the file", () => {
  assert.equal(followView({ from: 10_000, to: 20_000 }, 60_000, 0).from, 0);
  const atEnd = followView({ from: 10_000, to: 20_000 }, 60_000, 60_000);
  assert.equal(atEnd.to, 60_000);
});

test("followView does nothing when fully zoomed out", () => {
  // The whole file is visible, so there is nowhere to scroll to — returning a new object
  // here would re-render the tracks on every playback tick for no visual change.
  const view = { from: 0, to: 60_000 };
  assert.equal(followView(view, 60_000, 42_000), view);
});

test("followView never produces a window shorter than the minimum", () => {
  const next = followView({ from: 0, to: 3_000 }, 60_000, 59_000);
  assert.ok(next.to - next.from >= MIN_VISIBLE_MS);
  assert.ok(next.to <= 60_000);
});

const HOUR = 3_600_000;

test("clampView keeps a full view untouched", () => {
  assert.deepEqual(clampView({ from: 0, to: HOUR }, HOUR), { from: 0, to: HOUR });
});

test("clampView pulls a window back inside the file", () => {
  const view = clampView({ from: HOUR - 1000, to: HOUR + 5000 }, HOUR);
  assert.equal(view.to, HOUR);
  assert.equal(view.to - view.from, 6000, "length is preserved, not truncated");
});

test("clampView refuses to scroll before zero", () => {
  assert.deepEqual(clampView({ from: -5000, to: 5000 }, HOUR), { from: 0, to: 10_000 });
});

test("clampView enforces the minimum window", () => {
  const view = clampView({ from: 1000, to: 1001 }, HOUR);
  assert.equal(view.to - view.from, MIN_VISIBLE_MS);
});

test("the minimum window is three seconds, not one", () => {
  // The finest thing this app cuts on is a word; 3 s over ~1000 px is 3 ms per
  // pixel, so a word edge lands within a pixel of where it is drawn. Zooming
  // deeper would buy precision nobody can act on.
  assert.equal(MIN_VISIBLE_MS, 3000);
});

test("clampView survives a file shorter than the minimum window", () => {
  const view = clampView({ from: 0, to: 200 }, 200);
  assert.deepEqual(view, { from: 0, to: 200 });
});

test("zoomAt keeps the anchor under the pointer", () => {
  const before = { from: 0, to: HOUR };
  const anchor = HOUR / 4;
  const zoomed = zoomAt(before, HOUR, anchor, 2);
  // The anchor was 25% across before and must be 25% across after.
  assert.ok(Math.abs((anchor - zoomed.from) / (zoomed.to - zoomed.from) - 0.25) < 1e-9);
  assert.ok(zoomed.to - zoomed.from < before.to - before.from);
});

test("zoomAt out then in returns to the same window", () => {
  const start = { from: HOUR / 2, to: HOUR / 2 + 600_000 };
  const anchor = start.from + 200_000;
  const out = zoomAt(start, HOUR, anchor, 0.5);
  const back = zoomAt(out, HOUR, anchor, 2);
  assert.ok(Math.abs(back.from - start.from) < 1);
  assert.ok(Math.abs(back.to - start.to) < 1);
});

test("zoomAt clamps at full zoom-out", () => {
  const view = zoomAt({ from: 0, to: HOUR }, HOUR, HOUR / 2, 0.25);
  assert.deepEqual(view, { from: 0, to: HOUR });
});

test("zoomAt clamps at the minimum span", () => {
  const view = zoomAt({ from: 0, to: 5000 }, HOUR, 2500, 100);
  assert.equal(view.to - view.from, MIN_VISIBLE_MS);
});

test("zoomAt treats a zero factor as no zoom instead of dividing by zero", () => {
  const view = zoomAt({ from: 0, to: 10_000 }, HOUR, 5000, 0);
  assert.equal(Number.isFinite(view.to), true);
  assert.equal(view.to - view.from, 10_000);
});

test("panTo preserves the window length at the edges", () => {
  const start = { from: 0, to: 10_000 };
  assert.deepEqual(panTo(start, HOUR, 5000), { from: 5000, to: 15_000 });
  assert.deepEqual(panTo(start, HOUR, -1000), { from: 0, to: 10_000 });
  const end = panTo(start, HOUR, HOUR);
  assert.equal(end.to, HOUR);
  assert.equal(end.to - end.from, 10_000);
});

test("isZoomed is false for the whole file and true once narrowed", () => {
  assert.equal(isZoomed({ from: 0, to: HOUR }, HOUR), false);
  assert.equal(isZoomed({ from: 0, to: HOUR - 2 }, HOUR), true);
});

test("msToX and xToMs round-trip", () => {
  const view = { from: 10_000, to: 20_000 };
  const width = 800;
  const x = msToX(15_000, view, width);
  assert.equal(x, 400);
  assert.equal(xToMs(x, view, width), 15_000);
});

test("msToX puts the window edges at the canvas edges", () => {
  const view = { from: 30_000, to: 40_000 };
  assert.equal(msToX(view.from, view, 600), 0);
  assert.equal(msToX(view.to, view, 600), 600);
});

test("xToMs survives a zero-width canvas", () => {
  const view = { from: 0, to: 1000 };
  assert.equal(Number.isFinite(xToMs(10, view, 0)), true);
});

test("scrollFraction shrinks the thumb as you zoom in", () => {
  const full = scrollFraction({ from: 0, to: HOUR }, HOUR);
  assert.deepEqual(full, { start: 0, size: 1 });
  const half = scrollFraction({ from: HOUR / 2, to: HOUR }, HOUR);
  assert.equal(half.size, 0.5);
  assert.equal(half.start, 0.5);
});

test("scrollFraction never lets the thumb run past the track", () => {
  const { start, size } = scrollFraction({ from: HOUR - 1000, to: HOUR + 1000 }, HOUR);
  assert.ok(start + size <= 1 + 1e-9);
});

// --- remembering the window across a remount ----------------------------

test("shouldRememberView ignores the default full view", () => {
  assert.equal(shouldRememberView({ from: 0, to: HOUR }, HOUR), false);
});

test("shouldRememberView keeps any real zoom", () => {
  assert.equal(shouldRememberView({ from: 1000, to: 2000 }, HOUR), true);
});

test("initialView opens full when nothing is remembered", () => {
  assert.deepEqual(initialView(null, HOUR), { from: 0, to: HOUR });
});

test("initialView restores a remembered window unchanged", () => {
  const stored = { from: 60_000, to: 70_000 };
  assert.deepEqual(initialView(stored, HOUR), stored);
});

test("initialView clamps a window that no longer fits the file", () => {
  // Re-imported audio can be shorter than the zoom the user set up. The old
  // code restored the raw window, which drew an empty canvas with no message.
  const restored = initialView({ from: 40 * 60_000, to: 41 * 60_000 }, 3 * 60_000);
  assert.ok(restored.from >= 0);
  assert.ok(restored.to <= 3 * 60_000);
  assert.equal(restored.to - restored.from, 60_000);
});

// --- hitting an edge handle ----------------------------------------------

test("hitEdge picks the edge under the pointer", () => {
  assert.equal(hitEdge([10, 100, 400], 104, 6), 1);
  assert.equal(hitEdge([10, 100, 400], 10, 6), 0);
});

test("hitEdge misses when nothing is within reach", () => {
  assert.equal(hitEdge([10, 100], 50, 6), -1);
  assert.equal(hitEdge([], 50, 6), -1);
});

test("hitEdge refuses when the edges are too dense to aim at", () => {
  // The real case: 3364 edges across 978 px is 0.29 px apart, so *every* pixel
  // is within the 6 px radius of some handle. Answering "a handle" there is how
  // pressing on the waveform came to do nothing at all.
  const dense = [];
  for (let i = 0; i < 3364; i += 1) dense.push((i / 3364) * 978);
  for (const px of [0, 123.4, 500, 900]) assert.equal(hitEdge(dense, px, 6), -1);
});

test("hitEdge still works once zooming separates the edges", () => {
  // The same episode zoomed to a couple of seconds: a dozen edges over 978 px,
  // each ~80 px from the next, so a 6 px radius means something again.
  const edges = [0, 80, 160, 240, 320, 400];
  assert.equal(hitEdge(edges, 163, 6), 2);
  assert.equal(hitEdge(edges, 180, 6), -1);
});

test("hitEdge treats two coincident edges as one edge", () => {
  // Shared sentence boundaries land on the same pixel; dragging either means the
  // same thing, so the neighbour does not veto the hit.
  assert.equal(hitEdge([100, 100], 101, 6), 0);
});

test("hitEdge refuses when the runner-up is also inside the radius", () => {
  // Two handles 4 px apart: a click 2 px from each cannot say which was meant.
  assert.equal(hitEdge([100, 104], 102, 6), -1);
  // 13 px apart, click 6 px from the nearer one: unambiguous again.
  assert.equal(hitEdge([100, 113], 106, 6), 0);
});
