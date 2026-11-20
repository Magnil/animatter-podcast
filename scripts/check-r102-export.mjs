/**
 * Round 102 walkthrough: the three duplicate export affordances are gone, and the one that
 * replaced them works from the menu.
 *
 * Three facts, all computed rather than hard-coded:
 *
 *  1. The video panel has **no** 导出 button, **no** 下载成片 link, and **no** 「成片 … / 原始 …」
 *     line. Each is checked as "no element anywhere in the panel whose text matches", so a
 *     rename does not silently pass.
 *  2. The audio panel's transport has no 导出成片 button, and its 成片/原始 pair appears
 *     exactly **once** on the page — in the audio panel's summary, which is the copy that
 *     survives.
 *  3. 导出 → 导出成片视频… exists, is enabled iff the EDL has deletions, states its reason
 *     when it is not, and the 字幕 slider really reaches it: moving 字号 and reading its
 *     label back proves the two are wired to the same numbers, which is the whole reason
 *     those two values moved out of the panel.
 */

export default async function ({ evaluate, shot, wait, out }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };
  const text = (expression) => evaluate(expression);

  const openMenu = async (label) => {
    const found = await text(`(() => {
      const trigger = [...document.querySelectorAll(".menu-trigger")]
        .find((b) => b.textContent.trim() === ${JSON.stringify(label)});
      if (trigger) trigger.click();
      return !!trigger;
    })()`);
    await wait(180);
    return found && (await text(`!!document.querySelector(".menu-dropdown")`));
  };

  const closeMenus = async () => {
    await text(`(() => {
      document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      return true;
    })()`);
    await wait(120);
  };

  // --- 0. size the viewport first: the video panel folds by default ---------
  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return !!panel;
  })()`);
  await wait(800);

  // --- 1. the video panel lost all three -----------------------------------
  const videoPanel = await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (!panel) return { found: false };
    const flat = (panel.textContent ?? "").replace(/\\s+/g, " ");
    const buttons = [...panel.querySelectorAll("button")].map((b) => b.textContent.trim());
    const body = (document.body.textContent ?? "").replace(/\\s+/g, " ");
    return {
      found: true,
      hasExportButton: buttons.some((t) => t.includes("导出")),
      hasDownloadLink: !!panel.querySelector("a[download], .button-link"),
      hasLengthPair: /成片\\s*\\S+\\s*\\/\\s*原始/.test(flat),
      // The pair that was duplicated. It must survive exactly once: in 音频剪辑's summary.
      pairCount: (body.match(/成片\\s*\\S+\\s*\\/\\s*原始/g) ?? []).length,
    };
  })()`);
  console.log("video panel:", JSON.stringify(videoPanel));
  if (!videoPanel.found) note("找不到 .video-panel");
  if (videoPanel.hasExportButton) note("视频面板里还有导出按钮");
  if (videoPanel.hasDownloadLink) note("视频面板里还有下载链接");
  if (videoPanel.hasLengthPair) note("视频面板里还有「成片 / 原始」那一行");
  if (videoPanel.pairCount === 0) note("整页都没有「成片 / 原始」了：音频剪辑面板的摘要不该被删");
  if (videoPanel.pairCount > 1) note(`「成片 / 原始」在页面上出现了 ${videoPanel.pairCount} 次，应该只有音频剪辑摘要那一处`);

  // --- 2. the audio transport lost its export button ------------------------
  const audioPanel = await text(`(() => {
    const panel = document.querySelector(".audio-panel");
    if (!panel) return { found: false };
    const buttons = [...panel.querySelectorAll("button")].map((b) => b.textContent.trim());
    return {
      found: true,
      buttons,
      hasExport: buttons.some((t) => t.includes("导出成片")),
      hasPreview: buttons.some((t) => t.includes("试听成片")),
    };
  })()`);
  console.log("audio transport:", JSON.stringify(audioPanel));
  if (!audioPanel.found) note("找不到 .audio-panel");
  if (audioPanel.hasExport) note("音频剪辑里还有「导出成片」按钮");

  // --- 3. the menu entry ----------------------------------------------------
  const opened = await openMenu("导出");
  if (!opened) note("导出 菜单打不开");
  const entries = await text(`(() => {
    return [...document.querySelectorAll(".menu-dropdown .menu-item")].map((item) => ({
      label: (item.querySelector(".menu-label")?.textContent ?? "").trim(),
      hint: (item.querySelector(".menu-hint")?.textContent ?? "").trim(),
      disabled: item.disabled === true,
    }));
  })()`);
  console.log("导出 菜单:", JSON.stringify(entries));

  const film = entries.find((item) => item.label.includes("导出成片视频"));
  const removal = await text(`(() => {
    const match = (document.querySelector(".audio-panel")?.textContent ?? "")
      .replace(/\\s+/g, " ")
      .match(/删除\\s*(\\d+:\\d+)/);
    return match ? match[1] : "";
  })()`);
  if (!film) {
    note("导出 菜单里没有「导出成片视频」");
  } else {
    console.log("deletions caption:", JSON.stringify(removal), "entry:", JSON.stringify(film));
    if (!film.hint) note("导出成片视频 没有 hint：禁用时用户看不出为什么");
    // A deletion count on the panel is the page's own statement that an edit exists.
    if (film.disabled === (removal !== "")) {
      note(`导出成片视频 的 disabled=${film.disabled}，而删除时长是「${removal || "无"}」`);
    }
    if (!film.disabled && !/1080p/.test(film.hint)) note(`可用时 hint 没说是 1080p：${film.hint}`);
  }
  await closeMenus();

  // --- 4. the 字幕 slider reaches the export -------------------------------
  await text(`(() => {
    const tab = [...document.querySelectorAll(".video-settings .tab")]
      .find((t) => t.textContent.trim() === "字幕");
    tab?.click();
    return !!tab;
  })()`);
  await wait(400);
  const sliders = await text(`(() => {
    return [...document.querySelectorAll(".video-style input[type=range]")]
      .map((input) => ({ min: input.min, max: input.max, value: input.value }));
  })()`);
  console.log("字幕 sliders:", JSON.stringify(sliders));
  if (sliders.length !== 2) note(`字幕 滑杆应该有 2 个，实际 ${sliders.length} 个`);

  const wired = await text(`(() => {
    const input = document.querySelector(".video-style input[type=range]");
    if (!input) return { ok: false };
    const wanted = String(Number(input.max) - 4);
    // React reads the native setter, so assigning \`input.value\` alone is ignored.
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, wanted);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return { ok: true, wanted };
  })()`);
  await wait(300);
  const shown = await text(`(() => {
    const label = document.querySelector(".video-style .field span")?.textContent ?? "";
    const match = label.match(/(\\d+)/);
    return match ? match[1] : "";
  })()`);
  console.log("slider nudge:", JSON.stringify(wired), "label now:", JSON.stringify(shown));
  if (!wired.ok) note("找不到字幕字号滑杆");
  else if (shown !== wired.wanted) note(`拖动后标签是 ${shown}，期望 ${wired.wanted}`);

  // The value lives in App now; a panel that kept its own copy would show the new label
  // and hand the old number to the encoder. 生成静帧 sends the request with those numbers,
  // so the round trip is what proves they are the same object.
  const forwarded = await text(`(async () => {
    const seen = [];
    const original = window.fetch;
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.includes("/video/preview")) seen.push(url);
      return original(input, init);
    };
    const button = [...document.querySelectorAll(".video-stage-main button")]
      .find((b) => b.textContent.trim() === "生成静帧");
    button?.click();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    window.fetch = original;
    return { clicked: !!button, urls: seen };
  })()`);
  console.log("preview request:", JSON.stringify(forwarded));
  if (!forwarded.clicked) note("找不到「生成静帧」按钮");
  else {
    const url = forwarded.urls[0] ?? "";
    if (!url) note("生成静帧 没有发出请求");
    else if (!url.includes(`font_size=${wired.wanted}`)) {
      note(`生成静帧 的请求里 font_size 不是 ${wired.wanted}：${url}`);
    }
  }

  // --- 5. the entry is enabled by a real deletion, and goes back -----------
  // The demo project has no cuts, so the entry was disabled above. Make one through the
  // real UI (checkbox → 删除选中), read the menu, then undo it. This is the enabled path —
  // no export is started, because a 61-minute film is ~14 minutes of x264 and the encode
  // itself is already covered by the backend suite.
  const selectFirst = await text(`(() => {
    const pick = document.querySelector(".sentence-list .sentence-pick");
    if (!pick) return false;
    pick.click();
    return true;
  })()`);
  await wait(400);
  const deleted = await text(`(async () => {
    const button = [...document.querySelectorAll(".transport button")]
      .find((b) => b.textContent.trim() === "删除选中");
    if (!button || button.disabled) return { ok: false, disabled: button?.disabled ?? null };
    button.click();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    return { ok: true };
  })()`);
  console.log("made a deletion:", JSON.stringify({ selectFirst, deleted }));
  if (!deleted.ok) note(`没能做出一次删除（选句=${selectFirst}，按钮=${JSON.stringify(deleted)}）`);

  await openMenu("导出");
  const afterCut = await text(`(() => {
    const item = [...document.querySelectorAll(".menu-dropdown .menu-item")]
      .find((row) => (row.querySelector(".menu-label")?.textContent ?? "").includes("导出成片视频"));
    if (!item) return null;
    return {
      disabled: item.disabled === true,
      hint: (item.querySelector(".menu-hint")?.textContent ?? "").trim(),
    };
  })()`);
  console.log("entry after a cut:", JSON.stringify(afterCut));
  if (!afterCut) note("删了一句之后菜单里找不到「导出成片视频」");
  else {
    if (afterCut.disabled) note("已经删了一句，导出成片视频 仍然是禁用的");
    if (!/1080p/.test(afterCut.hint)) note(`可用时 hint 没说是 1080p：${afterCut.hint}`);
  }
  await closeMenus();

  // Undo, so the demo project is left exactly as it was found.
  const undone = await text(`(async () => {
    const button = [...document.querySelectorAll(".transport button")]
      .find((b) => b.textContent.trim() === "恢复删除");
    if (!button || button.disabled) return { ok: false };
    button.click();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    return { ok: true };
  })()`);
  console.log("undone:", JSON.stringify(undone));
  if (!undone.ok) note("恢复删除 没成功：演示项目被留在已删除状态");

  await openMenu("导出");
  const back = await text(`(() => {
    const item = [...document.querySelectorAll(".menu-dropdown .menu-item")]
      .find((row) => (row.querySelector(".menu-label")?.textContent ?? "").includes("导出成片视频"));
    return item ? item.disabled === true : null;
  })()`);
  console.log("entry after undo:", JSON.stringify(back));
  if (back !== true) note(`撤销之后 导出成片视频 的 disabled=${back}，应该回到 true`);
  await closeMenus();

  await shot(out);
  if (failures.length > 0) throw new Error(`走查失败 ${failures.length} 项：${failures.join("；")}`);
  console.log("ok");
}
