/**
 * 第 139 轮走查：画布编辑器能真的拖动、存下来，并且「全部恢复默认」能清干净。
 * 第 140 轮改造：它不再是「布局」页签，而是模板页签里「编辑布局…」打开的模态框。
 *
 * 判据（每一条都只能通过真浏览器拿到）：
 *   0. 「布局」页签**不存在**了，模板页签里有一个「编辑布局…」按钮，点开才出画布；
 *   1. 画布上按渲染链的次序叠了底图、底板、描边和四张头像，不是一张烤平的静帧；
 *   2. 用**真鼠标**拖「当期标题」的框，松手后读数是新位置、「已改动」亮起；
 *   3. 松手会落库：重新载入页面、再开一次面板之后牌子还在新位置；
 *   4. 「全部恢复默认」之后回到编译位置、牌子消失；
 *   5. 拖完的布局没有把项目其余状态弄脏（模板仍是 ready）。
 *
 * 用 `mouse()` 而不是 `evaluate("...click()")`：拖动是 pointer 事件序列，合成事件里
 * React 的 `setPointerCapture` 会因为没有真实 pointerId 抛错，把真正的问题盖掉。
 * 打开对话框的点击用合成点击就够——那只是 `setState`，不需要 user activation。
 */
const API = "http://127.0.0.1:8317";
const BASE = "http://localhost:5173/";

/** 展开视频面板（折叠态 class 是 `is-collapsed`，按钮在 `.panel-head` 里）。 */
const openPanel = `(() => {
  const panel = document.querySelector(".video-panel");
  if (panel && panel.classList.contains("is-collapsed")) {
    panel.querySelector(".panel-head button")?.click();
  }
  return 0;
})()`;

/** 点「编辑布局…」，返回没找到还是点到了。 */
const openDialog = `(() => {
  const button = [...document.querySelectorAll(".video-panel button")]
    .find((el) => el.textContent.trim() === "编辑布局…");
  if (!button) return { found: false };
  button.click();
  return { found: true };
})()`;

