/**
 * 画布编辑器的算术（第 139 轮）。
 *
 * 每一条都对着一个「看着对、其实错」的实现：
 *
 * - 拖出画布要**夹回来**而不是拒绝——拖动已经发生了，作品在用户屏幕上的位置必须和存下来的一致；
 * - 素材框必须偶数，否则 libx264 + yuv420p 会在几分钟后以「像素格式」报错，用户已经等过了；
 * - 头像框缩放要**锁宽高比**，否则 177×188 的脸会被压成 300×90；
 * - 字号跟**两个方向里小的**那个因子（⑬），只拖宽不该把字撑出框高。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  clampRect,
  describe,
  handlePoints,
  hitHandle,
  isMoved,
  moveRect,
  nudgeRect,
  payloadFor,
  resizeRect,
  scaledFont,
  snapRect,
  toCanvasPoint,
} from "./layoutMath.ts";

const CANVAS = { w: 1920, h: 1080 };
const TEXT_MIN = { w: 8, h: 8 };
const BOX_MIN = { w: 160, h: 90 };
const AVATAR_MIN = { w: 24, h: 24 };
const TITLE = { x: 1068, y: 88, w: 617, h: 109 };
const AVATAR = { x: 42, y: 200, w: 177, h: 188 };

// --- clamping -----------------------------------------------------------

test("a rectangle dragged off the canvas comes back to the edge", () => {
  const rect = clampRect({ x: 5000, y: -300, w: 617, h: 109 }, CANVAS, TEXT_MIN);
  assert.deepEqual(rect, { x: CANVAS.w - 617, y: 0, w: 617, h: 109 });
});

test("a rectangle smaller than its minimum grows to it", () => {
  const rect = clampRect({ x: 10, y: 10, w: 2, h: 3 }, CANVAS, BOX_MIN, { even: true });
  assert.equal(rect.w, BOX_MIN.w);
  assert.equal(rect.h, BOX_MIN.h);
  assert.equal(rect.w % 2, 0);
  assert.equal(rect.h % 2, 0);
});

test("an even box never exceeds an odd canvas", () => {
  // The rounding has to go **down** here: rounding 400 up to a 401-wide canvas would put the
  // right edge one pixel outside, which libx264 then refuses.
  const rect = clampRect({ x: 0, y: 0, w: 400, h: 300 }, { w: 401, h: 301 }, BOX_MIN, {
    even: true,
  });
  assert.equal(rect.w, 400);
  assert.equal(rect.h, 300);
});

test("moving keeps the size and pins the edges", () => {
  const rect = moveRect(TITLE, 10, 20, CANVAS, TEXT_MIN);
  assert.deepEqual(rect, { x: 1078, y: 108, w: 617, h: 109 });
  const pinned = moveRect(TITLE, -5000, 5000, CANVAS, TEXT_MIN);
  assert.equal(pinned.x, 0);
  assert.equal(pinned.y, CANVAS.h - 109);
});

// --- resize -------------------------------------------------------------

test("dragging the east edge leaves the west edge where it was", () => {
  const rect = resizeRect(TITLE, "e", 100, 0, CANVAS, TEXT_MIN);
  assert.equal(rect.x, TITLE.x);
  assert.equal(rect.w, TITLE.w + 100);
});

test("dragging the west edge moves the left edge and keeps the right one", () => {
  const right = TITLE.x + TITLE.w;
  const rect = resizeRect(TITLE, "w", 100, 0, CANVAS, TEXT_MIN);
  assert.equal(rect.x, TITLE.x + 100);
  assert.equal(rect.x + rect.w, right);
});

test("the north edge cannot cross the south edge", () => {
  const bottom = TITLE.y + TITLE.h;
  const rect = resizeRect(TITLE, "n", 0, 500, CANVAS, TEXT_MIN);
  assert.equal(rect.h, TEXT_MIN.h);
  assert.equal(rect.y + rect.h, bottom);
});

test("a corner drag scales an avatar by its ratio", () => {
  // ⑭: the PSD's frame is drawn at its own aspect; a 2× drag keeps 177:188.
  const rect = resizeRect(AVATAR, "se", 177, 94, CANVAS, AVATAR_MIN, { aspect: true });
  assert.ok(Math.abs(rect.w / rect.h - AVATAR.w / AVATAR.h) < 0.02, `${rect.w}x${rect.h}`);
  assert.equal(rect.x, AVATAR.x);
  assert.equal(rect.y, AVATAR.y);
});

test("a corner drag on the north-west corner keeps the south-east corner fixed", () => {
  // Shrinking, not growing: growing this slot by 2x pushes the left edge off the canvas (42 - 177
  // < 0) and clamping then moves the anchor, which is right but is not what this test is about.
  const right = AVATAR.x + AVATAR.w;
  const bottom = AVATAR.y + AVATAR.h;
  const rect = resizeRect(AVATAR, "nw", 50, 50, CANVAS, AVATAR_MIN, { aspect: true });
  assert.equal(rect.x + rect.w, right);
  assert.equal(rect.y + rect.h, bottom);
});

test("an edge drag on an avatar does not snap the ratio mid-gesture", () => {
  const rect = resizeRect(AVATAR, "e", 100, 0, CANVAS, AVATAR_MIN, { aspect: true });
  assert.equal(rect.w, AVATAR.w + 100);
  assert.equal(rect.h, AVATAR.h);
});

// --- handles ------------------------------------------------------------

test("the eight handles sit on the corners and the midpoints", () => {
  const points = handlePoints(TITLE);
  assert.deepEqual(points.nw, { x: TITLE.x, y: TITLE.y });
  assert.deepEqual(points.se, { x: TITLE.x + TITLE.w, y: TITLE.y + TITLE.h });
  assert.deepEqual(points.n, { x: TITLE.x + TITLE.w / 2, y: TITLE.y });
  assert.deepEqual(points.w, { x: TITLE.x, y: TITLE.y + TITLE.h / 2 });
});

test("a click near a corner grabs that corner", () => {
  const grabbed = hitHandle(TITLE, { x: TITLE.x + 2, y: TITLE.y + 1 }, 8);
  assert.equal(grabbed, "nw");
});

test("a click in the middle grabs nothing", () => {
  assert.equal(hitHandle(TITLE, { x: TITLE.x + 300, y: TITLE.y + 50 }, 8), null);
});

test("the closer of two handles wins", () => {
  // Near the north-east corner but nearest the east midpoint: the midpoint must win, or the
  // user resizes diagonally when they meant to stretch.
  const grabbed = hitHandle(TITLE, { x: TITLE.x + TITLE.w - 1, y: TITLE.y + TITLE.h / 2 - 1 }, 60);
  assert.equal(grabbed, "e");
});

// --- pointer conversion -------------------------------------------------

test("a pointer position becomes a canvas pixel through the fit scale", () => {
  const point = toCanvasPoint({ x: 500, y: 300 }, { left: 100, top: 100 }, 0.5);
  assert.deepEqual(point, { x: 800, y: 400 });
});

test("a zero scale does not divide by zero", () => {
  assert.deepEqual(toCanvasPoint({ x: 10, y: 10 }, { left: 0, top: 0 }, 0), { x: 0, y: 0 });
});

// --- snapping -----------------------------------------------------------

test("a dragged rectangle snaps to the canvas centre and reports a guide", () => {
  const rect = { x: 640, y: 400, w: 640, h: 200 };
  const result = snapRect(rect, { canvas: CANVAS, others: [] });
  // Centre is 960; the rect's centre is 640 + 320 = 960 exactly, so no move is needed.
  assert.equal(result.x, 640);
  assert.ok(result.guides.some((guide) => guide.axis === "x" && guide.at === 960));
});

test("a rectangle three pixels from an edge is pulled onto it", () => {
  const rect = { x: 3, y: 3, w: 100, h: 100 };
  const result = snapRect(rect, { canvas: CANVAS, others: [] });
  assert.equal(result.x, 0);
  assert.equal(result.y, 0);
});

test("a rectangle further than the tolerance is left alone", () => {
  const rect = { x: 40, y: 40, w: 100, h: 100 };
  const result = snapRect(rect, { canvas: CANVAS, others: [] }, 6);
  assert.equal(result.x, 40);
  assert.equal(result.y, 40);
  assert.deepEqual(result.guides, []);
});

test("one slot snaps to another slot's edge, not just to the canvas", () => {
  const other = { x: 500, y: 300, w: 200, h: 200 };
  const rect = { x: 497, y: 600, w: 100, h: 100 };
  const result = snapRect(rect, { canvas: CANVAS, others: [other] });
  assert.equal(result.x, 500);
});

// --- payload and readout ------------------------------------------------

test("the payload names the slot's own kind", () => {
  assert.deepEqual(payloadFor("box", "box", TITLE), { box: TITLE });
  assert.deepEqual(payloadFor("text", "title", TITLE), { texts: { title: TITLE } });
  assert.deepEqual(payloadFor("avatar", "头像1", AVATAR), { avatars: { 头像1: AVATAR } });
});

test("the payload rounds, because the server takes ints", () => {
  const payload = payloadFor("text", "title", { x: 10.6, y: 20.4, w: 100.5, h: 50.5 });
  assert.deepEqual(payload.texts, { title: { x: 11, y: 20, w: 101, h: 51 } });
});

test("a slot is 'moved' only past a whole pixel", () => {
  // Sub-pixel noise comes from the fit scale, not from the user; flagging it would light up
  // 「已改动」 on a project nobody touched.
  assert.equal(isMoved({ ...TITLE, x: TITLE.x + 0.4 }, TITLE), false);
  assert.equal(isMoved({ ...TITLE, x: TITLE.x + 2 }, TITLE), true);
  assert.equal(isMoved({ ...TITLE, w: TITLE.w - 2 }, TITLE), true);
});

test("the readout round-trips the numbers the panel shows", () => {
  assert.equal(describe(TITLE), "x 1068 · y 88 · 617 × 109");
});

// --- font scaling -------------------------------------------------------

test("the type scales only when both axes grow", () => {
  assert.equal(scaledFont(56, TITLE, { ...TITLE, w: TITLE.w * 2 }), 56);
  assert.equal(scaledFont(56, TITLE, { ...TITLE, h: TITLE.h * 2 }), 56);
  assert.equal(scaledFont(56, TITLE, { ...TITLE, w: TITLE.w * 2, h: TITLE.h * 2 }), 112);
});

test("the scaled size is even, like the overlay's own fitting", () => {
  assert.equal(scaledFont(69, { x: 0, y: 0, w: 100, h: 100 }, { x: 0, y: 0, w: 50, h: 50 }), 34);
});

test("a slot with no font size reports none", () => {
  assert.equal(scaledFont(undefined, TITLE, TITLE), null);
  assert.equal(scaledFont(0, TITLE, TITLE), null);
});

// --- nudge --------------------------------------------------------------

test("an arrow-key nudge is a one-pixel move", () => {
  assert.deepEqual(nudgeRect(TITLE, 1, 0, CANVAS, TEXT_MIN), {
    ...TITLE,
    x: TITLE.x + 1,
  });
});
