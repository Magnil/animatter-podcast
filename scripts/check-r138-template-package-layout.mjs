/**
 * 第 138 轮走查：模板包按钮那一排在真实布局里排得下、点得到。
 *
 * 视频面板的设置列只有 320px 宽，三个 ghost 按钮放在一行会挤成两行甚至被面板底部裁掉。
 * 判据是：三个按钮都在视口内、互不重叠、宽度都够读出文字。
 */
const BASE = "http://localhost:5173/";

export default async function ({ evaluate, shot, wait, out }) {
  const projects = await fetch("http://127.0.0.1:8317/api/projects").then((r) => r.json());
  let target = null;
  for (const project of projects.items) {
    const template = await fetch(
      `http://127.0.0.1:8317/api/projects/${project.id}/video/template`,
    ).then((r) => r.json());
    if (template && template.status === "ready") {
      target = project;
      break;
    }
  }

  await evaluate(`location.href = ${JSON.stringify(`${BASE}?project=${target.id}`)}, 0`);
  await wait(3000);
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    const head = panel?.querySelector(".collapsible-head, button");
    if (panel && !panel.classList.contains("is-open")) head?.click();
    return 0;
  })()`);
  await wait(1200);

  // 滚到设置列底部，那里是模板包那一排。
  await evaluate(`(() => {
    const body = document.querySelector(".video-panel .video-settings-body");
    if (body) body.scrollTop = body.scrollHeight;
    return 0;
  })()`);
  await wait(400);

  const geometry = await evaluate(`(() => {
    const wanted = ["导出模板包…", "换回默认模板", "导入模板包…"];
    const found = {};
    for (const button of document.querySelectorAll(".video-panel button")) {
      const label = button.textContent.trim();
      if (!wanted.includes(label)) continue;
      const r = button.getBoundingClientRect();
      found[label] = {
        x: Math.round(r.x),
        y: Math.round(r.y),
        w: Math.round(r.width),
        h: Math.round(r.height),
        inView: r.bottom > 0 && r.top < window.innerHeight,
        disabled: button.disabled,
      };
    }
    return { found, missing: wanted.filter((w) => !(w in found)) };
  })()`);

  const problems = [];
  if (geometry.missing.length) problems.push(`缺按钮: ${geometry.missing.join(", ")}`);
  const boxes = Object.entries(geometry.found);
  for (const [label, box] of boxes) {
    if (!box.inView) problems.push(`${label} 在视口外`);
    if (box.w < 70) problems.push(`${label} 太窄 (${box.w}px)`);
    if (box.disabled) problems.push(`${label} 是禁用的`);
  }
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const [aLabel, a] = boxes[i];
      const [bLabel, b] = boxes[j];
      const overlap =
        a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
      if (overlap) problems.push(`${aLabel} 与 ${bLabel} 重叠`);
    }
  }

  console.log(JSON.stringify({ geometry: geometry.found, problems }, null, 2));
  if (problems.length) throw new Error(problems.join("; "));

  await shot(out);
  return geometry.found;
}
