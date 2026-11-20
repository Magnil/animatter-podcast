/**
 * 走查（第 101 轮）：演示稿的标题字体和正文字体可以分开设。
 *
 * 断言落在**从像素里数出来的事实**上，因为这一轮最容易犯的错全都「看起来正常」：
 *
 * 1. 两个下拉框都在，且选项恰好是 `GET /projects/{id}/deck/fonts` 返回的那份（能画中文的）；
 * 2. 改**标题**字体：缩略图的标题区域变了，正文区域**一个像素都没变**；
 * 3. 改**正文**字体：正文区域变了，标题区域没变；
 * 4. 换过字体之后缩略图确实换成了**新的一张**（URL 只有 `&r=` 变了，`revision` 让它重取——
 *    不重取就会一直给你旧字体那一张，这就是 §19.11 陷阱 24）；
 * 5. 刷新页面后设置还在（存在 `deck.title_font`/`deck.body_font` 而不是组件 state 里）。
 *
 * 区域怎么划：把缩略图读进 canvas，按行数墨迹，切成若干段连续墨迹。标题和正文之间那条
 * **强调线**是最矮的那一段（高 3–6 px），它上面是标题、下面是正文。这条线是渲染器自己画的，
 * 所以分区是**从图里算出来的**，不是写死的像素坐标——字号是自动缩的，坐标会随字体变。
 */
