/**
 * 菜单项测试要跟上第 135 轮的新形状。
 *
 * - `exportingPercent` / `exportingNote` → `exporting: boolean`（进度搬进面板了）；
 * - 就绪时的 hint 从「MP4 1080p，字幕烧进画面」变成「文件名、文件夹、编码选项」，
 *   因为那两条硬事实现在写在面板正文里，能看到比挤在菜单边缘更清楚；
 * - 正在导出时的 hint 必须告诉用户**去哪看**（面板可能已经关了）。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { videoExportEntry } from "./videoExport.ts";

test("an empty edit is ready, not blocked (round 110)", () => {
  const entry = videoExportEntry({ hasEdl: true, exporting: false });
  assert.equal(entry.disabled, false);
  assert.doesNotMatch(entry.hint, /先删除/);
});

test("the EDL arriving enables it", () => {
  const entry = videoExportEntry({ hasEdl: true, exporting: false });
  assert.equal(entry.disabled, false);
  assert.equal(entry.label, "导出成片视频…");
});

test("a missing EDL is the only idle wait left", () => {
  const entry = videoExportEntry({ hasEdl: false, exporting: false });
  assert.equal(entry.disabled, true);
  assert.match(entry.hint, /计算剪辑方案/);
  assert.doesNotMatch(entry.hint, /先删除/);
});

test("a running encode outranks every other reason", () => {
  const entry = videoExportEntry({ hasEdl: true, exporting: true });
  assert.equal(entry.disabled, true);
  assert.match(entry.label, /正在导出/);
  // The dialog may have been closed while the 18-minute encode runs, so the hint has to say
  // where the progress and the stop button are.
  assert.match(entry.hint, /面板/);
  assert.match(entry.hint, /停止/);
});

test("the ready hint points at what the panel adds, not at fixed facts", () => {
  // 1080p / burned-in subtitles are now stated inside the dialog, where they can be read
  // before the click rather than as a hint clipped at the menu's edge (round 135).
  const entry = videoExportEntry({ hasEdl: true, exporting: false });
  assert.match(entry.hint, /文件名/);
  assert.match(entry.hint, /文件夹/);
});
