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
  abutNeighbour,
  blockAt,
  blockName,
  stretchToNeighbour,
  dropRange,
  FITS,
  galleryCaption,
  galleryDwellMs,
  layoutBlock,
  MIN_BLOCK_MS,
  moveBlock,
  PLACEMENTS,
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

// --- what a block is called -------------------------------------------

test("blockName prefers the server's label", () => {
  // Round 104: the track used to say 「素材」 / 「画廊」 / 「演示稿」 for every block, so two
  // stills on the same timeline were indistinguishable.
  assert.equal(blockName({ label: "开场封面.png", kind: "media" }), "开场封面.png");
  assert.equal(blockName({ label: "演示稿", kind: "slide" }), "演示稿");
});

test("blockName falls back to the kind when nothing names the block", () => {
  // A block created before an asset was picked has no label; a blank rectangle reads as a
  // rendering bug rather than as "no file yet".
  assert.equal(blockName({ label: "", kind: "media" }), "素材");
  assert.equal(blockName({ label: "", kind: "gallery" }), "画廊");
  assert.equal(blockName({ label: "", kind: "slide" }), "演示稿");
  assert.equal(blockName({ kind: "media" }), "素材");
});

test("blockName treats an unknown kind as material", () => {
  assert.equal(blockName({ label: "", kind: "nonsense" }), "素材");
});

// --- the fit list ------------------------------------------------------

test("FITS is exactly the strategies the server accepts", () => {
  // `services/video.py` validates against exactly this set. A label here whose id is not one
  // of those is a dropdown entry that 400s on pick; an id on the server that is missing here
  // is a strategy nobody can choose.
  assert.deepEqual(
    FITS.map((fit) => fit.id),
    ["blur_fill", "blur_crop", "contain", "cover", "stretch"],
  );
});

test("every fit has a label and a hint a tooltip can show", () => {
  for (const fit of FITS) {
    assert.ok(fit.label.length > 0, fit.id);
    assert.ok(fit.hint.length > 0, fit.id);
  }
});

// --- Ctrl+Left / Ctrl+Right -------------------------------------------

test("abutNeighbour closes the gap in front of a block, keeping its length", () => {
  const previous = { start_ms: 0, end_ms: 70_970 };
  const block = { start_ms: 75_000, end_ms: 100_000 };
  assert.deepEqual(abutNeighbour(block, "prev", [previous, block], 3_600_000), {
    start_ms: 70_970,
    end_ms: 95_970,
  });
});

test("abutNeighbour closes the gap after a block", () => {
  const block = { start_ms: 0, end_ms: 10_000 };
  const next = { start_ms: 12_000, end_ms: 30_000 };
  assert.deepEqual(abutNeighbour(block, "next", [block, next], 3_600_000), {
    start_ms: 2_000,
    end_ms: 12_000,
  });
});

test("abutNeighbour does nothing when the block is already flush", () => {
  // A shortcut that fires on every press would be worse than one that is quiet: the second
  // press has nothing to do, and a notice for it would appear on every other keypress.
  const previous = { start_ms: 0, end_ms: 10_000 };
  const block = { start_ms: 10_000, end_ms: 20_000 };
  assert.equal(abutNeighbour(block, "prev", [previous, block], 3_600_000), null);
});

test("abutNeighbour does nothing at the ends of the film", () => {
  const only = { start_ms: 5000, end_ms: 9000 };
  assert.equal(abutNeighbour(only, "prev", [only], 3_600_000), null);
  assert.equal(abutNeighbour(only, "next", [only], 3_600_000), null);
});

test("a block that already overlaps a neighbour can still be moved", () => {
  // Real timelines contain this: the demo project has 141700–145572 sitting inside the tail of
  // 119681–141993. Guarding the *whole* new span made both keys dead on such a block; only the
  // newly covered region must be clear.
  const overlapping = { start_ms: 119_681, end_ms: 141_993 };
  const block = { start_ms: 141_700, end_ms: 145_572 };
  const next = { start_ms: 145_579, end_ms: 160_060 };
  // 3872 ms long, and the next block starts 7 ms after this one ends: the end lands on
  // 145579, so the start becomes 141707 and the length is unchanged.
  assert.deepEqual(abutNeighbour(block, "next", [overlapping, block, next], 3_600_000), {
    start_ms: 141_707,
    end_ms: 145_579,
  });
  assert.deepEqual(stretchToNeighbour(block, "next", [overlapping, block, next], 3_600_000), {
    start_ms: 141_700,
    end_ms: 145_579,
  });
});

