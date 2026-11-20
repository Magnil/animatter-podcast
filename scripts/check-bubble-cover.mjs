// 探针：时间轴点击之后，字幕区那一片坐标上到底是什么元素？
//
// 怀疑：点时间轴选中词之后，浮出的 `.word-bubble` 盖住了整片句子，导致后续
// 点方框、点词都落在气泡上——走查里表现为「点了没反应」。

const probe = `(() => {
  const rows = [...document.querySelectorAll(".sentence")];
  const picks = rows.map((row) => {
    const r = row.querySelector(".sentence-pick")?.getBoundingClientRect();
    return r ? { ord: Number(row.dataset.ord), x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) } : null;
  }).filter(Boolean);
  const target = picks[Math.floor(picks.length / 2)] ?? null;
  const hits = target ? (() => {
    const el = document.elementFromPoint(target.x, target.y);
    return el ? { tag: el.tagName, cls: el.className, text: (el.textContent || "").slice(0, 24) } : null;
  })() : null;
  const bubble = document.querySelector(".word-bubble");
  const b = bubble ? bubble.getBoundingClientRect() : null;
  return {
    target,
    hits,
    bubble: b ? { left: Math.round(b.left), top: Math.round(b.top), right: Math.round(b.right), bottom: Math.round(b.bottom), text: (bubble.textContent || "").slice(0, 40) } : null,
    pickedCount: rows.filter((row) => row.classList.contains("picked")).length,
    selected: [...document.querySelectorAll(".token.selected")].map((el) => Number(el.dataset.token)),
  };
})()`;

export default async ({ evaluate, mouse, shot, wait, out }) => {
  await wait(2600);
  const before = await evaluate(probe);

  const canvas = await evaluate(`(() => {
    const r = document.querySelector("canvas").getBoundingClientRect();
    return { left: Math.round(r.left), top: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
  })()`);
  await mouse("mousePressed", canvas.left + Math.round(canvas.width * 0.62), canvas.top + Math.round(canvas.height * 0.55));
  await mouse("mouseReleased", canvas.left + Math.round(canvas.width * 0.62), canvas.top + Math.round(canvas.height * 0.55));
  await wait(900);
  const after = await evaluate(probe);

  await shot(`${out}`);
  const result = { before, after };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
