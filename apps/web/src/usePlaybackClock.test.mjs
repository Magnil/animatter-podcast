/**
 * The 50 ms playhead clock, and the mistake it exists to avoid.
 *
 * The DOM stage used to read `positionMs`, which is React state driven by the audio element's
 * `timeupdate`. Chrome throttles that to **217 ms** (measured: p50 217 ms, 42 updates in 12 s), so
 * the `<video>` inside the stage ran at ~49 fps while the *layer stack* — which cue band is up,
 * which segment is drawn — could only change four times a second. Measured: an instant held for up
 * to 232 ms, i.e. a subtitle band appearing five frames late.
 *
 * Raising `positionMs` itself to 20 Hz was the obvious fix and the worse one: it re-rendered the
 * whole workbench twenty times a second (transcript, waveform, video track, transport), measured
 * rAF **49 → 27 fps** and the block switch **232 → 430 ms**.
 *
 * So the fine clock is an external store only the stage subscribes to. These tests pin both
 * halves: the store's semantics, and the fact that the 50 ms tick publishes to it instead of
 * calling `setPositionMs` — which is the regression that is otherwise invisible until someone
 * notices the preview is half a second late.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { playheadClock } from "./playheadClock.ts";

const SOURCE = readFileSync(new URL("./usePlayback.ts", import.meta.url), "utf-8");
const CLOCK = readFileSync(new URL("./playheadClock.ts", import.meta.url), "utf-8");
const PANEL = readFileSync(new URL("./VideoPanel.tsx", import.meta.url), "utf-8");

test("the clock lives in its own module so it can be tested", () => {
  // `usePlayback.ts` imports `./playbackRecovery` without an extension, which Node's type
  // stripping cannot resolve — a test importing the hook cannot run at all. The store has no
  // imports, so it is both testable and where it belongs.
  assert.ok(CLOCK.includes("class PlayheadClock"));
  assert.ok(!CLOCK.includes("import "), "the clock module must stay dependency-free");
});

test("the clock notifies subscribers on change and stops when they leave", () => {
  let calls = 0;
  const unsubscribe = playheadClock.subscribe(() => {
    calls += 1;
  });
  playheadClock.publish(1_000);
  assert.equal(calls, 1);
  // Publishing the same value again is not a change: React must not re-render for it.
  playheadClock.publish(1_000);
  assert.equal(calls, 1);
  playheadClock.publish(1_050);
  assert.equal(calls, 2);
  assert.equal(playheadClock.read(), 1_050);
  unsubscribe();
  playheadClock.publish(2_000);
  assert.equal(calls, 2, "an unsubscribed listener must not be called");
});

test("a seek publishes unconditionally, even to the same millisecond", () => {
  // `publish` skips equal values, which is right for a 50 ms tick and wrong for a jump: seeking
  // back to where the playhead already is must still repaint, or the stage keeps the frame it
  // showed before the user moved away and came back.
  playheadClock.publish(5_000);
  let calls = 0;
  const unsubscribe = playheadClock.subscribe(() => {
    calls += 1;
  });
  playheadClock.reset(5_000);
  assert.equal(calls, 1);
  unsubscribe();
});

test("the 50 ms tick publishes the clock instead of setting React state", () => {
  const start = SOURCE.indexOf("const id = window.setInterval");
  assert.ok(start > 0, "the watchdog interval is gone");
  const end = SOURCE.indexOf("}, TICK_MS);", start);
  const body = SOURCE.slice(start, end);
  assert.ok(
    !body.includes("setPositionMs"),
    "the 50 ms tick must not call setPositionMs: that is the change that halved the frame rate",
  );
  assert.ok(body.includes("playheadClock.publish"), "the tick must publish the fine clock");
});

test("the timeupdate handler and seek() also publish the fine clock", () => {
  // Both move the playhead while the tick is not running (paused, or between ticks), so without
  // them the stage keeps drawing the instant from before the jump.
  const publishes = SOURCE.match(/playheadClock\.publish/g) ?? [];
  assert.ok(publishes.length >= 3, `expected at least 3 publishers, found ${publishes.length}`);
});

test("only the stage subscribes, through useSyncExternalStore", () => {
  // If `positionMs` were raised to 20 Hz instead, this assertion would have nothing to say —
  // which is why the check is on the *shape* of the subscription, not on a number.
  assert.ok(PANEL.includes("useSyncExternalStore(playheadClock.subscribe, playheadClock.read)"));
  assert.ok(PANEL.includes("function LiveDomStage"));
});
