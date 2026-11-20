// 走查「设置 → 一键删除与隐去」面板：改名、三类分组、共用词表。
//
// 只读，不点保存。

const openSettings = `(() => {
  const triggers = [...document.querySelectorAll(".menu-trigger")];
  const menu = triggers.find((b) => b.textContent.trim() === "设置");
  if (!menu) return { found: false, triggers: triggers.map((b) => b.textContent.trim()) };
  menu.click();
  return { found: true };
})()`;

const readSettingsMenu = `(() => {
  const items = [...document.querySelectorAll(".menu-item")].map((b) => ({
    label: (b.querySelector(".menu-label") || {}).textContent || "",
    hint: (b.querySelector(".menu-hint") || {}).textContent || "",
  }));
  return { items };
})()`;

const openCleanupSection = `(() => {
  const items = [...document.querySelectorAll(".menu-item")];
  const target = items.find((b) => {
    const label = (b.querySelector(".menu-label") || {}).textContent || "";
    return label.includes("一键删除与隐去");
  });
  if (!target) {
    return { clicked: false, labels: items.map((b) => (b.querySelector(".menu-label") || {}).textContent || "") };
  }
  target.click();
  return { clicked: true };
})()`;

const readPanel = `(() => {
  const modal = document.querySelector(".modal");
  if (!modal) return { open: false };
  const head = (modal.querySelector(".modal-head h2") || {}).textContent || "";
  const subheads = [...modal.querySelectorAll(".settings-subhead")].map((h) => h.textContent.trim());
  const labels = [...modal.querySelectorAll(".field > span")].map((s) => {
    const hint = s.querySelector(".hint");
    const label = hint ? s.textContent.replace(hint.textContent, "").trim() : s.textContent.trim();
    return { label, hint: hint ? hint.textContent.trim() : "" };
  });
  const notes = [...modal.querySelectorAll(".section-note")].map((p) => p.textContent.trim());
  const sidebar = [...modal.querySelectorAll(".settings-nav button, nav button")].map((b) => b.textContent.trim());
  return { open: true, head, subheads, labels, notes, sidebar };
})()`;

const close = `(() => {
  const modal = document.querySelector(".modal");
  if (!modal) return false;
  const btn = [...modal.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "取消");
  if (btn) { btn.click(); return true; }
  const x = modal.querySelector(".modal-head button");
  if (x) { x.click(); return true; }
  return false;
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2600);
  const opened = await evaluate(openSettings);
  await wait(400);
  const menu = await evaluate(readSettingsMenu);
  await shot(`${out}-menu`);
  const clicked = await evaluate(openCleanupSection);
  await wait(1200);
  const panel = await evaluate(readPanel);
  await shot(`${out}-panel`);
  const closed = await evaluate(close);

  const result = { opened, menu, clicked, panel, closed };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
