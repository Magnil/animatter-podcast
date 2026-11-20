/**
 * Two `<video>` slots, so a block switch costs no decode (§19.32, P2).
 *
 * **The defect this exists for.** At a video→video boundary the media layer used to change key,
 * React unmounted the playing `<video>` and mounted a fresh one, and `.layer-media` has
 * `background: #000`. Measured by instrumenting every animation frame inside the page
 * (`scripts/check-p2-switch.mjs`, round 130): the new element appears at `readyState 1`, then
 * reaches `readyState 4` with presented frames **116 ms later**. At 24 fps that is nearly three
 * film frames of black box in the middle of a cut — and a "switch latency" number happily passes
 * it, because it only asks when the DOM settled.
 *
 * **The fix is element reuse, not a faster load.** A second, hidden element holds the *next* video
 * segment `PREWARM_LEAD_MS` early, so it is already decoded and paused at its entry frame when the
 * boundary arrives; the switch is then a visibility flip between two elements that both already
 * hold frames.
 *
 * **Why this is a pure function and not a state machine.** Slot *k* always holds the *k*-th video
 * segment of the plan (`ordinal % 2`). Consecutive video ordinals therefore land in different
 * slots by construction, which makes "the standby already holds what I need" a property of the
 * plan rather than of the browsing history — no refs, no `useEffect` driving a queue, and the whole
 * thing is testable without a browser. The trade is that the *previous* clip is not kept hot; on a
 * 64-segment plan with 13 videos, jumping back across a cut is a rarer gesture than playing
 * through one.
 */

/** Everything about a slot that is not the playhead position. */
export interface DeckSlot {
  url: string;
  loop: boolean;
  fit: string;
  /** Only for the two blur fits: the baked backdrop URL. */
  backdrop: string | null;
  /** Canvas-pixel placement. Copied per slot so a flip also moves the box. */
  rect: { x: number; y: number; w: number; h: number };
}

/** A slot together with the video ordinal it holds. */
export interface ResidentSlot extends DeckSlot {
  ordinal: number;
}

export interface DeckLayout {
  /** Indexed by slot. `null` means "nothing loaded here" — one decoder, not two. */
  slots: [ResidentSlot | null, ResidentSlot | null];
  /** Which slot is on screen, or `null` when the current segment is not a video. */
  front: 0 | 1 | null;
  /** The video ordinal the playhead is inside, if any. */
  current: number | null;
  /** The ordinal warmed in the other slot, if any. */
  warmed: number | null;
  /** True when the second slot was deliberately given up (§19.32). */
  degraded: boolean;
  reason: "export" | "memory" | null;
}

/** Slot index for a video ordinal. The only place this arithmetic lives. */
export function slotOf(ordinal: number): 0 | 1 {
  return (ordinal % 2 === 0 ? 0 : 1) as 0 | 1;
}

/**
 * How far ahead of a block boundary the next video is fetched and decoded.
 *
 * 2.5 s comes from the measurement, not from taste: a cold `<video>` needs **116 ms** to present a
 * frame (above), so a second is already generous — but a *seek* into an element costs up to
 * **406 ms** on the sample's 4K clips (`scripts/check-p2-seek.mjs`), and the lead has to cover
 * whichever the browser does. The upper bound matters too: warming further ahead would keep the
 * second decoder busy for minutes of wall clock, which §19.32's memory row forbids.
 */
export const PREWARM_LEAD_MS = 2_500;

/**
 * How full the JS heap has to be before the stage gives up its second decoder.
 *
 * §19.32 says "memory pressure" without a number. 0.85 is Chromium's own ballpark for "collect
 * aggressively"; below it the extra element is a small fraction of the budget, and giving it up
 * would only buy back the black frames this module exists to remove.
 */
export const HEAP_PRESSURE_RATIO = 0.85;

/**
 * Whether the stage is under memory pressure.
 *
 * `performance.memory` is Chromium-only and non-standard, so a missing one means **no pressure**,
 * not "assume the worst": this gates a nicety, and failing it closed would disable prewarming on
 * every browser but Chrome for no reason.
 */
export function underHeapPressure(
  memory: { usedJSHeapSize?: number; jsHeapSizeLimit?: number } | undefined,
  ratio: number = HEAP_PRESSURE_RATIO,
): boolean {
  const used = memory?.usedJSHeapSize;
  const limit = memory?.jsHeapSizeLimit;
  if (typeof used !== "number" || typeof limit !== "number" || limit <= 0) return false;
  if (!Number.isFinite(used) || !Number.isFinite(limit)) return false;
  return used / limit >= ratio;
}

/** The pieces of a segment the deck needs. Structural so this module does not import the plan. */
export interface DeckSegment {
  start_ms: number;
  end_ms: number;
  kind: string;
  fit: string;
  loop: boolean;
  url: string;
  backdrop_url: string | null;
  placement: string;
}

export interface DeckGeometry {
  canvas: { w: number; h: number };
  box: { x: number; y: number; w: number; h: number };
}

export interface DeckOptions {
  /** An export is running: the stage must not compete for CPU (§19.32). */
  exportRunning?: boolean;
  /** Supplied by tests; the component reads `performance.memory` itself. */
  memory?: { usedJSHeapSize?: number; jsHeapSizeLimit?: number };
  leadMs?: number;
}

function rectOf(segment: DeckSegment, geometry: DeckGeometry): DeckSlot["rect"] {
  return segment.placement === "full"
    ? { x: 0, y: 0, w: geometry.canvas.w, h: geometry.canvas.h }
    : { x: geometry.box.x, y: geometry.box.y, w: geometry.box.w, h: geometry.box.h };
}

