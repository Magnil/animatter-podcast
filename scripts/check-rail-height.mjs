// 走查：rail 高度在**不同句子**之间是否也会变。
// 词长已经量过（稳定在 57），但这个 rail 里还有一行 `.rail-conflict`
// （flex-basis:100%），只在「搬词会改说话人归属」时出现——如果它会随句子开关，
// 那面板照样在跳，只是换了个触发条件。
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

const snapshot = `(() => {
  const rail = document.querySelector(".sentence-rail");
  const conflict = document.querySelector(".rail-conflict");
  const selected = [...document.querySelectorAll(".sentence-list .token.selected")];
  const row = selected[0] ? selected[0].closest(".sentence") : null;
  const speaker = row ? (row.querySelector(".speaker-name") || {}).textContent : null;
  return {
    rail: rail ? Math.round(rail.getBoundingClientRect().height) : null,
    conflict: conflict ? conflict.textContent.trim() : null,
    speaker: speaker ? speaker.replace("…", "").trim() : null,
    text: selected.map((t) => t.textContent).join(""),
  };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(1800);

  // 扫更多位置，覆盖几句不同说话人的行
  const samples = [];
  for (let n = 0; n < 120; n += 4) {
    const text = await evaluate(probe(n));
    if (text === null) break;
    await wait(220);
    samples.push({ n, ...(await evaluate(snapshot)) });
  }

  await shot(out);

  const heights = [...new Set(samples.map((s) => s.rail))];
  const toggle = samples.filter((s) => s.conflict === null).length;

  const result = {
    samples,
    distinctHeights: heights,
    conflictFreeSamples: toggle,
    totalSamples: samples.length,
    speakers: [...new Set(samples.map((s) => s.speaker))],
  };

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
