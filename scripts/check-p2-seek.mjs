// P2 的前置测量：4K 素材**停下来 seek 一次**要多久？
//
// P2 方案的三件事都是围绕这个数字设计的：720p 代理存在的前提是「4K 的 seek 慢到用户能感觉到」。
// 如果在本机它已经 < 250 ms，那代理就是纯粹的复杂度（多一份磁盘、多一道导入队列、一套降级路径），
// 应该**先量再决定做不做**（§19.34.6 的门槛测量）。
//
// 量的是 `currentTime = t` 到 `seeked` 事件之间的墙上时间，分三档：
//   * 同一段内 seek（最常见的操作：拖播放头）；
//   * 跨段进入 4K（新的 `<video>` 元素从 `src` 赋值到 `loadeddata`）；
//   * 同一元素上连续 seek（预热后，第二、三次应该明显更快）；
//
// Run:
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/p1/seek/shot.png scripts/check-p2-seek.mjs

export default async ({ evaluate, shot, out }) => {
  const report = await evaluate(`
    (async () => {
      const plan = await (await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/plan")).json();
      const segments = plan.segments.filter((item) => item.kind === "video");
      const results = [];

      // Which segments are 4K? Required up front, because a proxy test on a 1080p clip measures
      // nothing.
      const widths = [];
      for (const segment of segments) {
        const probe = document.createElement("video");
        probe.muted = true;
        probe.preload = "metadata";
        probe.src = segment.url;
        await new Promise((resolve) => {
          probe.addEventListener("loadedmetadata", resolve, { once: true });
          probe.addEventListener("error", resolve, { once: true });
          setTimeout(resolve, 8000);
        });
        widths.push({ segment, width: probe.videoWidth, height: probe.videoHeight });
      }

      const timeSeek = async (video, target, label) => {
        const started = performance.now();
        const done = new Promise((resolve) => {
          video.addEventListener("seeked", () => resolve(performance.now() - started), {
            once: true,
          });
          setTimeout(() => resolve(null), 15000);
        });
        video.currentTime = target;
        const elapsed = await done;
        return { label, targetSeconds: Math.round(target * 1000) / 1000, seekMs: elapsed };
      };

      for (const item of widths) {
        if (item.width < 3000) continue;
        const segment = item.segment;
        const video = document.createElement("video");
        video.muted = true;
        video.preload = "auto";
        video.src = segment.url;

        // Cold: from the src assignment to the first painted frame.
        const coldStart = performance.now();
        await new Promise((resolve) => {
          video.addEventListener("loadeddata", resolve, { once: true });
          video.addEventListener("error", resolve, { once: true });
          setTimeout(resolve, 20000);
        });
        const coldMs = Math.round(performance.now() - coldStart);

        // Four seeks inside the clip, spread out — the first is usually the expensive one.
        const seeks = [];
        for (const fraction of [0.15, 0.45, 0.75, 0.55]) {
          seeks.push(await timeSeek(video, video.duration * fraction, "4k-" + segment.start_ms));
        }

        results.push({
          segment: { start: segment.start_ms, end: segment.end_ms },
          width: item.width,
          height: item.height,
          coldMs,
          seeks,
          medianSeekMs: seeks
            .map((entry) => entry.seekMs)
            .filter((value) => value !== null)
            .sort((a, b) => a - b)[1] ?? null,
        });
        video.src = "";
      }
      return results;
    })()
  `);

  for (const entry of report) {
    console.log(
      `${entry.segment.start}-${entry.segment.end} ${entry.width}×${entry.height}: ` +
        `cold ${entry.coldMs} ms, seeks ${JSON.stringify(entry.seeks.map((s) => s.seekMs))}, ` +
        `median ${entry.medianSeekMs} ms`,
    );
  }
  await shot(out);
  return report;
};
