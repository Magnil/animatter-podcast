/**
 * The degradation paths (§19.32, P2).
 *
 * Four rows, and until now none of them had ever been *reachable* — the table was a design, not a
 * feature. These tests pin the two that are pure logic (the plan-level ones), and assert the shape
 * of the two that live in the component, because a JSX assertion is the only cheap way to notice
 * that a fallback was dropped in a refactor.
 *
 * The 720p proxy row is deliberately **not** implemented: §19.32 measured three 4K clips and found
 * cold loads of 93–149 ms and in-clip seeks of 170–236–406 ms, while cutting *into* a 4K segment
 * costs 189 ms — i.e. prewarming covers the common case and a proxy would buy a 200–260 MB artefact
 * and a per-asset compatibility matrix for the rest. That decision is D23.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  DEGRADED_STAGE_HEIGHT,
  cssFit,
  isUnscaled,
  needsBackdrop,
  stageScale,
} from "./videoPlan.ts";

const STAGE = readFileSync(new URL("./DomStage.tsx", import.meta.url), "utf-8");
const PANEL = readFileSync(new URL("./VideoPanel.tsx", import.meta.url), "utf-8");
const EXPORT_DIALOG = readFileSync(
  new URL("./VideoExportDialog.tsx", import.meta.url),
  "utf-8",
);

test("the degraded stage is capped at 444 px tall and never enlarged", () => {
  // §19.32's export row. The number came from the plan (a quarter of 1080p in *area* is 540, so
  // 444 is deliberately below that).
  assert.equal(DEGRADED_STAGE_HEIGHT, 444);
  // A 1920×1080 canvas at 444 px is a scale of 0.411.
  const capped = stageScale(1.0, 1080, true);
  assert.ok(Math.abs(capped - 444 / 1080) < 1e-9);
  // A panel that already fits in less than the budget keeps its own scale: shrinking it further
  // would make the text unreadable for no CPU saving worth having.
  assert.equal(stageScale(0.2, 1080, true), 0.2);
  // Not degraded means the fit scale, exactly.
  assert.equal(stageScale(0.63, 1080, false), 0.63);
  // No division by a canvas that has not been laid out yet.
  assert.equal(stageScale(0.5, 0, true), 0.5);
  assert.equal(stageScale(0, 1080, true), 0);
  assert.equal(stageScale(Number.NaN, 1080, true), 0);
});

test("an undecodable segment falls back to a still, never to a black box", () => {
  // §19.32 row 1. Black is indistinguishable from a broken preview, so the fallback must be either
  // the last decoded frame or an explicitly labelled placeholder.
  assert.ok(STAGE.includes("layer-undecodable"), "the labelled placeholder is gone");
  assert.ok(STAGE.includes("无法实时预览"));
  assert.ok(STAGE.includes("onFailed"), "the error path no longer replaces the element");
  assert.ok(STAGE.includes("onPoster"), "the last-good-frame capture is gone");
  // The notice still says the export is unaffected — the one thing the user actually needs to know.
  assert.ok(STAGE.includes("导出仍然是好的"));
});

test("the failure path does not retry a decode that is not going to succeed", () => {
  // A codec the browser cannot decode fails deterministically. Retrying re-fetches the same bytes
  // and flashes black on every cut into that segment, so the element is replaced instead.
  const failed = STAGE.indexOf("if (failed) {");
  assert.ok(failed > 0);
  const failedBlock = STAGE.slice(failed, STAGE.indexOf("return (\n    <div className", failed));
  assert.ok(!failedBlock.includes("<SyncedVideo"), "the video element must be gone, not re-tried");
});

test("the degradation is shown, not silent", () => {
  // §19.32's premise is that a degraded preview must be *visible*: 「导出中：预览降级」 is the
  // difference between a user knowing why the picture went soft and filing a bug.
  assert.ok(STAGE.includes("stage-degraded"));
  assert.ok(STAGE.includes("导出中：预览降级"));
  assert.ok(STAGE.includes("内存紧张：预览降级"));
});

test("the degradation condition is polled into state, not read inside a memo", () => {
  // The regression this pins, found by `scripts/check-p2-degrade.mjs` on the real page: the deck
  // read `exportRunning` and `performance.memory` inside its `useMemo`, whose deps are
  // `[plan, frameMs, …]`. While the audio is paused `frameMs` never changes, so both values were
  // latched at mount and the degradation could never appear — badge null, stage 584 px instead of
  // 444 px, on a page whose heap had been faked full.
  assert.ok(STAGE.includes("DEGRADE_POLL_MS"), "the poll interval is gone");
  assert.ok(STAGE.includes("window.setInterval(() => setDegrade(readDegrade())"));
  assert.ok(STAGE.includes("function readDegrade()"));
  // And the reading the deck judges pressure from must be the one the badge was decided on, or the
  // badge can say 「内存紧张」 while the deck disagrees. `readMemoryOf()` itself stays — it is what
  // `readDegrade` samples; what must not come back is the deck sampling it independently.
  assert.ok(STAGE.includes("memory: degrade.memory"));
  const deckCall = STAGE.slice(
    STAGE.indexOf("deckLayout(plan.segments"),
    STAGE.indexOf("deckLayout(plan.segments") + 400,
  );
  assert.ok(!deckCall.includes("readMemoryOf()"), "the deck must not sample memory on its own");
  assert.ok(!deckCall.includes("readExportRunning()"));
});

test("the export flag reaches the stage from the one place that starts an export", () => {
  // The flag is module-level because the encode lives in the export dialog and the stage is
  // three components down. The risk is that it stops being set — and then the stage quietly
  // keeps a second decoder running during an 18-minute encode, which is the exact CPU
  // competition §19.32 forbids.
  //
  // Round 135 moved the encode loop out of `App.tsx` into `VideoExportDialog.tsx` (the click
  // now opens a panel). The invariant is the same one, so the assertion follows the code
  // instead of being deleted: grep the dialog, and still require the clear to sit in a
  // `finally`.
  assert.ok(EXPORT_DIALOG.includes("markExportRunning(true)"));
  assert.ok(EXPORT_DIALOG.includes("markExportRunning(false)"));
  const clear = EXPORT_DIALOG.indexOf("markExportRunning(false)");
  const finallyAt = EXPORT_DIALOG.lastIndexOf("finally", clear);
  assert.ok(finallyAt > 0 && clear - finallyAt < 400, "the clear must live in the finally block");
  // And nothing else may set it: two writers would let one export's finish clear the flag
  // while another is still encoding.
  const APP = readFileSync(new URL("./App.tsx", import.meta.url), "utf-8");
  assert.ok(!APP.includes("markExportRunning("), "the shell must not set the export flag itself");
  assert.ok(PANEL.includes("LiveDomStage"));
});

test("the proxy row is genuinely not implemented, per D23", () => {
  // Guard against a half-landed proxy: a `/proxy` URL or a `proxy_url` field would mean assets are
  // being transcoded somewhere, and §19.32 measured that as unnecessary.
  for (const source of [STAGE, PANEL]) {
    assert.ok(!source.includes("proxy_url"));
    assert.ok(!source.includes("/proxy"));
  }
  // And the media layer still points at the asset file itself.
  assert.ok(STAGE.includes(".layer-media"));
});

test("the fits the chain bakes for are still the ones that need it", () => {
  // Cheap regression: the fallback draws `object-fit` from the same helpers as the live player, so
  // a fit that stopped being recognisable would silently change both.
  assert.deepEqual(cssFit("contain").objectFit, "contain");
  assert.equal(isUnscaled("blur_crop"), true);
  assert.equal(needsBackdrop("blur_fill"), true);
  assert.equal(needsBackdrop("contain"), false);
});
