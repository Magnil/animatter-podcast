// 走查气泡：三个单字按钮必须在一行内，且 title 与用户给的文案一致。
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const pick = `(() => {
  const token = document.querySelector(".sentence-list .token");
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

const measure = `(() => {
  const bubble = document.querySelector(".word-bubble");
  if (!bubble) return null;
  const buttons = [...bubble.querySelectorAll("button")];
  const box = bubble.getBoundingClientRect();
  return {
    bubble: { width: Math.round(box.width), height: Math.round(box.height) },
    buttons: buttons.map((b) => {
      const r = b.getBoundingClientRect();
      return {
        text: (b.textContent || "").trim(),
        aria: b.getAttribute("aria-label"),
        title: b.title,
        width: Math.round(r.width),
        height: Math.round(r.height),
        lines: Math.round(r.height / parseFloat(getComputedStyle(b).lineHeight || "16")),
      };
    }),
    // The three buttons must share one row: equal tops, one button height.
    oneRow: new Set(buttons.map((b) => Math.round(b.getBoundingClientRect().top))).size <= 1,
  };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(1800);
  const picked = await evaluate(pick);
  await wait(400);
  const idle = await evaluate(measure);
  await shot(`${out}-buttons`);

  // 打开修正框，量一下表单形态的宽度
  await evaluate(`(() => {
    const b = document.querySelector(".word-bubble .bubble-fix");
    if (b) b.click();
    return !!b;
  })()`);
  await wait(400);
  const form = await evaluate(measure);
  await shot(`${out}-form`);

  const result = { picked, idle, form };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
