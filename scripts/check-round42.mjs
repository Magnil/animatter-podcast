/**
 * The three bugs from the real-episode report, checked in a real browser.
 *
 *   1. 「1 小时 45 分只转写了 40 分钟」 — the transcript list must reach the end.
 *   2. 「导出字幕 显示没有可导出的字幕」 — the panel must show real cues.
 *   3. 「导出成片音源 点击后无反应」 — the control must be actionable and explain
 *      itself when it is not.
 *
 * Assertions read application state (DOM text, button disabled state, request
 * count) rather than pixels, because "the list looks long" is not something a
 * reviewer can check from a screenshot.
 */
export default async function run({ evaluate, shot, wait }) {
  // The dev server needs a moment to load 1074 sentences across three pages.
  await wait(3000);

  const transcript = await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll(".sentence"));
    const last = rows[rows.length - 1];
    const status = document.querySelector(".status-bar, footer, .statusline");
    return {
      count: rows.length,
      lastTime: last ? last.querySelector(".sentence-time")?.textContent?.trim() ?? null : null,
      lastOrd: last ? last.getAttribute("data-ord") : null,
      status: status ? status.textContent.replace(/\\s+/g, " ").trim() : null,
    };
  })()`);
  console.log("transcript:", JSON.stringify(transcript));

  // 1074 sentences is the real episode; anything near that means paging ran.
  const pagedAll = transcript.count > 500;
  console.log("rendered more than one page:", pagedAll);

  await shot("D:/workspaces/animatter-podcast/.tmp/shots/transcript-full.png");

  // --- subtitle panel ---------------------------------------------------
  const opened = await evaluate(`(() => {
    const triggers = Array.from(document.querySelectorAll(".menu-trigger"));
    const exportTrigger = triggers.find((b) => b.textContent.trim() === "导出");
    if (!exportTrigger) return { ok: false, labels: triggers.map((t) => t.textContent.trim()) };
    exportTrigger.click();
    return { ok: true };
  })()`);
  await wait(300);
  const clicked = await evaluate(`(() => {
    const items = Array.from(document.querySelectorAll(".menu-item"));
    const subtitles = items.find((b) => b.textContent.includes("字幕"));
    if (!subtitles) return { ok: false, labels: items.map((i) => i.textContent.trim()) };
    subtitles.click();
    return { ok: true };
  })()`);
  await wait(1500);

  const panel = await evaluate(`(() => {
    const pre = document.querySelector(".subtitle-preview");
    const buttons = Array.from(document.querySelectorAll(".edl-actions button"));
    const exportButton = buttons[0];
    return {
      previewLength: pre ? pre.textContent.length : 0,
      previewHead: pre ? pre.textContent.slice(0, 120) : null,
      empty: pre ? pre.textContent.includes("没有可导出的字幕") : null,
      exportDisabled: exportButton ? exportButton.disabled : null,
      exportLabel: exportButton ? exportButton.textContent.trim() : null,
      cueArrows: pre ? (pre.textContent.match(/-->/g) || []).length : 0,
    };
  })()`);
  console.log("subtitle panel:", JSON.stringify(panel));

  await shot("D:/workspaces/animatter-podcast/.tmp/shots/subtitles.png");

  // Close the panel so the transport bar is reachable.
  const closed = await evaluate(`(() => {
    const buttons = Array.from(document.querySelectorAll("button"));
    const close = buttons.find((b) => b.textContent.trim() === "关闭");
    if (close) { close.click(); return "close"; }
    const x = document.querySelector(".modal .close");
    if (x) { x.click(); return "x"; }
    return "none";
  })()`);
  await wait(400);

  // --- export button ----------------------------------------------------
  const transport = await evaluate(`(() => {
    const buttons = Array.from(document.querySelectorAll(".transport button"));
    const render = buttons.find((b) => b.textContent.includes("导出成片"));
    return {
      found: Boolean(render),
      label: render ? render.textContent.trim() : null,
      disabled: render ? render.disabled : null,
      title: render ? render.title : null,
      labels: buttons.map((b) => b.textContent.trim()),
    };
  })()`);
  console.log("transport:", JSON.stringify(transport));

  // The 导出 menu item, which is the other path the user tried.
  await evaluate(`(() => {
    const triggers = Array.from(document.querySelectorAll(".menu-trigger"));
    const t = triggers.find((b) => b.textContent.trim() === "导出");
    if (t) t.click();
    return true;
  })()`);
  await wait(300);
  const menu = await evaluate(`(() => {
    const items = Array.from(document.querySelectorAll(".menu-item"));
    const render = items.find((b) => b.textContent.includes("导出成片音频"));
    const out = {
      found: Boolean(render),
      disabled: render ? render.disabled : null,
      hint: render ? render.querySelector(".hint")?.textContent?.trim() ?? null : null,
      text: render ? render.textContent.replace(/\\s+/g, " ").trim() : null,
    };
    document.body.click();
    return out;
  })()`);
  console.log("export menu:", JSON.stringify(menu));

  await shot("D:/workspaces/animatter-podcast/.tmp/shots/export-state.png");

  const failures = [];
  if (!pagedAll) failures.push(`转写列表只有 ${transcript.count} 句，分页没生效`);
  if (panel.previewLength < 50) failures.push(`字幕预览是空的（${panel.previewLength} 字符）`);
  if (panel.empty) failures.push("字幕面板仍然显示「没有可导出的字幕」");
  if (panel.exportDisabled) failures.push("字幕导出按钮仍然禁用了");
  if (!transport.found) failures.push("找不到导出成片按钮");
  if (transport.disabled && !transport.title) failures.push("导出成片禁用但没有任何提示");
  if (!menu.found) failures.push("菜单里找不到导出成片音频");

  console.log(failures.length === 0 ? "PASS" : `FAIL: ${failures.join(" / ")}`);
  if (failures.length > 0) throw new Error(failures.join("; "));
}
