// 走查「隐去」：选中一个词 → 气泡三个按钮 → 点隐去 → 词变成虚下划线、音频没动。
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

const bubble = `(() => {
  const b = document.querySelector(".word-bubble");
  if (!b) return null;
  return [...b.querySelectorAll("button")].map((x) => ({
    label: (x.textContent || "").trim(),
    disabled: x.disabled,
    title: x.title,
  }));
})()`;

const hiddenState = `(() => {
  const nodes = [...document.querySelectorAll(".sentence-list .token.hidden-word")];
  return {
    count: nodes.length,
    texts: nodes.map((n) => n.textContent),
    tag: (document.querySelector(".hidden-tag") || {}).textContent || null,
  };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(1800);

  const picked = await evaluate(probe(0));
  await wait(400);
  const buttons = await evaluate(bubble);
  await shot(`${out}-bubble`);

  // 点「隐去」
  const clicked = await evaluate(`(() => {
    const b = document.querySelector(".word-bubble");
    if (!b) return null;
    const target = [...b.querySelectorAll("button")]
      .find((x) => (x.textContent || "").includes("隐去"));
    if (!target) return null;
    target.click();
    return (target.textContent || "").trim();
  })()`);
  await wait(2500);
  const after = await evaluate(hiddenState);
  await shot(`${out}-hidden`);

  const result = { picked, buttons, clicked, after };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
