// 走查：波形缩放的「入口」只有一个方向 —— 滚轮 + 下方三个按钮。
//
// 用户报的问题是：连点进度条 / 误点滚动条会让缩放重置成「全部」。原因是自绘滚动条
// 上挂了 onDoubleClick = 回到全览，而连点进度条的第二下正好落在滚动条上，变成双击。
//
// 这个脚本重放那条路径：放大 → 双击滚动条 → 读缩放读数，断言没变；再点一下轨道
// （只平移，不该改跨度）；最后点「全部」，断言确实回到全览（那是唯一合法的重置）。
//
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const readZoom = `(() => {
  const canvas = document.querySelector("canvas");
  const thumb = document.querySelector(".waveform-thumb");
  const readout = document.querySelector(".waveform-zoom .muted");
  const track = document.querySelector(".waveform-scroll");
  const buttons = [...document.querySelectorAll(".waveform-zoom button")].map((b) => ({
    label: (b.textContent || "").trim(),
    disabled: b.disabled,
  }));
  return {
    span: readout ? readout.textContent.trim() : null,
    thumbWidth: thumb ? thumb.style.width : null,
    thumbLeft: thumb ? thumb.style.left : null,
    idle: track ? track.classList.contains("idle") : null,
    buttons,
    canvasWidth: canvas ? Math.round(canvas.getBoundingClientRect().width) : null,
  };
})()`;

const wheelZoomIn = (times) => `(() => {
  const canvas = document.querySelector("canvas");
  const r = canvas.getBoundingClientRect();
  for (let i = 0; i < ${times}; i += 1) {
    canvas.dispatchEvent(new WheelEvent("wheel", {
      deltaY: -120,
      clientX: Math.round(r.left + r.width / 2),
      clientY: Math.round(r.top + r.height / 2),
      bubbles: true, cancelable: true,
    }));
  }
  return true;
})()`;

// 在滚动条上真发两次 click（浏览器会合成 dblclick），走的是真实的事件路径。
const doubleClickScrollbar = `(() => {
  const track = document.querySelector(".waveform-scroll");
  const r = track.getBoundingClientRect();
  const opts = {
    bubbles: true, cancelable: true, view: window, detail: 1,
    clientX: Math.round(r.left + r.width * 0.62),
    clientY: Math.round(r.top + r.height / 2),
  };
  const second = { ...opts, detail: 2 };
  track.dispatchEvent(new MouseEvent("mousedown", opts));
  track.dispatchEvent(new MouseEvent("mouseup", opts));
  track.dispatchEvent(new MouseEvent("click", opts));
  track.dispatchEvent(new MouseEvent("mousedown", second));
  track.dispatchEvent(new MouseEvent("mouseup", second));
  track.dispatchEvent(new MouseEvent("click", second));
  track.dispatchEvent(new MouseEvent("dblclick", second));
  return true;
})()`;

// 单击轨道空白处：只该平移，不该改跨度。
const clickTrack = `(() => {
  const track = document.querySelector(".waveform-scroll");
  const r = track.getBoundingClientRect();
  const opts = {
    bubbles: true, cancelable: true, view: window, detail: 1, pointerId: 1,
    clientX: Math.round(r.left + r.width * 0.35),
    clientY: Math.round(r.top + r.height / 2),
  };
  track.setPointerCapture = () => undefined;
  track.releasePointerCapture = () => undefined;
  track.hasPointerCapture = () => false;
  track.dispatchEvent(new PointerEvent("pointerdown", opts));
  track.dispatchEvent(new PointerEvent("pointerup", opts));
  return true;
})()`;

// 画布聚焦后按 0 / + / - / Home / End，都不该改跨度。
const pressKeys = `(async () => {
  const canvas = document.querySelector("canvas");
  canvas.focus();
  const results = {};
  for (const key of ["0", "+", "-"]) {
    const before = document.querySelector(".waveform-zoom .muted").textContent.trim();
    canvas.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 120));
    const after = document.querySelector(".waveform-zoom .muted").textContent.trim();
    results[key] = { before, after, changed: before !== after };
  }
  return results;
})()`;

const clickReset = `(() => {
  const all = [...document.querySelectorAll(".waveform-zoom button")];
  const reset = all.find((b) => (b.textContent || "").includes("全部"));
  if (!reset) return false;
  reset.click();
  return true;
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);

  const initial = await evaluate(readZoom);

  await evaluate(wheelZoomIn(6));
  await wait(600);
  const zoomed = await evaluate(readZoom);
  await shot(`${out}-zoomed`);

  await evaluate(doubleClickScrollbar);
  await wait(800);
  const afterDouble = await evaluate(readZoom);
  await shot(`${out}-after-dblclick`);

  await evaluate(clickTrack);
  await wait(600);
  const afterClick = await evaluate(readZoom);

  const keys = await evaluate(pressKeys);
  const afterKeys = await evaluate(readZoom);

  await evaluate(clickReset);
  await wait(600);
  const afterReset = await evaluate(readZoom);
  await shot(`${out}-after-reset`);

  const result = {
    initial,
    zoomed,
    afterDouble,
    afterClick,
    keys,
    afterKeys,
    afterReset,
    // 核心断言：双击滚动条没有把跨度变回全览
    dblclickKeptZoom: afterDouble.span === zoomed.span,
    clickKeptZoom: afterClick.span === zoomed.span,
    keysKeptZoom: ["0", "+", "-"].every((k) => keys[k].changed === false),
    resetRestoredFull: afterReset.idle === true && afterReset.buttons.every((b) => b.disabled || b.label !== "全部"),
  };

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
