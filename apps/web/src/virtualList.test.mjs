/**
 * Windowing arithmetic for the transcript list.
 *
 * The cost these tests guard is not hypothetical: with every row mounted, one
 * word click took 650 ms to paint, and slicing the list to 40 rows dropped the
 * same click to 199 ms. So the arithmetic has to be right — a window that is
 * one row short shows a blank strip, and a `paddingTop` that disagrees with the
 * mounted rows makes the scrollbar jump under the user's finger.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_ROW_HEIGHT,
  estimateRowHeight,
  planWindow,
  rowVisible,
  scrollOffsetFor,
  sumHeights,
} from "./virtualList.ts";

/** A list of `count` rows, all measured at `height`. */
function uniform(count, height) {
  const heights = new Map();
  for (let i = 0; i < count; i += 1) heights.set(i, height);
  return heights;
}

test("an empty list plans an empty window", () => {
  assert.deepEqual(planWindow(0, new Map(), 0, 500), {
    start: 0,
    end: 0,
    paddingTop: 0,
    paddingBottom: 0,
  });
});

test("the first screen mounts only what it can show", () => {
  const plan = planWindow(1000, uniform(1000, 50), 0, 500);
  // 500 px of viewport + 75% overscan = 875 px = 17.5 rows.
  assert.equal(plan.start, 0);
  assert.ok(plan.end >= 18, `expected at least 18 rows, got ${plan.end}`);
  assert.ok(plan.end <= 24, `expected at most 24 rows, got ${plan.end}`);
  assert.equal(plan.paddingTop, 0);
});

test("the window moves with the scroll offset", () => {
  const heights = uniform(1000, 50);
  const plan = planWindow(1000, heights, 5000, 500);
  assert.ok(plan.start > 0, "scrolling 5000 px must skip rows");
  // Row 100 starts at 5000 px, so the window must start at or before it.
  assert.ok(plan.start <= 100, `start ${plan.start} skipped past the viewport top`);
  assert.ok(plan.end > 110, `end ${plan.end} did not reach the viewport bottom`);
  assert.ok(plan.paddingTop > 0);
  assert.ok(plan.paddingBottom > 0);
});

test("spacers plus the mounted rows equal the whole list", () => {
  const heights = uniform(500, 40);
  for (const scrollTop of [0, 137, 4000, 19_000]) {
    const plan = planWindow(500, heights, scrollTop, 600);
    const mounted = sumHeights(plan.start, plan.end, heights);
    const total = plan.paddingTop + mounted + plan.paddingBottom;
    // Every row is measured here, so the identity is exact — a spacer that is
    // off by a row is what makes the scrollbar jump.
    assert.equal(total, 20_000, `scrollTop ${scrollTop}`);
  }
});

test("unmeasured rows use a shrunk mean, not a constant", () => {
  const heights = new Map();
  for (let i = 0; i < 10; i += 1) heights.set(i, 140);
  // Ten tall rows still pull the estimate up, but not all the way: they are one
  // contiguous stretch of the transcript, not a sample of it.
  const tenTall = estimateRowHeight(heights);
  assert.ok(tenTall > DEFAULT_ROW_HEIGHT, `expected > ${DEFAULT_ROW_HEIGHT}, got ${tenTall}`);
  assert.ok(tenTall < 140, `expected < 140, got ${tenTall}`);

  // One tiny measurement must not collapse the estimate for the other 1000
  // rows, or the spacer becomes invisible and the scrollbar lies.
  const skewed = estimateRowHeight(new Map([[0, 3]]));
  assert.ok(skewed > 60, `a single 3 px row must not drag the estimate down: ${skewed}`);

  // Many samples converge on the measured mean.
  const many = new Map();
  for (let i = 0; i < 500; i += 1) many.set(i, 120);
  assert.ok(estimateRowHeight(many) > 119, `expected convergence near 120, got ${estimateRowHeight(many)}`);

  assert.equal(estimateRowHeight(new Map()), DEFAULT_ROW_HEIGHT);
});

test("a zero-height viewport still mounts a row", () => {
  // First paint, or a pane inside a collapsed flex parent: the scroller can
  // measure 0 px before layout settles. Rendering nothing there looks like the
  // transcript failed to load.
  const plan = planWindow(10, uniform(10, 50), 0, 0);
  assert.equal(plan.start, 0);
  assert.ok(plan.end > 0);
});

test("a scroll offset past the end clamps instead of overflowing", () => {
  const heights = uniform(20, 50);
  const plan = planWindow(20, heights, 999_999, 500);
  assert.ok(plan.start >= 0 && plan.start <= 20);
  assert.ok(plan.end >= plan.start && plan.end <= 20);
  assert.equal(plan.paddingBottom, 0, "nothing is hidden below a clamped window");
});

test("scrollOffsetFor centres a tall row", () => {
  const heights = uniform(100, 50);
  assert.equal(scrollOffsetFor(0, heights, 500), 0);
  // Row 50 sits at 2500 px; centring a 50 px row in 500 px puts it at 2500-225.
  assert.equal(scrollOffsetFor(50, heights, 500), 2275);
});

test("scrollOffsetFor never goes negative for a row taller than the viewport", () => {
  const heights = new Map([[3, 900]]);
  assert.equal(scrollOffsetFor(3, new Map([[3, 900]]), 400), sumHeights(0, 3, heights));
});

test("the window never exceeds the row count", () => {
  const heights = uniform(3, 50);
  const plan = planWindow(3, heights, 0, 5000);
  assert.equal(plan.start, 0);
  assert.equal(plan.end, 3);
  assert.equal(plan.paddingBottom, 0);
});

test("rowVisible is true for a row fully on screen", () => {
  const heights = uniform(100, 50);
  // Rows 0..9 occupy 0..500; row 4 is 200..250.
  assert.equal(rowVisible(4, heights, 0, 500), true);
  assert.equal(rowVisible(4, heights, 200, 500), true);
});

test("rowVisible is false for a row scrolled past", () => {
  const heights = uniform(100, 50);
  // Scrolled to 1000, row 4 (200..250) is above the viewport.
  assert.equal(rowVisible(4, heights, 1000, 500), false);
  // Row 40 (2000..2050) is below it.
  assert.equal(rowVisible(40, heights, 0, 500), false);
});

test("rowVisible is false for a partly visible row", () => {
  // Half a row showing is not "in view": the word highlight could be in the
  // hidden half, which is exactly what the caller is trying to prevent.
  const heights = uniform(100, 50);
  assert.equal(rowVisible(4, heights, 225, 500), false);
});

test("rowVisible accepts a row taller than the viewport that fills it", () => {
  // A 900 px row can never be fully inside a 400 px viewport. A strict test
  // would scroll the list on every word clicked in such a row, and tall rows are
  // ordinary in this pane — so the test inverts for them.
  const heights = new Map([[3, 900]]);
  const above = sumHeights(0, 3, heights);
  assert.equal(rowVisible(3, heights, above, 400), true);
  assert.equal(rowVisible(3, heights, above + 300, 400), true);
  // Scrolled so the row is gone entirely: not visible any more.
  assert.equal(rowVisible(3, heights, above + 900, 400), false);
});

test("rowVisible is false for a missing row or an unmeasured viewport", () => {
  assert.equal(rowVisible(-1, new Map(), 0, 500), false);
  // Before the first ResizeObserver callback the viewport reads 0; treating that
  // as "nothing is visible" keeps the reveal path from scrolling on mount.
  assert.equal(rowVisible(0, uniform(10, 50), 0, 0), false);
});
