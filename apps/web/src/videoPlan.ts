/**
 * The DOM stage: what to draw at one output instant (§19.31.2, §19.31.3).
 *
 * **Why this replaces the still preview as the default.** One composited frame costs
 * 0.63–0.93 s because ffmpeg has to start, decode five inputs and burn the subtitle. But the
 * film does not change 24 times a second — only *which* segment is on screen, which avatars are
 * lit and which cue is up. Given the whole film as one description, moving the playhead is a
 * binary search and a handful of attribute flips: measured 144 fps / 0 dropped frames with a 4K
 * source in the stack (§19.31.1).
 *
 * **The layer order is not a design choice — it is `build_filtergraph`.'** A reordered stack
 * produces a plausible picture, which is the worst kind of bug, so the order lives in exactly
 * one function here (`stageAt`) and one test asserts it. Round 128 measured the two orders to
 * differ by 5930 pixels *inside the box rect alone* on the sample template.
 *
 * **The blurred backdrop is baked, the rest is native.** `contain`/`cover`/`stretch` are
 * `object-fit`; only `blur_fill`/`blur_crop` need ffmpeg, because `boxblur` is not a gaussian
 * and `eq=brightness` is not `filter: brightness()`. Even then only the *backdrop* is baked —
 * the foreground on top of it is native again.
 */

/** One stretch of box content, on the output clock. */
export interface PlanSegment {
  start_ms: number;
  end_ms: number;
  kind: "image" | "video" | string;
  fit: "stretch" | "cover" | "contain" | "blur_fill" | "blur_crop" | string;
  placement: "box" | "full" | string;
  loop: boolean;
  url: string;
  backdrop_url: string | null;
  /**
   * A **video** segment whose blurred backdrop is a still: the film's background moves, the
   * preview's does not. Reported rather than hidden (§19.32) — on the sample it is one segment
   * of 24.2 s, and a preview that silently freezes a moving background is exactly the
   * disagreement the rest of this module exists to prevent.
   */
  backdrop_frozen: boolean;
  /**
   * For a looped video, the period the browser's own `loop` repeats on, in milliseconds.
   *
   * A `<video loop>` wraps at the container duration while ffmpeg's `-stream_loop` wraps at the
   * stream duration; the two differ by 32–83 ms on the sample's clips, which after two loops is
   * whole frames. The server probes the container duration and publishes it so both sides reduce
   * the offset the same way instead of each guessing.
   */
  period_ms: number;
}

export interface PlanCue {
  start_ms: number;
  end_ms: number;
  index: number;
  top: number;
  height: number;
  url: string | null;
}

export interface PlanAvatar {
  name: string;
  rect: { x: number; y: number; w: number; h: number };
  lit_url: string;
  dim_url: string;
  intervals: { start_ms: number; end_ms: number }[];
}

export interface VideoPlan {
  digest: string;
  version_id: string;
  canvas: { w: number; h: number };
  box: { x: number; y: number; w: number; h: number };
  output_duration_ms: number;
  background_url: string;
  overlay_url: string | null;
  text_url: string | null;
  segments: PlanSegment[];
  avatars: PlanAvatar[];
  cues: PlanCue[];
  unsupported: string[];
}

/** One thing to draw, bottom-up. */
export interface StageLayer {
  key: string;
  /** Where it sits on the canvas, in canvas pixels. */
  rect: { x: number; y: number; w: number; h: number };
  kind: "image" | "video";
  url: string;
  /** The blurred backdrop behind a `blur_*` foreground, if this segment has one. */
  backdrop: string | null;
  fit: PlanSegment["fit"];
  /** For a video: where to seek inside the source, and whether it loops. */
  offsetMs: number;
  loop: boolean;
}

export interface Stage {
  /** The output instant this describes. */
  atMs: number;
  segment: PlanSegment | null;
  /** The media layer — the box content, or the 全屏 block. `null` in a plate gap. */
  media: StageLayer | null;
  lit: string[];
  cue: PlanCue | null;
}

/**
 * Binary search a list of half-open `[start, end)` spans.
 *
 * Half-open because every interval in this codebase is (§AGENTS): an instant exactly at a
 * segment's end belongs to the next one. A closed comparison freezes the previous frame for one
 * millisecond at every boundary, which at 24 fps is invisible — until it is the last frame of
 * the film and never advances.
 */
export function findSpan<T extends { start_ms: number; end_ms: number }>(
  spans: readonly T[],
  atMs: number,
): T | null {
  let low = 0;
  let high = spans.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const found = spans[middle];
    if (found === undefined) return null;
    if (atMs < found.start_ms) high = middle - 1;
    else if (atMs >= found.end_ms) low = middle + 1;
    else return found;
  }
  return null;
}

