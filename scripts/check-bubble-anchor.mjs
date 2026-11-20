// 探针：气泡是不是贴在它作用的那个词下方？
//
// 截图里出现过气泡飘在词下方一百多像素、甚至盖住「操作记录」的样子，
// 而「选中 → 点」的全部意义就是这个气泡贴着选区。这里直接量两者矩形。

const measure = `(() => {
  const token = document.querySelector(".token.selected");
  const bubble = document.querySelector(".word-bubble");
  const t = token ? token.getBoundingClientRect() : null;
  const b = bubble ? bubble.getBoundingClientRect() : null;
  return {
    token: t ? { left: Math.round(t.left), top: Math.round(t.top), right: Math.round(t.right), bottom: Math.round(t.bottom), text: token.textContent } : null,
    bubble: b ? { left: Math.round(b.left), top: Math.round(b.top), right: Math.round(b.right), bottom: Math.round(b.bottom) } : null,
    gap: t && b ? Math.round(b.top - t.bottom) : null,
    dx: t && b ? Math.round((b.left + b.right) / 2 - (t.left + t.right) / 2) : null,
    listScrollTop: Math.round(document.querySelector(".sentence-list")?.scrollTop ?? -1),
    viewportHeight: innerHeight,
  };
})()`;

export default async ({ evaluate, mouse, shot, wait, out }) => {
  await wait(2600);
  const steps = [];

  // 点三个不同位置的词，每次量气泡相对词的位置。
  for (const offset of [0.2, 0.55, 0.85]) {
    const box = await evaluate(`(() => {
      const list = document.querySelector(".sentence-list");
      const view = list.getBoundingClientRect();
      const els = [...document.querySelectorAll("[data-token]")].filter((el) => {
        const r = el.getBoundingClientRect();
        return r.top > view.top + 8 && r.bottom < view.bottom - 8 && r.width > 8;
      });
      if (!els.length) return null;
      const el = els[Math.floor(els.length * ${offset})];
      const r = el.getBoundingClientRect();
      return { idx: Number(el.dataset.token), text: el.textContent, x: Math.round(r.left + Math.min(20, r.width / 2)), y: Math.round(r.top + r.height / 2) };
    })()`);
    if (!box) continue;
    const hit = await evaluate(`(() => {
      const el = document.elementFromPoint(${box.x}, ${box.y});
      return el ? String(el.className || el.tagName) : null;
    })()`);
    await mouse("mousePressed", box.x, box.y);
    await mouse("mouseReleased", box.x, box.y);
    await wait(700);
    const state = await evaluate(measure);
    steps.push({ ...box, hit, ...state });
  }

  await shot(`${out}`);
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify({ steps }, null, 2), "utf8");
  return { steps };
};
