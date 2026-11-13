/**
 * 第 115 轮走查：全屏改变画面，且缓存键跟着变；气泡没有因为这一轮而回归。
 *
 * 这一轮改的是**层序**，而层序在浏览器里看不出来（静帧是服务端合成的）。所以脚本的能量集中在
 * 两件能在界面里量到的事：
 *
 * 1. 把某个块改成全屏，断言静帧预览的 URL **变了**——不变就是「点了没反应」那类 bug，因为
 *    静帧的缓存键里现在带着 `placement`；再把两张图取回来逐字节比，URL 变了但图没变同样是错的。
 * 2. 气泡仍然是上一轮的形状：两个**不带文字标签**的下拉、M 和 S 都在。
 *
 * 层序本身的证据在 `.tmp/l1_order_proof.py`（真 ffmpeg，同一张图里同时摆头像和文字），
 * 因为头像要绑定到说话人才有画面，这个演示项目的模板里一个都没绑。
 */

export default async function ({ evaluate, shot, wait, out, send }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };
  const text = (expression) => evaluate(expression);
  const VERSION = "ver_1m6v2q5javqm1j82xsg";

  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return true;
  })()`);
  await wait(900);

  const items = await text(`(async () => {
    const body = await (await fetch("/api/versions/${VERSION}/video/timeline")).json();
    return body.items.map((item) => ({
      id: item.id, label: item.label, start_ms: item.start_ms, end_ms: item.end_ms,
      has_audio: item.has_audio, muted: item.muted, solo: item.solo, placement: item.placement,
    }));
  })()`);
  const blocks = await text(`document.querySelectorAll(".video-block").length`);
  console.log(`blocks: ${items.length} (DOM ${blocks})`);
  if (blocks !== items.length) note(`轴上画了 ${blocks} 个块，接口有 ${items.length} 个`);

  const target = items.find((item) => item.has_audio);
  if (!target) {
    note("这条时间轴上没有带画面的块");
  } else {
    const at = Math.round((target.start_ms + target.end_ms) / 2);
    const setPlacement = async (value) =>
      text(`(async () => {
        const response = await fetch("/api/video/items/${target.id}", {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ placement: ${JSON.stringify(value)} }),
        });
        return response.ok ? "ok" : "HTTP " + response.status;
      })()`);
    const stillUrl = async () =>
      text(`(async () => {
        const response = await fetch("/api/versions/${VERSION}/video/preview?at_ms=${at}");
        // Reading the response's url off an error payload yields undefined, which looks exactly
        // like "the cache key did not change" — hence the explicit status check. (No backticks
        // in this comment: it lives inside a template literal.)
        if (!response.ok) return "HTTP " + response.status + " " + (await response.text()).slice(0, 160);
        return (await response.json()).url;
      })()`);

    try {
      // Start from 「素材框」 regardless of what a previous run left behind: if the block were
      // already 全屏, the patch below would change nothing and the two stills would match.
      console.log("reset to box:", await setPlacement("box"));
      await wait(600);

      const before = await stillUrl();
      console.log("still before:", before);

      console.log("set to full:", await setPlacement("full"));
      await wait(800);
      const after = await stillUrl();
      console.log("still after: ", after);

      if (before === after) {
        note("改成全屏之后静帧的 URL 没变——缓存键没有带上 placement");
      }
      const differs = await text(`(async () => {
        const PROJECT = "prj_1m6v2m8anfpyqq3w8qt";
        const load = async (url) => {
          // The file endpoint requires the project. Without it this is a 422 and the
          // comparison below would be comparing two identical error bodies.
          const response = await fetch(url + "?project_id=" + PROJECT);
          if (!response.ok) return null;
          return new Uint8Array(await response.arrayBuffer());
        };
        const a = await load(${JSON.stringify(before)});
        const b = await load(${JSON.stringify(after)});
        if (a === null || b === null) return "missing";
        if (a.length !== b.length) return true;
        for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return true;
        return false;
      })()`);
      console.log("stills differ:", differs);
      if (differs === "missing") note("取不到静帧文件（状态码不是 200）");
      else if (!differs) note("两张静帧字节完全一样——全屏没有改变画面");
    } finally {
      // In `finally`: a failed assertion above must not leave the project with a full-frame
      // block, which is exactly how the previous run corrupted the next one.
      console.log("restored:", await setPlacement(target.placement));
    }
  }

  const index = items.findIndex((item) => item.has_audio);
  const where = await text(`(() => {
    const node = document.querySelectorAll(".video-block")[${index}];
    const track = document.querySelector(".video-track");
    const r = node.getBoundingClientRect();
    const t = track.getBoundingClientRect();
    const left = Math.max(r.left, t.left + 2);
    const right = Math.min(r.right, t.right - 2);
    return { x: left + Math.min((right - left) / 2, 60), y: r.top + r.height / 2 };
  })()`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type, x: where.x, y: where.y, button: "left", clickCount: 1 });
  }
  await wait(500);

  const bubble = await text(`(() => {
    const node = document.querySelector(".video-bubble");
    if (!node) return null;
    return {
      selects: [...node.querySelectorAll("select")].map((s) => ({
        options: [...s.options].map((o) => o.textContent.trim()),
        label: s.closest("label")?.querySelector("span")?.textContent.trim() ?? null,
      })),
      letters: [...node.querySelectorAll("button")].map((b) => b.textContent.trim()),
    };
  })()`);
  console.log("bubble:", JSON.stringify(bubble));
  if (bubble === null) {
    note("点不到气泡");
  } else {
    if (bubble.selects.length !== 2) note(`气泡里应该有两个下拉，实际 ${bubble.selects.length}`);
    if (bubble.selects[0]?.options.join("/") !== "素材框/全屏") {
      note(`第一个下拉应该是 素材框/全屏，实际 ${JSON.stringify(bubble.selects[0]?.options)}`);
    }
    for (const select of bubble.selects) {
      if (select.label !== null) note(`下拉前面不该有文字标签，实际有「${select.label}」`);
    }
    if (!bubble.letters.includes("M") || !bubble.letters.includes("S")) {
      note(`字母按钮不全：${JSON.stringify(bubble.letters)}`);
    }
  }

  await shot(out);
  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: 全屏改变静帧且缓存键跟着变；气泡仍是两个无标签下拉 + M/S");
}