/** Names of the avatar slots that are lit at `atMs`. */
export function litAt(plan: VideoPlan, atMs: number): string[] {
  const out: string[] = [];
  for (const avatar of plan.avatars) {
    if (findSpan(avatar.intervals, atMs) !== null) out.push(avatar.name);
  }
  return out;
}

/**
 * Everything the stage must show at one output instant.
 *
 * The order of the returned fields is the drawing order, and it is `build_filtergraph`'s:
 * background → 描边 (`overlay.png`) → box content → avatars → text → 全屏 → subtitles.
 * Note the second step: the artwork overlay goes **under** the box content because the chain
 * overlays `layers` before `box`. The first draft of §19.31.3 had it the other way round, and
 * 5930 semi-opaque pixels of the sample's stroke fall inside the box rect — so the two orders
 * are not interchangeable.
 */
export function stageAt(plan: VideoPlan, atMs: number): Stage {
  const segment = findSpan(plan.segments, atMs);
  return {
    atMs,
    segment,
    media: segment ? mediaLayer(plan, segment, atMs) : null,
    lit: litAt(plan, atMs),
    cue: findSpan(plan.cues, atMs),
  };
}

/**
 * The media layer for one segment.
 *
 * A `full` segment fills the canvas; a `box` one fills the box. `offsetMs` is how far into the
 * *source* the segment has played — the film reads every input from its own beginning
 * (`box_track.input_args`), so no source-time mapping is involved.
 */
export function mediaLayer(
  plan: VideoPlan,
  segment: PlanSegment,
  atMs: number,
): StageLayer {
  const full = segment.placement === "full";
  const rect = full
    ? { x: 0, y: 0, w: plan.canvas.w, h: plan.canvas.h }
    : { x: plan.box.x, y: plan.box.y, w: plan.box.w, h: plan.box.h };
  return {
    key: `${segment.start_ms}-${segment.end_ms}-${segment.url}`,
    rect,
    kind: segment.kind === "video" ? "video" : "image",
    url: segment.url,
    backdrop: segment.backdrop_url,
    fit: segment.fit,
    // The offset inside the clip is measured **on the film's frame grid**, from the frame the
    // segment starts on. See `PlanSegment.start_frame`: using `atMs - start_ms` puts a clip one
    // frame behind the still whenever a block boundary falls mid-frame, which on the sample is
    // every block.
    offsetMs: localOffsetMs(segment, atMs),
    loop: segment.loop,
  };
}

/**
 * The film's frame rate.
 *
 * The box chain ends in `fps=24` and the export writes 24 fps, so an output frame covers
 * `[k/24, (k+1)/24)` and carries PTS `k/24`. `box_track.FPS` is the same number on the other side
 * of the wire; the plan is built at it rather than carrying it because a plan and a film at
 * different rates would be a different feature, not a configuration.
 */
export const FILM_FPS = 24;

/**
 * The **start of the film frame that shows this instant**, in milliseconds.
 *
 * Everything the preview draws is decided at this time, not at the raw instant. Two reasons, both
 * measured on the sample project:
 *
 * * **Cues.** At 2 624 610 ms a cue begins, but the frame covering that instant started at
 *   2 624 583 ms — so the film still shows the previous cue. Evaluating the plan at the raw
 *   instant showed the *next* cue, one word early, for up to 42 ms. The still had the same bug
 *   from the other side and they disagreed on `blur_fill` at 6.5 % of the frame.
 * * **Segments and avatars.** A block boundary or a lit/dim flip lands mid-frame in the timeline
 *   and on a frame boundary in the film; snapping is what makes "which block is up" a question
 *   with one answer instead of two.
 *
 * Floor, never round: the film's `fps` filter holds frame *k* for the whole interval after it, so
 * rounding would put the top half of every frame on the next frame's content.
 */
export function filmFrame(atMs: number): number {
  if (!Number.isFinite(atMs) || atMs <= 0) return 0;
  return Math.floor((atMs * FILM_FPS) / 1000);
}

export function frameStartMs(atMs: number): number {
  return Math.floor((filmFrame(atMs) * 1000) / FILM_FPS);
}

/**
 * How far a `<video>`'s `currentTime` may drift before it is worth a seek.
 *
 * A seek is not free: it stalls the decoder for a frame or two, and on a 4K source it is
 * hundreds of milliseconds. 200 ms is under one cue boundary and over the error a couple of
 * dropped frames produces, so the correction happens rarely and is never visible.
 */
export const SEEK_TOLERANCE_MS = 200;

