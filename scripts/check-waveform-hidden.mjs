// 走查：隐去画成「灰色区域 + 斜线阴影」，两端有边线。
//
// 数据由调用方先用 API 准备好；脚本只截图 + 记录画布位图，供外部做开/关差分。
//
// 为什么不按颜色找区域：画布上还叠着句子带（浅灰）和删除块（红色），三者都会
// 染色，单张图分不开。开/关两张图逐像素差分才可靠。
//
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const zoom = Number(process.env.ZOOM_STEPS || "22");

const script = (steps) => `(() => {
  const canvas = document.querySelector("canvas");
  if (!canvas) return 0;
  const r = canvas.getBoundingClientRect();
  for (let i = 0; i < ${steps}; i += 1) {
    canvas.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true, cancelable: true, deltaY: -120,
      clientX: Math.round(r.left + 4),
      clientY: Math.round(r.top + r.height / 2),
    }));
  }
  return ${steps};
})()`;

const grab = `(() => {
  const canvas = document.querySelector("canvas");
  if (!canvas) return null;
  const ctx = canvas.getContext("2d");
  const { width, height } = canvas;
  const r = canvas.getBoundingClientRect();
  return {
    geo: { left: r.left, top: r.top, width: r.width, height: r.height, deviceWidth: width, deviceHeight: height },
    span: (document.querySelector(".waveform-zoom span") || {}).textContent || null,
    hiddenTokens: document.querySelectorAll(".sentence-list .token.hidden-word").length,
    hint: ((document.querySelector(".waveform-hint") || {}).textContent || "").trim(),
    data: [...ctx.getImageData(0, 0, width, height).data],
  };
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);
  if (zoom > 0) {
    await evaluate(script(zoom));
    await wait(1500);
  }
  const state = await evaluate(grab);
  await shot(out);

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(state), "utf8");
  return { span: state.span, hiddenTokens: state.hiddenTokens };
};
