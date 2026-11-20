// What does a block switch actually cost, at rAF granularity?
//
// `check-p1-playback.mjs` reports "time until the new element is ready" (75 ms at 1080p, 189 ms into
// 4K), which reads like the switch is nearly free. That number is measured from outside the page and
// answers a different question: *when did the DOM settle*. What the viewer sees is **when the first
// frame of the new clip is presented**, and between those two moments the box shows either black
// (`.layer-media { background: #000 }`) or the new clip's frame 0 instead of the frame the playhead
// is on.
//
// So this script runs the measurement *inside* the page, on every animation frame: it watches
// `.layer-media video`, notices when the element identity changes, and records how long it takes
// that element to present its first frame (`getVideoPlaybackQuality().totalVideoFrames` leaving 0).
//
// Run:
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/p2/switch/shot.png scripts/check-p2-switch.mjs
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/p2/switch/4k.png scripts/check-p2-switch.mjs --4k
//
// Writes `<dir>/switch.json` with one row per animation frame across the boundary.

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { parkAtOutputMs } from "./p1-park.mjs";

const FOUR_K = process.argv.includes("--4k");
const DEGRADED = process.argv.includes("--degraded");
const VERSION = "ver_1m6v2q5javqm1j82xsg";

export default async ({ evaluate, mouse, send, out, shot }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const dir = dirname(out);
  mkdirSync(dir, { recursive: true });
  const log = [];
  const say = (line) => {
    log.push(line);
    console.log(line);
  };

  await send("Emulation.setDeviceMetricsOverride", {
    width: 2400,
    // Tall enough for the stage **plus** the transport beneath it: a click below the viewport is a
    // silent no-op, and the failure looks exactly like "playback did not start".
    height: 2400,
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

  // Pick a video→video boundary whose destination survives a few seconds.
  const target = await evaluate(`
    (async () => {
      const plan = await (await fetch(location.origin + "/api/versions/${VERSION}/video/plan")).json();
      const pairs = plan.segments
        .slice(1)
        .map((segment, index) => ({ previous: plan.segments[index], segment }));
      const candidates = pairs.filter(
        (item) =>
          item.previous.kind === "video" &&
          item.segment.kind === "video" &&
          item.segment.end_ms - item.segment.start_ms > 8_000,
      );
      const probed = [];
      for (const item of candidates) {
        const probe = document.createElement("video");
        probe.muted = true;
        probe.preload = "metadata";
        probe.src = item.segment.url;
        await new Promise((resolve) => {
          probe.addEventListener("loadedmetadata", resolve, { once: true });
          probe.addEventListener("error", resolve, { once: true });
          setTimeout(resolve, 8000);
        });
        probed.push({ boundary: item.segment.start_ms, width: probe.videoWidth ?? 0 });
      }
      const want4k = ${FOUR_K ? "true" : "false"};
      // On the sample plan **no** video→video boundary is 4K: all three 4K clips are entered from
      // an image segment, so there is nothing to flip between. Returning null there is the honest
      // answer — the alternative is silently measuring the 1080p cut under a "4K" label.
      const fourK = probed.filter((item) => item.width >= 3000);
      const chosen = want4k ? (fourK[0] ?? null) : (probed[0] ?? null);
      return { chosen, probed, duration: plan.output_duration_ms, box: plan.box };
    })()
  `);
  const chosen = target.chosen;
  if (!chosen) {
    say(
      FOUR_K
        ? "NO 4K video→video boundary: every 4K clip on this plan is entered from an image segment"
        : "no video→video boundary found",
    );
    await shot(out);
    return log;
  }
  say(`boundary ${chosen.boundary} ms, destination ${chosen.width} px${FOUR_K ? " (4K run)" : ""}`);
  say(`all candidates: ${JSON.stringify(target.probed)}`);

  const parked = await parkAtOutputMs({ evaluate, send }, Math.max(1_000, chosen.boundary - 3_000), {
    total: target.duration,
  });
  say(`parked: landed ${parked.landed}`);

  const playBox = await evaluate(`
    (() => {
      const button = [...document.querySelectorAll(".transport button")].find((node) =>
        (node.textContent || "").includes("播放"),
      );
      if (!button) return null;
      const rect = button.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    })()
  `);
  if (!playBox) {
    say("no play button");
    await shot(out);
    return log;
  }
  const viewport = await evaluate("({ w: innerWidth, h: innerHeight })");
  if (playBox.y > viewport.h - 5 || playBox.x > viewport.w - 5) {
    // A click outside the viewport is a no-op, and silence is not a result.
    say(`STOP: play button at y=${Math.round(playBox.y)} is outside a ${viewport.h} px viewport`);
    await shot(out);
    return log;
  }

  // Instrument, in the page. `totalVideoFrames` counts *presented* frames, which is the only signal
  // that says "the viewer saw this clip" rather than "the element has metadata".
  await evaluate(`
    (() => {
      window.__p2probe = { rows: [], run: true };
      const state = { element: null, firstFrameAt: null, mountedAt: null, presentation: 0 };
      window.__p2probeState = state;
      const tick = () => {
        if (!window.__p2probe.run) return;
        const stage = document.querySelector(".dom-stage");
        const deck = document.querySelector(".layer-deck");
        if (!stage || !deck) {
          requestAnimationFrame(tick);
          return;
        }
        const atMs = Number(stage.dataset.atMs);
        const frontIndex = deck.dataset.deckFront;
        // The **visible** slot is the one whose content the viewer sees. Both slots are mounted;
        // asking about the standby would measure a decoder nobody is looking at.
        const visible = deck.querySelector(".layer-media:not(.is-standby) video");
        const element = visible;
        const same = element === state.element;
        if (element && !same) {
          state.element = element;
          state.mountedAt = performance.now();
          state.firstFrameAt = null;
        }
        let presented = 0;
        let readyState = -1;
        let seeking = false;
        if (element) {
          try {
            presented = element.getVideoPlaybackQuality().totalVideoFrames;
          } catch {
            presented = 0;
          }
          readyState = element.readyState;
          seeking = element.seeking;
        }
        if (element && presented > 0 && state.firstFrameAt === null) {
          state.firstFrameAt = performance.now();
        }
        // How many slots hold a decodable source, and whether the warm one is ready to be shown.
        const slots = [...deck.querySelectorAll(".layer-media")].map((node) => ({
          standby: node.classList.contains("is-standby"),
          url: node.querySelector("video")?.currentSrc.slice(-14) ?? null,
          readyState: node.querySelector("video")?.readyState ?? -1,
          presented: (() => {
            try {
              return node.querySelector("video")?.getVideoPlaybackQuality().totalVideoFrames ?? 0;
            } catch {
              return 0;
            }
          })(),
        }));
        window.__p2probe.rows.push({
          t: performance.now(),
          atMs,
          frontIndex,
          warmed: deck.dataset.deckWarmed,
          src: element ? element.currentSrc.slice(-14) : null,
          readyState,
          seeking,
          presented,
          slots,
          sinceMountMs:
            state.mountedAt === null ? null : Math.round(performance.now() - state.mountedAt),
          firstFrameMs:
            state.firstFrameAt === null || state.mountedAt === null
              ? null
              : Math.round(state.firstFrameAt - state.mountedAt),
        });
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
      return true;
    })()
  `);

  if (DEGRADED) {
    // Exactly the path a running export takes: the deck gives up its second decoder.
    await evaluate("window.__p2 ? window.__p2.setExportRunning(true) : null");
  }
  await mouse("mousePressed", playBox.x, playBox.y);
  await mouse("mouseReleased", playBox.x, playBox.y);
  await sleep(4_500);
  await mouse("mousePressed", playBox.x, playBox.y);
  await mouse("mouseReleased", playBox.x, playBox.y);

  const rows = JSON.parse(await evaluate("JSON.stringify(window.__p2probe.rows)"));
  writeFileSync(`${dir}/switch.json`, JSON.stringify({ boundary: chosen.boundary, rows }, null, 1));

  // Report every element change, and how long that element took to present a frame.
  const changes = [];
  let previous = null;
  let mark = null;
  for (const row of rows) {
    if (row.src !== previous) {
      if (mark) changes.push(mark);
      previous = row.src;
      mark = { t: row.t, src: row.src, atMs: row.atMs, firstFrameMs: null };
    }
    if (mark && row.firstFrameMs !== null && mark.firstFrameMs === null) mark.firstFrameMs = row.firstFrameMs;
    if (mark) mark.nextAt = row.atMs;
  }
  say(`animation frames recorded: ${rows.length}${DEGRADED ? " (degraded: no standby)" : ""}`);
  const summary = changes.map((item) => ({
    atMs: item.atMs,
    clip: (item.src ?? "").slice(-14),
    firstFrameMs: item.firstFrameMs,
  }));
  say(`clips in order: ${JSON.stringify(summary)}`);

  const nearBoundary = rows.filter((row) => Math.abs(row.atMs - chosen.boundary) <= 600);
  const staleBy = nearBoundary.filter((row) => row.firstFrameMs === null).length;
  say(`frames within 600 ms of the boundary: ${nearBoundary.length}, of them unready: ${staleBy}`);

  // The question this whole script exists for: how long is the box showing nothing, at the cut?
  const before = rows.filter((row) => row.atMs < chosen.boundary && row.atMs >= chosen.boundary - 400);
  const after = rows.filter((row) => row.atMs >= chosen.boundary && row.atMs < chosen.boundary + 600);
  const blankAfter = after.filter((row) => row.presented === 0);
  say(
    `before boundary: ${before.length} frames, last presented=${before.at(-1)?.presented ?? "-"} ` +
      `readyState=${before.at(-1)?.readyState ?? "-"}`,
  );
  say(
    `after boundary: ${after.length} frames, ${blankAfter.length} with no presented frame ` +
      `(first ready frame at +${after.find((row) => row.presented > 0)?.atMs !== undefined ? after.find((row) => row.presented > 0).atMs - chosen.boundary : "never"} ms)`,
  );
  const crossing = rows.find((row) => row.atMs >= chosen.boundary);
  if (crossing) {
    const blankRows = rows.filter((row) => row.atMs >= chosen.boundary && row.atMs < chosen.boundary + 1_000 && row.presented === 0);
    const lastBlank = blankRows.at(-1);
    say(
      `**unready window after the cut: ${blankRows.length} frames**` +
        (lastBlank ? ` , ending +${lastBlank.atMs - chosen.boundary} ms` : " (none)"),
    );
  }
  const switched = rows.findIndex((row) => row.atMs >= chosen.boundary && row.frontIndex !== null && row.src !== null);
  say(`first row after the boundary with a source: ${JSON.stringify(switched >= 0 ? { atMs: rows[switched].atMs, src: rows[switched].src, readyState: rows[switched].readyState, slots: rows[switched].slots } : null)}`);
  await shot(out);
  return log;
};
