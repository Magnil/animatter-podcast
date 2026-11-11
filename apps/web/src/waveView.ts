/**
 * Pure view arithmetic for the waveform: what window is visible, and what
 * bucket resolution to ask the server for.
 *
 * Kept out of ``Waveform.tsx`` because zoom bugs are arithmetic bugs: an off-by-
 * one in the clamp lets the window drift past the end and the canvas goes blank,
 * and an anchor that uses the wrong edge makes the mouse appear to push the
 * waveform sideways instead of zooming where you point. Both are cheap to test
 * here and expensive to debug in a canvas.
 */

export interface View {
  from: number;
  to: number;
}

/** Never zoom in past three seconds of audio.
 *
 * Not one second, which is what a waveform editor would allow: the finest thing
 * this app cuts on is a word, and the shortest word is a few hundred ms. At a
 * 3-second window a 1000 px canvas is 3 ms per pixel, so a word edge lands
 * within a pixel of where it is drawn — deeper zoom buys precision the user
 * cannot act on, and only makes the waveform look smoother and emptier.
 */
export const MIN_VISIBLE_MS = 3000;
/** One wheel notch. 1.25^4 ≈ 2.4× — brisk but still controllable. */
export const ZOOM_STEP = 1.25;

/** Clamp a window into ``[0, total]``, keeping the requested length if possible. */
export function clampView(view: View, total: number): View {
  const safeTotal = Math.max(1, total);
  const minSpan = Math.min(MIN_VISIBLE_MS, safeTotal);
  const requested = view.to - view.from;
  const span = Math.min(safeTotal, Math.max(minSpan, requested));
  const from = Math.min(Math.max(0, view.from), Math.max(0, safeTotal - span));
  return { from, to: from + span };
}

/**
 * Zoom by ``factor`` (>1 zooms in) keeping ``anchorMs`` at the same pixel.
 *
 * The anchor is what separates a usable zoom from a disorienting one: zooming
 * around the window centre means the word under the cursor slides away, so the
 * user has to chase it with the scrollbar after every notch.
 */
export function zoomAt(view: View, total: number, anchorMs: number, factor: number): View {
  const span = Math.max(1, view.to - view.from);
  const ratio = Math.min(1, Math.max(0, (anchorMs - view.from) / span));
  const nextSpan = span / (factor > 0 ? factor : 1);
  const from = anchorMs - ratio * nextSpan;
  return clampView({ from, to: from + nextSpan }, total);
}

/** Shift the window to start at ``from``, preserving its length. */
export function panTo(view: View, total: number, from: number): View {
  return clampView({ from, to: from + (view.to - view.from) }, total);
}

/** True when the window is smaller than the whole file. */
export function isZoomed(view: View, total: number): boolean {
  return view.to - view.from < Math.max(1, total) - 1;
}

/**
 * Whether this window is worth remembering across a remount.
 *
 * Full zoom-out is the default, so storing it would be noise — and it would
 * outlive the reason it was stored: a project whose duration later changes
 * would still match the old "whole file" entry and get restored verbatim.
 * Delegating to ``isZoomed`` keeps one definition of "zoomed" in the codebase;
 * a separate threshold is how the two drift apart.
 */
export function shouldRememberView(view: View, total: number): boolean {
  return isZoomed(view, total);
}

/**
 * The window to open with: a remembered one (clamped) or the whole file.
 *
 * The clamp matters more than it looks. A remembered window belongs to a media
 * file that may have been replaced — re-imported audio, a project loaded from
 * an archive whose media relinked to a different file — and a window starting
 * at 40 minutes in a 3-minute file draws an empty canvas with no explanation.
 * Restoring a *clamped* window keeps the zoom without ever pointing past the end.
 */
export function initialView(stored: View | null, total: number): View {
  const full = { from: 0, to: Math.max(1, total) };
  if (stored === null) return full;
  return clampView(stored, total);
}

/** Map a time to an x offset inside the window, in viewport pixels. */
export function msToX(ms: number, view: View, width: number): number {
  const span = Math.max(1, view.to - view.from);
  return ((ms - view.from) / span) * width;
}

