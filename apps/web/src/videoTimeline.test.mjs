/**
 * Video timeline arithmetic (AGENTS.md §19.3 / §19.7).
 *
 * Every function here exists because getting it wrong produces a *plausible* screen
 * rather than an error:
 *
 * - drawing a block at its output width makes two adjacent blocks leave a gap the
 *   moment one of them loses audio to a cut, which reads as "no video here";
 * - snapping to raw sentence starts instead of the collapsed targets makes a block
 *   visibly jump when the server's answer arrives;
 * - reporting a gallery's dwell time without the original value turns proportional
 *   pacing into what looks like accidental fast-forward.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  blockAt,
  dropRange,
  galleryCaption,
  galleryDwellMs,
  layoutBlock,
  lengthCaption,
  MIN_BLOCK_MS,
  moveBlock,
  msToX,
  orphanedBlocks,
  resizeBlock,
  snapEdge,
  summaryLine,
  viewAround,
  xToMs,
} from "./videoTimeline.ts";

const HOUR = 3_600_000;
const FULL = { from: 0, to: HOUR };

// --- pixels -------------------------------------------------------------

test("msToX maps the window onto the track", () => {
  assert.equal(msToX(0, FULL, 1000), 0);
  assert.equal(msToX(HOUR, FULL, 1000), 1000);
  assert.equal(msToX(HOUR / 2, FULL, 1000), 500);
});

test("msToX clamps instead of going negative", () => {
  // A block starting before the window must be drawn from the left edge; a negative
  // offset would put invisible content in front of the canvas.
  assert.equal(msToX(-5000, { from: 0, to: HOUR }, 1000), 0);
});

test("msToX clamps past the right edge", () => {
  assert.equal(msToX(HOUR + 5000, FULL, 1000), 1000);
});

test("xToMs round-trips through msToX", () => {
  const view = { from: 60_000, to: 120_000 };
  const ms = 90_000;
  const x = msToX(ms, view, 800);
  assert.equal(xToMs(x, view, 800), ms);
});

test("xToMs of a zero-width track is the window start, not NaN", () => {
  assert.equal(xToMs(10, FULL, 0), 0);
});

test("xToMs clamps a pointer dragged outside the track", () => {
  assert.equal(xToMs(-50, FULL, 1000), 0);
  assert.equal(xToMs(2000, FULL, 1000), HOUR);
});

// --- block layout -------------------------------------------------------

test("layoutBlock uses the block's own edges, not its output length", () => {
  // 10 s block, 2 s of its audio deleted: it must still be 10 s wide on the
  // timeline, or the neighbour it shares a boundary with leaves a gap.
  const block = { start_ms: 0, end_ms: 10_000, duration_ms: 10_000, output_ms: 8000 };
  const layout = layoutBlock(block, { from: 0, to: 10_000 }, 1000);
  assert.equal(layout.left, 0);
  assert.equal(layout.width, 1000);
  assert.equal(layout.keptRatio, 0.8);
});

test("layoutBlock of a partly visible block is clipped, not shifted", () => {
  const block = { start_ms: 0, end_ms: 10_000, duration_ms: 10_000, output_ms: 10_000 };
  const layout = layoutBlock(block, { from: 5000, to: 15_000 }, 1000);
  assert.equal(layout.left, 0, "starts at the left edge, not off-screen");
  assert.equal(layout.width, 500, "half the block is visible");
});

test("layoutBlock keeps a fully orphaned block at zero ratio but non-zero width", () => {
  // The block still occupies its stretch of the timeline; the UI needs somewhere to
  // put the 「对应的音频已删除」 notice and its two actions.
  const block = { start_ms: 1000, end_ms: 3000, duration_ms: 2000, output_ms: 0 };
  const layout = layoutBlock(block, FULL, 1000);
  assert.ok(layout.width > 0);
  assert.equal(layout.keptRatio, 0);
});

test("layoutBlock of a zero-length block does not divide by zero", () => {
  const block = { start_ms: 500, end_ms: 500, duration_ms: 0, output_ms: 0 };
  const layout = layoutBlock(block, FULL, 1000);
  assert.equal(layout.keptRatio, 0);
  assert.equal(layout.width, 0);
});

// --- snapping -----------------------------------------------------------

test("snapEdge pulls within the radius", () => {
  assert.equal(snapEdge(1080, [0, 1000, 2000]), 1000);
});

test("snapEdge leaves a drop outside the radius alone", () => {
  assert.equal(snapEdge(1400, [0, 1000, 2000]), 1400);
});

test("snapEdge ties go to the earlier boundary", () => {
  // Same rule as the server: keeping audio is the recoverable mistake.
  assert.equal(snapEdge(1050, [0, 1000, 1100]), 1000);
});

test("snapEdge with no targets is identity", () => {
  assert.equal(snapEdge(1234, []), 1234);
});

test("snapEdge accepts a boundary exactly at the radius", () => {
  assert.equal(snapEdge(1120, [0, 1000]), 1000);
});

test("snapEdge is idempotent on a target", () => {
  assert.equal(snapEdge(1000, [0, 1000, 2000]), 1000);
});

// --- lengths ------------------------------------------------------------

test("lengthCaption shows one number when nothing was cut", () => {
  assert.equal(
    lengthCaption({ start_ms: 0, end_ms: 10_000, duration_ms: 10_000, output_ms: 10_000 }),
    "10.0s",
  );
});

test("lengthCaption shows both numbers when audio was cut", () => {
  // A single number here is what makes the user say 「我明明设了 10 秒」.
  assert.equal(
    lengthCaption({ start_ms: 0, end_ms: 10_000, duration_ms: 10_000, output_ms: 8000 }),
    "原始 10.0s / 成片 8.0s",
  );
});

// --- gallery pacing -----------------------------------------------------

test("galleryDwellMs scales with the output length", () => {
  const before = { start_ms: 0, end_ms: 16_000, duration_ms: 16_000, output_ms: 16_000 };
  const after = { ...before, output_ms: 14_000 };
  assert.equal(galleryDwellMs(before, 8), 2000);
  assert.equal(galleryDwellMs(after, 8), 1750);
});

test("galleryDwellMs keeps the number of turns unchanged", () => {
  // Proportional pacing: the turns come from the image count, not from the length.
  const block = { start_ms: 0, end_ms: 8000, duration_ms: 8000, output_ms: 6000 };
  const dwell = galleryDwellMs(block, 8);
  assert.equal(dwell * 8, block.output_ms);
});

test("galleryDwellMs of an empty gallery is zero, not a crash", () => {
  const block = { start_ms: 0, end_ms: 8000, duration_ms: 8000, output_ms: 6000 };
  assert.equal(galleryDwellMs(block, 0), 0);
});

test("galleryCaption shows the original pace when it changed", () => {
  const block = { start_ms: 0, end_ms: 16_000, duration_ms: 16_000, output_ms: 14_000 };
  // Without the 「原 2.0s」 half, a 12 % speed-up reads as a glitch.
  assert.equal(galleryCaption(block, 8), "8 张 · 每张 1.8s（原 2.0s）");
});

test("galleryCaption stays quiet when nothing changed", () => {
  const block = { start_ms: 0, end_ms: 16_000, duration_ms: 16_000, output_ms: 16_000 };
  assert.equal(galleryCaption(block, 8), "8 张 · 每张 2.0s");
});

test("galleryCaption of an empty gallery says so", () => {
  const block = { start_ms: 0, end_ms: 8000, duration_ms: 8000, output_ms: 8000 };
  assert.match(galleryCaption(block, 0), /还没选图片/);
});

// --- orphans and the summary -------------------------------------------

test("orphanedBlocks selects and orders them", () => {
  const blocks = [
    { orphaned: false, start_ms: 0 },
    { orphaned: true, start_ms: 5000 },
    { orphaned: true, start_ms: 1000 },
  ];
  assert.deepEqual(
    orphanedBlocks(blocks).map((block) => block.start_ms),
    [1000, 5000],
  );
});

test("summaryLine covers every template state", () => {
  assert.equal(summaryLine(null, 0, 0), "未启用");
  assert.equal(summaryLine("failed", 0, 0), "模板编译失败");
  assert.equal(summaryLine("compiling", 0, 0), "模板编译中…");
  assert.equal(summaryLine("pending", 0, 0), "模板编译中…");
  assert.equal(summaryLine("ready", 0, 0), "模板已套用 · 还没有素材块");
  assert.equal(summaryLine("ready", 4, 0), "模板已套用 · 4 个素材块");
});

test("summaryLine surfaces orphans so a collapsed panel is not a lie", () => {
  assert.equal(summaryLine("ready", 4, 1), "模板已套用 · 4 个素材块（1 个待处理）");
});

// --- dropping a new block ----------------------------------------------

const TARGETS = [0, 4000, 9000, 20000];

test("dropRange snaps the start and runs to the next boundary", () => {
  const range = dropRange(3900, TARGETS, 30000, []);
  assert.deepEqual(range, { start_ms: 4000, end_ms: 9000 });
});

test("dropRange never produces a fixed default length", () => {
  // A 5 s constant would be wrong for both of these: the tiles are 4 s and 11 s.
  assert.equal(dropRange(100, TARGETS, 30000, []).end_ms, 4000);
  assert.equal(dropRange(4100, TARGETS, 30000, []).end_ms, 9000);
});

test("dropRange slides past a block it landed inside", () => {
  const occupied = [{ start_ms: 0, end_ms: 8000 }];
  const range = dropRange(2000, TARGETS, 30000, occupied);
  assert.equal(range.start_ms, 8000);
});

test("dropRange stops at the next block instead of overlapping it", () => {
  const occupied = [{ start_ms: 9000, end_ms: 12000 }];
  const range = dropRange(4100, TARGETS, 30000, occupied);
  assert.deepEqual(range, { start_ms: 4000, end_ms: 9000 });
});

test("dropRange refuses when there is no room", () => {
  const occupied = [{ start_ms: 0, end_ms: 30000 }];
  assert.equal(dropRange(1000, TARGETS, 30000, occupied), null);
});

test("dropRange refuses a sliver at the end of the file", () => {
  assert.equal(dropRange(29950, TARGETS, 30000, []), null);
});

test("dropRange measures the sliver limit, not a magic number", () => {
  const range = dropRange(30000 - MIN_BLOCK_MS, TARGETS, 30000, []);
  assert.deepEqual(range, { start_ms: 30000 - MIN_BLOCK_MS, end_ms: 30000 });
});

// --- moving and resizing ----------------------------------------------

test("moveBlock keeps the length and snaps the start", () => {
  // 100 ms is inside the 120 ms snap radius, so the block settles back on its boundary.
  const nudged = moveBlock({ start_ms: 4000, end_ms: 8000 }, 100, TARGETS, 30000);
  assert.deepEqual(nudged, { start_ms: 4000, end_ms: 8000 });
  // 4000 + 5000 = 9000 is a boundary, so the length is preserved exactly.
  const further = moveBlock({ start_ms: 4000, end_ms: 8000 }, 5000, TARGETS, 30000);
  assert.deepEqual(further, { start_ms: 9000, end_ms: 13000 });
  // A drag that ends between boundaries keeps its length and is not rounded anywhere:
  // re-seating it would silently rewrite the offset the user dragged to.
  const free = moveBlock({ start_ms: 4000, end_ms: 8000 }, 6000, TARGETS, 30000);
  assert.deepEqual(free, { start_ms: 10000, end_ms: 14000 });
});

test("moveBlock cannot push a block off the end", () => {
  const moved = moveBlock({ start_ms: 25000, end_ms: 29000 }, 60000, [0, 30000], 30000);
  assert.equal(moved.end_ms, 30000);
  assert.equal(moved.end_ms - moved.start_ms, 4000);
});

test("moveBlock cannot push a block before zero", () => {
  const moved = moveBlock({ start_ms: 1000, end_ms: 3000 }, -60000, [0, 30000], 30000);
  assert.equal(moved.start_ms, 0);
});

test("resizeBlock moves the start edge", () => {
  const resized = resizeBlock({ start_ms: 4000, end_ms: 9000 }, "start", 3900, TARGETS);
  assert.deepEqual(resized, { start_ms: 4000, end_ms: 9000 });
});

test("resizeBlock moves the end edge", () => {
  const resized = resizeBlock({ start_ms: 4000, end_ms: 9000 }, "end", 20050, TARGETS);
  assert.deepEqual(resized, { start_ms: 4000, end_ms: 20000 });
});

test("resizeBlock refuses to invert the block", () => {
  // Dragging the start past the end would create a negative-length block; clamping to a
  // sliver is visible and reversible, an inverted block is a 500 from the server.
  const resized = resizeBlock({ start_ms: 4000, end_ms: 5000 }, "start", 9000, TARGETS);
  assert.equal(resized.end_ms - resized.start_ms, MIN_BLOCK_MS);
});

// --- hit testing -------------------------------------------------------

test("blockAt finds the block under an instant", () => {
  const blocks = [{ start_ms: 0, end_ms: 4000 }, { start_ms: 4000, end_ms: 8000 }];
  assert.deepEqual(blockAt(500, blocks), blocks[0]);
  assert.deepEqual(blockAt(4000, blocks), blocks[1], "half-open: 4000 belongs to the second");
});

test("blockAt returns null outside every block", () => {
  assert.equal(blockAt(9999, [{ start_ms: 0, end_ms: 4000 }]), null);
});

test("blockAt prefers the later block when two overlap", () => {
  const blocks = [{ start_ms: 0, end_ms: 5000 }, { start_ms: 3000, end_ms: 8000 }];
  assert.deepEqual(blockAt(3500, blocks), blocks[1]);
});

// --- keeping the block on screen --------------------------------------

test("viewAround centres a block without changing the span", () => {
  const view = viewAround(60000, { from: 0, to: 20000 }, 120000);
  assert.equal(view.to - view.from, 20000);
  assert.equal(view.from, 50000);
});

test("viewAround clamps at the start and the end", () => {
  assert.equal(viewAround(0, { from: 0, to: 20000 }, 120000).from, 0);
  const atEnd = viewAround(120000, { from: 0, to: 20000 }, 120000);
  assert.equal(atEnd.to, 120000);
});
