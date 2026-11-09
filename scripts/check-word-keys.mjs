// 走查快捷键：Del / Enter / Backspace 必须作用于当前词选，且不能打字时抢键。
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const probe = (n) => `(() => {
  const tokens = [...document.querySelectorAll(".sentence-list .token")];
  const token = tokens[${n}];
  if (!token) return null;
  token.scrollIntoView({ block: "center" });
  const r = token.getBoundingClientRect();
  const opts = {
    bubbles: true, cancelable: true, view: window,
    clientX: Math.round(r.left + r.width / 2),
    clientY: Math.round(r.top + r.height / 2),
  };
  token.dispatchEvent(new MouseEvent("mousedown", opts));
  token.dispatchEvent(new MouseEvent("mouseup", opts));
  return token.textContent;
})()`;

const key = (k, code) => `(() => {
  const event = new KeyboardEvent("keydown", {
    key: ${JSON.stringify(k)},
    code: ${JSON.stringify(code)},
    bubbles: true,
    cancelable: true,
  });
  document.body.dispatchEvent(event);
  return event.defaultPrevented;
})()`;

const state = `(() => ({
  bubble: !!document.querySelector(".word-bubble"),
  input: !!document.querySelector(".bubble-form input"),
  hidden: [...document.querySelectorAll(".sentence-list .token.hidden-word")].map((n) => n.textContent),
  revised: [...document.querySelectorAll(".sentence-list .token.revised")].map((n) => n.textContent),
  cut: [...document.querySelectorAll(".sentence-list .token.cut")].map((n) => n.textContent),
  selection: [...document.querySelectorAll(".sentence-list .token.selected")].map((n) => n.textContent),
}))()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(1800);
  const results = {};

  // Enter → 修正输入框
  await evaluate(probe(2));
  await wait(300);
  results.enterPrevented = await evaluate(key("Enter", "Enter"));
  await wait(400);
  results.afterEnter = await evaluate(state);
  await shot(`${out}-enter`);

  // Esc 关掉输入框
  await evaluate(key("Escape", "Escape"));
  await wait(300);

  // Backspace → 隐去
  await evaluate(probe(6));
  await wait(300);
  results.backspacePrevented = await evaluate(key("Backspace", "Backspace"));
  await wait(2500);
  results.afterBackspace = await evaluate(state);
  await shot(`${out}-backspace`);

  // Del → 删除（会真的剪音频，放最后）
  await evaluate(probe(10));
  await wait(300);
  results.deletePrevented = await evaluate(key("Delete", "Delete"));
  await wait(2500);
  results.afterDelete = await evaluate(state);
  await shot(`${out}-delete`);

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(results, null, 2), "utf8");
  return results;
};
