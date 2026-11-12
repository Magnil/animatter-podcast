/**
 * The 导出 → 导出成片视频… entry (round 102).
 *
 * Two functions, and both exist because the defect they prevent is *invisible*: a menu item
 * that is greyed out for the wrong reason looks exactly like one greyed out for the right
 * one, and a film saved under the wrong name is only noticed a week later in a downloads
 * folder.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { filmFilename, videoExportEntry } from "./videoExport.ts";

// --- enablement ---------------------------------------------------------

test("an empty edit disables the entry and says why", () => {
  const entry = videoExportEntry({ hasEdl: true, deletionCount: 0, exportingPercent: null });
  assert.equal(entry.disabled, true);
  // The dependency is on the *audio* edit — the film is built from the cut audio — and that
  // is not guessable from the label 「导出成片视频」.
  assert.match(entry.hint, /先删除/);
});

test("the EDL arriving enables it", () => {
  const entry = videoExportEntry({ hasEdl: true, deletionCount: 3, exportingPercent: null });
  assert.equal(entry.disabled, false);
  assert.equal(entry.label, "导出成片视频…");
});

test("a missing EDL is a different reason from an empty edit", () => {
  const entry = videoExportEntry({ hasEdl: false, deletionCount: 0, exportingPercent: null });
  assert.equal(entry.disabled, true);
  // "先删除要剪掉的内容" would be a lie: the EDL is how the app knows there is nothing cut.
  assert.match(entry.hint, /计算剪辑方案/);
  assert.doesNotMatch(entry.hint, /先删除/);
});

test("a running encode outranks every other reason", () => {
  // Both "no EDL" and "no deletions" would be wrong here: the film is already rendering.
  const entry = videoExportEntry({ hasEdl: true, deletionCount: 5, exportingPercent: 41 });
  assert.equal(entry.disabled, true);
  assert.match(entry.label, /41%/);
  assert.match(entry.hint, /后台/);
});

test("the ready hint states what the render is fixed at", () => {
  const entry = videoExportEntry({ hasEdl: true, deletionCount: 1, exportingPercent: null });
  // There is no dialog holding these two facts, and a film that turns out to have burned-in
  // subtitles you did not want is discovered after fourteen minutes of encoding.
  assert.match(entry.hint, /1080p/);
  assert.match(entry.hint, /字幕/);
});

// --- file name ----------------------------------------------------------

test("the film is named after the episode, not the render id", () => {
  const name = filmFilename("第 07 期 · 罗小黑", new Date(2026, 10, 12));
  assert.equal(name, "第 07 期 · 罗小黑-20261112.mp4");
});

test("a local date is used, not UTC", () => {
  // 00:30 on the 13th in Shanghai is still the 12th in UTC. `toISOString().slice(0, 10)`
  // would name the file with yesterday's date for anyone filming after 08:00 local.
  const name = filmFilename("第 07 期", new Date(2026, 10, 13, 0, 30));
  assert.match(name, /20261113/);
});

test("characters Windows forbids are replaced", () => {
  const name = filmFilename('第 7 期: "罗小黑" / 专访', new Date(2026, 10, 12));
  assert.doesNotMatch(name, /[\\/:*?"<>|]/);
  assert.match(name, /第 7 期/);
});

test("an empty episode name still produces a usable file", () => {
  assert.equal(filmFilename("   ", new Date(2026, 10, 12)), "成片视频-20261112.mp4");
});
