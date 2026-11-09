// 走查多词修正：Shift+点选 3 个词 → Enter → 输入框预填合并文本 → 提交后合并成一个词。
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const pick = (n, shift) => `(() => {
  const tokens = [...document.querySelectorAll(".sentence-list .token")];
  const token = tokens[${n}];
  if (!token) return null;
  token.scrollIntoView({ block: "center" });
  const r = token.getBoundingClientRect();
  const opts = {
    bubbles: true, cancelable: true, view: window, shiftKey: ${shift ? "true" : "false"},
    clientX: Math.round(r.left + r.width / 2),
    clientY: Math.round(r.top + r.height / 2),
  };
  token.dispatchEvent(new MouseEvent("mousedown", opts));
  token.dispatchEvent(new MouseEvent("mouseup", opts));
  return token.textContent;
})()`;

const snapshot = `(() => ({
  selected: [...document.querySelectorAll(".sentence-list .token.selected")].map((n) => n.textContent),
  bubbleTitle: (document.querySelector(".word-bubble .bubble-fix") || {}).title || null,
  input: (document.querySelector(".bubble-form input") || {}).value ?? null,
}))()`;

const pressEnter = `(() => {
  const event = new KeyboardEvent("keydown", {
    key: "Enter", code: "Enter", bubbles: true, cancelable: true,
  });
  document.body.dispatchEvent(event);
  return event.defaultPrevented;
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(1800);

  const first = await evaluate(pick(0, false));
  await evaluate(pick(1, true));
  const last = await evaluate(pick(2, true));
  await wait(300);
  const dragged = await evaluate(snapshot);

  await evaluate(pressEnter);
  await wait(500);
  const opened = await evaluate(snapshot);
  await shot(`${out}-form`);

  // 提交：把输入框改成合并后的文本再按 Enter
  const submitted = await evaluate(`(() => {
    const input = document.querySelector(".bubble-form input");
    if (!input) return null;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(input, "送別");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    const form = document.querySelector(".bubble-form");
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    return "送別";
  })()`);
  await wait(2500);
  const after = await evaluate(`(() => ({
    tokens: [...document.querySelectorAll(".sentence-list .token.revised")].map((n) => n.textContent),
  }))()`);
  await shot(`${out}-merged`);

  const result = { first, last, dragged, opened, submitted, after };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
