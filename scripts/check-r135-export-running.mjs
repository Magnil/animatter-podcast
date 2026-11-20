/**
 * 第 135 轮走查（第二段）：进度条与「导出 ⇄ 停止」是真的。
 *
 * 这一段必须真跑，因为它测的是**状态机**：点「导出」之后按钮变成「停止」、进度条出现；
 * 点「停止」之后按钮回到「导出」、进度条写「已停止」且**不是红色**。这些只有在真的有一次
 * 导出在跑的时候才存在。
 *
 * 成本很低：起一次导出、等两秒看到进度条、点停止，然后等它变成 cancelled。
 * 一小时的片子完整编码要 18 分钟，但停止只要几百毫秒——这正是这一轮做的功能。
 */
export default async function ({ evaluate, shot, wait, out }) {
  const log = [];
  const results = [];
  const check = (name, ok) => {
    results.push({ name, ok: Boolean(ok) });
    log.push(`${ok ? "PASS" : "FAIL"}  ${name}`);
  };

  const snapshot = () =>
    evaluate(`
      (() => {
        const dialog = document.querySelector('[role=dialog][aria-label="视频导出"]');
        if (!dialog) return null;
        const bar = dialog.querySelector(".export-progress");
        const fill = dialog.querySelector(".export-progress-fill");
        const text = dialog.querySelector(".export-progress-text");
        const primary = [...dialog.querySelectorAll(".modal-foot button")].pop();
        return {
          button: primary ? primary.textContent.trim() : "",
          buttonDisabled: primary ? primary.disabled : null,
          buttonClass: primary ? primary.className : "",
          hasBar: Boolean(bar),
          tone: bar ? bar.getAttribute("data-tone") : null,
          width: fill ? fill.style.width : null,
          text: text ? text.textContent.trim() : null,
          inputsDisabled: [...dialog.querySelectorAll("input")].every((i) => i.disabled),
        };
      })()
    `);

  // 打开面板
  await wait(500);
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

  const idle = await snapshot();
  check("面板打开时按钮是「导出」", idle && idle.button === "导出");
  check("面板打开时没有进度条", idle && !idle.hasBar);

  // 点导出
  await evaluate(`(() => {
    const dialog = document.querySelector('[role=dialog][aria-label="视频导出"]');
    [...dialog.querySelectorAll(".modal-foot button")].pop().click();
    return true;
  })()`);

  // 等到进度条出现（最慢 25 s：服务端要先决定 worker 数）
  let running = null;
  for (let i = 0; i < 50; i += 1) {
    await wait(500);
    running = await snapshot();
    if (running && running.hasBar) break;
  }
  await shot(out);
  check("点「导出」后出现进度条", running && running.hasBar);
  check("按钮变成「停止」", running && running.button === "停止");
  check("导出中输入框被禁用", running && running.inputsDisabled);
  check("进度文字里有百分比或排队提示", running && /%|排队/.test(running.text || ""));
  check("进度条色调是 work（蓝色系）", running && running.tone === "work");

  // 点停止
  await evaluate(`(() => {
    const dialog = document.querySelector('[role=dialog][aria-label="视频导出"]');
    [...dialog.querySelectorAll(".modal-foot button")].pop().click();
    return true;
  })()`);
  await wait(120);

  const stopping = await snapshot();
  check("点「停止」后按钮写「正在停止…」", stopping && stopping.button.startsWith("正在停止"));
  check("正在停止时按钮被禁用（防重复取消）", stopping && stopping.buttonDisabled === true);

  // 等终态。判据是**按钮回到「导出」**，不能用 tone：「正在停止」也是 warn，
  // 用 tone 判会在点完停止 100 ms 后就把「正在停止…」当成终态（这条走查自己踩过）。
  let final = null;
  for (let i = 0; i < 80; i += 1) {
    await wait(500);
    final = await snapshot();
    if (final && final.button === "导出" && (final.tone === "warn" || final.tone === "bad")) break;
  }
  await shot(out.replace(/\.png$/, "-stopped.png"));
  check("终态进度条是 warn（黄）而不是 bad（红）", final && final.tone === "warn");
  check("进度文字是「已停止」", final && final.text === "已停止");
  check("停止后按钮回到「导出」", final && final.button === "导出");
  check("停止后输入框恢复可编辑", final && final.inputsDisabled === false);

  console.log(log.join("\n"));
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} 通过`);
  if (failed.length > 0) throw new Error(`${failed.length} 项失败：${failed.map((f) => f.name).join("；")}`);
}
