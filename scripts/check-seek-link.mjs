// 走查：字幕区与时间轴的联动（第 83 轮）。
//
// 1. 勾句子方框 → 时间轴跳到该句开头，不缩放
// 2. 点句子里的词 → 时间轴跳到该词开头，不缩放
// 3. 点时间轴**不**勾选方框（用户第 83 轮明确否决了原先的第 3 条）
// 4. 点时间轴 → 字幕区对应词高亮
//
// 只点不写：全程不改任何编辑数据。
//
// 两条教训都写进脚本里：
//   * 每次点击前**重新量坐标**。列表的 reveal 效果会在播放头移动后自己滚，
//     两秒前的快照点下去会落在别的行上。
//   * 用 `elementFromPoint` 记录每一步真正命中的元素。坐标对了但命中的是别的
//     东西时，日志里能立刻看出来，而不会误判成「功能没实现」。

const snapshot = `(() => {
  const readout = document.querySelector(".waveform-zoom .muted");
  const clock = document.querySelector(".clock");
  const list = document.querySelector(".sentence-list");
  const view = list ? list.getBoundingClientRect() : { top: 0, bottom: innerHeight, left: 0, right: innerWidth };
  const rows = [...document.querySelectorAll(".sentence")];
  // 只认**字幕区自己的滚动窗口**里的元素。用 window.innerHeight 判断是不够的：
  // 下面还压着「操作记录」面板，那里面的坐标点下去会打到别人的标题上——
  // 走查结果里出现过「H3 操作记录」，就是这么来的。
  const inside = (r) => r.top > view.top + 6 && r.bottom < view.bottom - 6 && r.width > 8 && r.height > 8;
  const tokens = [];
  for (const row of rows) {
    const ord = Number(row.dataset.ord);
    for (const el of row.querySelectorAll("[data-token]")) {
      const r = el.getBoundingClientRect();
      if (!inside(r)) continue;
      tokens.push({
        idx: Number(el.dataset.token),
        ord,
        text: el.textContent,
        x: Math.round(r.left + Math.min(20, r.width / 2)),
        y: Math.round(r.top + r.height / 2),
      });
    }
  }
  const boxes = rows
    .map((row) => {
      const r = row.querySelector(".sentence-pick")?.getBoundingClientRect();
      if (!r || !inside(r)) return null;
      return { ord: Number(row.dataset.ord), x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
    })
    .filter(Boolean);
  return {
    span: readout ? readout.textContent.trim() : null,
    clock: clock ? clock.textContent.trim() : null,
    picked: rows.filter((row) => row.classList.contains("picked")).map((row) => Number(row.dataset.ord)),
    active: rows.filter((row) => row.classList.contains("active")).map((row) => Number(row.dataset.ord)),
    selected: [...document.querySelectorAll(".token.selected")].map((el) => Number(el.dataset.token)),
    tokens,
    boxes,
  };
})()`;

const hitAt = (x, y) => `(() => {
  const el = document.elementFromPoint(${x}, ${y});
  return el ? { tag: el.tagName, cls: String(el.className || ""), text: (el.textContent || "").slice(0, 16) } : null;
})()`;

/** Clock as milliseconds, so "did it move" is a number test, not a string test. */
const toMs = (clock) => {
  const match = /(\d+):(\d+)\.(\d+)/.exec(clock ?? "");
  if (!match) return null;
  return Number(match[1]) * 60000 + Number(match[2]) * 1000 + Number(match[3]);
};

export default async ({ evaluate, mouse, shot, wait, out }) => {
  await wait(2600);

  const canvas = await evaluate(`(() => {
    const r = document.querySelector("canvas").getBoundingClientRect();
    return { left: Math.round(r.left), top: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
  })()`);

  const start = await evaluate(snapshot);

  // ---- 4 + 3. 点时间轴 ----
  const seekX = canvas.left + Math.round(canvas.width * 0.62);
  const seekY = canvas.top + Math.round(canvas.height * 0.55);
  await mouse("mousePressed", seekX, seekY);
  await mouse("mouseReleased", seekX, seekY);
  await wait(900);
  const afterSeek = await evaluate(snapshot);

  // ---- 1. 勾两个不同句子的方框 ----
  const ticks = [];
  for (const offset of [0.35, 0.7]) {
    const now = await evaluate(snapshot);
    if (now.boxes.length < 3) break;
    const box = now.boxes[Math.floor(now.boxes.length * offset)];
    // 坐标是刚量的，但点之前再确认一次真正命中的是方框：命中别的元素就跳过，
    // 免得把「坐标偏了」记成「功能坏了」。
    const hit = await evaluate(hitAt(box.x, box.y));
    if (!hit || hit.cls !== "sentence-pick") continue;
    const before = now.clock;
    await mouse("mousePressed", box.x, box.y);
    await mouse("mouseReleased", box.x, box.y);
    await wait(900);
    const after = await evaluate(snapshot);
    ticks.push({
      ord: box.ord,
      hit,
      before,
      after: after.clock,
      moved: toMs(after.clock) !== toMs(before),
      picked: after.picked,
      span: after.span,
    });
  }

  // ---- 2. 点两个不同的词 ----
  const words = [];
  for (const offset of [0.4, 0.75]) {
    const now = await evaluate(snapshot);
    const candidates = now.tokens.filter((t) => !now.selected.includes(t.idx));
    if (candidates.length < 3) break;
    const word = candidates[Math.floor(candidates.length * offset)];
    const hit = await evaluate(hitAt(word.x, word.y));
    if (!hit || hit.cls !== "token") continue;
    const before = now.clock;
    await mouse("mousePressed", word.x, word.y);
    await mouse("mouseReleased", word.x, word.y);
    await wait(900);
    const after = await evaluate(snapshot);
    words.push({
      idx: word.idx,
      text: word.text,
      ord: word.ord,
      hit,
      before,
      after: after.clock,
      moved: toMs(after.clock) !== toMs(before),
      selected: after.selected,
      picked: after.picked,
      span: after.span,
    });
  }

  await shot(`${out}`);

  const result = {
    canvas,
    start: { span: start.span, clock: start.clock },
    seek: {
      before: start.clock,
      after: afterSeek.clock,
      moved: toMs(afterSeek.clock) !== toMs(start.clock),
      selected: afterSeek.selected,
      picked: afterSeek.picked,
      span: afterSeek.span,
    },
    ticks,
    words,
    checks: {
      seekHighlightedWord: afterSeek.selected.length === 1,
      seekDidNotTickBox: afterSeek.picked.length === 0,
      seekDidNotZoom: afterSeek.span === start.span,
      tickJumped: ticks.length > 0 && ticks.every((row) => row.moved),
      tickTickedBox: ticks.every((row) => row.picked.includes(row.ord)),
      tickDidNotZoom: ticks.every((row) => row.span === start.span),
      wordJumped: words.length > 0 && words.every((row) => row.moved),
      wordSelected: words.every((row) => row.selected.length === 1 && row.selected[0] === row.idx),
      wordDidNotZoom: words.every((row) => row.span === start.span),
    },
  };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
