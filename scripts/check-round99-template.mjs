/**
 * 走查（第 99 轮）：模板标签页只剩「一列属性」，不再有重复的主播/嘉宾设置和说明文字。
 *
 * 断言落在算出来的事实上：
 *
 * 1. 表单行依次是 期号 / 当期标题 / 主播1 / 主播2 / 嘉宾1 / 嘉宾2 / 页脚 —— 一个列表，不是两个；
 * 2. 七个标签的 computed color 完全一致（上一轮两半用了两种颜色）；
 * 3. 头像行里**没有**文字输入框，但都有「换图」；名字来自绑定的说话人（主播1 马小褂、嘉宾1 呈AA）；
 * 4. 标题的两行在控件里是**两行**（`<input type=text>` 会把换行吞掉，上一版就是这样丢的）；
 * 5. 每行都没有横向溢出；
 * 6. 面板里不再出现那四句说明。
 */
export default async function run({ evaluate, shot, wait, out }) {
  const fail = (message) => {
    throw new Error(message);
  };
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
    const avatarRows = rows.filter((row) => row.classList.contains("is-avatar"));
    const titleRow = rows.find(
      (row) => row.querySelector(".template-row-label")?.textContent === "当期标题",
    );
    const titleField = titleRow?.querySelector("textarea, input[type=text]") ?? null;
    const episodeField = rows
      .find((row) => row.querySelector(".template-row-label")?.textContent === "期号")
      ?.querySelector("textarea, input[type=text]");
    return {
      labels: rows.map((row) =>
        (row.querySelector(".template-row-label")?.textContent ?? "").trim(),
      ),
      kinds: rows.map((row) => (row.classList.contains("is-avatar") ? "avatar" : "text")),
      colours: rows.map((row) => {
        const label = row.querySelector(".template-row-label");
        return label ? getComputedStyle(label).color : null;
      }),
      avatarInputs: avatarRows.map((row) => row.querySelectorAll('input[type="text"]').length),
      avatarSelects: avatarRows.map((row) => row.querySelector("select")?.value ?? null),
      avatarButtons: avatarRows.map((row) =>
        [...row.querySelectorAll("button, label")].map((node) => node.textContent.trim()),
      ),
      titleTag: titleField?.tagName ?? null,
      titleLines: titleField ? titleField.value.split("\\n").length : null,
      episodeTag: episodeField?.tagName ?? null,
      overflow: rows
        .filter((row) => row.scrollWidth > row.clientWidth)
        .map((row) => row.querySelector(".template-row-label")?.textContent ?? "?"),
      panelText: document.querySelector(".video-settings-body")?.textContent ?? "",
    };
  })()`);
  console.log("facts:", JSON.stringify({ ...facts, panelText: undefined }));

  const expected = ["期号", "当期标题", "主播1", "主播2", "嘉宾1", "嘉宾2", "页脚"];
  if (JSON.stringify(facts.labels) !== JSON.stringify(expected)) {
    fail(`rows are ${JSON.stringify(facts.labels)}, expected ${JSON.stringify(expected)}`);
  }
  const expectedKinds = ["text", "text", "avatar", "avatar", "avatar", "avatar", "text"];
  if (JSON.stringify(facts.kinds) !== JSON.stringify(expectedKinds)) {
    fail(`row kinds are ${JSON.stringify(facts.kinds)}`);
  }
  if (new Set(facts.colours).size !== 1) {
    fail(`label colours differ across rows: ${JSON.stringify(facts.colours)}`);
  }
  if (facts.avatarInputs.some((count) => count > 0)) {
    fail(`avatar rows still hold a text input: ${JSON.stringify(facts.avatarInputs)}`);
  }
  for (const labels of facts.avatarButtons) {
    if (!labels.some((text) => text.includes("换图"))) {
      fail(`an avatar row has no 换图 button: ${JSON.stringify(facts.avatarButtons)}`);
    }
  }
  if (facts.titleTag !== "TEXTAREA" || facts.titleLines !== 2) {
    fail(`the two-line title is not editable as two lines: ${facts.titleTag} ${facts.titleLines}`);
  }
  if (facts.episodeTag !== "INPUT") fail(`期号 should stay a single-line input: ${facts.episodeTag}`);
  if (facts.overflow.length > 0) fail(`rows overflow horizontally: ${JSON.stringify(facts.overflow)}`);

  // 绑定是真的：主播1 = 马小褂(1)、嘉宾1 = 呈AA(0)、其余未绑定。
  const wanted = { 主播1: "1", 主播2: "", 嘉宾1: "0", 嘉宾2: "" };
  ["主播1", "主播2", "嘉宾1", "嘉宾2"].forEach((label, index) => {
    if (facts.avatarSelects[index] !== wanted[label]) {
      fail(`${label} selects ${JSON.stringify(facts.avatarSelects[index])}, expected ${wanted[label]}`);
    }
  });

  const banned = ["两边不成对", "框内有", "素材框 1440×810", "隐藏的头像组", "个头像位"];
  for (const phrase of banned) {
    if (facts.panelText.includes(phrase)) fail(`the panel still says 「${phrase}」`);
  }

  await shot(out);
  return facts;
}