/**
 * How far into a segment's clip the film is at `atMs`, in **frame-aligned** milliseconds.
 *
 * Three things make this more than a subtraction, and each of them was measured as a visible
 * disagreement before it was fixed:
 *
 * 1. **The segment starts on a frame, not on its millisecond.** A block boundary is arbitrary;
 *    the film's chain can only begin a picture on a frame, and `frames_of` counts each segment
 *    from `round(start * 24 / 1000)`. So the offset is counted between *film frames*.
 * 2. **A looped clip has to be reduced by the right period.** ffmpeg's `-stream_loop` wraps at the
 *    stream duration while a browser's `loop` wraps at the container duration — 32 ms and 83 ms
 *    apart on the sample's two clips, which after two loops is whole frames. The server publishes
 *    the container period (`period_ms`), which is the one the element actually obeys.
 * 3. **The frame itself covers a range.** The returned offset is the frame's own start, so the
 *    still (which renders that frame) and the element (which shows the frame containing the seek
 *    target) land on the same picture.
 *
 * Mirrors `box_track.local_offset_ms`; the two are asserted equal by
 * `scripts/check-p1-consistency.mjs`'s frame numbers.
 */
export function localOffsetMs(segment: PlanSegment, atMs: number): number {
  // The **same expression the still uses**: `box_track.frame_start_ms(at_ms - start_ms)`.
  // Snapping the absolute instant and then subtracting the segment's start frame looks
  // equivalent and is not — `floor(a·24/1000) − round(b·24/1000)` and `floor((a−b)·24/1000)`
  // differ by a frame whenever the boundary's sub-frame offset is large enough. Writing the
  // still's own expression here is what keeps the two sides from drifting apart again.
  let offset = frameStartMs(atMs - segment.start_ms);
  if (segment.period_ms > 0) offset %= segment.period_ms;
  return Math.max(0, offset);
}

/**
 * Where a live `<video>` should be, in seconds.
 *
 * A looped source wraps: `-stream_loop -1` restarts it, and the browser's own `loop` attribute
 * does the same, so the expected position is the offset modulo the clip's length. Without the
 * modulo every looped segment would be "200 ms out of sync" forever and seek once per tick.
 */
export function expectedVideoSeconds(offsetMs: number): number {
  if (!Number.isFinite(offsetMs)) return 0;
  // **No modulo here.** `offsetMs` already arrives reduced by `PlanSegment.period_ms`, which is
  // the container duration the element itself loops on (see `localOffsetMs`). Reducing again with
  // `element.duration` looks harmless and is not: ffmpeg wraps at the *stream* duration while the
  // element reports the *container* one, 32 ms apart on the sample, and applying both reductions
  // puts the element one frame behind the still on a long segment.
  return Math.max(0, offsetMs) / 1000;
}

/** Whether a video element at `currentSeconds` is far enough off to need a seek. */
export function needsSeek(
  currentSeconds: number,
  expectedSeconds: number,
  toleranceMs: number = SEEK_TOLERANCE_MS,
): boolean {
  if (!Number.isFinite(currentSeconds)) return true;
  return Math.abs(currentSeconds - expectedSeconds) * 1000 > toleranceMs;
}

/**
 * The CSS `object-fit`/`object-position` for a fit.
 *
 * `cover` is anchored `left top` because ffmpeg's chain spells `crop=W:H:0:0` — the material is
 * screenshots and posters, subject at the top, dead margin at the bottom, so `center` would
 * throw away the thing the picture is about.
 *
 * `blur_*` are not equal to any single `object-fit`: they are a baked backdrop plus a foreground
 * that is `contain`-like (`blur_fill`) or **unscaled** (`blur_crop`). Returning `contain` for
 * both is right for `blur_fill`; `blur_crop`'s foreground is positioned separately by the stage.
 */
export function cssFit(fit: string): { objectFit: "fill" | "cover" | "contain"; objectPosition: string } {
  switch (fit) {
    case "stretch":
      return { objectFit: "fill", objectPosition: "center" };
    case "cover":
      return { objectFit: "cover", objectPosition: "left top" };
    case "contain":
    case "blur_fill":
      return { objectFit: "contain", objectPosition: "center" };
    case "blur_crop":
      // No scaling at all: the picture keeps its own pixels and only loses what the box clips.
      return { objectFit: "contain", objectPosition: "left top" };
    default:
      return { objectFit: "contain", objectPosition: "center" };
  }
}

