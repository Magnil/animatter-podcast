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

/** Never zoom in past one second of audio. */
export const MIN_VISIBLE_MS = 1000;
/** One wheel notch. 1.25^4 ≈ 2.4× — brisk but still controllable. */
export const ZOOM_STEP = 1.25;
/** Two buckets per pixel is plenty; more just costs payload. */
export const BUCKETS_PER_PIXEL = 2;
export const MAX_BUCKETS_PER_SECOND = 4096;

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
 * Bucket resolution for one window.
 *
 * ``buckets_per_second`` has to follow the zoom: asking 64/s for a 3h file
 * yields 691k buckets, but asking 64/s for a 10-second window yields 640 and
 * the waveform becomes a staircase. Conversely a fixed 4096/s for a 3h file
 * would be 44M buckets — the request would time out before anything was drawn.
 */
export function bucketsForWindow(viewportPx: number, spanMs: number): number {
  const seconds = Math.max(0.001, spanMs / 1000);
  const pixels = Math.max(1, viewportPx);
  const wanted = Math.round((pixels / seconds) * BUCKETS_PER_PIXEL);
  return Math.min(MAX_BUCKETS_PER_SECOND, Math.max(1, wanted));
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
