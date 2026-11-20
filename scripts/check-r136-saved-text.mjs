/**
 * 走查：保存完成之后，进度行必须从「正在保存…」变成「已保存到 …」。
 *
 * 用户报的是「进度条走完后出现『编码完成，正在保存……』，保存完这行还在，会误导」。
 *
 * 为什么打桩 HTTP 而不是真跑一次导出：一小时的片子要 18 分钟，而这条要验的是**面板的接线**
 * （done → delivery=saving → 调 save-to → delivery=saved → 文字变化），不是编码本身。
 * 编码路径另有 `test_video_cancel.py`（真 `Popen`）与 `check-r135-export-running.mjs` 覆盖。
 *
 * 桩的三个端点都返回**服务端的真实形状**，所以走的是真的 state 流转，不是假的组件。
 */
export default async function ({ evaluate, shot, wait, out }) {
  const log = [];
  const results = [];
  const check = (name, ok) => {
    results.push({ name, ok: Boolean(ok) });
    log.push(`${ok ? "PASS" : "FAIL"}  ${name}`);
  };

  await wait(500);

  // --- 打桩 ---------------------------------------------------------------
  const patched = await evaluate(`
    (() => {
      if (window.__r135OrigFetch) return "already";
      window.__r135OrigFetch = window.fetch;
      const job = (status, progress) => ({
        id: "vren_walkthrough", status, progress, duration_ms: 3695928,
        url: "/api/video/exports/vren_walkthrough/file", error: null,
        note: "14 块串行编码：空闲 4.2 GB < 2 个并行需要的 8.3 GB",
      });
      let polls = 0;
      window.fetch = async (input, init) => {
        const url = typeof input === "string" ? input : input.url;
        const json = (body) =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        if (url.includes("/video/export") && (init?.method || "GET") === "POST") {
          return json(job("running", 0.05));
        }
        if (url.includes("/video/exports/") && url.endsWith("/cancel")) {
          return json(job("cancelled", 0.2));
        }
        if (url.match(/\\/video\\/exports\\/[^/]+$/)) {
          polls += 1;
          // 两次 polling 之后就完成，免得走查等太久
          return json(polls >= 2 ? job("done", 1) : job("running", 0.4));
        }
        if (url.includes("/save-to")) {
          // 模拟一次真实耗时 400 ms 的拷贝，好让「正在保存…」这一帧能被看见
          await new Promise((resolve) => setTimeout(resolve, 400));
          return json({
            path: "D:/导出/走查-20261120.mp4",
            bytes: 924297354,
            filename: "走查-20261120.mp4",
          });
        }
        return window.__r135OrigFetch(input, init);
      };
      return "patched";
    })()
  `);
  check("HTTP 打桩成功", patched === "patched" || patched === "already");

  // --- 打开面板并点导出 ---------------------------------------------------
  await evaluate(`(() => {
    [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "导出").click();
    return true;
  })()`);
  await wait(250);
  await evaluate(`(() => {
    [...document.querySelectorAll(".menu-item")]
      .find((x) => (x.querySelector(".menu-label")?.textContent || "").trim() === "导出成片视频…")
      .click();
    return true;
  })()`);
  await wait(400);
  await evaluate(`(() => {
    const dialog = document.querySelector('[role=dialog][aria-label="视频导出"]');
    [...dialog.querySelectorAll(".modal-foot button")].pop().click();
    return true;
  })()`);

  const read = () =>
    evaluate(`
      (() => {
        const dialog = document.querySelector('[role=dialog][aria-label="视频导出"]');
        if (!dialog) return null;
        const bar = dialog.querySelector(".export-progress");
        const text = dialog.querySelector(".export-progress-text");
        const primary = [...dialog.querySelectorAll(".modal-foot button")].pop();
        return {
          tone: bar ? bar.getAttribute("data-tone") : null,
          text: text ? text.textContent.trim() : "",
          button: primary ? primary.textContent.trim() : "",
        };
      })()
    `);

  // 等到「正在保存」出现（它只存在 400 ms，所以要密集轮询）
  let sawSaving = null;
  for (let i = 0; i < 120; i += 1) {
    await wait(100);
    const state = await read();
    if (state && /正在保存/.test(state.text)) {
      sawSaving = state;
      break;
    }
  }
  check("编码完成时写「正在保存…」", sawSaving !== null);
  if (sawSaving) check("「正在保存」时进度条是 100 % 的 ok 色调", sawSaving.tone === "ok");

  // 再等到「已保存到 …」
  let sawSaved = null;
  for (let i = 0; i < 60; i += 1) {
    await wait(100);
    const state = await read();
    if (state && /已保存/.test(state.text)) {
      sawSaved = state;
      break;
    }
  }
  await shot(out);
  check("保存完成后不再写「正在保存」", sawSaved !== null);
  if (sawSaved) {
    check("改成「已保存到 <路径>」", /^已保存到 D:\/导出\/走查-20261120\.mp4$/.test(sawSaved.text));
    check("保存完成后按钮回到「导出」", sawSaved.button === "导出");
  }

  // 复原
  await evaluate(`(() => { window.fetch = window.__r135OrigFetch; return true; })()`);
  await evaluate(`(() => {
    const dialog = document.querySelector('[role=dialog][aria-label="视频导出"]');
    const close = dialog && dialog.querySelector(".modal-head button");
    if (close) close.click();
    return true;
  })()`);

  console.log(log.join("\n"));
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} 通过`);
  if (failed.length > 0) throw new Error(`${failed.length} 项失败：${failed.map((f) => f.name).join("；")}`);
}
