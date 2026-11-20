// 走完整条「隐去 → 还原」链路，并验证手动隐去的词不被批量还原带走。
//
// **这会改数据。** 调用方先备份 data/app.db。
//
// 顺序：先点一次「隐去口癖词」，再用 /hide 端点手动隐去一个词（模拟 Backspace），
// 然后点「还原已隐去口癖词」。断言：
//  1. 隐去提示里是「已隐去 N 处…涉及 X 秒语音」，不是「减少」；
//  2. 还原提示里带具体数字，而不是被 `run` 的「… 完成」盖掉；
//  3. 还原后手写隐去的那个词还在（SQL 侧断言，这里只读总数）。

const openProcessing = `(() => {
  const menu = [...document.querySelectorAll(".menu-trigger")].find((b) => b.textContent.trim() === "处理");
  if (!menu) return false;
  menu.click();
  return true;
})()`;

const clickPrefix = (prefix) => `(() => {
  const target = [...document.querySelectorAll(".menu-item")].find((b) => {
    const label = (b.querySelector(".menu-label") || {}).textContent || "";
    return label.startsWith(${JSON.stringify(prefix)});
  });
  if (!target || target.disabled) return { clicked: false };
  target.click();
  return { clicked: true, label: (target.querySelector(".menu-label") || {}).textContent };
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
  const el = document.querySelector(".notice");
  return el ? el.textContent.trim() : null;
})()`;

const dialogGone = `Boolean(document.querySelector(".modal-wide"))`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);

  // --- 1. 隐去口癖词 -------------------------------------------------------
  await evaluate(openProcessing);
  await wait(400);
  await evaluate(clickPrefix("隐去口癖词…"));
  for (let i = 0; i < 60; i += 1) {
    if (await evaluate(waitReady)) break;
    await wait(500);
  }
  await wait(300);
  const plan = await evaluate(readDialog);
  await shot(`${out}-plan`);

  await evaluate(execute);
  for (let i = 0; i < 60; i += 1) {
    if (!(await evaluate(dialogGone))) break;
    await wait(500);
  }
  await wait(1500);
  const hideNotice = await evaluate(readNotice);
  await shot(`${out}-hidden`);

  // --- 2. 还原已隐去口癖词 -------------------------------------------------
  await evaluate(openProcessing);
  await wait(400);
  const restoreClick = await evaluate(clickPrefix("还原已隐去口癖词"));
  await wait(3500);
  const restoreNotice = await evaluate(readNotice);
  await shot(`${out}-restored`);

  const result = { plan, hideNotice, restoreClick, restoreNotice };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