export default async function run({ evaluate, shot, wait, out }) {
  const PROJECT = "prj_1m6v2m8anfpyqq3w8qt";
  const fail = (message) => {
    throw new Error(message);
  };

  /** Ink profile of the preview: one entry per image row, and the canvas kept off-DOM. */
  const PROFILE = `(() => {
    const img = document.querySelector(".deck-preview");
    if (!img) return null;
    // A re-fetching <img> has naturalWidth 0 for a moment. Measuring then throws
    // IndexSizeError inside getImageData, so the poll has to treat it as "not yet".
    if (!img.complete || img.naturalWidth === 0) {
      return { width: 0, height: 0, rows: [], moment: [], src: img.src, loaded: false };
    }
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    const rows = new Array(canvas.height).fill(0);
    // A row's signature is count + Σx, not just count: two faces can put the same number of
    // dark pixels on a line while moving them sideways, and "count only" would call that
    // unchanged. Σx catches a glyph swap that keeps the weight.
    const moment = new Array(canvas.height).fill(0);
    for (let y = 0; y < canvas.height; y += 1) {
      let count = 0;
      let sum = 0;
      const base = y * canvas.width * 4;
      for (let x = 0; x < canvas.width; x += 1) {
        if (data[base + x * 4] < 200) {
          count += 1;
          sum += x;
        }
      }
      rows[y] = count;
      moment[y] = sum;
    }
    return {
      width: canvas.width,
      height: canvas.height,
      rows,
      moment,
      src: img.src,
      loaded: img.naturalWidth > 0,
    };
  })()`;

  const BANDS = `(profile) => {
    const bands = [];
    let start = -1;
    for (let y = 0; y < profile.rows.length; y += 1) {
      const ink = profile.rows[y] > 0;
      if (ink && start < 0) start = y;
      if (!ink && start >= 0) {
        bands.push({ top: start, bottom: y - 1, ink: 0 });
        start = -1;
      }
    }
    if (start >= 0) bands.push({ top: start, bottom: profile.rows.length - 1, ink: 0 });
    for (const band of bands) {
      for (let y = band.top; y <= band.bottom; y += 1) band.ink += profile.rows[y];
    }
    return bands;
  }`;

  await wait(1500);
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(2000);
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll(".video-settings .tab")].find(
      (b) => b.textContent.trim() === "演示稿",
    );
    if (tab && !tab.classList.contains("active")) tab.click();
    return true;
  })()`);
  await wait(2500);

  // --- 0. a known starting point -----------------------------------------

  // The demo project's deck is persistent state, and a previous run (or the last time someone
  // opened the panel) may have left a font set. Measuring "changed to SimHei" from a deck that
  // is *already* in SimHei is a no-op with a diff of zero — which reads exactly like a bug in
  // the feature. Reset through the same endpoint the UI uses, then reload the panel.
  const reset = await evaluate(
    `fetch("/api/projects/${PROJECT}/deck", {
       method: "PATCH",
       headers: { "content-type": "application/json" },
       body: JSON.stringify({ title_font: "", body_font: "" }),
     }).then((r) => r.json()).then((d) => [d.title_font, d.body_font])`,
  );
  console.log("reset to:", JSON.stringify(reset));
  if (reset[0] !== "Microsoft YaHei" || reset[1] !== "Microsoft YaHei") {
    fail(`重置后是 ${JSON.stringify(reset)}，期望两个都是 Microsoft YaHei`);
  }
  await evaluate(`location.reload()`);
  await wait(4000);
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(1500);
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll(".video-settings .tab")].find(
      (b) => b.textContent.trim() === "演示稿",
    );
    if (tab && !tab.classList.contains("active")) tab.click();
    return true;
  })()`);
  await wait(2500);

  // --- 1. the two pickers ------------------------------------------------

  const pickers = await evaluate(`(() => {
    const title = document.querySelector('select[aria-label="标题字体"]');
    const body = document.querySelector('select[aria-label="正文字体"]');
    const options = (el) => (el ? [...el.options].map((o) => o.value) : null);
    return {
      hasTitle: Boolean(title),
      hasBody: Boolean(body),
      titleValue: title?.value ?? null,
      bodyValue: body?.value ?? null,
      titleOptions: options(title),
      bodyOptions: options(body),
      disabled: [title?.disabled ?? null, body?.disabled ?? null],
    };
  })()`);
  console.log(
    "pickers:",
    JSON.stringify({
      hasTitle: pickers.hasTitle,
      hasBody: pickers.hasBody,
      titleValue: pickers.titleValue,
      bodyValue: pickers.bodyValue,
      options: pickers.titleOptions?.length ?? null,
      disabled: pickers.disabled,
    }),
  );
  if (!pickers.hasTitle || !pickers.hasBody) fail("两次字体下拉框没有都渲染出来");
  if (pickers.disabled.some((flag) => flag === true)) fail("字体下拉框是 disabled");
  if (!pickers.titleOptions?.length) fail("字体下拉框没有选项");

  // 选项必须就是服务端那份「能画中文的」列表——前端自己攒一份列表就会和渲染器漂移。
  // 比的是**集合**，不是顺序：下拉框把当前字体提到第一位（`<select>` 的 value 匹配不到任何
  // option 时会渲染成空白，看起来像「没有字体」），所以顺序本来就和接口不同。
  const served = await evaluate(
    `fetch("/api/projects/${PROJECT}/deck/fonts").then((r) => r.json())`,
  );
  const picked = [...pickers.titleOptions].sort();
  const offered = [...served.families].sort();
  if (JSON.stringify(picked) !== JSON.stringify(offered)) {
    fail(
      `下拉框 ${picked.length} 项和服务端 ${offered.length} 项不是同一份列表：` +
        JSON.stringify(picked.filter((name) => !offered.includes(name))),
    );
  }
  if (pickers.titleOptions[0] !== pickers.titleValue) {
    fail(`下拉框第一项是 ${pickers.titleOptions[0]}，当前值却是 ${pickers.titleValue}`);
  }
  if (!served.families.includes("SimHei")) fail("这台机器上没有 SimHei，走查没法换字体");

  // --- 2/3. the pixels ---------------------------------------------------

  const baseline = await evaluate(PROFILE);
  if (!baseline?.loaded) fail("缩略图没有加载");
  const bands = (await evaluate(`(${BANDS})(${JSON.stringify(baseline)})`)) ?? [];
  console.log("bands:", JSON.stringify(bands));
  if (bands.length < 3) fail(`只找到 ${bands.length} 段墨迹，页面看起来是空的`);

  // 头条强调线 = 除标题外最矮的一段：高 ≤ 8 行、且在第一段正文之前。
  const rule = bands.find((band) => band.bottom - band.top <= 8);
  if (!rule) fail("找不到标题下面那条强调线，无法把页面切成标题区/正文区");
  const titleBottom = rule.top;
  const bodyTop = rule.bottom + 1;
  console.log(
    "split:",
    JSON.stringify({ titleBottom, bodyTop, ruleHeight: rule.bottom - rule.top + 1 }),
  );

  const diffRows = (a, b) => {
    const out = [];
    for (let y = 0; y < Math.min(a.rows.length, b.rows.length); y += 1) {
      if (a.rows[y] !== b.rows[y] || a.moment[y] !== b.moment[y]) out.push(y);
    }
    return out;
  };

  const setFont = async (role, family) => {
    const before = await evaluate(
      `document.querySelector(".deck-preview")?.src ?? null`,
    );
    await evaluate(`(() => {
      const select = document.querySelector('select[aria-label="${role}"]');
      if (!select) throw new Error("no select for ${role}");
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
      setter.call(select, ${JSON.stringify(family)});
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    })()`);
    // 受控组件的 value 来自 deck：React 立刻会用**旧的** deck 重渲染一次，所以不能同步读。
    // 等服务端回来、deck 更新之后 select 才会真的停在目标字体上。
    let settled = false;
    for (let i = 0; i < 80; i += 1) {
      await wait(250);
      const value = await evaluate(
        `document.querySelector('select[aria-label="${role}"]')?.value ?? null`,
      );
      if (value === family) {
        settled = true;
        break;
      }
    }
    if (!settled) {
      const notice = await evaluate(
        `document.querySelector(".notice, .toast, [role='status']")?.textContent ?? null`,
      );
      fail(`选 ${role}=${family} 之后 select 没有变成它（notice: ${notice}）`);
    }
    // 换字体会清掉 PNG，缩略图的 URL 只有 `&r=` 变了，所以要等**新的一张**画完再量。只等
    // `naturalWidth > 0` 不够：旧图在 src 换掉之后仍会短暂满足这个条件，量到的还是旧像素，
    // 于是「换了字体没变化」——一个完全假的失败。
    for (let i = 0; i < 80; i += 1) {
      await wait(200);
      const profile = await evaluate(PROFILE);
      if (!profile?.loaded || profile.src === before) continue;
      const canvas = await evaluate(`(() => {
        const img = document.querySelector(".deck-preview");
        return img ? { complete: img.complete, width: img.naturalWidth } : null;
      })()`);
      if (canvas?.complete && canvas.width > 0) return profile;
    }
    fail(`${role} 换成 ${family} 之后缩略图没有重新加载`);
  };

  // 标题换成黑体：标题区必须变，正文区必须一动不动。
  const titleChanged = await setFont("标题字体", "SimHei");
  const titleDiff = diffRows(baseline, titleChanged);
  console.log(
    "title font change:",
    JSON.stringify({
      srcChanged: titleChanged.src !== baseline.src,
      diffRows: titleDiff.length,
      first: titleDiff[0] ?? null,
      last: titleDiff[titleDiff.length - 1] ?? null,
      bodyTop,
    }),
  );
  if (titleDiff.length === 0) fail("换了标题字体，标题区一个像素都没变");
  const leaked = titleDiff.filter((y) => y >= bodyTop);
  if (leaked.length) {
    fail(`换标题字体动了正文区 ${leaked.length} 行（第一行 ${leaked[0]}，正文从 ${bodyTop} 开始）`);
  }
  if (titleChanged.src === baseline.src) fail("换字体后缩略图 URL 没变，浏览器不会重取");

  // 正文换成宋体：正文区必须变，标题区必须不动。基准换成 titleChanged，标题已经是黑体。
  const bodyChanged = await setFont("正文字体", "SimSun");
  const bodyDiff = diffRows(titleChanged, bodyChanged);
  console.log(
    "body font change:",
    JSON.stringify({
      diffRows: bodyDiff.length,
      first: bodyDiff[0] ?? null,
      last: bodyDiff[bodyDiff.length - 1] ?? null,
      bodyTop,
    }),
  );
  if (bodyDiff.length === 0) fail("换了正文字体，正文区一个像素都没变");
  const above = bodyDiff.filter((y) => y < bodyTop);
  if (above.length) fail(`换正文字体动了标题区 ${above.length} 行（第一行 ${above[0]}）`);

  await shot(out);

  // --- 5. persistence ----------------------------------------------------

  await evaluate(`location.reload()`);
  await wait(4000);
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(1500);
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll(".video-settings .tab")].find(
      (b) => b.textContent.trim() === "演示稿",
    );
    if (tab && !tab.classList.contains("active")) tab.click();
    return true;
  })()`);
  await wait(2500);
  const after = await evaluate(`(() => ({
    title: document.querySelector('select[aria-label="标题字体"]')?.value ?? null,
    body: document.querySelector('select[aria-label="正文字体"]')?.value ?? null,
  }))()`);
  console.log("after reload:", JSON.stringify(after));
  if (after.title !== "SimHei") fail(`刷新后标题字体是 ${after.title}，期望 SimHei`);
  if (after.body !== "SimSun") fail(`刷新后正文字体是 ${after.body}，期望 SimSun`);

  // 把样例项目恢复成默认字体，别把走查的结果留在演示库里。
  await setFont("标题字体", "Microsoft YaHei");
  await setFont("正文字体", "Microsoft YaHei");

  return {
    options: pickers.titleOptions.length,
    bodyTop,
    titleDiffRows: titleDiff.length,
    bodyDiffRows: bodyDiff.length,
  };
}
