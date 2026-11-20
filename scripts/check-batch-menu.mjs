// 走查「处理」菜单的五个动作与四个还原条目。
//
// 只读 + 打开弹窗，**不点执行**：点执行会真的改数据。要验证写入路径请用
// API 或在临时项目上跑。
//
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const readMenus = `(() => {
  const triggers = [...document.querySelectorAll(".menu-trigger")].map((b) => b.textContent.trim());
  return { triggers };
})()`;

const openProcessing = `(() => {
  const triggers = [...document.querySelectorAll(".menu-trigger")];
  const menu = triggers.find((b) => b.textContent.trim() === "处理");
  if (!menu) return { found: false, triggers: triggers.map((b) => b.textContent.trim()) };
  menu.click();
  return { found: true };
})()`;

const readItems = `(() => {
  const items = [...document.querySelectorAll(".menu-item")].map((b) => ({
    label: (b.querySelector(".menu-label") || {}).textContent || "",
    hint: (b.querySelector(".menu-hint") || {}).textContent || "",
    disabled: b.disabled,
  }));
  return { items };
})()`;

// 点某一项打开弹窗，读标题与正文里的关键句
const openItem = (label) => `(() => {
  const items = [...document.querySelectorAll(".menu-item")];
  const target = items.find((b) => (b.querySelector(".menu-label") || {}).textContent === ${JSON.stringify(label)});
  if (!target) return { clicked: false };
  target.click();
  return { clicked: true };
})()`;

const readDialog = `(() => {
  const modal = document.querySelector(".modal-wide");
  if (!modal) return { open: false };
  const head = (modal.querySelector(".modal-head h2") || {}).textContent || "";
  const kinds = [...modal.querySelectorAll(".cleanup-kind")].map((row) => ({
    name: (row.querySelector(".cleanup-kind-name") || {}).textContent || "",
    count: (row.querySelector(".cleanup-kind-count") || {}).textContent || "",
    hint: (row.querySelector("em.hint") || {}).textContent || "",
    disabled: row.classList.contains("disabled"),
    checked: Boolean(row.querySelector("input")?.checked),
  }));
  const summary = (modal.querySelector(".cleanup-list-head strong") || {}).textContent || "";
  const submit = (modal.querySelector(".modal-foot button:last-child") || {}).textContent || "";
  const thresholdsShown = Boolean(modal.querySelector(".cleanup-thresholds"));
  const notes = [...modal.querySelectorAll(".section-note")].map((p) => p.textContent.trim());
  return { open: true, head, kinds, summary, submit, thresholdsShown, notes };
})()`;

const close = `(() => {
  const modal = document.querySelector(".modal-wide");
  if (!modal) return false;
  const btn = [...modal.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "取消");
  if (btn) btn.click();
  return true;
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);

  const menus = await evaluate(readMenus);
  const opened = await evaluate(openProcessing);
  await wait(300);
  const items = await evaluate(readItems);
  await shot(`${out}-menu`);
  await evaluate(`document.querySelector(".menu-trigger")?.click()`);
  await wait(200);

  const dialogs = {};
  for (const [key, label] of [
    ["hideFiller", "隐去口癖词…"],
    ["deleteFiller", "删除口癖词…"],
    ["deleteSilence", "删除静音…"],
    ["deleteAll", "删除全部三类…"],
  ]) {
    await evaluate(openProcessing);
    await wait(200);
    const clicked = await evaluate(openItem(label));
    // 第一次打开要跑静音检测（ffmpeg，3 小时素材几秒到十几秒），所以轮询等到
    // 「正在计算…」消失，而不是猜一个固定时长——上一版写死 4s，首个弹窗读到的
    // 还是空清单。
    for (let i = 0; i < 60; i += 1) {
      const ready = await evaluate(`(() => {
        const s = document.querySelector(".cleanup-list-head strong");
        return s ? !s.textContent.includes("正在计算") : false;
      })()`);
      if (ready) break;
      await wait(500);
    }
    await wait(200);
    dialogs[key] = { clicked, ...(await evaluate(readDialog)) };
    await shot(`${out}-${key}`);
    await evaluate(close);
    await wait(300);
  }

  const result = { menus, opened, items, dialogs };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
