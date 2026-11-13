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

/**
 * What a block is called on the track.
 *
 * The server sends a `label` — the imported file's own name, the deck's title, or the first
 * gallery member's name — and it is empty in exactly one normal case: a block the user
 * created before choosing anything. Falling back to the kind keeps those blocks from
 * rendering as a blank rectangle, which reads as a rendering bug rather than as "no file
 * picked yet".
 */
export function blockName(block: { label?: string; kind: string }): string {
  if (block.label) return block.label;
  if (block.kind === "gallery") return "画廊";
  if (block.kind === "slide") return "演示稿";
  return "素材";
}

/**
 * The four fit strategies, with the words the UI uses for them.
 *
 * Lives here rather than in `VideoPanel` because two places offer the same choice now: the
 * 模板 tab's 「框内默认填充」 and the bubble that opens on a selected block. Two copies of
 * this list would drift the moment one gained a strategy, and the drift would look like the
 * bubble silently refusing a fit the template offers.
 */
export const FITS: { id: string; label: string; hint: string }[] = [
  { id: "blur_fill", label: "模糊填充", hint: "竖图放进 16:9 框：模糊放大作底 + 完整居中" },
  {
    id: "blur_crop",
    label: "模糊裁切",
    hint: "按原图像素摆放、顶边中点对齐，溢出裁掉，没满的地方模糊填充（图不会被放大，最清晰）",
  },
  { id: "contain", label: "完整显示", hint: "留边，不裁切" },
  { id: "cover", label: "填满裁切", hint: "铺满，裁掉溢出" },
  { id: "stretch", label: "拉伸", hint: "会变形" },
];

/**
 * Where a block is drawn: inside the template's material box, or over the whole canvas.
 *
 * Shown **before** the fit list in the bubble (§19.25) because it answers a bigger question:
 * the fit decides how the picture fills its rectangle, and this decides which rectangle. Picking
 * 全屏 with 完整显示 is meaningful; so is 素材框 with 填满裁切.
 *
 * Two entries, no hints — the bubble must stay short, and both words say what they do.
 */
export const PLACEMENTS: { id: string; label: string }[] = [
  { id: "box", label: "素材框" },
  { id: "full", label: "全屏" },
];

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

/**
 * Whether any *other* block covers part of ``[from, to)``.
 *
 * Used by both shortcuts on the region they are about to **newly** cover, not on the whole new
 * span: real timelines contain blocks that already overlap their neighbour by a few
 * milliseconds, and refusing to move those would make the keys dead on exactly the blocks a
 * user is trying to tidy up. What must never happen is covering something that was clear.
 */
function overlapsOther<T extends { start_ms: number; end_ms: number }>(
  others: T[],
  block: { start_ms: number; end_ms: number },
  from: number,
  to: number,
): boolean {
  if (to <= from) return false;
  for (const other of others) {
    if (other === block) continue;
    if (from < other.end_ms && other.start_ms < to) return true;
  }
  return false;
}

/** The neighbouring block on one side, ignoring the block being moved. */
function neighbourOf<T extends { start_ms: number; end_ms: number }>(
  block: { start_ms: number; end_ms: number },
  others: T[],
  direction: "prev" | "next",
): T | null {
  let best: T | null = null;
  for (const other of others) {
    // Identity, not coordinates: two blocks can legitimately have the same edges (a stack at
    // one instant), and skipping both would make the jump miss the neighbour entirely.
    if (other === block) continue;
    if (direction === "prev") {
      // Ends at or before this block starts.
      if (other.end_ms <= block.start_ms && (best === null || other.end_ms > best.end_ms)) {
        best = other;
      }
    } else if (other.start_ms >= block.end_ms && (best === null || other.start_ms < best.start_ms)) {
      best = other;
    }
  }
  return best;
}

