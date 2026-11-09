// 走查：隐去后的灰线。
//
// 数据由**调用方先用 API 准备好**（本脚本不点任何会写库的按钮），脚本只做
// 「截图 + 记录画布几何 + 找出灰线的 device x」，方便外部精确裁剪比对。
//
// 用法（先隐去，再跑，最后用 undo/恢复还原）：
//   POST /api/versions/{V}/hide {"token_start":1,"token_end":8}
//   node scripts/cdp.mjs http://localhost:5173/ .tmp/h-on scripts/check-waveform-hidden.mjs
//   POST /api/versions/{V}/hide {"token_start":1,"token_end":8,"hidden":false}
//
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const zoomLeft = `(() => {
  const canvas = document.querySelector("canvas");
  if (!canvas) return 0;
  const r = canvas.getBoundingClientRect();
  for (let i = 0; i < 22; i += 1) {
    canvas.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true, cancelable: true, deltaY: -120,
      clientX: Math.round(r.left + 4),
      clientY: Math.round(r.top + r.height / 2),
    }));
  }
  return 22;
})()`;

const report = `(() => {
  const canvas = document.querySelector("canvas");
  if (!canvas) return null;
  const ctx = canvas.getContext("2d");
  const { width, height } = canvas;
  const r = canvas.getBoundingClientRect();
  const data = ctx.getImageData(0, 0, width, height).data;

  // 浅灰竖线：三通道接近、亮度在 180..235（远处白底是 248 左右，波形是 71..105）
  const y = Math.floor(height * 0.5);
  const hits = [];
  for (let x = 0; x < width; x += 1) {
    const i = (y * width + x) * 4;
    const [red, green, blue] = [data[i], data[i + 1], data[i + 2]];
    if (Math.abs(red - green) < 14 && Math.abs(green - blue) < 18 && red > 180 && red < 240) {
      hits.push(x);
    }
  }
  return {
    geo: { left: r.left, top: r.top, width: r.width, height: r.height, deviceWidth: width, deviceHeight: height },
    span: (document.querySelector(".waveform-zoom span") || {}).textContent || null,
    hiddenTokens: document.querySelectorAll(".sentence-list .token.hidden-word").length,
    lineHits: hits,
  };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);
  await evaluate(zoomLeft);
  await wait(1500);
  const state = await evaluate(report);
  await shot(out);

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(state, null, 2), "utf8");
  return state;
};
