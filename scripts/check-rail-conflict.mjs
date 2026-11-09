// 走查：确认预留行不会把提示文字吃掉——选一个会跨说话人的词，
// 提示必须还在（只是不再改变高度）。
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

const state = `(() => {
  const rail = document.querySelector(".sentence-rail");
  const conflict = document.querySelector(".rail-conflict");
  return {
    rail: rail ? Math.round(rail.getBoundingClientRect().height) : null,
    conflictText: conflict ? conflict.textContent.trim() : null,
    bubble: !!document.querySelector(".word-bubble"),
    bubbleButtons: [...document.querySelectorAll(".word-bubble button")]
      .map((b) => b.textContent.trim()),
  };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(1800);

  const idle = await evaluate(state);
  const picked = await evaluate(probe(0));
  await wait(500);
  const selected = await evaluate(state);
  await shot(out);

  const result = {
    idle,
    picked,
    selected,
    heightUnchanged: idle.rail === selected.rail,
    conflictStillRendered: (selected.conflictText || "").length > 0,
  };

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
