/**
 * The canvas editor's arithmetic (§19.38, round 139) — pure, so it can be tested without a DOM.
 *
 * Every function here answers "where does the rectangle end up", and each one has a wrong answer
 * that looks right on screen:
 *
 * * **Clamping, not refusing.** A drag that leaves the canvas is a drag the user already made.
 *   Snapping it back to the edge shows them the limit; rejecting the drop would leave the
 *   rectangle under their cursor and the stored value somewhere else.
 * * **The box stays even.** libx264 with `yuv420p` needs even dimensions, and an odd box fails the
 *   encode minutes later with a message about pixel formats — after the user has waited.
 * * **Avatars keep their aspect.** A 177×188 frame resized to 300×90 is a face squeezed into a
 *   letterbox slot; the PSD's frame is drawn at its own ratio, so the box may only scale.
 * * **Type follows the smaller factor.** A text box dragged wider but not taller must not produce
 *   type taller than the box (⑬) — otherwise `_fit` shrinks it back and the resize looks ignored.
 */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type SlotKind = "text" | "avatar";

export type Handle = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

export interface Canvas {
  w: number;
  h: number;
}

/** Smallest rectangle each kind may become. Mirrors `core/layout.py`; the server clamps too. */
export const MIN_SIZE: Record<SlotKind | "box", { w: number; h: number }> = {
  box: { w: 160, h: 90 },
  text: { w: 8, h: 8 },
  avatar: { w: 24, h: 24 },
};

/**
 * How close two edges have to be before the drag is pulled onto them, in canvas pixels.
 *
 * 6 px at a 0.6 fit scale is about 4 screen pixels: close enough to feel magnetic, far enough
 * that a deliberate 5 px offset is still reachable.
 */
export const SNAP_PX = 6;

const HANDLES: Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

export function evenValue(value: number): number {
  const rounded = Math.round(value);
  return rounded - (rounded % 2);
}

