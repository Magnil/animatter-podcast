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

test("an empty edit is ready, not blocked (round 110)", () => {
  // Someone who only proofread subtitles and then wants the film has nothing to cut first.
  // The entry used to be disabled here with 「先删除要剪掉的内容」, which was a rule about the
  // app's plumbing rather than about their work. `deletionCount` is not even an input any
  // more, so the condition cannot come back by accident.
  const entry = videoExportEntry({ hasEdl: true, exportingPercent: null });
  assert.equal(entry.disabled, false);
  assert.doesNotMatch(entry.hint, /先删除/);
  assert.match(entry.hint, /1080p/);
});

test("the EDL arriving enables it", () => {
  const entry = videoExportEntry({ hasEdl: true, exportingPercent: null });
  assert.equal(entry.disabled, false);
  assert.equal(entry.label, "导出成片视频…");
});

test("a missing EDL is the only wait left", () => {
  const entry = videoExportEntry({ hasEdl: false, exportingPercent: null });
  assert.equal(entry.disabled, true);
  // It is a *wait* — the EDL is still being computed — and it is the only reason besides a
  // running encode that still disables the entry.
  assert.match(entry.hint, /计算剪辑方案/);
  assert.doesNotMatch(entry.hint, /先删除/);
});

test("a running encode outranks every other reason", () => {
  // "no EDL" would be wrong here: the film is already rendering.
  const entry = videoExportEntry({ hasEdl: true, exportingPercent: 41 });
  assert.equal(entry.disabled, true);
  assert.match(entry.label, /41%/);
  assert.match(entry.hint, /后台/);
});

test("the ready hint states what the render is fixed at", () => {
  const entry = videoExportEntry({ hasEdl: true, exportingPercent: null });
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
