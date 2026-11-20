/**
 * 第 134 轮走查：确认「生成静帧」按钮和它右边那行「输出时间 … · 实时预览（成片 1080p）」
 * 真的从界面上消失了，而**实时预览本身仍然出图**（删的是按钮，不是预览）。
 *
 * 为什么要有这一支：这两样东西都在一个默认收起的面板里，`tsc` 和单测都看不见它们。
 * 「按钮删掉了」和「整个预览都删掉了」在源码 diff 里长得一模一样，只有真的展开面板、
 * 数一数舞台里有没有图层，才能区分。
 *
 * 判据三组：
 *   1. 视频面板里 `生成静帧` 出现 0 次、`输出时间…实时预览` 出现 0 次；
 *   2. `.video-preview-box.is-live` 存在且有子元素；
 *   3. 舞台里画了东西（img 数量 > 0）——背景、素材块、头像、字幕条带都是 img。
 */
export default async function run({ evaluate, shot, wait }) {
  await wait(3500);

  // 展开视频面板：`.video-panel` 就是那个 collapsible，默认 is-collapsed
  const expanded = await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (!panel) return { ok: false, why: "no .video-panel" };
    const wasCollapsed = panel.classList.contains("is-collapsed");
    if (wasCollapsed) panel.querySelector(".panel-head button")?.click();
    return { ok: true, wasCollapsed };
  })()`);
  console.log("expand:", JSON.stringify(expanded));
  await wait(3500);

  const state = await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (!panel) return { found: false };
    const text = panel.innerText;
    const liveBox = panel.querySelector(".video-preview-box.is-live");
    const stage = liveBox ? liveBox.firstElementChild : null;
    return {
      found: true,
      collapsed: panel.classList.contains("is-collapsed"),
      hasStillButton: text.includes("生成静帧"),
      hasStatusLine: /输出时间[\\s\\S]{0,40}实时预览/.test(text),
      hasLiveBox: Boolean(liveBox),
      stageChildCount: stage ? stage.children.length : 0,
      imgCount: stage ? stage.querySelectorAll("img").length : 0,
      previewText: text.replace(/\\n+/g, " | ").slice(0, 220),
    };
  })()`);
  console.log("state:", JSON.stringify(state, null, 1));

  await shot("D:/workspaces/animatter-podcast/.tmp/shots/r134-live-preview-no-button.png");

  const failures = [];
  if (!state.found) failures.push("找不到视频面板");
  if (state.collapsed) failures.push("视频面板没展开");
  if (state.hasStillButton) failures.push("界面上仍然有「生成静帧」");
  if (state.hasStatusLine) failures.push("界面上仍然有「输出时间 … · 实时预览」那行");
  if (!state.hasLiveBox) failures.push("实时预览框不见了（删过头了）");
  if (state.stageChildCount === 0) failures.push("实时舞台上没有任何图层");
  if (state.imgCount === 0) failures.push("舞台里没有任何图片层");

  console.log(failures.length === 0 ? "PASS" : `FAIL: ${failures.join(" / ")}`);
  if (failures.length > 0) throw new Error(failures.join("; "));
}