export default async function ({ evaluate, shot, wait, mouse, out }) {
  const projects = await fetch(`${API}/api/projects`).then((r) => r.json());
  let target = null;
  for (const project of projects.items) {
    const template = await fetch(`${API}/api/projects/${project.id}/video/template`).then((r) =>
      r.json(),
    );
    if (template && template.status === "ready") {
      target = project;
      break;
    }
  }
  if (target === null) throw new Error("没有编译好的模板，走查无从谈起");

  await fetch(`${API}/api/projects/${target.id}/video/layout/reset`, { method: "POST" });

  await evaluate(`location.href = ${JSON.stringify(`${BASE}?project=${target.id}`)}, 0`);
  await wait(3000);
  await evaluate(openPanel);
  await wait(1000);

  const problems = [];

  // 0. 页签没了，按钮在。这两条一起钉住「不是两个入口」。
  const entry = await evaluate(`(() => {
    const labels = [...document.querySelectorAll(".video-panel .tabs .tab")].map((el) =>
      el.textContent.trim(),
    );
    const button = [...document.querySelectorAll(".video-panel button")]
      .find((el) => el.textContent.trim() === "编辑布局…");
    return {
      tabs: labels,
      hasButton: Boolean(button),
      dialogBefore: Boolean(document.querySelector(".modal.layout-modal")),
    };
  })()`);
  if (entry.tabs.includes("布局")) problems.push(`「布局」页签应该已经去掉，实际页签是 ${entry.tabs.join("/")}`);
  if (!entry.hasButton) problems.push("模板页签里找不到「编辑布局…」按钮");
  if (entry.dialogBefore) problems.push("还没点按钮，模态框就已经在了");

  const opened = await evaluate(openDialog);
  if (!opened.found) throw new Error("找不到「编辑布局…」按钮");
  await wait(1500);

  // 1. 画布上的图层
  const layers = await evaluate(`(() => {
    const host = document.querySelector(".layout-modal .layout-host");
    const stage = host?.querySelector(".layout-stage");
    if (!host || !stage) return null;
    const box = stage.getBoundingClientRect();
    const modal = document.querySelector(".modal.layout-modal")?.getBoundingClientRect();
    return {
      pieces: stage.querySelectorAll("img.layout-piece").length,
      hasBackground: Boolean(stage.querySelector("img.layout-background")),
      slots: stage.querySelectorAll(".layout-slot").length,
      boxSlot: Boolean(stage.querySelector(".layout-slot.is-box")),
      textSlots: stage.querySelectorAll(".layout-slot.is-text").length,
      avatarSlots: stage.querySelectorAll(".layout-slot.is-avatar").length,
      stageWidth: Math.round(box.width),
      hostWidth: Math.round(host.getBoundingClientRect().width),
      modalWidth: modal ? Math.round(modal.width) : null,
      inViewport: modal ? modal.left >= 0 && modal.right <= window.innerWidth + 1 : false,
    };
  })()`);
  if (layers === null) throw new Error("模态框里没有画布");
  // 底板 + 描边 + 四个头像 = 6；底图是单独一个 <img>。
  if (layers.pieces !== 6) problems.push(`画布上应有 6 张图（底板/描边/四头像），实际 ${layers.pieces}`);
  if (!layers.hasBackground) problems.push("画布缺底图");
  if (!layers.boxSlot) problems.push("画布缺素材框的框");
  if (layers.textSlots !== 7) problems.push(`文字框应有 7 个（两个 content 槽不参与布局），实际 ${layers.textSlots}`);
  if (layers.avatarSlots !== 4) problems.push(`头像框应有 4 个，实际 ${layers.avatarSlots}`);
  if (layers.slots !== 12) problems.push(`框的总数应为 12，实际 ${layers.slots}`);
  if (!layers.inViewport) problems.push("模态框超出了视口");
  // 比在页签里更宽：对话框 980 px，画布应该明显宽于原来 480 px 栏里的 478。
  if (layers.stageWidth <= 600) problems.push(`画布只有 ${layers.stageWidth} px 宽，对话框没起到「更大」的作用`);

  // 2. 真鼠标拖「当期标题」
  const grab = await evaluate(`(() => {
    const slot = [...document.querySelectorAll(".layout-slot.is-text")]
      .find((el) => el.querySelector(".layout-slot-label")?.textContent === "当期标题");
    if (!slot) return null;
    const r = slot.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })()`);
  if (grab === null) throw new Error("画布上找不到「当期标题」");

  await mouse("mousePressed", grab.x, grab.y);
  await wait(80);
  for (const step of [1, 2, 3, 4, 5, 6]) {
    await mouse("mouseMoved", grab.x - step * 10, grab.y + step * 4);
    await wait(40);
  }
  await mouse("mouseReleased", grab.x - 60, grab.y + 24);
  await wait(1200);

  const afterDrag = await evaluate(`(() => {
    const readout = document.querySelector(".layout-modal .layout-readout")?.textContent ?? "";
    return {
      readout,
      flagged: Boolean(document.querySelector(".layout-modal .layout-flag")),
      selected: document.querySelectorAll(".layout-slot.is-active").length,
    };
  })()`);
  if (!afterDrag.flagged) problems.push("拖动之后没有出现「已改动」");
  if (afterDrag.selected !== 1) problems.push(`拖动之后选中的框应恰好 1 个，实际 ${afterDrag.selected}`);
  if (!/当期标题/.test(afterDrag.readout)) problems.push(`读数没有说是哪一块：${afterDrag.readout}`);

  const saved = await fetch(`${API}/api/projects/${target.id}/video/layout`).then((r) => r.json());
  const moved = saved.changed?.texts?.title;
  if (!moved) problems.push("松手之后库里的 layout 里没有 title");
  else if (moved.x >= 1068) problems.push(`title 没有被拖到左边：x=${moved.x}`);

  // 3. 重新载入、再开一次面板，还是新位置
  await evaluate(`location.reload(), 0`);
  await wait(3500);
  await evaluate(openPanel);
  await wait(1000);
  await evaluate(openDialog);
  await wait(1500);

  const reloaded = await evaluate(`(() => {
    const slot = [...document.querySelectorAll(".layout-slot.is-text")]
      .find((el) => el.querySelector(".layout-slot-label")?.textContent === "当期标题");
    const box = slot?.getBoundingClientRect();
    return {
      flagged: Boolean(document.querySelector(".layout-modal .layout-flag")),
      left: box ? Math.round(box.left) : null,
    };
  })()`);
  if (!reloaded.flagged) problems.push("重新载入之后「已改动」消失——布局没落库，只活在前端内存里");

  // 4. 全部恢复默认
  await evaluate(`(() => {
    const button = [...document.querySelectorAll(".layout-modal .layout-actions button")]
      .find((el) => el.textContent.trim() === "全部恢复默认");
    if (!button) return { found: false };
    button.click();
    return { found: true };
  })()`);
  await wait(1500);

  const cleared = await evaluate(`(() => ({
    flagged: Boolean(document.querySelector(".layout-modal .layout-flag")),
    active: document.querySelectorAll(".layout-slot.is-active").length,
  }))()`);
  if (cleared.flagged) problems.push("「全部恢复默认」之后「已改动」还亮着");

  const finalState = await fetch(`${API}/api/projects/${target.id}/video/layout`).then((r) => r.json());
  if (!finalState.is_default) problems.push("「全部恢复默认」之后库里仍不是默认布局");
  if (Object.keys(finalState.changed ?? {}).length) {
    problems.push(`「全部恢复默认」之后库里还留着 ${JSON.stringify(finalState.changed)}`);
  }

  // 5. 关闭之后对话框消失、预览还在
  await evaluate(`(() => {
    const button = [...document.querySelectorAll(".modal.layout-modal .modal-foot button")]
      .find((el) => el.textContent.trim() === "关闭");
    button?.click();
    return 0;
  })()`);
  await wait(800);
  const closed = await evaluate(`(() => ({
    dialog: Boolean(document.querySelector(".modal.layout-modal")),
    preview: Boolean(document.querySelector(".video-preview-box")),
  }))()`);
  if (closed.dialog) problems.push("点「关闭」之后对话框还在");
  if (!closed.preview) problems.push("关掉对话框之后预览不见了");

  const template = await fetch(`${API}/api/projects/${target.id}/video/template`).then((r) => r.json());
  if (template.status !== "ready") problems.push(`模板被弄成了 ${template.status}`);

  console.log(
    JSON.stringify(
      { entry, layers, afterDrag, moved: moved ?? null, reloaded, cleared, closed, problems },
      null,
      2,
    ),
  );
  if (problems.length) throw new Error(problems.join("; "));

  await shot(out);
  return { layers, afterDrag, moved, reloaded, cleared, closed };
}
