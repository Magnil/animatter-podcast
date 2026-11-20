/**
 * 两个导出面板的纯逻辑（第 135 轮）。
 *
 * 每一条都对应一个「看起来对、实际错」的写法。这个仓库的教训是：面板里的字没人测，
 * 而这里的每一条都会在真实使用中被看见。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_FOLDER,
  IDLE,
  defaultExportName,
  deliveryNotice,
  isRunning,
  primaryDisabled,
  primaryLabel,
  progressView,
} from "./exportPanels.ts";

const running = (progress, note = "") => ({
  job: { status: "running", progress, error: null, note },
  stopping: false,
});

// --- primary button -----------------------------------------------------

test("idle says 导出, a running job says 停止", () => {
  assert.equal(primaryLabel(IDLE), "导出");
  assert.equal(primaryLabel(running(0.3)), "停止");
});

test("a job that is already stopping does not offer 停止 again", () => {
  // A second click would send a second cancel to a job that is already dying, and — worse —
  // the button would look like the first click did nothing.
  const state = { job: running(0.4).job, stopping: true };
  assert.equal(primaryLabel(state), "正在停止…");
  assert.equal(primaryDisabled(state), true);
});

test("the primary button is disabled before 导出 when a precondition fails", () => {
  // Both preconditions are checked at once so the reason is never "one of them".
  assert.equal(primaryDisabled(IDLE, false), false);
  assert.equal(primaryDisabled(IDLE, true), true);
  assert.equal(primaryDisabled(IDLE, false, true), true);
});

test("a running job keeps 停止 clickable even when a precondition is false now", () => {
  // The folder field is disabled while encoding; that must not also disable 停止.
  assert.equal(primaryDisabled(running(0.2), true), false);
});

test("isRunning tracks the job, not the button", () => {
  assert.equal(isRunning(IDLE), false);
  assert.equal(isRunning(running(0.01)), true);
  assert.equal(isRunning({ job: running(0).job, stopping: true }), true);
});

// --- progress bar -------------------------------------------------------

test("a queued job says 排队中 instead of 0 %", () => {
  // The server has not decided the worker count yet, so 0 % with no note reads as stuck.
  const view = progressView({
    job: { status: "queued", progress: 0, error: null, note: "" },
    stopping: false,
  });
  assert.equal(view.text, "排队中…");
  assert.equal(view.tone, "work");
});

test("a running job shows percent, eta and the server's note", () => {
  const note = "14 块串行编码：空闲 4.2 GB < 2 个并行需要的 8.3 GB";
  const view = progressView(running(0.42, note), "约 10 分钟");
  assert.equal(view.percent, 42);
  assert.match(view.text, /42%/);
  assert.match(view.text, /约 10 分钟/);
  assert.match(view.text, /串行/);
});

test("the note is omitted rather than leaving a trailing gap", () => {
  const view = progressView(running(0.1, ""), "正在估算…");
  assert.doesNotMatch(view.text, /undefined/);
  assert.doesNotMatch(view.text, /　$/);
});

test("a cancelled job is a warning, not a failure", () => {
  // The user asked for it. Colouring it like an error teaches them to distrust the one
  // signal that means something actually broke.
  const view = progressView({
    job: { status: "cancelled", progress: 0.37, error: null, note: "" },
    stopping: false,
  });
  assert.equal(view.tone, "warn");
  assert.equal(view.text, "已停止");
  assert.equal(view.percent, 37);
});

test("a failed job shows the server's error text", () => {
  const view = progressView({
    job: { status: "failed", progress: 0.2, error: "ffmpeg 退出码 -12", note: "" },
    stopping: false,
  });
  assert.equal(view.tone, "bad");
  assert.equal(view.text, "ffmpeg 退出码 -12");
});

test("a failed job with no message still says something", () => {
  const view = progressView({
    job: { status: "failed", progress: 0.2, error: null, note: "" },
    stopping: false,
  });
  assert.equal(view.text, "导出失败");
});

test("done pins the bar at 100 even if the last poll said 89", () => {
  // The server writes progress=1.0 with the terminal state, but the client may act on a
  // response from one poll earlier. A bar that stops at 89 % under 「已完成」 looks broken.
  const view = progressView({
    job: { status: "done", progress: 0.89, error: null, note: "" },
    stopping: false,
  });
  assert.equal(view.percent, 100);
  assert.equal(view.tone, "ok");
});

test("progress outside 0..1 is clamped", () => {
  assert.equal(progressView(running(-0.2)).percent, 0);
  assert.equal(progressView(running(1.4)).percent, 100);
});

// --- file name ----------------------------------------------------------

test("the default name is the episode plus a local date, with no suffix", () => {
  // No suffix: the container is decided by the export path, and a suffix in the box is how
  // 「成片.wav.m4a」 happens.
  const name = defaultExportName("第 07 期 · 罗小黑", new Date(2026, 10, 12));
  assert.equal(name, "第 07 期 · 罗小黑-20261112");
  assert.doesNotMatch(name, /\.(mp4|m4a|wav)$/);
});

test("the date is local, so a midnight export is not dated yesterday", () => {
  const name = defaultExportName("第 07 期", new Date(2026, 10, 13, 0, 30));
  assert.match(name, /20261113/);
});

test("characters Windows forbids never reach the name", () => {
  const name = defaultExportName('第 7 期: "罗小黑" / 专访', new Date(2026, 10, 12));
  assert.doesNotMatch(name, /[\\/:*?"<>|]/);
  assert.match(name, /第 7 期/);
});

test("an empty episode still produces a usable name", () => {
  assert.equal(defaultExportName("   ", new Date(2026, 10, 12)), "成片-20261112");
});

// --- delivery -----------------------------------------------------------

test("the folder field starts empty, which means the server's default", () => {
  // Empty rather than a guessed absolute path: the guess would be wrong on every machine
  // but this one, and the server already knows what "Downloads" means.
  assert.equal(DEFAULT_FOLDER, "");
});

test("the delivery notice leads with the path, because that is the question", () => {
  const notice = deliveryNotice("D:/导出/第 7 期-20261112.mp4", 924_297_354);
  assert.match(notice, /^已保存到 D:\/导出\//);
  assert.match(notice, /881 MB/);
});

test("a small file is reported in KB rather than 0 MB", () => {
  assert.match(deliveryNotice("D:/x.m4a", 512 * 1024), /512 KB/);
});
