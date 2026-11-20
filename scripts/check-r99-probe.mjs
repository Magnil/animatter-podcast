/**
 * 走查探针：标题是多行值，控件必须能表达换行，且每行没有横向溢出。
 *
 * 背景：`<input type=text>` 的 value sanitization **会删掉 CR/LF**。上一版就是这样 —— 标题的
 * 31 个字里有 `\n`，但输入框读回来没有换行，于是随手改一个字符就会把设计的两行标题提交成一行。
 */
export default async function run({ evaluate, shot, wait, out }) {
  await wait(1500);
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(2200);
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll(".video-settings .tab")].find(
      (b) => b.textContent.trim() === "模板",
    );
    if (tab && !tab.classList.contains("active")) tab.click();
    return true;
  })()`);
  await wait(800);

  const facts = await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".template-form .template-row")];
    const rowOf = (label) =>
      rows.find((r) => r.querySelector(".template-row-label")?.textContent === label);
    const fieldOf = (row) => row?.querySelector("textarea, input[type=text]") ?? null;
    const titleRow = rowOf("当期标题");
    const titleField = fieldOf(titleRow);
    const episodeField = fieldOf(rowOf("期号"));
    return {
      titleTag: titleField?.tagName ?? null,
      titleHasNewline: titleField ? titleField.value.includes("\\n") : null,
      titleLines: titleField ? titleField.value.split("\\n").length : null,
      titleRows: titleRow?.querySelector("textarea")?.rows ?? null,
      episodeTag: episodeField?.tagName ?? null,
      overflow: rows.map((row) => ({
        label: row.querySelector(".template-row-label")?.textContent,
        scroll: row.scrollWidth,
        client: row.clientWidth,
      })),
    };
  })()`);
  console.log("facts:", JSON.stringify(facts, null, 1));

  if (facts.titleHasNewline !== true) throw new Error("标题的换行在控件里丢了");
  if (facts.titleLines !== 2) throw new Error(`标题应有 2 行，实际 ${facts.titleLines}`);
  if (facts.episodeTag !== "INPUT") throw new Error("期号应该还是单行 input");
  for (const row of facts.overflow) {
    if (row.scroll > row.client) throw new Error(`${row.label} 行横向溢出 ${row.scroll}/${row.client}`);
  }

  await shot(out);
  return facts;
}
