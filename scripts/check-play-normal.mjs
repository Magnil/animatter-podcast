// 回归检查：**正常**句子点 ▶ 不能被新增的重试逻辑拖慢或打断。
//
// 上一版走查证明了坏点会恢复；这一版证明好句子仍然「点一下就开始」——
// 重试阶梯最大的风险就是把正常路径也拖进 600ms 的探测窗口。

const patch = `(() => {
  window.__log = [];
  window.__media = null;
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    window.__media = this;
    window.__log.push({ op: "play", at: performance.now(), from: +this.currentTime.toFixed(2) });
    return origPlay.call(this);
  };
  const origLoad = HTMLMediaElement.prototype.load;
  HTMLMediaElement.prototype.load = function () {
    window.__log.push({ op: "load" });
    return origLoad.call(this);
  };
  return true;
})()`;

const scrollToLabel = (label) => `(async () => {
  const list = document.querySelector(".sentence-list");
  const total = list.scrollHeight;
  const step = Math.max(200, Math.floor(total / 40));
  for (let pos = 0; pos < total + step; pos += step) {
    list.scrollTop = pos;
    await new Promise((r) => setTimeout(r, 100));
    const hit = [...document.querySelectorAll(".sentence-time")].find(
      (n) => n.textContent.trim() === ${JSON.stringify(label)},
    );
    if (hit) { hit.scrollIntoView({ block: "center" }); await new Promise((r) => setTimeout(r, 250)); return true; }
  }
  return false;
})()`;

const boxOf = (label) => `(() => {
  for (const row of [...document.querySelectorAll(".sentence")]) {
    const time = row.querySelector(".sentence-time");
    if (time && time.textContent.trim() === ${JSON.stringify(label)}) {
      const t = row.querySelector(".sentence-play");
      t.scrollIntoView({ block: "center" });
      const r = t.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }
  }
  return null;
})()`;

const state = `(() => {
  const a = window.__media;
  return {
    log: window.__log || [],
    currentTime: a ? +a.currentTime.toFixed(2) : null,
    error: a && a.error ? a.error.code : null,
    paused: a ? a.paused : null,
    transport: (document.querySelector(".transport button") || {}).textContent || null,
    notice: (document.querySelector(".notice") || {}).textContent || null,
  };
})()`;

export default async ({ evaluate, mouse, wait, shot, out }) => {
  await wait(2600);
  await evaluate(patch);

  const result = {};
  // 00:05.100 是干净的起点，前面所有实验里都能正常播放
  await evaluate(scrollToLabel("00:05.100"));
  const box = await evaluate(boxOf("00:05.100"));
  await mouse("mousePressed", box.x, box.y);
  await mouse("mouseReleased", box.x, box.y);
  await wait(1800);
  result.afterPlay = await evaluate(state);
  await shot(`${out}-normal`);

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
