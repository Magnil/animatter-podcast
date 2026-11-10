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
  galleryCaption,
  galleryDwellMs,
  layoutBlock,
  lengthCaption,
  msToX,
  orphanedBlocks,
  snapEdge,
  summaryLine,
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
