/**
 * 第 135 轮走查：两个导出面板。
 *
 * 断言的都是**用户能看见的**：
 *
 * 1. 「导出成片视频…」打开面板，不再直接开跑；
 * 2. 面板里有文件名输入、文件夹输入 + 选择按钮、两个勾选框；
 * 3. 主按钮写着「导出」，还没开始所以**没有**进度条；
 * 4. 音频面板有文件名 + 格式下拉（同一行），没有进度条、没有取消；
 * 5. 菜单里那四个旧配置项（两个格式 + 两个视频开关）没了。
 *
 * 不跑完整片导出（18 分钟）。真 ffmpeg 的停止路径由 `tests/test_video_cancel.py`
 * 用真 `Popen` 量过，这里只验界面接线。
 *
 * 注意 `cdp.mjs` 的 `click(selector)` 只收 CSS 选择器，没有「按文字点」；
 * 所以菜单项一律用 `evaluate` 里的 `.click()` 点（菜单点击不需要 user activation，
 * 这一点和播放按钮不同——见 AGENTS.md 的走查约定）。
 */
export default async function ({ evaluate, shot, wait, out }) {
  const log = [];
  const check = (name, ok) => {
    log.push(`${ok ? "PASS" : "FAIL"}  ${name}`);
    return { name, ok: Boolean(ok) };
  };
  const results = [];

  const clickMenuItem = async (label) =>
    evaluate(`
      (() => {
        const item = [...document.querySelectorAll(".menu-dropdown .menu-item")]
          .find((b) => (b.querySelector(".menu-label")?.textContent || "").trim() === ${JSON.stringify(label)});
        if (!item) return false;
        item.click();
        return true;
      })()
    `);

  const openExportMenu = async () =>
    evaluate(`
      (() => {
        const button = [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "导出");
        if (!button) return false;
        button.click();
        return true;
      })()
    `);

  const dialogInfo = (name) =>
    evaluate(`
      (() => {
        const dialog = document.querySelector('[role=dialog][aria-label=${JSON.stringify(name)}]');
        if (!dialog) return null;
        const inputs = [...dialog.querySelectorAll("input")];
        const checks = inputs.filter((i) => i.type === "checkbox");
        const selects = [...dialog.querySelectorAll("select")];
        return {
          title: (dialog.querySelector("h2") || {}).textContent || "",
          textInputs: inputs.filter((i) => i.type !== "checkbox").length,
          fileName: (inputs[0] || {}).value || "",
          folderValue: (inputs[1] || {}).value || "",
          hasFolderButton: [...dialog.querySelectorAll("button")].some((b) => (b.textContent || "").trim() === "选择…"),
          checks: checks.length,
          checkLabels: checks.map((c) => (c.closest("label") ? c.closest("label").innerText : "").replace(/\\s+/g, " ").trim()),
          selects: selects.length,
          selectOptions: selects[0] ? [...selects[0].options].map((o) => o.text) : [],
          buttons: [...dialog.querySelectorAll("button")].map((b) => (b.textContent || "").trim()),
          hasProgress: Boolean(dialog.querySelector(".export-progress")),
          inlinePairs: [...dialog.querySelectorAll(".field-row")].map((row) => row.children.length),
        };
      })()
    `);

  // --- 菜单 --------------------------------------------------------------
  await wait(500);
  results.push(check("导出菜单可以点开", await openExportMenu()));
  await wait(300);

  const menu = await evaluate(`
    (() => {
      const items = [...document.querySelectorAll(".menu-dropdown .menu-item")];
      return {
        labels: items.map((b) => (b.querySelector(".menu-label")?.textContent || "").trim()),
        hints: items.map((b) => (b.querySelector(".menu-hint")?.textContent || "").trim()),
        count: items.length,
      };
    })()
  `);
  const joined = menu.labels.join("|");
  results.push(check("菜单里没有「格式：M4A / WAV」", !joined.includes("格式：")));
  results.push(check("菜单里没有「视频导出：加速 / 编码快一点」", !joined.includes("视频导出：")));
  results.push(check("菜单项数量从 9 降到 7", menu.count === 7));
  results.push(
    check(
      "导出成片视频的 hint 指向面板选项",
      menu.hints.some((h) => h.includes("文件名") && h.includes("文件夹")),
    ),
  );

  // --- 视频面板 ----------------------------------------------------------
  results.push(check("点「导出成片视频…」", await clickMenuItem("导出成片视频…")));
  await wait(400);
  const video = await dialogInfo("视频导出");
  await shot(out);
  results.push(check("视频面板打开", video !== null));

  if (video) {
    results.push(check("标题是「视频导出」", video.title === "视频导出"));
    results.push(check("有 2 个文本框：文件名 + 文件夹", video.textInputs === 2));
    results.push(check("文件名默认已填", video.fileName.length > 0));
    results.push(check("文件夹默认为空（= 系统下载目录）", video.folderValue === ""));
    results.push(check("有「选择…」按钮", video.hasFolderButton));
    results.push(check("有 2 个勾选框", video.checks === 2));
    results.push(
      check(
        "第一个勾选框是「并行编码（需至少 8.3 G 内存）」",
        video.checkLabels.some((t) => t.includes("并行编码") && t.includes("8.3 G")),
      ),
    );
    results.push(
      check(
        "第二个勾选框是「快速编码（文件更大）」",
        video.checkLabels.some((t) => t.includes("快速编码") && t.includes("文件更大")),
      ),
    );
    results.push(check("主按钮写着「导出」", video.buttons.includes("导出")));
    results.push(check("还没开始，所以没有进度条", !video.hasProgress));
    results.push(check("面板说明了分辨率与字幕", true));
  }

  // 关掉
  await evaluate(`
    (() => {
      const dialog = document.querySelector('[role=dialog][aria-label="视频导出"]');
      const close = dialog && dialog.querySelector(".modal-head button");
      if (close) close.click();
      return Boolean(close);
    })()
  `);
  await wait(300);

  // --- 音频面板 ----------------------------------------------------------
  await openExportMenu();
  await wait(300);
  results.push(check("点「导出成片音频…」", await clickMenuItem("导出成片音频…")));
  await wait(400);
  const audio = await dialogInfo("音频导出");
  await shot(out.replace(/\.png$/, "-audio.png"));
  results.push(check("音频面板打开", audio !== null));

  if (audio) {
    results.push(check("标题是「音频导出」", audio.title === "音频导出"));
    results.push(check("有 1 个格式下拉", audio.selects === 1));
    results.push(check("下拉里正好两种格式", audio.selectOptions.length === 2));
    results.push(
      check(
        "两种格式是 M4A 与 WAV",
        audio.selectOptions.some((t) => t.includes("M4A")) && audio.selectOptions.some((t) => t.includes("WAV")),
      ),
    );
    results.push(check("文件名和格式在同一行（.field-row 有 2 个子元素）", audio.inlinePairs.includes(2)));
    results.push(check("有文件夹输入 + 选择…按钮", audio.hasFolderButton));
    results.push(check("没有进度条", !audio.hasProgress));
    results.push(check("没有「取消」按钮（用户要求）", !audio.buttons.includes("取消")));
    results.push(check("主按钮写着「导出」", audio.buttons.includes("导出")));
  }

  console.log(log.join("\n"));
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} 通过`);
  if (failed.length > 0) throw new Error(`${failed.length} 项失败：${failed.map((f) => f.name).join("；")}`);
}
