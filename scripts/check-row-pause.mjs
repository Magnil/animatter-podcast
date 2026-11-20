// ▶/⏸ 真机验证，第二版。
//
// 上一版点 ⏸ 没反应，日志里连 pause 都没有——但按钮的 disabled=false、opacity=1，
// 说明它是在的。最可能是坐标失效：点 ▶ 之后播放头动起来，虚拟列表跟着滚，
// 行位置变了，而我用的还是点击前量的坐标。
//
// 这一版每次点击前**重新量**坐标，并且点完核对日志里有没有 pause。

const patch = `(() => {
  window.__log = [];
  window.__media = null;
  if (window.__patched) return true;
  window.__patched = true;
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    window.__media = this;
    window.__log.push({ op: "play", from: +this.currentTime.toFixed(2) });
    return origPlay.call(this);
  };
  const origPause = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.pause = function () {
    window.__media = this;
    window.__log.push({ op: "pause", at: +this.currentTime.toFixed(2) });
    return origPause.call(this);
  };
  return true;
})()`;

// 找「当前活动的那一行」（.sentence.active）上的两个按钮；没有活动行就用第一行
const boxesOf = `(() => {
  const row = document.querySelector(".sentence.active") || document.querySelector(".sentence");
  if (!row) return null;
  const buttons = [...row.querySelectorAll(".sentence-play")];
  return {
    ord: row.getAttribute("data-ord"),
    buttons: buttons.map((b) => {
      const r = b.getBoundingClientRect();
      return {
        text: b.textContent.trim(),
        disabled: b.disabled,
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
      };
    }),
  };
})()`;

const state = `(() => {
  const a = window.__media;
  return {
    playing: a ? !a.paused : null,
    currentTime: a ? +a.currentTime.toFixed(2) : null,
    log: (window.__log || []).slice(-6),
  };
})()`;

const clickAt = async (mouse, x, y) => {
  await mouse("mousePressed", x, y);
  await mouse("mouseReleased", x, y);
};

export default async ({ evaluate, mouse, wait, shot, out }) => {
  const result = {};
  await wait(3500);
  await evaluate(patch);

  const before = await evaluate(boxesOf);
  result.before = before;
  const playBtn = before.buttons[0];
  await clickAt(mouse, playBtn.x, playBtn.y);
  await wait(1800);
  result.afterPlay = await evaluate(state);
  await shot(`${out}-1-playing`);

  // 关键：重新量坐标再点 ⏸
  const mid = await evaluate(boxesOf);
  result.beforePause = mid;
  const pauseBtn = mid.buttons[1];
  await clickAt(mouse, pauseBtn.x, pauseBtn.y);
  await wait(1500);
  result.afterPause = await evaluate(state);
  await shot(`${out}-2-paused`);

  result.worked = (result.afterPlay.log || []).some((e) => e.op === "play")
    && (result.afterPause.log || []).some((e) => e.op === "pause");

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
