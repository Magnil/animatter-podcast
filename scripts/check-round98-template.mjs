/**
 * 走查（第 98 轮）：模板文字的角色名、多段颜色、以及「未绑定就消失」。
 *
 * 断言落在算出来的事实上：
 *
 * 1. 七行的标签必须是 期号 / 当期标题 / 主播1 / 主播2 / 嘉宾1 / 嘉宾2 / 页脚 ——
 *    不是 Photoshop 的「头像1 拷贝」，也不是页脚自己的正文；
 * 2. 期号与当期标题的默认值必须带 `**`（PSD 本身就是蓝红双色），页脚不带；
 * 3. 头像绑定行显示 主播1/嘉宾1，而不是 PSD 的组名；
 * 4. 改期号为 `EP.**24**` 之后重新生成静帧，新图必须真的加载成功 —— 而且它的文件名
 *    必须变（digest 覆盖了文字）。
 *
 * cdp.mjs 不打印脚本返回值，所以这里自己 console.log，事实不对就 throw。
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
  await wait(600);

  const rows = await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".template-text-row")];
    return {
      labels: rows.map((row) =>
        (row.querySelector(".template-text-label")?.textContent ?? "").trim(),
      ),
      texts: rows.map((row) => row.querySelector('input[type="text"]')?.value ?? ""),
      avatarRows: [...document.querySelectorAll(".video-avatar-name")].map((node) =>
        node.textContent.trim(),
      ),
      hasHint: [...document.querySelectorAll(".section-note")].some((node) =>
        node.textContent.includes("**"),
      ),
    };
  })()`);
  console.log("rows:", JSON.stringify(rows));

  const expected = ["期号", "当期标题", "主播1", "主播2", "嘉宾1", "嘉宾2", "页脚"];
  if (JSON.stringify(rows.labels) !== JSON.stringify(expected)) {
    fail(`labels are ${JSON.stringify(rows.labels)}, expected ${JSON.stringify(expected)}`);
  }
  if (!rows.texts[0].includes("**")) fail(`期号 has no colour markup: ${rows.texts[0]}`);
  if (!rows.texts[1].includes("**")) fail(`当期标题 has no colour markup: ${rows.texts[1]}`);
  if (rows.texts[6].includes("**")) fail(`页脚 should be one colour: ${rows.texts[6]}`);
  if (!rows.hasHint) fail("the ** hint is missing from the panel");
  for (const name of ["主播1", "主播2", "嘉宾1", "嘉宾2"]) {
    if (!rows.avatarRows.some((text) => text.startsWith(name))) {
      fail(`avatar binding rows do not mention ${name}: ${JSON.stringify(rows.avatarRows)}`);
    }
  }

  const before = await evaluate(
    `document.querySelector(".video-preview-box img")?.getAttribute("src") ?? null`,
  );

  // 改期号：加一段红色，检查它真的存了下来。
  await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".template-text-row")];
    const row = rows.find(
      (item) => (item.querySelector(".template-text-label")?.textContent ?? "").includes("期号"),
    );
    if (!row) throw new Error("no 期号 row");
    const input = row.querySelector('input[type="text"]');
    input.focus();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value",
    ).set;
    setter.call(input, "EP.**24**");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    // Headless Chrome never focuses the window, so a real blur never fires there; React 17+
    // listens to focusout, so dispatch that.
    input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    return true;
  })()`);
  await wait(2500);

  const after = await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".template-text-row")];
    const row = rows.find(
      (item) => (item.querySelector(".template-text-label")?.textContent ?? "").includes("期号"),
    );
    return row?.querySelector('input[type="text"]')?.value ?? null;
  })()`);
  console.log("episode:", JSON.stringify({ before: "EP.**23**", after }));
  if (after !== "EP.**24**") fail(`the edit did not round-trip: ${after}`);

  const still = await evaluate(`(async () => {
    const img = document.querySelector(".video-preview-box img");
    const previous = img ? img.getAttribute("src") : null;
    const button = [...document.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "生成静帧",
    );
    if (!button) return { ok: false, reason: "no 生成静帧 button" };
    button.click();
    for (let i = 0; i < 60; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const now = document.querySelector(".video-preview-box img");
      if (now && now.getAttribute("src") !== previous && now.complete && now.naturalWidth > 0) {
        return { ok: true, previous, after: now.getAttribute("src"), width: now.naturalWidth };
      }
    }
    const now = document.querySelector(".video-preview-box img");
    return { ok: false, previous, after: now ? now.getAttribute("src") : null };
  })()`);
  console.log("still:", JSON.stringify(still));
  if (!still.ok) fail(`the still did not refresh: ${JSON.stringify(still)}`);

  await shot(out);
  return { rows, still };
}
