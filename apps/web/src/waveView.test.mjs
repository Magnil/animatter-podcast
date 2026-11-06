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
  MIN_VISIBLE_MS,
  clampView,
  initialView,
  shouldRememberView,
  isZoomed,
  msToX,
  panTo,
  scrollFraction,
  xToMs,
  zoomAt,
} from "./waveView.ts";

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
