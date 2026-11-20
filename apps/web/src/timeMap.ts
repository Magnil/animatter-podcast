/**
 * Original ↔ output time, for the video preview (P0-1, §19.31.5).
 *
 * **The bug this exists to fix.** `VideoPanel` asked for a preview with
 * `playback.positionMs`, which is a position on the **original** timeline; the endpoint
 * reads `at_ms` as **output** time. The two agree only while nothing is deleted — so on
 * every project that had been edited, the still showed a frame from 1.0–1.2 s later than
 * the playhead (measured on three real projects; the 费那奇MV sample has no deletions at
 * all, which is why it never showed there).
 *
 * `App` already holds the server's EDL, whose `keeps` are exactly this mapping. This
 * module is the pure half of it: given `keeps`, convert both ways.
 *
 * Why it is a separate module rather than two lines in the component: the direction
 * matters and getting it backwards is silent. Mapping output → original when the caller
 * wanted the opposite still lands inside the film, still produces a plausible picture, and
 * only disagrees by however much was cut before that point.
 */

export interface Keep {
  orig_start: number;
  orig_end: number;
  new_start: number;
}

export interface TimeMap {
  keeps: Keep[];
  originalDurationMs: number;
}

/**
 * Build a map from the EDL's keep list.
 *
 * A version with no EDL yet (the request is still in flight, or the project has no media)
 * maps identically. That is the honest fallback: without deletions, original and output
 * time *are* the same, and refusing to preview would trade a rare small error for a
 * common empty panel.
 */
export function timeMapFrom(keeps: Keep[] | null | undefined): TimeMap {
  const usable = (keeps ?? [])
    .filter(
      (keep) =>
        Number.isFinite(keep.orig_start) &&
        Number.isFinite(keep.orig_end) &&
        Number.isFinite(keep.new_start),
    )
    .sort((left, right) => left.orig_start - right.orig_start);
  const last = usable.length > 0 ? usable[usable.length - 1] : undefined;
  const originalDurationMs = last ? last.orig_end : 0;
  return { keeps: usable, originalDurationMs };
}

/**
 * Original → output. Returns `null` for an instant inside a deleted span.
 *
 * Half-open `[orig_start, orig_end)`, like every interval in this codebase: an instant
 * equal to a keep's end belongs to whatever comes next, not to this keep.
 *
 * **With no keeps at all the clocks are identical**, and that is not a special case to
 * guard at the call sites: an empty keep list means the EDL has not arrived (or the project
 * has no media), and while nothing is deleted original and output time *are* the same
 * number. Returning `null` there would leave the panel blank on every fresh project.
 */
export function toOutput(map: TimeMap, origMs: number): number | null {
  if (map.keeps.length === 0) return origMs;
  for (const keep of map.keeps) {
    if (origMs >= keep.orig_start && origMs < keep.orig_end) {
      return keep.new_start + (origMs - keep.orig_start);
    }
  }
  return null;
}

/**
 * Original → output, snapping a deleted instant to the start of the next surviving gap.
 *
 * A playhead that stopped one millisecond inside a cut is still a place the user is
 * looking at. Returning `null` there would leave the panel showing the previous frame with
 * no explanation, so the still comes from the next audible instant instead — which is also
 * where `usePlayback` puts the audio when it skips a deletion.
 */
export function toOutputClamped(map: TimeMap, origMs: number): number {
  const direct = toOutput(map, origMs);
  if (direct !== null) return direct;
  const next = map.keeps.find((keep) => keep.orig_start > origMs);
  if (next) return next.new_start;
  const last = map.keeps[map.keeps.length - 1];
  if (last) return last.new_start + (last.orig_end - last.orig_start);
  return Math.max(0, Math.round(origMs));
}

/** Output → original. `null` when the instant is past the end of the film. */
export function toOriginal(map: TimeMap, outMs: number): number | null {
  for (const keep of map.keeps) {
    const newEnd = keep.new_start + (keep.orig_end - keep.orig_start);
    if (outMs >= keep.new_start && outMs < newEnd) {
      return keep.orig_start + (outMs - keep.new_start);
    }
  }
  return null;
}
