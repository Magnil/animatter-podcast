// P1 播放验收：真播一段，量「跟不跟得上」和「跨块切换多久」。
//
// 四条要量的事，每条失败的样子都不一样：
//
//   1. **不掉帧**：`getVideoPlaybackQuality().droppedVideoFrames`。那才是用户感觉到的东西。
//   2. **跟得上实时**：播放头的推进速度 / 墙上时间，从「开始移动」那一刻起算（点击到音频真正
//      起动有 0.5–0.7 s，把它算进去会得到一个假的 0.94×）。
//   3. **跨块切换 ≤ 300 ms**：进入新段 → 新段元素 readyState ≥ 2 且不在 seeking。
//   4. **画面不冻结**：同一时刻连续停留的最长跨度（用户真正看到的卡顿）。实测 234 ms，
//      而这个 234 ms 正是切换本身——不是额外的。
//
// 播放必须用真实鼠标事件（`mouse()`，见 cdp.mjs）：合成 click 没有 user activation，
// `play()` 会被 `NotAllowedError` 拒掉，把真正的解码错误盖住。
//
// Run:
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/p1/playback/shot.png \
//     scripts/check-p1-playback.mjs

import { writeFileSync } from "node:fs";
import { parkAtOutputMs } from "./p1-park.mjs";

/** Set by `--4k`: park in a segment whose source is 4K instead of the first video→video boundary. */
const FOUR_K = process.argv.includes("--4k");

