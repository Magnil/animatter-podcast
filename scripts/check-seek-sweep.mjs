// 找出哪些 seek 位置会坏。
//
// 已经确认：seek 到 215.18s 得到 MEDIA_ERR_DECODE(3)，seek 到 5.1s 正常。
// 现在扫一批位置，看是「这一个点」还是「某一段」还是「所有远处位置」。
//
// headless Chrome 没有音频输出设备，play() 一定是 NotAllowedError，所以这里
// 不看 play，只看到位之后 element.error 有没有被设为 3。seek 本身不需要设备。

const probeOne = `(async (seekTo) => {
  const el = document.createElement("audio");
  el.preload = "auto";
  el.src = "/api/projects/prj_1m6v2m8anfpyqq3w8qt/media/med_1m6v2m8sbaq4y5cqtx7/audio";
  const loaded = await new Promise((resolve) => {
    const timer = setTimeout(() => resolve("timeout"), 10000);
    el.addEventListener("loadedmetadata", () => { clearTimeout(timer); resolve("ok"); }, { once: true });
    el.addEventListener("error", () => { clearTimeout(timer); resolve("error"); }, { once: true });
  });
  if (loaded !== "ok") { el.remove(); return { seekTo, loaded }; }

  el.currentTime = seekTo;
  const seeked = await new Promise((resolve) => {
    const timer = setTimeout(() => resolve("timeout"), 6000);
    el.addEventListener("seeked", () => { clearTimeout(timer); resolve("ok"); }, { once: true });
    el.addEventListener("error", () => { clearTimeout(timer); resolve("error"); }, { once: true });
  });
  await new Promise((r) => setTimeout(r, 250));
  const out = { seekTo, seeked, err: el.error ? el.error.code : null, rs: el.readyState };
  el.remove();
  return out;
})`;

const MP3 = "/api/projects/prj_1m6v2m8anfpyqq3w8qt/media/med_1m6v2m8sbaq4y5cqtx7/audio";

export default async ({ evaluate, wait, shot, out }) => {
  await wait(1200);
  const points = [215.0, 215.05, 215.1, 215.12, 215.14, 215.16, 215.18, 215.2, 215.22, 215.24, 215.3, 215.4, 215.5, 215.6];
  const rows = [];
  for (const p of points) {
    rows.push(await evaluate(`(${probeOne})(${p})`));
  }
  await shot(`${out}-sweep`);
  const result = { mp3: MP3, rows };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
