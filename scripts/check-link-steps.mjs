// 探针：先点时间轴，再点同一屏上的方框和词，逐步记录每一步命中的元素。
//
// 目的：走查里「点方框没反应」到底是没命中、命中了但没生效、还是生效后被别的东西清掉。

const pickRead = `(() => {
  const rows = [...document.querySelectorAll(".sentence")];
  const picks = rows.map((row) => {
    const r = row.querySelector(".sentence-pick")?.getBoundingClientRect();
    return r ? {
      ord: Number(row.dataset.ord),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      top: Math.round(r.top),
      bottom: Math.round(r.bottom),
    } : null;
  }).filter(Boolean);
  const tokens = [];
  for (const row of rows) {
    for (const el of row.querySelectorAll("[data-token]")) {
      const r = el.getBoundingClientRect();
      if (r.top < 100 || r.bottom > innerHeight - 8 || r.width < 8) continue;
      tokens.push({ idx: Number(el.dataset.token), ord: Number(row.dataset.ord), text: el.textContent, x: Math.round(r.left + 6), y: Math.round(r.top + r.height / 2) });
    }
  }
  const clock = document.querySelector(".clock");
  return {
    clock: clock ? clock.textContent.trim() : null,
    picks,
    tokens,
    picked: rows.filter((row) => row.classList.contains("picked")).map((row) => Number(row.dataset.ord)),
    selected: [...document.querySelectorAll(".token.selected")].map((el) => Number(el.dataset.token)),
    scrollTop: Math.round(document.querySelector(".sentence-list, .sentences, [role=list]")?.scrollTop ?? -1),
  };
})()`;

const hitAt = (x, y) => `(() => {
  const el = document.elementFromPoint(${x}, ${y});
  return el ? { tag: el.tagName, cls: String(el.className || ""), text: (el.textContent || "").slice(0, 20) } : null;
})()`;

export default async ({ evaluate, mouse, shot, wait, out }) => {
  await wait(2600);
  const steps = [];

  const canvas = await evaluate(`(() => {
    const r = document.querySelector("canvas").getBoundingClientRect();
    return { left: Math.round(r.left), top: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
  })()`);

  // 1. 点时间轴
  const cx = canvas.left + Math.round(canvas.width * 0.62);
  const cy = canvas.top + Math.round(canvas.height * 0.55);
  const before1 = await evaluate(pickRead);
  await mouse("mousePressed", cx, cy);
  await mouse("mouseReleased", cx, cy);
  await wait(1000);
  const after1 = await evaluate(pickRead);
  steps.push({ step: "canvas-click", at: { x: cx, y: cy }, before: before1.clock, after: after1.clock, picked: after1.picked, selected: after1.selected, scrollTop: after1.scrollTop });

  // 2. 点当前屏中间那一行的方框
  const mid = after1.picks[Math.floor(after1.picks.length / 2)];
  if (mid) {
    const hit = await evaluate(hitAt(mid.x, mid.y));
    await mouse("mousePressed", mid.x, mid.y);
    await mouse("mouseReleased", mid.x, mid.y);
    await wait(1000);
    const after = await evaluate(pickRead);
    steps.push({ step: "tick-box", ord: mid.ord, at: { x: mid.x, y: mid.y }, hit, before: after1.clock, after: after.clock, picked: after.picked, selected: after.selected, scrollTop: after.scrollTop });
  }

  // 3. 点当前屏中间那个词
  const after2 = await evaluate(pickRead);
  const word = after2.tokens[Math.floor(after2.tokens.length / 2)];
  if (word) {
    const hit = await evaluate(hitAt(word.x, word.y));
    await mouse("mousePressed", word.x, word.y);
    await mouse("mouseReleased", word.x, word.y);
    await wait(1000);
    const after = await evaluate(pickRead);
    steps.push({ step: "word-click", idx: word.idx, ord: word.ord, text: word.text, at: { x: word.x, y: word.y }, hit, before: after2.clock, after: after.clock, picked: after.picked, selected: after.selected, scrollTop: after.scrollTop });
  }

  await shot(`${out}`);
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify({ canvas, steps }, null, 2), "utf8");
  return { steps };
};
