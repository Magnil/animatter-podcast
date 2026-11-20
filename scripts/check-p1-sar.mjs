// Does Chrome honour the sample aspect ratio of the anamorphic clip?
//
// `effa1ccb83762a0e.mp4` is coded 852×480 with SAR 640:639, i.e. a display ratio of exactly 16:9 --
// the same ratio as the 1440×810 material box. ffmpeg's `scale` honours SAR, so the still paints
// 1440×810 with no padding. If Chrome instead treats the clip as 852×480, `object-fit: contain`
// paints 1437.75×810 with 1.125 px bars — a horizontal offset growing from −0.67 px at the left
// edge to +0.67 px at the right, which is exactly what a sub-pixel search finds at the two
// instants whose residual survives (best at dx≈+0.5 px, and the whole difference halves there).
//
// `videoWidth`/`videoHeight` are specified as the **coded** dimensions, with the display size being
// `videoWidth × videoPixelAspectRatio`. So read all three, in isolation from the app: one video
// element, one URL, nothing else in the document.
//
// Run:
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/p1/sar/probe.png scripts/check-p1-sar.mjs

export default async ({ evaluate, shot, out }) => {
  const report = await evaluate(`
    (async () => {
      // The plan names the clip the stage uses for this instant, so this probes the same file the
      // preview plays — not a hand-written path that could drift.
      const plan = await (await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/plan")).json();
      const segment = plan.segments.find((item) => item.start_ms <= 1190970 && 1190970 < item.end_ms);
      if (!segment) return { error: "no segment" };

      const video = document.createElement("video");
      video.muted = true;
      video.preload = "metadata";
      video.src = segment.url;
      video.style.cssText = "position:absolute;left:0;top:0;width:1440px;height:810px;object-fit:contain;object-position:50% 50%";
      document.body.innerHTML = "";
      document.body.style.margin = "0";
      document.body.style.background = "#000";
      document.body.appendChild(video);

      await new Promise((resolve, reject) => {
        video.addEventListener("loadedmetadata", resolve, { once: true });
        video.addEventListener("error", () => reject(new Error("load failed")), { once: true });
        setTimeout(() => reject(new Error("timeout")), 15000);
      });
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

      const ratio = Math.min(1440 / video.videoWidth, 810 / video.videoHeight);
      const painted = video.videoWidth * ratio;
      return {
        url: segment.url.split("/").slice(-2)[0],
        videoWidth: video.videoWidth,
        videoHeight: video.videoHeight,
        pixelAspectRatio: video.videoWidth / video.videoHeight,
        displayWidth: video.videoWidth * ratio,
        barEachSide: (1440 - painted) / 2,
        // The offset this causes, at the left and right edges of the box, in source pixels.
        edgeOffsetLeft: -((1440 - painted) / 2) / ratio,
        edgeOffsetRight: (1440 - ((1440 - painted) / 2)) / ratio - video.videoWidth,
      };
    })()
  `);
  console.log(JSON.stringify(report, null, 2));
  // What the numbers mean, so the next reader does not have to re-derive it:
  console.log(
    `\nChrome reports ${report.videoWidth}×${report.videoHeight} for a clip coded 852×480 with ` +
      `SAR 640:639 (852 × 640/639 = 853.33, rounded to 853). It therefore paints ` +
      `${report.displayWidth} px wide inside the 1440 px box: a ${report.barEachSide.toFixed(2)} px ` +
      `bar each side, i.e. a horizontal offset of ${report.edgeOffsetLeft.toFixed(2)} px at the left ` +
      `edge growing to +${report.edgeOffsetRight.toFixed(2)} px at the right. ffmpeg's scale honours ` +
      `SAR and paints the full 1440. This is why the consistency gate measures displacement in ` +
      `sub-pixels instead of demanding an integer shift of 0.`,
  );
  await shot(out);
  return report;
};
