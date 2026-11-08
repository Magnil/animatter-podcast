// 走查：生成 → 点第二个备选标题 → 确认只有 H1 变了，正文其余部分逐字不动。
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。
const clickStartsWith = (text) => `(() => {
  const target = [...document.querySelectorAll("button, a, [role=tab], [role=menuitem]")]
    .find((el) => (el.textContent || "").trim().startsWith(${JSON.stringify(text)}));
  if (!target) return false;
  target.click();
  return true;
})()`;

const bodyText = `(document.querySelector(".script-body") || {}).value || ""`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(1200);
  await evaluate(clickStartsWith("导出"));
  await wait(500);
  await evaluate(clickStartsWith("ShowNotes"));
  await wait(700);
  await evaluate(`${`(() => {
    const target = [...document.querySelectorAll("button")]
      .find((b) => (b.textContent || "").trim() === "生成");
    if (!target) return false;
    target.click();
    return true;
  })()`}`);

  const started = Date.now();
  let ready = false;
  while (Date.now() - started < 300000) {
    await wait(3000);
    ready = await evaluate(`document.querySelectorAll(".title-option").length > 0`);
    if (ready) break;
  }
  if (!ready) {
    await shot(out);
    return { error: "生成超时或失败" };
  }

  const before = await evaluate(bodyText);
  const clicked = await evaluate(`(() => {
    const buttons = [...document.querySelectorAll(".title-option")];
    if (buttons.length < 2) return null;
    const label = (buttons[1].querySelector(".title-text") || {}).textContent;
    buttons[1].click();
    return label;
  })()`);
  await wait(600);
  await shot(out);

  const after = await evaluate(bodyText);
  const head = (text) => text.split("\n")[0];
  const tail = (text) => text.split("\n").slice(1).join("\n");

  return {
    clickedLabel: clicked,
    beforeLine: head(before),
    afterLine: head(after),
    onlyHeadlineChanged: tail(before) === tail(after),
    waitedMs: Date.now() - started,
  };
};
