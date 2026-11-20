// 点一次「还原已隐去口癖词」，验证它只还原批量隐去的。
//
// **这会改数据。** 调用方先备份 data/app.db。
//
// 断言点：
//  1. 提示里出现「已还原 N 处已隐去口癖词」；
//  2. 还原后「还原已隐去口癖词」变灰、菜单里没有（N）；
//  3. 手写隐去的词（hide_source=''）不受影响——这一条由 SQL 侧断言，UI 只看总数。

const openProcessing = `(() => {
  const menu = [...document.querySelectorAll(".menu-trigger")].find((b) => b.textContent.trim() === "处理");
  if (!menu) return false;
  menu.click();
  return true;
})()`;

const clickByLabel = (label) => `(() => {
  const target = [...document.querySelectorAll(".menu-item")].find(
    (b) => (b.querySelector(".menu-label") || {}).textContent === ${JSON.stringify(label)},
  );
  if (!target || target.disabled) return false;
  target.click();
  return true;
})()`;

const readNotice = `(() => {
  const el = document.querySelector(".notice");
  return el ? el.textContent.trim() : null;
})()`;

const readMenu = `(() => [...document.querySelectorAll(".menu-item")].map((b) => ({
  label: (b.querySelector(".menu-label") || {}).textContent || "",
  disabled: b.disabled,
})))()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);
  await evaluate(openProcessing);
  await wait(400);
  const before = await evaluate(readMenu);
  await shot(`${out}-before`);

  const clicked = await evaluate(clickByLabel("还原已隐去口癖词（183）"));
  await wait(3000);
  const notice = await evaluate(readNotice);
  await shot(`${out}-after`);

  await evaluate(openProcessing);
  await wait(400);
  const after = await evaluate(readMenu);
  await shot(`${out}-menu`);

  const result = { clicked, notice, before, after };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
