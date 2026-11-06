/**
 * Tests for export progress reporting.
 *
 * The user's report was 「导出成片音频 点击后无反应」. The encode was running; it
 * simply had no observable state for twelve minutes. An estimate that says
 * "还剩约 1 分钟" is the whole fix, so the arithmetic that produces it is worth
 * testing — a wrong one is worse than none.
 *
 * The estimate is derived from *observed throughput* (output written so far over
 * wall time spent), not from the output duration. That distinction was found by
 * measuring this machine: a 6067-second export took 12 minutes, so encoding is
 * roughly eight times real time. Deriving the remaining time from the output
 * duration alone claimed eight times more time than was left.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { isTerminal, percentOf, pollDelayMs, renderEta } from "./renderProgress.ts";

const MINUTE = 60_000;

test("no estimate before there is throughput to measure", () => {
  assert.equal(renderEta(0, 0.0), "正在估算…");
  assert.equal(renderEta(MINUTE, 0.01), "正在估算…");
  assert.equal(renderEta(MINUTE, 0.02), "正在估算…");
});

test("a steady encode predicts the remaining wall time", () => {
  // Half way through after 6 minutes → about 6 more, regardless of how long the
  // output is. This is the real 105-minute episode: 12 min total for 6067 s.
  assert.equal(renderEta(MINUTE * 6, 0.5), "约 6 分钟");
  assert.equal(renderEta(MINUTE * 10.8, 0.9), "约 1 分钟");
});

test("nearly finished counts in seconds, not a rounded zero", () => {
  assert.equal(renderEta(MINUTE * 12, 0.9999), "不到 1 分钟");
});

test("long encodes switch to hours", () => {
  assert.equal(renderEta(MINUTE * 180, 0.5), "约 3 小时");
  assert.equal(renderEta(MINUTE * 360, 0.5), "约 6 小时");
  // Not a whole number of hours: keep the minutes, they are not noise.
  assert.equal(renderEta(MINUTE * 150, 0.5), "约 2 小时 30 分钟");
});

test("an encode that slows down is not double counted", () => {
  // 90 % done after 20 minutes is genuinely slower output; the estimate must
  // still be about the observed rate, not zero.
  assert.equal(renderEta(MINUTE * 20, 0.9), "约 2 分钟");
});

test("percent is clamped both ways", () => {
  assert.equal(percentOf(0), 0);
  assert.equal(percentOf(0.5), 50);
  assert.equal(percentOf(1), 100);
  // ffmpeg can overshoot on a rounded duration; showing 103 % looks broken.
  assert.equal(percentOf(1.03), 100);
  assert.equal(percentOf(-0.2), 0);
  assert.equal(percentOf(Number.NaN), 0);
});

test("polling is eager at first, then backs off", () => {
  assert.equal(pollDelayMs(0), 400);
  assert.equal(pollDelayMs(4_000), 400);
  assert.equal(pollDelayMs(6_000), 1_500);
  // A twelve-minute wait must not become thousands of requests.
  assert.equal(pollDelayMs(10 * MINUTE), 1_500);
});

test("only done and failed are terminal", () => {
  assert.equal(isTerminal("done"), true);
  assert.equal(isTerminal("failed"), true);
  assert.equal(isTerminal("queued"), false);
  assert.equal(isTerminal("running"), false);
});
