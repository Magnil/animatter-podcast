// 探针：全览下点时间轴到底有没有 seek？
//
// 怀疑点：`handleAt` 的命中半径是 6px，而 1074 句 → 2148 个边界手柄分摊在
// 978px 上，平均每 0.45px 一个。也就是说全览下**每一次点下去都命中手柄**，
// 走的是拖拽分支，松手不改 previewMs 就什么都不发生。
//
// 这个脚本横向扫几个位置，各点一次，记录 clock 有没有动；再单独点顶部字幕条，
// 那条路会走 onCaptionClick（播放该句），如果它动了就说明「不动的只有手柄路径」。

const readClock = `(() => {
  const clock = document.querySelector(".clock");
  return {
    clock: clock ? clock.textContent.trim() : null,
    active: [...document.querySelectorAll(".sentence.active")].map((el) => Number(el.dataset.ord)),
    selected: [...document.querySelectorAll(".token.selected")].map((el) => Number(el.dataset.token)),
  };
})()`;

export default async ({ evaluate, mouse, shot, wait, out }) => {
  await wait(2600);
  const canvas = await evaluate(`(() => {
    const c = document.querySelector("canvas");
    const r = c.getBoundingClientRect();
    return { left: Math.round(r.left), top: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
  })()`);

  const rows = [];
  for (const ratio of [0.1, 0.3, 0.5, 0.62, 0.75, 0.9]) {
    const before = await evaluate(readClock);
    const x = canvas.left + Math.round(canvas.width * ratio);
    const y = canvas.top + Math.round(canvas.height * 0.55);
    await mouse("mousePressed", x, y);
    await mouse("mouseReleased", x, y);
    await wait(450);
    const after = await evaluate(readClock);
    rows.push({ ratio, x, y, before: before.clock, after: after.clock, moved: before.clock !== after.clock, selected: after.selected.length, active: after.active });
  }

  // 顶部字幕条：22px 以内，走 onCaptionClick。
  const captionX = canvas.left + Math.round(canvas.width * 0.62);
  const captionY = canvas.top + 10;
  const beforeCaption = await evaluate(readClock);
  await mouse("mousePressed", captionX, captionY);
  await mouse("mouseReleased", captionX, captionY);
  await wait(600);
  const afterCaption = await evaluate(readClock);

  await shot(`${out}`);

  const result = {
    canvas,
    rows,
    caption: { x: captionX, y: captionY, before: beforeCaption.clock, after: afterCaption.clock, moved: beforeCaption.clock !== afterCaption.clock },
    anyBodyClickMoved: rows.some((row) => row.moved),
  };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
