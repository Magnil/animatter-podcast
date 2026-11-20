// Does the degradation actually look like something? (§19.32, P2)
//
// Three rows of the degradation table are visible state, and "it is implemented" is not the same as
// "the user can see it": the badge has to be legible, the stage has to be *smaller* without being
// broken, and the video has to keep playing while it happens. The fourth row (an undecodable
// asset) cannot be triggered through a real asset in this project, so its fallback is pinned by
// `apps/web/src/videoDegrade.test.mjs` instead — a fake asset would measure the fake, not the path.
//
// Run:
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/p2/degrade/shot.png scripts/check-p2-degrade.mjs

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { parkAtOutputMs } from "./p1-park.mjs";

const VERSION = "ver_1m6v2q5javqm1j82xsg";

export default async ({ evaluate, send, out, shot }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const dir = dirname(out);
  mkdirSync(dir, { recursive: true });
  const log = [];
  const say = (line) => {
    log.push(line);
    console.log(line);
  };

  await send("Emulation.setDeviceMetricsOverride", {
    width: 2000,
    height: 1600,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await sleep(400);
  await evaluate(`
    (() => {
      const panel = document.querySelector(".video-panel");
      if (!panel || !panel.classList.contains("is-collapsed")) return false;
      [...panel.querySelectorAll("button")]
        .find((node) => (node.textContent || "").trim() === "展开")
        ?.click();
      return true;
    })()
  `);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await evaluate(`Boolean(document.querySelector(".dom-stage"))`)) break;
    await sleep(500);
  }

  const duration = await evaluate(`
    (async () => {
      const response = await fetch(location.origin + "/api/versions/${VERSION}/video/plan");
      return (await response.json()).output_duration_ms;
    })()
  `);
  // Inside a video segment, so there is a decoder to give up.
  await parkAtOutputMs({ evaluate, send }, 2_100_000, { total: duration });
  await sleep(600);

  const sample = async (label) => {
    const state = await evaluate(`
      (() => {
        const stage = document.querySelector(".dom-stage");
        const host = stage?.parentElement;
        const badge = document.querySelector(".stage-degraded");
        const deck = document.querySelector(".layer-deck");
        const videos = [...document.querySelectorAll(".layer-deck video")];
        const stageRect = stage?.getBoundingClientRect();
        return {
          badge: badge ? badge.textContent.trim() : null,
          stageHeight: stageRect ? Math.round(stageRect.height) : null,
          stageScale: stage ? stage.style.transform : null,
          deckReason: deck?.dataset.deckDegraded ?? null,
          videos: videos.length,
          videosWithFrames: videos.filter((video) => video.readyState >= 2).length,
          standby: document.querySelectorAll(".layer-deck .layer-media.is-standby").length,
        };
      })()
    `);
    say(`${label.padEnd(10)} ${JSON.stringify(state)}`);
    return state;
  };

  const normal = await sample("normal");
  await shot(`${dir}/normal.png`);

  await evaluate(`window.__p2.setMemoryOverride({ usedJSHeapSize: 999, jsHeapSizeLimit: 1000 })`);
  await sleep(1_400);
  const degraded = await sample("degraded");
  await shot(`${dir}/degraded.png`);

  // The other trigger: a real export sets this flag from `App`, and the badge must say so rather
  // than the generic memory wording. Setting it through the hook is the same code path
  // `markExportRunning(true)` takes in `exportFilm`.
  await evaluate(`window.__p2.setExportRunning(true)`);
  await sleep(1_400);
  const exporting = await sample("exporting");
  await evaluate("window.__p2.setExportRunning(false)");

  await evaluate("window.__p2.setMemoryOverride(null)");
  await sleep(1_400);
  const restored = await sample("restored");

  const verdict = [];
  if (degraded.badge === null) verdict.push("FAIL: no badge while degraded");
  else if (!degraded.badge.includes("降级")) verdict.push(`FAIL: badge reads ${degraded.badge}`);
  if (degraded.stageHeight === null || degraded.stageHeight > 460) {
    verdict.push(`FAIL: the degraded stage is ${degraded.stageHeight} px tall, budget is 444`);
  }
  if (normal.stageHeight !== null && degraded.stageHeight !== null && degraded.stageHeight >= normal.stageHeight) {
    verdict.push("FAIL: degrading did not shrink the stage");
  }
  if (degraded.standby !== 0) verdict.push(`FAIL: ${degraded.standby} standby slots survived`);
  if (degraded.videos === 0) verdict.push("FAIL: the playing element was dropped too");
  if (exporting.badge === null || !exporting.badge.includes("导出")) {
    verdict.push(`FAIL: while an export runs the badge reads ${exporting.badge}`);
  }
  if (exporting.stageHeight === null || exporting.stageHeight > 460) {
    verdict.push(`FAIL: the export-degraded stage is ${exporting.stageHeight} px tall`);
  }
  if (restored.badge !== null) verdict.push("FAIL: the badge outlived the pressure");
  if (restored.stageHeight !== normal.stageHeight) verdict.push("FAIL: the stage did not grow back");

  writeFileSync(
    `${dir}/degrade.json`,
    JSON.stringify({ normal, degraded, exporting, restored }, null, 1),
  );
  say(verdict.length === 0 ? "PASS: degradation is visible, shrinks the stage, keeps the playing clip" : verdict.join("; "));
  await shot(out);
  return log;
};
