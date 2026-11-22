/**
 * 第 138 轮走查：模板包的三条路都能从界面上走到。
 *
 * 判据是**按钮存在且可用**，不是「点了真的下载了」——下载是浏览器的行为，CDP 里看不到
 * `link.click()` 落到了哪个文件。可以看的是文案、禁用状态，以及「没有 PSD 的提示语不再
 * 是唯一出口」这件事。
 *
 * 做法：从 `/api/projects` 里找一个**已经有模板**的项目，用它启动（`?project=`），
 * 然后展开视频面板、核对模板 tab 底部那排按钮。
 */
const BASE = "http://localhost:5173/";

export default async function ({ evaluate, click, shot, wait, out }) {
  const report = [];
  const check = (name, ok, detail) => {
    report.push({ name, ok, detail });
    if (!ok) throw new Error(`${name}${detail ? `: ${detail}` : ""}`);
  };

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
  check("找到一个模板已经就绪的项目", target !== null, JSON.stringify(projects.items.length));

  await evaluate(`location.href = ${JSON.stringify(`${BASE}?project=${target.id}`)}, 0`);
  await wait(3000);

  // 展开视频面板（默认折叠）。
  const opened = await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (!panel) return "no-panel";
    const head = panel.querySelector(".collapsible-head, button, [role=button]");
    if (panel.classList.contains("is-open")) return "already";
    if (head) head.click();
    return "clicked";
  })()`);
  await wait(1500);

  const shell = await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (!panel) return null;
    const labels = [...panel.querySelectorAll("button")].map((b) => b.textContent.trim());
    return {
      open: panel.classList.contains("is-open") || panel.querySelector(".video-ready") !== null,
      labels,
      hasImportPath: panel.querySelector("input[placeholder*='.psd']") !== null,
      note: panel.querySelector(".video-import .section-note")?.textContent?.trim() ?? "",
    };
  })()`);
  check("视频面板展开", shell !== null, opened);
  check("面板里有模板区的按钮", shell !== null && shell.labels.length > 0, JSON.stringify(shell));

  const wanted = ["导出模板包…", "换回默认模板", "导入模板包…"];
  for (const label of wanted) {
    check(`模板区有「${label}」`, shell.labels.includes(label), shell.labels.join(" / "));
  }

  const hidden = await evaluate(`(() => {
    const inputs = [...document.querySelectorAll(".video-panel input[type=file]")];
    return inputs.map((input) => ({ accept: input.accept, hidden: input.hidden }));
  })()`);
  check(
    "文件选择器接受 .template",
    hidden.some((item) => item.accept.includes(".template")),
    JSON.stringify(hidden),
  );

  await shot(out);
  return report;
}
