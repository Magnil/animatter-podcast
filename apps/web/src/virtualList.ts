/**
 * Windowing for the transcript list.
 *
 * The list is the reason the workbench felt heavy: 1074 sentences holding 11582
 * words is ~25k DOM nodes, and *any* state change — selecting one word, the
 * playhead crossing a sentence — re-rendered all of them. Measured end to end
 * in the browser, one word click took 630 ms to reach a painted frame.
 *
 * So only the rows near the viewport are mounted. That needs two things this
 * file provides, both pure so they can be tested without a browser:
 *
 * * **A height model**, because rows are not uniform (77–235 px, seven common
 *   heights). A fixed row height would make the scrollbar jump every time a
 *   tall row scrolled past the measuring window.
 * * **Variable-height windowing**, which is harder than `index * ROW_HEIGHT`.
 *   The rule here: keep measured heights, guess unmeasured ones from the mean,
 *   and walk outward from the scroll offset until the viewport plus a margin is
 *   covered. No binary search, because the estimate is only monotone once every
 *   row in the prefix has been measured, and it never is — the user scrolls.
 */

/** Rows kept mounted above and below the viewport, in viewport heights. */
const OVERSCAN_RATIO = 0.75;

/**
 * How long after a manual scroll the playhead stops dragging the list along.
 *
 * Reading and listening are different activities on the same widget. When the
 * user scrolls, they asked to look somewhere else; jumping back on the next
 * sentence boundary would make the list unusable for reading. Long enough to
 * cover reading a paragraph, short enough that pressing play takes over again.
 */
export const FOLLOW_GRACE_MS = 4000;

/** Used until a real row has been measured. Matches the common 77 px row. */
export const DEFAULT_ROW_HEIGHT = 96;

/**
 * Weight of `DEFAULT_ROW_HEIGHT` against measured samples, in rows.
 *
 * The plain mean of what has been measured is biased at first: the rows that
 * happen to be on screen are one contiguous stretch of the transcript, not a
 * sample of it. The first four rows of this episode measure 109–140 px, which
 * put the mean 31% above the true 94.9 px and made `scrollHeight` read 125230
 * against a real 101906 — a scrollbar that shrinks by a fifth while you read.
 * Shrinking toward the default while samples are few keeps the estimate honest
 * until enough of the file has actually been seen.
 */
const PRIOR_ROWS = 20;

export interface WindowPlan {
  /** First mounted row, inclusive. */
  start: number;
  /** Last mounted row, exclusive. */
  end: number;
  /** Height above `start` occupied by unmounted rows. */
  paddingTop: number;
  /** Height below `end` occupied by unmounted rows. */
  paddingBottom: number;
}

/**
 * Height to assume for unmeasured rows.
 *
 * Shrunk toward `DEFAULT_ROW_HEIGHT` by `PRIOR_ROWS` pseudo-samples. A single
 * 1 px measurement therefore cannot collapse the spacer and make the scrollbar
 * useless, and a handful of unusually tall rows cannot inflate it either.
 */
export function estimateRowHeight(heights: Map<number, number>): number {
  if (heights.size === 0) return DEFAULT_ROW_HEIGHT;
  let total = 0;
  for (const height of heights.values()) total += height;
  const estimate = (total + DEFAULT_ROW_HEIGHT * PRIOR_ROWS) / (heights.size + PRIOR_ROWS);
  return Math.max(24, estimate);
}

/** Height of one row, measured if possible, estimated otherwise. */
function rowHeight(index: number, heights: Map<number, number>, fallback: number): number {
  return heights.get(index) ?? fallback;
}

/**
 * Plan which rows to mount for a given scroll offset.
 *
 * `count` rows, a `scrollTop` inside a `viewportHeight` tall scroller, with the
 * whole list known to be `measuredTotal + estimated` tall.
 *
 * Returns the half-open row range plus the two spacers. `start`/`end` are
 * clamped to `[0, count]` and `end > start` whenever `count > 0`, so the caller
 * never renders an empty listitem container for a list that has contents.
 */
export function planWindow(
  count: number,
  heights: Map<number, number>,
  scrollTop: number,
  viewportHeight: number,
): WindowPlan {
  if (count <= 0) return { start: 0, end: 0, paddingTop: 0, paddingBottom: 0 };

  const fallback = estimateRowHeight(heights);
  const overscan = Math.max(0, viewportHeight) * OVERSCAN_RATIO;
  const top = Math.max(0, scrollTop - overscan);
  const bottom = Math.max(0, scrollTop) + Math.max(0, viewportHeight) + overscan;

  let start = 0;
  let offset = 0;
  while (start < count) {
    const next = offset + rowHeight(start, heights, fallback);
    if (next > top) break;
    offset = next;
    start += 1;
  }

  let end = start;
  offset = sumHeights(0, start, heights, fallback);
  while (end < count && offset < bottom) {
    offset += rowHeight(end, heights, fallback);
    end += 1;
  }
  // Always mount at least one row: a zero-height scroller (first paint, or a
  // hidden pane) would otherwise plan an empty window and look like data loss.
  if (end === start && start < count) end = start + 1;

  return {
    start,
    end,
    paddingTop: sumHeights(0, start, heights, fallback),
    paddingBottom: Math.max(0, sumHeights(end, count, heights, fallback)),
  };
}

/**
 * Sum of row heights in `[from, to)`.
 *
 * Unmeasured rows contribute the estimate, so a spacer stays proportional to
 * how much is hidden rather than snapping to zero. `count` is 1074 and this is
 * called a handful of times per scroll event, which is nothing next to laying
 * out 25k nodes.
 */
export function sumHeights(
  from: number,
  to: number,
  heights: Map<number, number>,
  fallback: number = estimateRowHeight(heights),
): number {
  let total = 0;
  for (let i = Math.max(0, from); i < to; i += 1) total += heights.get(i) ?? fallback;
  return total;
}

/**
 * Index to scroll to so row `index` is visible.
 *
 * Used when selection moves programmatically (clicking a word in the waveform,
 * or playing past the current sentence) — the selection must come into view
 * even for a row that is not currently mounted, which is exactly the case a
 * plain `scrollIntoView()` can no longer handle.
 */
export function scrollOffsetFor(
  index: number,
  heights: Map<number, number>,
  viewportHeight: number,
): number {
  if (index <= 0) return 0;
  const fallback = estimateRowHeight(heights);
  const above = sumHeights(0, index, heights, fallback);
  const height = rowHeight(index, heights, fallback);
  // Centre the row rather than aligning its top: a sentence is often taller
  // than the viewport at this pane width, and its top edge alone hides the
  // words the user just asked to see.
  return Math.max(0, above - Math.max(0, viewportHeight - height) / 2);
}