/** Map an x offset inside the window back to a time. */
export function xToMs(x: number, view: View, width: number): number {
  const span = Math.max(1, view.to - view.from);
  return view.from + (x / Math.max(1, width)) * span;
}

/** Thumb geometry for the custom scrollbar, as 0–1 fractions. */
export function scrollFraction(view: View, total: number): { start: number; size: number } {
  const safeTotal = Math.max(1, total);
  const span = Math.min(safeTotal, Math.max(1, view.to - view.from));
  const size = span / safeTotal;
  const start = Math.min(Math.max(0, view.from / safeTotal), Math.max(0, 1 - size));
  return { start, size };
}

/**
 * The window to use after the playhead moves, keeping it on screen while zoomed.
 *
 * Purely arithmetic, so it belongs here next to `zoomAt`/`panTo` rather than inside a
 * React hook: the rule is "if the playhead left the window, put it 30 % from the left
 * edge", and getting that ratio wrong is invisible in a screenshot but obvious in a
 * one-line assertion.
 *
 * Returns the window unchanged when it already contains the playhead — a follow rule that
 * re-centres on every frame would fight the user's own panning, and re-centring while the
 * playhead is 5 % from the edge would yank the view for no reason.
 */
export function followView(view: View, total: number, ms: number): View {
  const safeTotal = Math.max(1, total);
  // Fully zoomed out: there is nothing to scroll to, and clamping would only ever
  // rewrite the same window.
  if (view.to - view.from >= safeTotal - 1) return view;
  if (ms >= view.from && ms < view.to) return view;
  const span = view.to - view.from;
  return clampView({ from: ms - span * 0.3, to: ms + span * 0.7 }, safeTotal);
}

/**
 * Two edges closer than this in pixels are the same edge.
 *
 * Sentence boundaries are shared — one sentence's `end_ms` is the next one's
 * `start_ms` — so their handles land on exactly the same pixel, and dragging
 * either means the same thing. Only exactly-coincident positions are merged;
 * anything with real distance between them stays a separate edge.
 */
const SAME_EDGE_PX = 1e-6;

/**
 * Index of the edge handle under the pointer, or -1 when the click is not
 * unambiguously on one.
 *
 * The bug this exists for: with 1682 sentences in a 61-minute episode, the
 * timeline holds 3364 edges across 978 px — 0.45 px apart — while the hit radius
 * is 6 px. So *every* click anywhere on the waveform body hit a handle, took the
 * drag branch, and then released without moving: pressing on the waveform did
 * nothing at all. Measured with `scripts/check-seek-probe.mjs` before the fix:
 * six clicks spread across the canvas, the clock never moved once, while a click
 * on the caption band (a different code path) jumped to 38:08.
 *
 * The rule is "closest, and clearly so": the nearest edge must be within
 * `hitPx`, *and* the next distinct edge must be further than `hitPx`. Where that
 * fails the edges are too dense to aim at, and the honest answer is "no handle"
 * — the click falls through to a seek, which is what the user meant anyway.
 * Zooming in restores dragging as soon as edges are far enough apart to be
 * distinguishable, which is the same condition under which a drag can hit what
 * the user is pointing at.
 */
export function hitEdge(xs: number[], px: number, hitPx: number): number {
  let best = -1;
  let nearest = Number.POSITIVE_INFINITY;
  for (let index = 0; index < xs.length; index += 1) {
    const distance = Math.abs((xs[index] ?? 0) - px);
    if (distance < nearest) {
      nearest = distance;
      best = index;
    }
  }
  if (best < 0 || nearest > hitPx) return -1;

  // A second edge inside the radius makes the click ambiguous — unless it is the
  // same edge, which is what a shared sentence boundary produces. Comparing
  // *positions* rather than distances is what separates the two cases: two
  // distinct edges at 100 and 104 are equidistant from 102 and genuinely
  // ambiguous, while two edges both at 100 are one edge drawn twice.
  const chosen = xs[best] ?? 0;
  for (const other of xs) {
    if (Math.abs(other - chosen) <= SAME_EDGE_PX) continue;
    if (Math.abs(other - px) <= hitPx) return -1;
  }
  return best;
}
