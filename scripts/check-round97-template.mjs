/**
 * 走查（第 97 轮）：模板导入后的可编辑文字与头像换图。
 *
 * 断言落在算出来的事实上，截图看不出来的都读回来：
 *
 * 1. 「模板」标签页里必须出现 7 行输入（1 期号 + 1 标题 + 4 头像名 + 1 页脚）；
 * 2. 字体下拉框里必须有「Alimama ShuHeiTi」——它既不在注册表里也不在 C:\Windows\Fonts，
 *    只有字由的下载目录里有，所以这一条同时证明了后端确实去读了字由的缓存；
 * 3. 改标题后重新生成静帧，图片 URL 必须变（digest 覆盖了文字），并且新图必须真的加载
 *    成功（naturalWidth > 0）——404 和空白图在截图里无法区分；
 * 4. 每个头像行都有「换图」按钮。
 *
 * cdp.mjs 不会打印脚本的返回值，所以这里自己 console.log，并在事实不对时 throw。
 */
export default async function run({ evaluate, shot, wait, out }) {
  const fail = (message) => {
    throw new Error(message);
  };
  await wait(1500);

  // 展开视频区（默认折叠）。
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(2200);

  // 「模板」是默认标签页，确认一下。
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
      rowCount: rows.length,
      labels: rows.map((row) => row.querySelector(".template-text-label")?.textContent?.trim() ?? ""),
      sizes: rows.map((row) => row.querySelector(".template-size")?.value ?? ""),
      fonts: rows.map((row) => row.querySelector("select")?.value ?? ""),
      fontOptionCount: rows[0] ? rows[0].querySelectorAll("select option").length : 0,
      hasShuHei: rows.length > 0
        ? [...rows[0].querySelectorAll("select option")].some(
            (o) => o.value === "Alimama ShuHeiTi",
          )
        : false,
      photoButtons: document.querySelectorAll(".avatar-photo-actions").length,
      settingsWidth: Math.round(
        document.querySelector(".video-settings")?.getBoundingClientRect().width ?? 0,
      ),
    };
  })()`);
  console.log("rows:", JSON.stringify(rows));

  if (rows.rowCount !== 7) fail(`expected 7 editable text rows, got ${rows.rowCount}`);
  if (rows.labels[0] !== "期号") fail(`first row should be 期号, got ${rows.labels[0]}`);
  if (rows.labels[1] !== "当期标题") fail(`second row should be 当期标题, got ${rows.labels[1]}`);
  if (!rows.hasShuHei) fail("数黑 is not in the font list — 字由's store was not read");
  if (rows.photoButtons !== 4) fail(`expected 4 avatar photo buttons, got ${rows.photoButtons}`);

  const before = await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".template-text-row")];
    const titleRow = rows.find(
      (row) => row.querySelector(".template-text-label")?.textContent?.includes("当期标题"),
    );
    if (!titleRow) throw new Error("no title row");
    return titleRow.querySelector('input[type="text"]').value;
  })()`);

  await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".template-text-row")];
    const titleRow = rows.find(
      (row) => row.querySelector(".template-text-label")?.textContent?.includes("当期标题"),
    );
    const input = titleRow.querySelector('input[type="text"]');
    // Dispatch the event React actually listens for. Headless Chrome never focuses the
    // window (document.hasFocus() is false), so a real blur never fires there — measured:
    // activeElement changed but neither "blur" nor "focusout" was dispatched.
    input.focus();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value",
    ).set;
    setter.call(input, "走查用的新标题\\n第二行");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    return true;
  })()`);
  await wait(2500);

  const after = await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".template-text-row")];
    const titleRow = rows.find(
      (row) => row.querySelector(".template-text-label")?.textContent?.includes("当期标题"),
    );
    return titleRow?.querySelector('input[type="text"]')?.value ?? null;
  })()`);
  console.log("title:", JSON.stringify({ before, after }));
  if (after === before) fail("the title edit did not round-trip through the server");

  const still = await evaluate(`(async () => {
    const img = document.querySelector(".video-preview-box img");
    const before = img ? img.getAttribute("src") : null;
    const button = [...document.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "生成静帧",
    );
    if (!button) return { ok: false, reason: "no 生成静帧 button", before };
    button.click();
    for (let i = 0; i < 60; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const now = document.querySelector(".video-preview-box img");
      if (now && now.getAttribute("src") !== before && now.complete && now.naturalWidth > 0) {
        return {
          ok: true,
          before,
          after: now.getAttribute("src"),
          width: now.naturalWidth,
          height: now.naturalHeight,
        };
      }
    }
    const now = document.querySelector(".video-preview-box img");
    return {
      ok: false,
      before,
      after: now ? now.getAttribute("src") : null,
      loaded: now ? now.naturalWidth : 0,
    };
  })()`);
  console.log("still:", JSON.stringify(still));
  if (!still.ok) fail(`the still did not change or load: ${JSON.stringify(still)}`);
  if (!still.after.includes("still-")) fail(`unexpected still url ${still.after}`);

  await shot(out);
  return { rows, still, title: { before, after } };
}
