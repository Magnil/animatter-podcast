/**
 * Click 导出成片 in the real UI and watch it actually produce a player.
 *
 * The reported symptom was 「点击后无反应」. Asserting that a click yields a
 * visible result is the only check that would have caught it: the endpoint was
 * fine, the polling was what was missing.
 *
 * Because this project already has a finished render for the current edit, the
 * click should resolve through the cache and finish quickly — which is also the
 * behaviour a second export must have, since re-encoding 105 minutes to hand back
 * an identical file would be indefensible.
 */
export default async function run({ evaluate, shot, wait }) {
  await wait(3000);

  const before = await evaluate(`(() => {
    const buttons = Array.from(document.querySelectorAll(".transport button"));
    const render = buttons.find((b) => b.textContent.includes("导出成片"));
    return {
      label: render ? render.textContent.trim() : null,
      disabled: render ? render.disabled : null,
      hasPlayer: Boolean(document.querySelector(".output-player")),
    };
  })()`);
  console.log("before:", JSON.stringify(before));

  const clicked = await evaluate(`(() => {
    const buttons = Array.from(document.querySelectorAll(".transport button"));
    const render = buttons.find((b) => b.textContent.includes("导出成片"));
    if (!render) return { ok: false };
    render.click();
    return { ok: true };
  })()`);
  console.log("clicked:", JSON.stringify(clicked));

  // Poll the DOM for the outcome rather than sleeping a fixed amount: a cache hit
  // lands in well under a second, and a real encode would take minutes.
  let observed = null;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await wait(500);
    observed = await evaluate(`(() => {
      const player = document.querySelector(".output-player audio");
      const notice = document.querySelector(".notice");
      const buttons = Array.from(document.querySelectorAll(".transport button"));
      const render = buttons.find((b) => b.textContent.includes("导出成片"));
      return {
        hasPlayer: Boolean(player),
        src: player ? player.getAttribute("src") : null,
        notice: notice ? notice.textContent.trim() : null,
        buttonLabel: render ? render.textContent.trim() : null,
      };
    })()`);
    if (observed.hasPlayer && observed.src) break;
  }
  console.log("after:", JSON.stringify(observed));

  await shot("D:/workspaces/animatter-podcast/.tmp/shots/export-done.png");

  const failures = [];
  if (!clicked.ok) failures.push("找不到导出成片按钮");
  if (before.disabled) failures.push("导出成片按钮是禁用的");
  if (!observed?.hasPlayer) failures.push("点击后没有出现成片播放器");
  if (!observed?.src?.includes("/api/renders/")) failures.push(`播放器地址不对：${observed?.src}`);

  console.log(failures.length === 0 ? "PASS" : `FAIL: ${failures.join(" / ")}`);
  if (failures.length > 0) throw new Error(failures.join("; "));
}
