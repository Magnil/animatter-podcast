/**
 * Pure layout arithmetic for the video timeline (AGENTS.md §19.3 / §19.7).
 *
 * The video track is drawn on the same clock as the waveform and reuses
 * `waveView.ts` for zoom/pan; what is specific to it lives here. Three things are
 * worth keeping out of the component:
 *
 * 1. **Two lengths per block.** A block's own edges span `duration_ms`, but the film
 *    plays it for `output_ms`, which is shorter whenever audio inside it was cut. The
 *    block must be drawn at its *original* width (that is the timeline it lives on),
 *    with the output length shown as text — drawing it at the output width would make
 *    blocks overlap each other after an edit.
 *
 * 2. **Snapping is to collapsed targets, not sentence starts.** Boundaries closer
 *    together than 500 ms are one drag target on the server, so the client must snap
 *    to the same list or a drop will appear to move after the round-trip.
 *
 * 3. **Gallery pacing is proportional.** Deleting a second of audio scales every
 *    image's dwell time by `output/duration` and keeps the number of turns. That is
 *    the chosen behaviour (round 86), and it is worth surfacing as text: a 3-second
 *    cut on 8 images makes each one 12 % shorter, which looks like accidental
 *    fast-forward unless the UI says so.
 */

export interface View {
  from: number;
  to: number;
}

export interface BlockSpan {
  start_ms: number;
  end_ms: number;
  output_ms: number;
  duration_ms: number;
}

/** Where a block sits on screen, in CSS pixels, for a given view and width. */
export interface BlockLayout {
  left: number;
  width: number;
  /** Fraction of the block that survives in the film (0..1). */
  keptRatio: number;
}

/**
 * X position of an instant, given the visible window and the track width.
 *
 * Clamped to the view rather than to the media: a block that starts before the
 * window must be drawn from the left edge, and letting it go negative would put a
 * scrollbar-width of invisible content in front of the canvas.
 */
export function msToX(ms: number, view: View, width: number): number {
  const span = Math.max(1, view.to - view.from);
  const ratio = (ms - view.from) / span;
  return Math.max(0, Math.min(width, ratio * width));
}

/** Inverse of `msToX`, for turning a pointer position into an instant. */
export function xToMs(x: number, view: View, width: number): number {
  const span = Math.max(1, view.to - view.from);
  const ratio = width <= 0 ? 0 : Math.max(0, Math.min(1, x / width));
  return Math.round(view.from + ratio * span);
}

/**
 * Lay a block out on the track.
 *
 * Width comes from the block's own edges, never from `output_ms`: two adjacent
 * blocks that share a boundary would otherwise leave a gap as soon as one of them
 * lost audio to a cut, and the gap reads as "there is no video here" when in fact
 * the block plays straight through.
 */
export function layoutBlock(block: BlockSpan, view: View, width: number): BlockLayout {
  const left = msToX(block.start_ms, view, width);
  const right = msToX(block.end_ms, view, width);
  const total = Math.max(1, block.end_ms - block.start_ms);
  return {
    left,
    width: Math.max(0, right - left),
    keptRatio: Math.max(0, Math.min(1, block.output_ms / total)),
  };
}

/**
 * Snap an edge to the nearest drag target within `radiusMs`.
 *
 * Mirrors the server's rule exactly — including the tie going to the *earlier*
 * boundary — because a client that snaps differently produces a block that jumps
 * the moment the response comes back.
 */
export function snapEdge(ms: number, targets: number[], radiusMs = 120): number {
  if (targets.length === 0 || radiusMs <= 0) return ms;
  let best = ms;
  let bestDistance = radiusMs + 1;
  for (const point of targets) {
    const distance = Math.abs(point - ms);
    if (distance < bestDistance) {
      best = point;
      bestDistance = distance;
    }
  }
  return bestDistance <= radiusMs ? best : ms;
}

/**
 * How long one image stays on screen in a gallery block.
 *
 * Proportional (round 86 choice): every image is scaled by the same factor and the
 * number of turns is unchanged. Returns `0` for an empty gallery rather than
 * dividing by zero — an empty block is a valid intermediate state while the user is
 * still dropping images in.
 */
export function galleryDwellMs(block: BlockSpan, imageCount: number): number {
  if (imageCount <= 0) return 0;
  return Math.round(block.output_ms / imageCount);
}

/** `"8 张 · 每张 1.9s（原 2.2s）"` — the speed change has to be visible. */
export function galleryCaption(block: BlockSpan, imageCount: number): string {
  if (imageCount <= 0) return "画廊（还没选图片）";
  const dwell = galleryDwellMs(block, imageCount);
  const original = Math.round(block.duration_ms / imageCount);
  const now = (dwell / 1000).toFixed(1);
  if (dwell === original) return `${imageCount} 张 · 每张 ${now}s`;
  return `${imageCount} 张 · 每张 ${now}s（原 ${(original / 1000).toFixed(1)}s）`;
}

