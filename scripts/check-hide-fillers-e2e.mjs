// 真点一次「隐去口癖词」，验证写入路径、提示文案与菜单计数。
//
// **这会改数据。** 调用方必须先备份 data/app.db 并在结束后还原。
//
// 断言点：
//  1. 弹窗底部写「本次会隐去 N 处 · 涉及 X 秒语音」，不是「减少」；
//  2. 执行后提示里出现同一个 N；
//  3. 「处理」菜单里「还原已隐去口癖词（N）」变成可用，N 与上面一致；
//  4. 「还原已删除口癖词」仍然是禁用的——隐去没有写 deletion 行；
//  5. 波形上出现灰色斜线区。

const openProcessing = `(() => {
  const menu = [...document.querySelectorAll(".menu-trigger")].find((b) => b.textContent.trim() === "处理");
  if (!menu) return false;
  menu.click();
  return true;
})()`;

const clickItem = (label) => `(() => {
  const target = [...document.querySelectorAll(".menu-item")].find(
    (b) => (b.querySelector(".menu-label") || {}).textContent === ${JSON.stringify(label)},
  );
  if (!target) return false;
  target.click();
  return true;
})()`;

const waitReady = `(() => {
  const s = document.querySelector(".cleanup-list-head strong");
  return s ? !s.textContent.includes("正在计算") : false;
})()`;

const readDialog = `(() => {
  const m = document.querySelector(".modal-wide");
  if (!m) return { open: false };
  return {
    open: true,
    head: (m.querySelector(".modal-head h2") || {}).textContent || "",
    summary: (m.querySelector(".cleanup-list-head strong") || {}).textContent || "",
    submit: (m.querySelector(".modal-foot button:last-child") || {}).textContent || "",
    submitDisabled: (m.querySelector(".modal-foot button:last-child") || {}).disabled,
  };
})()`;

const execute = `(() => {
  const m = document.querySelector(".modal-wide");
  if (!m) return false;
  const btn = [...m.querySelectorAll(".modal-foot button")].pop();
  if (!btn || btn.disabled) return false;
  btn.click();
  return true;
})()`;

const readNotice = `(() => {
  const el = document.querySelector(".notice, .toast, [role=status]");
  return el ? el.textContent.trim() : null;
})()`;

const readMenu = `(() => {
  const menu = [...document.querySelectorAll(".menu-trigger")].find((b) => b.textContent.trim() === "处理");
  if (!menu) return { items: [] };
  menu.click();
  return {
    items: [...document.querySelectorAll(".menu-item")].map((b) => ({
      label: (b.querySelector(".menu-label") || {}).textContent || "",
      disabled: b.disabled,
    })),
  };
})()`;

const hatchProbe = `(() => {
  const canvas = document.querySelector("canvas");
  if (!canvas) return { found: false };
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const data = ctx.getImageData(0, 0, w, h).data;
  // 灰色斜线的填充是 rgba(148,163,184,0.22)。canvas 先 clearRect，所以
  // getImageData 拿到的是**预合成**值：alpha=26 的灰蓝。
  let hits = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] > 0 && data[i + 3] < 90) {
      const r = data[i], g = data[i + 1], b = data[i + 2];
      if (Math.abs(r - 147) <= 6 && Math.abs(g - 167) <= 6 && Math.abs(b - 186) <= 6) hits += 1;
    }
  }
  return { found: true, hits, width: w, height: h };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);
  await evaluate(openProcessing);
  await wait(250);
  await evaluate(clickItem("隐去口癖词…"));
  for (let i = 0; i < 60; i += 1) {
    if (await evaluate(waitReady)) break;
    await wait(500);
  }
  await wait(300);
  const before = await evaluate(readDialog);
  await shot(`${out}-before`);

  const clicked = await evaluate(execute);
  // 等弹窗真的关掉。上一版写死 2.5s，读到的还是「隐去中…」，于是 notice 是空的
  // ——而那正是要验证的东西之一。183 个词的写入 + 重载要几秒。
  for (let i = 0; i < 60; i += 1) {
    if (!(await evaluate('Boolean(document.querySelector(".modal-wide"))'))) break;
    await wait(500);
  }
  await wait(1200);
  const notice = await evaluate(readNotice);
  const hatch = await evaluate(hatchProbe);
  await shot(`${out}-after`);

  const menu = await evaluate(readMenu);
  await shot(`${out}-menu`);

  const result = { before, clicked, notice, hatch, menu };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
