// P1 consistency capture, at **native canvas size**.
//
// The first version screenshot the page at 1.5× and compared a 478 px-wide stage crop against a
// 1920 px render downscaled to 480. That is not a fair comparison: the two sides are resampled
// from different resolutions, so every glyph edge and every one-pixel registration difference at
// 480 becomes a difference, and the numbers (3–4 % on instants that are actually identical) said
// more about the crop than about the layers.
//
// So this version removes the scaling entirely: it sets the stage's transform to `none` and
// screenshots the element at its own 1920×1080, on a viewport big enough to contain it. The layers
// are positioned in canvas pixels, so `transform: none` changes nothing about the picture — only
// how much of it the window can show.
//
// Run:
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/p1/consistency/frame.png \
//     scripts/check-p1-consistency.mjs

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

/**
 * Instants by fraction through the transcript.
 *
 * §19.31.5's acceptance line asks for **20** instants, and the count matters: the failures found so
 * far sat at 1 190 970 / 1 473 980 / 2 624 610 ms, three instants out of ten — a spread of ten
 * samples leaves whole minutes of the film unchecked. These 20 are spread evenly but not on a
 * regular grid (a regular grid can alias against block boundaries, which are the things most
 * likely to be wrong).
 */
const FRACTIONS = [
  0.02, 0.05, 0.09, 0.12, 0.17, 0.2, 0.25, 0.3, 0.35, 0.4,
  0.46, 0.52, 0.58, 0.64, 0.7, 0.76, 0.82, 0.88, 0.93, 0.97,
];

export default async ({ evaluate, shot, out, send }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const dir = dirname(out);
  mkdirSync(dir, { recursive: true });

  // A viewport tall and wide enough for the whole 1920×1080 stage at scale 1, plus the panel
  // above and to the left of it. 1× device scale: the point is to avoid resampling, and a
  // 1.5× screenshot of a 1920 px stage would be 2880 px for no gain.
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
      const toggle = [...panel.querySelectorAll("button")].find(
        (node) => (node.textContent || "").trim() === "展开",
      );
      toggle?.click();
      return true;
    })()
  `);
  await sleep(2000);

  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await evaluate(`Boolean(document.querySelector(".dom-stage"))`)) break;
    await sleep(500);
  }

  // Undo the fit-to-panel scaling and let the stage lay out at its real size. The host's
  // `overflow: hidden` has to go too, or the stage is clipped to the panel's 478 px.
  const unbounded = await evaluate(`
    (() => {
      const stage = document.querySelector(".dom-stage");
      if (!stage) return null;
      stage.style.transform = "none";
      const host = stage.parentElement;
      host.style.width = "1920px";
      host.style.height = "1080px";
      host.style.overflow = "visible";
      const box = host.parentElement;
      if (box) {
        box.style.width = "auto";
        box.style.height = "auto";
        box.style.aspectRatio = "auto";
        box.style.display = "block";
        box.style.overflow = "visible";
      }
      const rect = stage.getBoundingClientRect();
      return { x: rect.x, y: rect.y, w: rect.width, h: rect.height };
    })()
  `);
  if (!unbounded) {
    console.log("no stage");
    return null;
  }
  console.log(`stage at ${unbounded.w}×${unbounded.h} (scale 1)`);
  if (Math.abs(unbounded.w - 1920) > 1 || Math.abs(unbounded.h - 1080) > 1) {
    console.log("STOP: the stage did not lay out at its canvas size");
    await shot(out);
    return null;
  }

  const frames = [];
  for (const fraction of FRACTIONS) {
    await evaluate(`
      (async () => {
        const list = document.querySelector(".sentence-list");
        if (!list) return false;
        list.scrollTop = Math.floor(list.scrollHeight * ${fraction});
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        await new Promise((r) => setTimeout(r, 300));
        const box = list.getBoundingClientRect();
        const rows = [...list.querySelectorAll(".sentence-time")].filter((node) => {
          const rect = node.getBoundingClientRect();
          return rect.top > box.top + 20 && rect.bottom < box.bottom - 20;
        });
        (rows[0] ?? list.querySelector(".sentence-time"))?.dispatchEvent(
          new MouseEvent("click", { bubbles: true }),
        );
        return true;
      })()
    `);
    await sleep(900);
    await evaluate(`
      (async () => {
        const stage = document.querySelector(".dom-stage");
        if (!stage) return "no stage";
        await Promise.all(
          [...stage.querySelectorAll("img")].map((img) =>
            img.decode ? img.decode().catch(() => undefined) : undefined,
          ),
        );
        // A video needs more than readyState: assigning currentTime starts a seek, and
        // until the seeked event fires the element is still painting the PREVIOUS frame.
        // Waiting for readiness alone screenshotted a frame up to a second stale on a 190 s
        // clip, which the comparison then scored as a 15 % layer mismatch.
        await Promise.all(
          [...stage.querySelectorAll("video")].map((video) => {
            if (video.readyState < 2) {
              return new Promise((resolve) => {
                video.addEventListener("loadeddata", () => resolve(undefined), { once: true });
                video.addEventListener("error", () => resolve(undefined), { once: true });
                setTimeout(() => resolve(undefined), 4000);
              });
            }
            if (!video.seeking) return undefined;
            return new Promise((resolve) => {
              video.addEventListener("seeked", () => resolve(undefined), { once: true });
              setTimeout(() => resolve(undefined), 4000);
            });
          }),
        );
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        return "ready";
      })()
    `);
    await sleep(350);

    const info = await evaluate(`
      (() => {
        const stage = document.querySelector(".dom-stage");
        if (!stage) return null;
        const rect = stage.getBoundingClientRect();
        return {
          atMs: Math.round(Number(stage.dataset.atMs)),
          rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
          layers: [...stage.children].map((node) => node.className),
          videos: [...stage.querySelectorAll("video")].map((video) => ({
            readyState: video.readyState,
            currentTime: video.currentTime,
            duration: video.duration,
            seeking: video.seeking,
            error: video.error ? video.error.code : null,
          })),
        };
      })()
    `);
    if (!info || !Number.isFinite(info.atMs)) continue;

    // `captureBeyondViewport` + a clip at the stage's own rect: the result is exactly
    // 1920×1080 of the stage, at 1× — the same pixel grid the still renders on.
    const { data } = await send("Page.captureScreenshot", {
      format: "png",
      clip: { x: info.rect.x, y: info.rect.y, width: 1920, height: 1080, scale: 1 },
      captureBeyondViewport: true,
    });
    const file = `${dir}/frame-${info.atMs}.png`;
    writeFileSync(file, Buffer.from(data, "base64"));

    frames.push({ ...info, file });
    console.log(`at ${info.atMs} ms: ${info.layers.length} layers, ${info.videos.length} videos`);
  }

  writeFileSync(`${dir}/frames.json`, JSON.stringify({ dpr: 1, native: true, frames }, null, 2));
  console.log(`wrote ${frames.length} native frames`);
  return { frames };
};