/** `"原始 10.0s / 成片 8.0s"`, or just the one number when nothing was cut. */
export function lengthCaption(block: BlockSpan): string {
  const original = (block.duration_ms / 1000).toFixed(1);
  const output = (block.output_ms / 1000).toFixed(1);
  if (block.output_ms === block.duration_ms) return `${original}s`;
  return `原始 ${original}s / 成片 ${output}s`;
}

/** Blocks whose audio was deleted in full, sorted so the list is stable. */
export function orphanedBlocks<T extends { orphaned: boolean; start_ms: number }>(blocks: T[]): T[] {
  return blocks.filter((block) => block.orphaned).sort((a, b) => a.start_ms - b.start_ms);
}

/**
 * Where a new block should land when something is dropped, and how big it should be.
 *
 * Two rules, both learned from how people actually drop things:
 *
 * 1. **Snap the start, then choose the end.** A drop lands at a sentence start, and the
 *    block then runs to the *next* boundary rather than a fixed default length. A fixed
 *    5 s default ignores the material: a 2-sentence aside is 3 s and the next topic is
 *    40 s, so any constant is wrong most of the time.
 * 2. **Never overlap an existing block.** Dropping onto an occupied stretch would create
 *    a block that mostly does not play (the chain trims overlaps), which looks like the
 *    drop failed. Landing in the gap after the occupying block is the honest answer, and
 *    it is also what the user meant by dropping there.
 *
 * Returns `null` when there is no room after `atMs` — the caller tells the user rather
 * than creating a zero-length block.
 */
export function dropRange(
  atMs: number,
  targets: number[],
  durationMs: number,
  existing: { start_ms: number; end_ms: number }[],
): { start_ms: number; end_ms: number } | null {
  const sorted = [...existing].sort((a, b) => a.start_ms - b.start_ms);
  let start = snapEdge(atMs, targets);
  // Slide past anything the drop landed inside.
  for (const block of sorted) {
    if (start >= block.start_ms && start < block.end_ms) start = block.end_ms;
  }
  const nextTarget = targets.filter((point) => point > start).sort((a, b) => a - b)[0];
  const occupied = sorted.filter((block) => block.start_ms >= start);
  const ceiling = occupied.length > 0 ? (occupied[0]?.start_ms ?? durationMs) : durationMs;
  const end = Math.min(nextTarget ?? durationMs, ceiling, durationMs);
  if (end - start < MIN_BLOCK_MS) return null;
  return { start_ms: start, end_ms: end };
}

/**
 * Shortest block worth creating. Below this the picture is a flash, and the server
 * refuses blocks under one millisecond anyway — saying so here gives a message instead of
 * a 400.
 */
export const MIN_BLOCK_MS = 200;

/** Which block sits at `ms`, topmost last so a later block wins the hit test. */
export function blockAt<T extends { start_ms: number; end_ms: number }>(ms: number, blocks: T[]): T | null {
  let found: T | null = null;
  for (const block of blocks) {
    if (block.start_ms <= ms && ms < block.end_ms) found = block;
  }
  return found;
}

/** Drag a whole block by `deltaMs`, then snap its start and keep its length. */
export function moveBlock(
  block: { start_ms: number; end_ms: number },
  deltaMs: number,
  targets: number[],
  durationMs: number,
): { start_ms: number; end_ms: number } {
  const length = block.end_ms - block.start_ms;
  const snapped = snapEdge(block.start_ms + deltaMs, targets);
  const start = Math.max(0, Math.min(snapped, Math.max(0, durationMs - length)));
  return { start_ms: start, end_ms: start + length };
}

/** Resize one edge, refusing to invert the block. */
export function resizeBlock(
  block: { start_ms: number; end_ms: number },
  edge: "start" | "end",
  ms: number,
  targets: number[],
): { start_ms: number; end_ms: number } {
  const snapped = snapEdge(ms, targets);
  if (edge === "start") {
    return { start_ms: Math.min(snapped, block.end_ms - MIN_BLOCK_MS), end_ms: block.end_ms };
  }
  return { start_ms: block.start_ms, end_ms: Math.max(snapped, block.start_ms + MIN_BLOCK_MS) };
}

/** The view window needed to show a block, keeping the current span if it fits. */
export function viewAround(ms: number, view: View, total: number): View {
  const span = Math.max(MIN_BLOCK_MS, view.to - view.from);
  const from = Math.max(0, Math.min(ms - span / 2, Math.max(0, total - span)));
  return { from, to: from + span };
}

/**
 * What the collapsed summary line says.
 *
 * Deliberately counts *blocks*, not seconds: "视频：模板已套用 · 4 个素材块" answers "is this
 * project using video?" at a glance, which is the only question a collapsed panel has to
 * answer.
 */
export function summaryLine(
  templateStatus: string | null,
  blockCount: number,
  orphans: number,
): string {
  if (templateStatus === null) return "未启用";
  if (templateStatus === "failed") return "模板编译失败";
  if (templateStatus === "pending" || templateStatus === "compiling") return "模板编译中…";
  const base = blockCount === 0 ? "模板已套用 · 还没有素材块" : `模板已套用 · ${blockCount} 个素材块`;
  return orphans > 0 ? `${base}（${orphans} 个待处理）` : base;
}