function resident(segment: DeckSegment, ordinal: number, geometry: DeckGeometry): ResidentSlot {
  return {
    ordinal,
    url: segment.url,
    loop: segment.loop,
    fit: segment.fit,
    backdrop: segment.backdrop_url,
    rect: rectOf(segment, geometry),
  };
}

/**
 * Lay out the two slots for one instant.
 *
 * The residents are always the **held** video (the one the playhead is inside, or the last one it
 * passed) and the **next** video if that is inside the lead. Held and next are consecutive video
 * ordinals, so they can never collide in a slot; nothing here needs to look at history.
 *
 * Degradation (§19.32) drops the second decoder. That is a real trade — at the next boundary the
 * incoming element has to load from cold, worth the 116 ms of black box this module removes — and
 * it is why the caller surfaces `reason` rather than silently doing it.
 */
export function deckLayout(
  segments: readonly DeckSegment[],
  geometry: DeckGeometry,
  atMs: number,
  options: DeckOptions = {},
): DeckLayout {
  const lead = options.leadMs ?? PREWARM_LEAD_MS;
  const videos: DeckSegment[] = segments.filter((segment) => segment.kind === "video");

  let current: number | null = null;
  let lastPlayed: number | null = null;
  let next: number | null = null;
  for (let ordinal = 0; ordinal < videos.length; ordinal += 1) {
    const segment = videos[ordinal];
    if (segment === undefined) continue;
    if (atMs >= segment.start_ms && atMs < segment.end_ms) current = ordinal;
    else if (segment.end_ms <= atMs) lastPlayed = ordinal;
    else if (next === null) next = ordinal;
  }

  const reason: DeckLayout["reason"] = options.exportRunning
    ? "export"
    : underHeapPressure(options.memory)
      ? "memory"
      : null;

  if (reason !== null) {
    // One decoder, and only while it is the one on screen. Keeping the "held" clip resident through
    // a stretch of image segments would be exactly the CPU the export needs.
    if (current === null) {
      return { slots: [null, null], front: null, current, warmed: null, degraded: true, reason };
    }
    const only = videos[current];
    const slots: [ResidentSlot | null, ResidentSlot | null] = [null, null];
    slots[slotOf(current)] =
      only === undefined ? null : resident(only, current, geometry);
    return { slots, front: slotOf(current), current, warmed: null, degraded: true, reason };
  }

  const held = current ?? lastPlayed;
  const nextSegment = next === null ? undefined : videos[next];
  const warmOrdinal =
    next !== null && next !== held && nextSegment !== undefined && nextSegment.start_ms - atMs <= lead
      ? next
      : null;

  const slots: [ResidentSlot | null, ResidentSlot | null] = [null, null];
  if (held !== null) {
    const segment = videos[held];
    if (segment !== undefined) slots[slotOf(held)] = resident(segment, held, geometry);
  }
  if (warmOrdinal !== null) {
    const segment = videos[warmOrdinal];
    if (segment !== undefined) slots[slotOf(warmOrdinal)] = resident(segment, warmOrdinal, geometry);
  }

  return {
    slots,
    front: current === null ? null : slotOf(current),
    current,
    warmed: warmOrdinal,
    degraded: false,
    reason: null,
  };
}

/**
 * Whether a film export is in flight.
 *
 * A module-level flag rather than a prop: the export lives in the 导出 menu (`App`), the stage lives
 * three components down inside `VideoPanel`, and the only thing the stage needs is this boolean.
 * Threading it through as a prop would touch every layer between them for a value that changes
 * twice per export.
 */
let exportRunning = false;

export function setExportRunning(value: boolean): void {
  exportRunning = value;
}

export function readExportRunning(): boolean {
  return exportRunning;
}

/**
 * `performance.memory`, typed as the non-standard extra it is.
 *
 * Absent from `lib.dom.d.ts` — it is a Chromium extension — and typing it as `any` would hide the
 * fact that every other browser returns `undefined`, which `underHeapPressure` treats as "no
 * pressure" rather than "assume the worst".
 */
let memoryOverride: { usedJSHeapSize?: number; jsHeapSizeLimit?: number } | null = null;

export function readMemoryOf():
  | { usedJSHeapSize?: number; jsHeapSizeLimit?: number }
  | undefined {
  if (memoryOverride !== null) return memoryOverride;
  return (
    performance as unknown as {
      memory?: { usedJSHeapSize?: number; jsHeapSizeLimit?: number };
    }
  ).memory;
}

/**
 * Pretend the heap is full (or stop pretending).
 *
 * Used by `scripts/check-p2-cut.mjs` to A/B the standby on and off **at the same stage scale**:
 * forcing the export flag would also shrink the stage to 444 px (§19.32), which changes how much
 * the compositor has to do and therefore what the measurement is measuring. `null` restores the
 * real `performance.memory`.
 */
export function setMemoryOverride(
  value: { usedJSHeapSize?: number; jsHeapSizeLimit?: number } | null,
): void {
  memoryOverride = value;
}

/*
  A debug handle, so the two-slot deck can be A/B measured against its own degraded path.

  `scripts/check-p2-cutpicture.mjs` needs to turn the standby off *in the page* and measure the
  same build with one behaviour toggled. The alternative — stashing the change and measuring the
  stashed checkout — measures a different page (different code split, different cache state), which
  is not an A/B at all.

  Two functions and a getter on `window`, no behaviour change; the names are prefixed `__p2` so
  nothing mistakes it for an API.
*/
if (typeof window !== "undefined") {
  (window as unknown as { __p2?: unknown }).__p2 = {
    setExportRunning,
    readExportRunning,
    readMemoryOf,
    setMemoryOverride,
  };
}