export default async ({ evaluate, mouse, send, out, shot }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const log = [];
  const say = (line) => {
    log.push(line);
    console.log(line);
  };

  await send("Emulation.setDeviceMetricsOverride", {
    width: 2400,
    height: 1500,
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
  say(`stage mounted: ${await evaluate(`Boolean(document.querySelector(".dom-stage"))`)}`);

  // Park before a **video → video** boundary. Two reasons: both sides then have a `<video>`, so
  // the switch is a like-for-like swap; and crossing into an image segment would leave nothing to
  // sample, which reads as a failure ("0/46 samples with a playing video") without being one.
  const target = await evaluate(`
    (async () => {
      const plan = await (await fetch(location.origin + "/api/versions/ver_1m6v2q5javqm1j82xsg/video/plan")).json();
      const pairs = plan.segments
        .slice(1)
        .map((segment, index) => ({ previous: plan.segments[index], segment }));
      const pair =
        pairs.find(
          (item) =>
            item.previous.kind === "video" &&
            item.segment.kind === "video" &&
            item.segment.end_ms - item.segment.start_ms > 15_000,
        ) ?? { segment: plan.segments[0] };
      return {
        boundary: pair.segment.start_ms,
        end: pair.segment.end_ms,
        duration: plan.output_duration_ms,
        // Every video segment's width, so a 4K take can pick one without hard-coding a timestamp.
        videoSegments: plan.segments
          .filter((item) => item.kind === "video")
          .map((item) => ({ start: item.start_ms, end: item.end_ms, url: item.url })),
      };
    })()
  `);
  let seekPoint = Math.max(1_000, target.boundary - 5_000);
  if (FOUR_K) {
    // Probe each video segment's source for its width, then park in the middle of the first 4K one.
    const widths = await evaluate(`
      (async () => {
        const out = [];
        for (const segment of ${JSON.stringify(target.videoSegments)}) {
          const video = document.createElement("video");
          video.muted = true;
          video.preload = "metadata";
          video.src = segment.url;
          const ok = await new Promise((resolve) => {
            video.addEventListener("loadedmetadata", () => resolve(true), { once: true });
            video.addEventListener("error", () => resolve(false), { once: true });
            setTimeout(() => resolve(false), 8000);
          });
          out.push({ start: segment.start, end: segment.end, width: video.videoWidth, ok });
        }
        return out;
      })()
    `);
    say(`segment widths: ${JSON.stringify(widths)}`);
    const fourK = widths.filter((item) => item.ok && item.width >= 3000);
    if (!fourK.length) {
      say("no 4K segment found; run without --4k");
      await shot(out);
      return log;
    }
    const chosen = fourK[0];
    // 5 s **before** the 4K segment, so a 12 s playback enters it and the switch is measured.
    // Starting in the middle measures realtime playback of 4K and reports no switch at all.
    seekPoint = Math.max(1_000, chosen.start - 5_000);
    say(`4K segment ${chosen.start}-${chosen.end} (${chosen.width} px), parking at ${seekPoint}`);
  }
  const want = seekPoint;
  const parked = await parkAtOutputMs({ evaluate, send }, want, { total: target.duration });
  say(`parked before boundary ${target.boundary} ms: wanted ${want}, landed ${parked.landed}`);

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
  await mouse("mousePressed", playBox.x, playBox.y);
  await mouse("mouseReleased", playBox.x, playBox.y);
  say("pressed play");

  const sample = await evaluate(`
    (async () => {
      // Sample on **every animation frame**, not on a 250 ms timer. The first version used
      // setTimeout(250) and reported a block-switch latency of 0 ms — the sampling interval was
      // longer than the thing being measured. At ~50 fps a frame is 20 ms.
      const samples = [];
      const started = performance.now();
      await new Promise((resolve) => {
        const tick = () => {
          const stage = document.querySelector(".dom-stage");
          const videos = [...(stage?.querySelectorAll("video") ?? [])];
          samples.push({
            t: Math.round(performance.now() - started),
            atMs: stage ? Number(stage.dataset.atMs) : null,
            videos: videos.map((video) => ({
              readyState: video.readyState,
              seeking: video.seeking,
              paused: video.paused,
              currentTime: Math.round(video.currentTime * 1000),
              quality: video.getVideoPlaybackQuality
                ? {
                    total: video.getVideoPlaybackQuality().totalVideoFrames,
                    dropped: video.getVideoPlaybackQuality().droppedVideoFrames,
                  }
                : null,
            })),
          });
          if (performance.now() - started < 12000) requestAnimationFrame(tick);
          else resolve(undefined);
        };
        requestAnimationFrame(tick);
      });
      return { samples, elapsed: Math.round(performance.now() - started) };
    })()
  `);

  await mouse("mousePressed", playBox.x, playBox.y);
  await mouse("mouseReleased", playBox.x, playBox.y);

  const first = sample.samples[0];
  const last = sample.samples[sample.samples.length - 1];
  say(`rAF: ${sample.samples.length} frames in ${sample.elapsed} ms → ${((sample.samples.length / sample.elapsed) * 1000).toFixed(1)} fps`);

  // Frame counters restart when an element's `src` is replaced, so a running total is built from
  // per-source maxima. Last-minus-first reported 155 frames for 12 s of 25 fps playback, which is
  // a reset being read as a decode rate.
  let decoded = 0;
  let dropped = 0;
  let run = { total: 0, dropped: 0 };
  for (const item of sample.samples) {
    const quality = item.videos[0]?.quality;
    if (!quality) continue;
    if (quality.total < run.total) {
      decoded += run.total;
      dropped += run.dropped;
      run = { total: 0, dropped: 0 };
    }
    run.total = Math.max(run.total, quality.total);
    run.dropped = Math.max(run.dropped, quality.dropped);
  }
  decoded += run.total;
  dropped += run.dropped;
  say(`decoded ${decoded} video frames, dropped ${dropped}`);

  // Realtime-ness from the first *moving* sample: the audio element starts 0.5–0.7 s after the
  // click, and counting that dead time made a realtime run read as 0.94×.
  const moving = sample.samples.findIndex(
    (item, index) => index > 0 && item.atMs !== sample.samples[0].atMs,
  );
  say(`playback started after ${sample.samples[Math.max(0, moving)]?.t ?? "?"} ms`);
  if (moving > 0) {
    const ratio = (last.atMs - sample.samples[moving].atMs) / (last.t - sample.samples[moving].t);
    say(`advance ratio: ${ratio.toFixed(3)} (1.0 = realtime)`);
  }

  // Longest stretch at one instant — the freeze a user sees, with startup excluded.
  let longest = { ms: 0, at: null };
  let runStart = Math.max(0, moving);
  for (let i = runStart + 1; i < sample.samples.length; i += 1) {
    if (sample.samples[i].atMs === sample.samples[i - 1].atMs) continue;
    const held = sample.samples[i - 1].t - sample.samples[runStart].t;
    if (held > longest.ms) longest = { ms: held, at: sample.samples[runStart].atMs };
    runStart = i;
  }
  say(`longest picture freeze: ${longest.ms} ms at ${longest.at} ms of output`);

  // **How granular is the picture?** The stage's instant comes from the audio element's
  // `timeupdate`, which Chrome throttles to ~250 ms. So the *video* plays at 48 fps while the
  // *layer stack* (which cue band is up, which segment is drawn) can only change four times a
  // second. That is a real preview-fidelity limit, and it is measured here rather than assumed:
  // the median hold below is the granularity.
  const holds = [];
  let holdStart = Math.max(0, moving);
  for (let i = holdStart + 1; i < sample.samples.length; i += 1) {
    if (sample.samples[i].atMs === sample.samples[i - 1].atMs) continue;
    holds.push(sample.samples[i - 1].t - sample.samples[holdStart].t);
    holdStart = i;
  }
  if (holds.length) {
    const sorted = [...holds].sort((a, b) => a - b);
    const at = (q) => sorted[Math.min(sorted.length - 1, Math.round(q * (sorted.length - 1)))];
    say(
      `instant granularity: p50 ${at(0.5)} ms, p90 ${at(0.9)} ms, max ${sorted[sorted.length - 1]} ms ` +
        `(${holds.length} updates in ${sample.elapsed} ms)`,
    );
  }

  // Switch latency: where the playhead entered a new segment, and how long until its element
  // reported a painted, non-seeking frame.
  const segments = await evaluate(`
    (async () => {
      const plan = await (await fetch(location.origin + "/api/versions/ver_1m6v2q5javqm1j82xsg/video/plan")).json();
      return plan.segments.map((item) => ({ start: item.start_ms, end: item.end_ms, kind: item.kind }));
    })()
  `);
  const segmentOf = (at) => segments.find((item) => item.start <= at && at < item.end);
  const switches = [];
  for (let i = 1; i < sample.samples.length; i += 1) {
    const previous = segmentOf(sample.samples[i - 1].atMs);
    const current = segmentOf(sample.samples[i].atMs);
    if (!previous || !current || previous.start === current.start) continue;
    const ready = sample.samples.find(
      (item, index) => index >= i && item.videos.some((v) => v.readyState >= 2 && !v.seeking),
    );
    switches.push({
      from: previous.start,
      to: current.start,
      latencyMs: ready ? ready.t - sample.samples[i].t : null,
    });
  }
  say(`segment switches: ${JSON.stringify(switches)}`);

  const path = out.replace(/\.png$/, "-samples.json");
  writeFileSync(path, JSON.stringify(sample.samples, null, 1));
  say(`samples: ${path}`);

  await shot(out);
  return log;
};