/** One drawable piece of the stage, in bottom-up order. */
export interface DrawLayer {
  /**
   * Stable identity, used as the React key. It encodes the *role* so a re-render moves the same
   * element rather than unmounting an `<img>` and losing its decoded bitmap.
   */
  role: "background" | "overlay" | "media" | "avatar" | "text" | "cue";
  kind: "image" | "video";
  url: string;
  /** Canvas-pixel placement. */
  rect: { x: number; y: number; w: number; h: number };
  fit: string;
  /** Only on `media`: the baked blurred backdrop behind the foreground. */
  backdrop?: string | null;
  /** Only on `media`, and only for a video: where to seek inside it. */
  offsetMs?: number;
  loop?: boolean;
  /** Only on `avatar`: which slot, and which of its two photos is showing. */
  slot?: string;
  /** Only on `avatar`: `true` when the slot is lit at this instant. */
  lit?: boolean;
  /** Only on `avatar`: the dim photo, kept mounted so switching does not decode a new file. */
  dimUrl?: string;
  /**
   * Only on `cue`: the band's own geometry. `url` is null until the band has been baked, which
   * is why the rect has to exist independently of the bytes (§19.31.4).
   */
  top?: number;
  height?: number;
  index?: number;
  blank?: boolean;
}

/**
 * The full stack to draw at one instant, **bottom-up**.
 *
 * This is the one place the layer order is written, and it mirrors `build_filtergraph`:
 * background → 描边 → box content → avatars → text → 全屏 → subtitles. The component draws this
 * array in order, so a reordering bug is a one-line change here that the test next to it fails
 * on, rather than a subtle mis-stack that only shows up in a screenshot comparison.
 */
export function layersAt(plan: VideoPlan, atMs: number): DrawLayer[] {
  const stage = stageAt(plan, atMs);
  const layers: DrawLayer[] = [];
  const canvas = { x: 0, y: 0, w: plan.canvas.w, h: plan.canvas.h };

  layers.push({ role: "background", kind: "image", url: plan.background_url, rect: canvas, fit: "stretch" });
  if (plan.overlay_url) {
    // Above the poster but **below** the box: the chain overlays the artwork layers first.
    layers.push({ role: "overlay", kind: "image", url: plan.overlay_url, rect: canvas, fit: "stretch" });
  }
  // The 全屏 block is the *same* slot as the box content — promoted, not an extra layer. Two
  // elements would leave the box one visible underneath whenever a full-frame block is up.
  if (stage.media) {
    layers.push({
      role: "media",
      kind: stage.media.kind,
      url: stage.media.url,
      rect: stage.media.rect,
      fit: stage.media.fit,
      backdrop: stage.media.backdrop,
      offsetMs: stage.media.offsetMs,
      loop: stage.media.loop,
    });
  }
  for (const avatar of plan.avatars) {
    const lit = stage.lit.includes(avatar.name);
    layers.push({
      role: "avatar",
      kind: "image",
      url: lit ? avatar.lit_url : avatar.dim_url,
      dimUrl: avatar.dim_url,
      rect: avatar.rect,
      fit: "stretch",
      slot: avatar.name,
      lit,
    });
  }
  if (plan.text_url) {
    layers.push({ role: "text", kind: "image", url: plan.text_url, rect: canvas, fit: "stretch" });
  }
  if (stage.cue) {
    layers.push({
      role: "cue",
      kind: "image",
      url: stage.cue.url ?? "",
      rect: { x: 0, y: stage.cue.top, w: plan.canvas.w, h: stage.cue.height },
      fit: "contain",
      top: stage.cue.top,
      height: stage.cue.height,
      index: stage.cue.index,
      // A cue whose band is still being baked must keep its element: mounting it later would
      // shift every layer above it and repaint the whole stage.
      blank: stage.cue.url === null,
    });
  }
  return layers;
}

/** Whether a segment's foreground is drawn at its natural size rather than fitted. */
export function isUnscaled(fit: string): boolean {
  return fit === "blur_crop";
}

/** Whether the segment needs the baked backdrop behind its foreground. */
export function needsBackdrop(fit: string): boolean {
  return fit === "blur_fill" || fit === "blur_crop";
}

/**
 * Cap the stage's rendered height, in canvas pixels.
 *
 * §19.32's export row: while a 14-minute x264 encode is running, the preview must not compete
 * for CPU. The cheapest honest degradation is to composite fewer pixels — the stage still shows the
 * right layout, cue and segment, just smaller. 444 px is §19.32's number (a quarter of 1080p in
 * area is 540 px, so 444 is deliberately less).
 */
export const DEGRADED_STAGE_HEIGHT = 444;

/**
 * The scale to draw the stage at, given the panel's natural fit and whether a degradation applies.
 *
 * Never *enlarges*: a small panel keeps its own scale even when the budget is bigger.
 */
export function stageScale(fitScale: number, canvasHeight: number, degrade: boolean): number {
  if (!Number.isFinite(fitScale) || fitScale <= 0) return 0;
  if (!degrade || canvasHeight <= 0) return fitScale;
  const budget = DEGRADED_STAGE_HEIGHT / canvasHeight;
  return Math.min(fitScale, budget);
}

