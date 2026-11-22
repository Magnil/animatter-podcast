/**
 * 第 138 轮走查：**没有模板**的项目打开视频面板时，出口不只是「找一份 PSD 并手打路径」。
 *
 * 难点：界面里没有切换项目的入口（`App` 只认启动时那一个项目），所以拿不到真实的无模板
 * 项目。做法是把 `GET /video/template` 的响应截成 `null`——这是真实存在的状态（新建项目
 * 装默认模板失败时、老项目里被清掉时），而面板对它的处理正是这条走查要验的东西。
 *
 * 只核对文案与按钮，不点「用默认模板」：那会真的往项目里装一份模板，而走查应当可重复。
 * 装成功这件事由 `tests/test_video_template_default.py` 与 `/template/default` 覆盖。
 */
const BASE = "http://localhost:5173/";

/** 让下一次加载里，模板接口一律回答 null。 */
const STUB = `(() => {
  const original = window.fetch;
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : String(input?.url ?? input);
    if (/\\/video\\/template$/.test(url)) {
      return new Response("null", { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return original(input, init);
  };
})();`;

export default async function ({ evaluate, send, shot, wait, out }) {
  await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB });
  await evaluate("location.reload(), 0");
  await wait(3500);

  const opened = await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    const button = panel?.querySelector(".panel-head button");
    if (panel && panel.classList.contains("is-collapsed")) button?.click();
    return panel?.className ?? "no-panel";
  })()`);
  await wait(1200);

  const state = await evaluate(`(() => {
    const box = document.querySelector(".video-panel .video-import");
    if (!box) return null;
    return {
      labels: [...box.querySelectorAll("button")].map((b) => b.textContent.trim()),
      notes: [...box.querySelectorAll(".section-note")].map((p) => p.textContent.trim()),
    };
  })()`);

  if (!state) throw new Error(`没有找到 .video-import（面板状态：${opened}）`);

  const problems = [];
  for (const label of ["用默认模板", "导入模板包…", "导入并编译"]) {
    if (!state.labels.includes(label)) problems.push(`缺按钮「${label}」`);
  }
  // 「用默认模板」不能排第一：那会让第一次用的人以为必须先有 PSD。
  if (state.labels[0] === "用默认模板") problems.push("「用默认模板」不该是第一个按钮");
  if (!state.notes.some((note) => note.includes(".template"))) {
    problems.push("没有说明可以导入 .template");
  }

  console.log(JSON.stringify({ ...state, problems }, null, 2));
  if (problems.length) throw new Error(problems.join("; "));

  await shot(out);
  return state;
}