test("abutNeighbour refuses to bury this block under one that overlaps its edge", () => {
  // Real timelines contain this. `overlapper` covers the first second of `block`, so butting
  // against `previous` would put the whole 12–20 s span on top of it — and an overlapped block
  // is trimmed away at render time, which looks like the block was deleted.
  const previous = { start_ms: 0, end_ms: 10_000 };
  const overlapper = { start_ms: 11_500, end_ms: 13_000 };
  const block = { start_ms: 12_000, end_ms: 20_000 };
  assert.equal(
    abutNeighbour(block, "prev", [previous, overlapper, block], 3_600_000),
    null,
  );
});

test("abutNeighbour refuses an edge outside the film", () => {
  // With consistent data this cannot happen — butting left starts at the neighbour's *end*,
  // so the block's own length keeps it inside. It happens when `durationMs` lags: the track
  // takes the total from the timeline grid, and a re-transcription can shorten the media
  // while a render is in flight. Refusing is the honest answer; writing a block at 200 s of a
  // 100 s film would land in the database and show up as a block that never plays.
  const block = { start_ms: 0, end_ms: 10_000 };
  const next = { start_ms: 200_000, end_ms: 210_000 };
  assert.equal(abutNeighbour(block, "next", [block, next], 100_000), null);
});

test("abutNeighbour does not require the block to be in the list", () => {
  // The track passes its own array, which does contain the block; a caller that filters it out
  // must still get the same answer.
  const previous = { start_ms: 0, end_ms: 10_000 };
  const block = { start_ms: 12_000, end_ms: 20_000 };
  assert.deepEqual(abutNeighbour(block, "prev", [previous], 3_600_000), {
    start_ms: 10_000,
    end_ms: 18_000,
  });
});

test("abutNeighbour measures gaps against the neighbour's own edges", () => {
  // Two candidates on the left: the closer *end* wins, not the closer start. A block that
  // ends later but starts earlier is the one that is actually adjacent.
  const far = { start_ms: 0, end_ms: 5000 };
  const near = { start_ms: 3000, end_ms: 9000 };
  const block = { start_ms: 12_000, end_ms: 20_000 };
  assert.equal(abutNeighbour(block, "prev", [far, near, block], 3_600_000)?.start_ms, 9000);
});

// --- Shift+Left / Shift+Right ------------------------------------------

test("stretchToNeighbour pulls the end forward to the next block", () => {
  const block = { start_ms: 0, end_ms: 10_000 };
  const next = { start_ms: 12_000, end_ms: 30_000 };
  assert.deepEqual(stretchToNeighbour(block, "next", [block, next], 3_600_000), {
    start_ms: 0,
    end_ms: 12_000,
  });
});

test("stretchToNeighbour pulls the start back to the previous block", () => {
  const previous = { start_ms: 0, end_ms: 10_000 };
  const block = { start_ms: 12_000, end_ms: 30_000 };
  assert.deepEqual(stretchToNeighbour(block, "prev", [previous, block], 3_600_000), {
    start_ms: 10_000,
    end_ms: 30_000,
  });
});

test("stretchToNeighbour never eats an overlapping block", () => {
  // `neighbourOf("next")` only reports blocks starting at or after this block's end, so a
  // block that already overlaps it is not a neighbour. Shrinking onto it would delete part of
  // a block the user placed by hand, from a key that claims to extend.
  const block = { start_ms: 0, end_ms: 20_000 };
  const overlapping = { start_ms: 12_000, end_ms: 30_000 };
  assert.equal(stretchToNeighbour(block, "next", [block, overlapping], 3_600_000), null);
  assert.equal(stretchToNeighbour(overlapping, "prev", [block, overlapping], 3_600_000), null);
});