export function clampRect(
  rect: Rect,
  canvas: Canvas,
  min: { w: number; h: number },
  options: { even?: boolean } = {},
): Rect {
  const maxW = Math.min(min.w, canvas.w);
  const maxH = Math.min(min.h, canvas.h);
  let w = Math.min(Math.max(rect.w, maxW), canvas.w);
  let h = Math.min(Math.max(rect.h, maxH), canvas.h);
  if (options.even) {
    // Round down so an even box never exceeds the canvas on an odd canvas.
    w = Math.min(evenValue(w), evenValue(canvas.w));
    h = Math.min(evenValue(h), evenValue(canvas.h));
  }
  const x = Math.min(Math.max(0, rect.x), canvas.w - w);
  const y = Math.min(Math.max(0, rect.y), canvas.h - h);
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

export function moveRect(
  start: Rect,
  dx: number,
  dy: number,
  canvas: Canvas,
  min: { w: number; h: number },
  options: { even?: boolean } = {},
): Rect {
  return clampRect({ x: start.x + dx, y: start.y + dy, w: start.w, h: start.h }, canvas, min, options);
}

/**
 * Resize from one edge or corner. The opposite edge stays put — that is what makes a resize feel
 * anchored, and it is why this cannot be expressed as a `moveRect` with a different size.
 */
export function resizeRect(
  start: Rect,
  handle: Handle,
  dx: number,
  dy: number,
  canvas: Canvas,
  min: { w: number; h: number },
  options: { even?: boolean; aspect?: boolean } = {},
): Rect {
  let { x, y, w, h } = start;
  const right = start.x + start.w;
  const bottom = start.y + start.h;
  if (handle.includes("w")) {
    x = Math.min(x + dx, right - min.w);
    w = right - x;
  }
  if (handle.includes("e")) {
    w = Math.max(min.w, w + dx);
  }
  if (handle.includes("n")) {
    y = Math.min(y + dy, bottom - min.h);
    h = bottom - y;
  }
  if (handle.includes("s")) {
    h = Math.max(min.h, h + dy);
  }
  if (options.aspect) {
    // Corner drags scale the frame by the dominant axis, then re-derive the other one from the
    // compiled ratio. Edge drags leave the ratio alone rather than snapping to it mid-gesture.
    const corner = handle === "nw" || handle === "ne" || handle === "sw" || handle === "se";
    if (corner && start.w > 0 && start.h > 0) {
      const factor = Math.max(w / start.w, h / start.h);
      const scaled = { w: Math.round(start.w * factor), h: Math.round(start.h * factor) };
      if (handle.includes("w")) {
        w = scaled.w;
        x = right - w;
      } else {
        w = scaled.w;
      }
      if (handle.includes("n")) {
        h = scaled.h;
        y = bottom - h;
      } else {
        h = scaled.h;
      }
    }
  }
  return clampRect({ x, y, w, h }, canvas, min, options);
}

export function nudgeRect(
  rect: Rect,
  dx: number,
  dy: number,
  canvas: Canvas,
  min: { w: number; h: number },
  options: { even?: boolean } = {},
): Rect {
  return moveRect(rect, dx, dy, canvas, min, options);
}

/** The eight grab points, in canvas coordinates. */
export function handlePoints(rect: Rect): Record<Handle, { x: number; y: number }> {
  const midX = rect.x + rect.w / 2;
  const midY = rect.y + rect.h / 2;
  const right = rect.x + rect.w;
  const bottom = rect.y + rect.h;
  return {
    nw: { x: rect.x, y: rect.y },
    n: { x: midX, y: rect.y },
    ne: { x: right, y: rect.y },
    e: { x: right, y: midY },
    se: { x: right, y: bottom },
    s: { x: midX, y: bottom },
    sw: { x: rect.x, y: bottom },
    w: { x: rect.x, y: midY },
  };
}

/**
 * Which handle is under the cursor, or `null`.
 *
 * Tolerance is in **canvas pixels** and the caller passes `screenTolerance / scale`, so the grab
 * area stays 8 screen pixels whether the canvas is shown at 0.4 or 1.0.
 */
export function hitHandle(rect: Rect, point: { x: number; y: number }, tolerance: number): Handle | null {
  const points = handlePoints(rect);
  let best: Handle | null = null;
  let bestDistance = tolerance;
  for (const handle of HANDLES) {
    const distance = Math.hypot(points[handle].x - point.x, points[handle].y - point.y);
    if (distance <= bestDistance) {
      best = handle;
      bestDistance = distance;
    }
  }
  return best;
}

/** Convert a pointer position to canvas pixels. `host` is the scaled canvas element's rect. */
export function toCanvasPoint(
  client: { x: number; y: number },
  host: { left: number; top: number },
  scale: number,
): { x: number; y: number } {
  if (scale <= 0) return { x: 0, y: 0 };
  return { x: (client.x - host.left) / scale, y: (client.y - host.top) / scale };
}

export interface SnapTargets {
  canvas: Canvas;
  /** Edges and centres of every *other* slot, so one box can line up with another. */
  others: Rect[];
}

export interface SnapResult {
  x: number;
  y: number;
  /** Canvas-space lines to draw, so the snap is visible instead of merely felt. */
  guides: { axis: "x" | "y"; at: number }[];
}

/**
 * Pull a moved rectangle onto nearby edges and centres.
 *
 * Candidates are the canvas edges and its midpoint plus the other slots' edges and midpoints. The
 * rectangle's own left/centre/right are each tested, which is what makes "align this avatar with
 * that one" work from either side.
 */
export function snapRect(rect: Rect, targets: SnapTargets, tolerance: number = SNAP_PX): SnapResult {
  const candidatesX: number[] = [0, targets.canvas.w / 2, targets.canvas.w];
  const candidatesY: number[] = [0, targets.canvas.h / 2, targets.canvas.h];
  for (const other of targets.others) {
    candidatesX.push(other.x, other.x + other.w / 2, other.x + other.w);
    candidatesY.push(other.y, other.y + other.h / 2, other.y + other.h);
  }
  const guides: SnapResult["guides"] = [];

  const bestX = nearest(
    [rect.x, rect.x + rect.w / 2, rect.x + rect.w],
    candidatesX,
    tolerance,
  );
  const bestY = nearest(
    [rect.y, rect.y + rect.h / 2, rect.y + rect.h],
    candidatesY,
    tolerance,
  );
  let x = rect.x;
  let y = rect.y;
  if (bestX !== null) {
    x = rect.x + bestX.delta;
    guides.push({ axis: "x", at: bestX.at });
  }
  if (bestY !== null) {
    y = rect.y + bestY.delta;
    guides.push({ axis: "y", at: bestY.at });
  }
  return { x, y, guides };
}

function nearest(
  edges: number[],
  candidates: number[],
  tolerance: number,
): { delta: number; at: number } | null {
  let best: { delta: number; at: number } | null = null;
  for (const edge of edges) {
    for (const candidate of candidates) {
      const delta = candidate - edge;
      if (Math.abs(delta) > tolerance) continue;
      if (best === null || Math.abs(delta) < Math.abs(best.delta)) {
        best = { delta, at: candidate };
      }
    }
  }
  return best;
}

/** "x 240 · y 209 · 1440 × 810", the readout under the canvas. */
export function describe(rect: Rect): string {
  return `x ${Math.round(rect.x)} · y ${Math.round(rect.y)} · ${Math.round(rect.w)} × ${Math.round(rect.h)}`;
}

/**
 * The request body for one slot, in the shape `PUT /video/layout` takes.
 *
 * `replace` is not passed: the payload always carries the slot's full rect, and the server merges
 * by key, so "moved back to where the compile put it" is the same call as any other move.
 */
export function payloadFor(
  kind: SlotKind | "box",
  key: string,
  rect: Rect,
): { box?: Rect; texts?: Record<string, Rect>; avatars?: Record<string, Rect> } {
  if (kind === "box") return { box: roundRect(rect) };
  if (kind === "avatar") return { avatars: { [key]: roundRect(rect) } };
  return { texts: { [key]: roundRect(rect) } };
}

function roundRect(rect: Rect): Rect {
  return {
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    w: Math.round(rect.w),
    h: Math.round(rect.h),
  };
}

/** Whether a slot has moved from where the compile put it — within a pixel, because of rounding. */
export function isMoved(rect: Rect, compiled: Rect): boolean {
  return (
    Math.abs(rect.x - compiled.x) > 1 ||
    Math.abs(rect.y - compiled.y) > 1 ||
    Math.abs(rect.w - compiled.w) > 1 ||
    Math.abs(rect.h - compiled.h) > 1
  );
}

/** The type size a resize produces, for the readout. `null` when the slot has no font size. */
export function scaledFont(
  font: number | undefined,
  compiled: Rect,
  moved: Rect,
): number | null {
  if (font === undefined || font <= 0 || compiled.w <= 0 || compiled.h <= 0) return null;
  const factor = Math.min(moved.w / compiled.w, moved.h / compiled.h);
  return Math.max(2, evenValue(font * factor));
}
