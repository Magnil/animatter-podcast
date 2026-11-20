// 读「处理」菜单的条目与禁用状态。菜单是异步渲染的，点击后要等一帧再读，
// 上一条脚本在同一次 evaluate 里点完就读，于是 items 恒为空。
export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);
  const open = await evaluate(`(() => {
    const menu = [...document.querySelectorAll(".menu-trigger")].find((b) => b.textContent.trim() === "处理");
    if (!menu) return false;
    menu.click();
    return true;
  })()`);
  await wait(500);
  const items = await evaluate(`[...document.querySelectorAll(".menu-item")].map((b) => ({
    label: (b.querySelector(".menu-label") || {}).textContent || "",
    hint: (b.querySelector(".menu-hint") || {}).textContent || "",
    disabled: b.disabled,
  }))`);
  await shot(`${out}-menu`);
  const result = { open, items };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
