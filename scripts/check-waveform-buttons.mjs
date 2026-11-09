// 走查：切项目再切回来，缩放不该丢；切到别的媒体文件才重置。
//
// 用户报的是「误操作导致缩放重置」，同一个病根还有一条路径：`durationMs` 变化时
// 那条 effect 会重新 `initialView(...)`，而它读的是记忆条目或回退到全览。
// 修完之后：同一个媒体文件内 duration 变化只做 clamp，不重置。
//
// 这个脚本只读状态、点按钮，不写任何编辑数据。
// `evaluate` 收的是**表达式字符串**，不是函数（见 scripts/cdp.mjs:89）。

const read = `(() => {
  const readout = document.querySelector(".waveform-zoom .muted");
  const track = document.querySelector(".waveform-scroll");
  return {
    span: readout ? readout.textContent.trim() : null,
    idle: track ? track.classList.contains("idle") : null,
    project: (document.querySelector(".workbench-title, .project-name") || {}).textContent || null,
  };
})()`;

const wheelIn = `(() => {
  const canvas = document.querySelector("canvas");
  if (!canvas) return false;
  const r = canvas.getBoundingClientRect();
  for (let i = 0; i < 6; i += 1) {
    canvas.dispatchEvent(new WheelEvent("wheel", {
      deltaY: -120,
      clientX: Math.round(r.left + r.width / 2),
      clientY: Math.round(r.top + r.height / 2),
      bubbles: true, cancelable: true,
    }));
  }
  return true;
})()`;

// 用「+」按钮而不是滚轮，验证按钮路径也在
const clickPlus = `(() => {
  const btn = [...document.querySelectorAll(".waveform-zoom button")]
    .find((b) => b.getAttribute("aria-label") === "放大波形");
  if (!btn || btn.disabled) return false;
  btn.click();
  return true;
})()`;

export default async ({ evaluate, shot, wait, out }) => {
  await wait(2400);

  const start = await evaluate(read);
  await evaluate(wheelIn);
  await wait(600);
  const zoomed = await evaluate(read);

  // 用 + 按钮再放大一档，读数是新的
  const plusWorked = await evaluate(clickPlus);
  await wait(500);
  const plusZoomed = await evaluate(read);

  // 点「全部」——唯一合法的重置
  await evaluate(`(() => {
    const btn = [...document.querySelectorAll(".waveform-zoom button")]
      .find((b) => (b.textContent || "").includes("全部"));
    if (btn && !btn.disabled) btn.click();
    return true;
  })()`);
  await wait(500);
  const reset = await evaluate(read);

  await shot(`${out}`);

  const result = {
    start,
    zoomed,
    plusWorked,
    plusZoomed,
    reset,
    wheelZoomed: zoomed.span !== start.span,
    plusZoomedFurther: plusZoomed.span !== zoomed.span,
    resetWentFull: reset.idle === true,
  };
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