test("stretchToNeighbour does nothing without a neighbour on that side", () => {
  const only = { start_ms: 5000, end_ms: 9000 };
  assert.equal(stretchToNeighbour(only, "prev", [only], 3_600_000), null);
  assert.equal(stretchToNeighbour(only, "next", [only], 3_600_000), null);
});

test("stretchToNeighbour closes a gap as small as one millisecond", () => {
  // Grow-only, so there is no minimum-length floor to hit: the block was already long enough
  // and only ever gets longer. A guard here would be unreachable code.
  const block = { start_ms: 0, end_ms: 10_000 };
  const next = { start_ms: 10_001, end_ms: 30_000 };
  assert.equal(stretchToNeighbour(block, "next", [block, next], 3_600_000)?.end_ms, 10_001);
});

test("stretchToNeighbour does nothing when the edge is already there", () => {
  const block = { start_ms: 10_000, end_ms: 20_000 };
  const previous = { start_ms: 0, end_ms: 10_000 };
  const next = { start_ms: 20_000, end_ms: 30_000 };
  assert.equal(stretchToNeighbour(block, "prev", [previous, block, next], 3_600_000), null);
  assert.equal(stretchToNeighbour(block, "next", [previous, block, next], 3_600_000), null);
});

test("stretchToNeighbour refuses to grow over a block that straddles the edge", () => {
  // `straddler` starts before our end, so it is not a "next" neighbour — but the edge would
  // land inside it. Two blocks in one place are trimmed at render time, which looks like the
  // middle one was deleted.
  const block = { start_ms: 0, end_ms: 10_000 };
  const straddler = { start_ms: 8000, end_ms: 15_000 };
  const after = { start_ms: 20_000, end_ms: 30_000 };
  assert.equal(
    stretchToNeighbour(block, "next", [block, straddler, after], 3_600_000),
    null,
  );
});

test("stretchToNeighbour butts against the nearest block beyond the gap", () => {
  // The ordinary case: a block wholly inside the gap *is* the next neighbour, and butting
  // against its start is exactly right.
  const block = { start_ms: 0, end_ms: 10_000 };
  const inside = { start_ms: 12_000, end_ms: 15_000 };
  const after = { start_ms: 20_000, end_ms: 30_000 };
  assert.equal(stretchToNeighbour(block, "next", [block, inside, after], 3_600_000)?.end_ms, 12_000);
});

test("stretchToNeighbour keeps the other edge exactly where it was", () => {
  const block = { start_ms: 4000, end_ms: 10_000 };
  const next = { start_ms: 14_000, end_ms: 30_000 };
  assert.equal(stretchToNeighbour(block, "next", [block, next], 3_600_000)?.start_ms, 4000);
  const previous = { start_ms: 0, end_ms: 1000 };
  assert.equal(
    stretchToNeighbour(block, "prev", [previous, block], 3_600_000)?.end_ms,
    10_000,
  );
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
// --- 展现方式 (§19.25) ----------------------------------------------------

test("PLACEMENTS is 素材框 then 全屏, and nothing else", () => {
  // Order matters: this select sits **before** the fit select in the bubble, so its first entry
  // is what a project defaults to on screen. Two entries, because there are two rectangles.
  assert.deepEqual(
    PLACEMENTS.map((option) => option.id),
    ["box", "full"],
  );
  assert.deepEqual(
    PLACEMENTS.map((option) => option.label),
    ["素材框", "全屏"],
  );
});

test("PLACEMENTS carries no hint text", () => {
  // The user's constraint was 「气泡面板上不要出现说明，否则太长了」: the bubble shows two selects,
  // two letters and a delete, and any prose here would be rendered into it. `title` on the
  // select is where an explanation may live; this list must stay label-only.
  for (const option of PLACEMENTS) {
    assert.deepEqual(Object.keys(option).sort(), ["id", "label"], JSON.stringify(option));
  }
});

test("box is the first entry, so it is the default on screen", () => {
  // The default has to be 「目前的做法」. Rendering `placement` therefore falls back to
  // `PLACEMENTS[0].id` when a block predates the column.
  assert.equal(PLACEMENTS[0].id, "box");
});