/**
 * Stretch one edge until it meets its neighbour, leaving the other edge where it is.
 *
 * This is what ``Shift+←`` / ``Shift+→`` do, and it is deliberately the *sibling* of
 * :func:`abutNeighbour` rather than a mode of it: that one keeps the length and moves the block,
 * this one keeps one edge and changes the length. Same neighbour, opposite verb — and the two
 * keys are next to each other on purpose, because "put this block here" and "make this block
 * reach there" are the two things one wants after dropping something in roughly the right place.
 *
 * ``Shift+→`` pulls the **end** to the next block's start; ``Shift+←`` pulls the **start** to
 * the previous block's end. Both only ever *grow* the block, because a neighbour is defined as
 * sitting wholly on one side: reaching it is always outward. That is also the only direction the
 * request asked for — 「延长到贴到前一个 / 后一个素材上」 — and a key that sometimes silently ate a
 * block would be worse than one that occasionally has nothing to do.
 *
 * Returns ``null`` when there is nothing to do: no neighbour on that side, or the edge is
 * already there (which is what makes the key quiet on the second press).
 */
export function stretchToNeighbour<T extends { start_ms: number; end_ms: number }>(
  block: { start_ms: number; end_ms: number },
  direction: "prev" | "next",
  others: T[],
  durationMs: number,
): { start_ms: number; end_ms: number } | null {
  const neighbour = neighbourOf(block, others, direction);
  if (neighbour === null) return null;

  const start = direction === "prev" ? neighbour.end_ms : block.start_ms;
  const end = direction === "next" ? neighbour.start_ms : block.end_ms;
  if (start === block.start_ms && end === block.end_ms) return null;
  if (start < 0 || end > durationMs) return null;
  // A block standing *inside* the gap is not a "next" neighbour (it does not start at or after
  // this block's end), but the stretched edge would still land on top of it — and an overlapped
  // block is trimmed away at render time, which looks like it was deleted. Only the newly
  // covered part is checked: a block that is *already* overlapped can still be stretched.
  const fresh =
    direction === "prev"
      ? overlapsOther(others, block, start, block.start_ms)
      : overlapsOther(others, block, block.end_ms, end);
  if (fresh) return null;
  return { start_ms: start, end_ms: end };
}

/**
 * Move a block so it butts against its neighbour on one side, keeping its length.
 *
 * This is what ``Ctrl+←`` / ``Ctrl+→`` do on the track. Dragging by hand can close a gap, but
 * not exactly: the snap grid is ~500 ms apart, so a hand drag lands "about there" and a block
 * that should share an edge with its neighbour ends up a few hundred milliseconds off. Two
 * blocks that are meant to be adjacent have to be exactly adjacent — a gap between them reads
 * as "there is no video here" while the one before it is still playing.
 *
 * **Length is preserved**, not the end or the start. Closing a gap by stretching the block
 * would silently change how long a picture is on screen, which is a number the user set.
 *
 * Returns ``null`` when there is nothing to do (no neighbour on that side, no gap, or the move
 * would overlap the block on the *other* side). The caller then leaves the block alone and
 * says nothing — a shortcut that visibly does nothing when it is already flush is correct,
 * and a notice for it would be noise on every second press.
 */
export function abutNeighbour<T extends { start_ms: number; end_ms: number }>(
  block: { start_ms: number; end_ms: number },
  direction: "prev" | "next",
  others: T[],
  durationMs: number,
): { start_ms: number; end_ms: number } | null {
  const neighbour = neighbourOf(block, others, direction);
  if (neighbour === null) return null;
  const length = block.end_ms - block.start_ms;
  if (length <= 0) return null;

  const start =
    direction === "prev" ? neighbour.end_ms : neighbour.start_ms - length;
  const end = start + length;
  // Nothing to do: already flush with that neighbour, or the direction points the wrong way.
  if (start === block.start_ms) return null;
  if (direction === "prev" && start > block.start_ms) return null;
  if (direction === "next" && start < block.start_ms) return null;
  // Refuse a move that would leave the film or cover ground that is not already this block's.
  if (start < 0 || end > durationMs) return null;
  const fresh =
    direction === "prev"
      ? overlapsOther(others, block, start, block.start_ms)
      : overlapsOther(others, block, block.end_ms, end);
  if (fresh) return null;
  return { start_ms: start, end_ms: end };
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
