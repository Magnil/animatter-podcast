// 走查字幕区气泡的「删 / 还」与「隐 / 显」四个状态和它们的互斥。
//
// **会改数据。** 调用方先备份 data/app.db，结束后还原。
//
// 步骤：
//   1. 选中一个未处理的词 → 读三个按钮（应为 删 / 修 / 隐）
//   2. 点「删」→ 再选中同一个词 → 应为 还 / 修 / 隐且「隐」灰掉
//   3. 点「还」→ 再选中 → 回到 删 / 修 / 隐
//   4. 点「隐」→ 再选中 → 应为 删(灰) / 修 / 显
//   5. 点「显」→ 再选中 → 回到初始
//
// 每个状态都截一张图。

const clickWord = (idx) => `(() => {
  const el = document.querySelector('[data-token="${idx}"]');
  if (!el) return { found: false };
  el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  return { found: true, text: el.textContent };
})()`;

const readBubble = `(() => {
  const bubble = document.querySelector(".word-bubble");
  if (!bubble) return { open: false };
  return {
    open: true,
    buttons: [...bubble.querySelectorAll("button")].map((b) => ({
      label: b.textContent.trim(),
      aria: b.getAttribute("aria-label"),
      title: b.getAttribute("title"),
      disabled: b.disabled,
      cls: b.className,
      background: getComputedStyle(b).backgroundColor,
    })),
  };
})()`;

const clickBubble = (label) => `(() => {
  const bubble = document.querySelector(".word-bubble");
  if (!bubble) return { clicked: false, reason: "no bubble" };
  const btn = [...bubble.querySelectorAll("button")].find((b) => b.textContent.trim() === ${JSON.stringify(label)});
  if (!btn) return { clicked: false, reason: "no button " + ${JSON.stringify(label)} };
  if (btn.disabled) return { clicked: false, reason: "disabled" };
  btn.click();
  return { clicked: true };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2600);

  const steps = {};

  const first = await evaluate(clickWord(3));
  await wait(500);
  steps.initial = { word: first, bubble: await evaluate(readBubble) };
  await shot(`${out}-1-initial`);

  const deleted = await evaluate(clickBubble("删"));
  await wait(2200);
  await evaluate(clickWord(3));
  await wait(500);
  steps.afterDelete = { clicked: deleted, bubble: await evaluate(readBubble) };
  await shot(`${out}-2-deleted`);

  const restored = await evaluate(clickBubble("还"));
  await wait(2200);
  await evaluate(clickWord(3));
  await wait(500);
  steps.afterRestore = { clicked: restored, bubble: await evaluate(readBubble) };
  await shot(`${out}-3-restored`);

  const hid = await evaluate(clickBubble("隐"));
  await wait(2200);
  await evaluate(clickWord(3));
  await wait(500);
  steps.afterHide = { clicked: hid, bubble: await evaluate(readBubble) };
  await shot(`${out}-4-hidden`);

  const shown = await evaluate(clickBubble("显"));
  await wait(2200);
  await evaluate(clickWord(3));
  await wait(500);
  steps.afterShow = { clicked: shown, bubble: await evaluate(readBubble) };
  await shot(`${out}-5-shown`);

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(steps, null, 2), "utf8");
  return steps;
};
