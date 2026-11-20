// 验证修复：点 03:35.180 的那句 ▶，应当自动跳过坏点并播放，同时提示条说明。
//
// 用真实鼠标事件（合成 click 拿不到 user activation，play() 必然被拒）。
// 通过 prototype 打补丁抓住应用的那个 Audio 元素，观察：
//   - play() 的调用次数和错误
//   - load() 是否被调用（说明走了重试阶梯）
//   - 最终 currentTime 是否越过 215.18 并在前进

const patch = `(() => {
  window.__log = [];
  window.__media = null;
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    window.__media = this;
    const entry = { op: "play", from: +this.currentTime.toFixed(3), err: this.error ? this.error.code : null };
    window.__log.push(entry);
    const p = origPlay.call(this);
    if (p && p.then) {
      p.then(() => { entry.ok = true; }, (e) => { entry.ok = false; entry.name = e && e.name; });
    }
    return p;
  };
  const origLoad = HTMLMediaElement.prototype.load;
  HTMLMediaElement.prototype.load = function () {
    window.__media = this;
    window.__log.push({ op: "load" });
    return origLoad.call(this);
  };
  const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "currentTime");
  Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
    configurable: true,
    get: desc.get,
    set(v) {
      window.__media = this;
      window.__log.push({ op: "seek", to: +Number(v).toFixed(3) });
      return desc.set.call(this, v);
    },
  });
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
    readyState: a ? a.readyState : null,
    paused: a ? a.paused : null,
    transport: (document.querySelector(".transport button") || {}).textContent || null,
    notice: (document.querySelector(".notice") || {}).textContent || null,
  };
})()`;

export default async ({ evaluate, mouse, wait, shot, out }) => {
  await wait(2600);
  await evaluate(patch);
  await evaluate(scrollToLabel("03:35.180"));
  const box = await evaluate(boxOf("03:35.180"));
  const result = { box };
  if (box) {
    await mouse("mousePressed", box.x, box.y);
    await mouse("mouseReleased", box.x, box.y);
  }
  await wait(3000);
  result.afterPlay = await evaluate(state);
  await wait(1500);
  result.later = await evaluate(state);
  await shot(`${out}-recovered`);

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
