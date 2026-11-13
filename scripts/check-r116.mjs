/**
 * 第 116 轮走查：M 和 S 里**恰好一个亮**，且默认是 M。
 *
 * 复现的那一块就在演示项目里：用户自己在「开场片头.mp4」上按了 S，库里现在是
 * `solo=true, muted=false`。修之前那个块上 M 和 S 同时高亮（M 的高亮用的是反向条件），
 * 这条脚本读 `classList` 和 `aria-pressed` 两样，断言它们**一致**且**只有一个亮**。
 *
 * 然后走一遍完整循环：按 M → 只有 M 亮；按 S → 只有 S 亮；再按 S → 回到 M（不是两个都不亮）。
 * 结束时把块恢复成进入脚本时的样子。
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
      id: item.id, label: item.label, has_audio: item.has_audio,
      muted: item.muted, solo: item.solo, start_ms: item.start_ms,
    }));
  })()`);
  const target = items.find((item) => item.has_audio);
  if (!target) {
    note("这条时间轴上没有带声音的块");
    await shot(out);
    throw new Error("没有带声音的块");
  }
  console.log(`target: ${target.label} muted=${target.muted} solo=${target.solo}`);

  const openBubble = async () => {
    const index = items.findIndex((item) => item.id === target.id);
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
    await wait(450);
  };

  /** Both letters' highlight and `aria-pressed`, plus the server's view of the block. */
  const snapshot = async () =>
    text(`(async () => {
      const bubble = document.querySelector(".video-bubble");
      if (!bubble) return null;
      const letters = {};
      for (const node of bubble.querySelectorAll("button")) {
        const letter = node.textContent.trim();
        if (letter !== "M" && letter !== "S") continue;
        letters[letter] = {
          lit: node.classList.contains("is-on"),
          pressed: node.getAttribute("aria-pressed"),
        };
      }
      const body = await (await fetch("/api/versions/${VERSION}/video/timeline")).json();
      const row = body.items.find((item) => item.id === "${target.id}");
      return { letters, muted: row.muted, solo: row.solo };
    })()`);

  const check = (label, snapshotState) => {
    if (snapshotState === null) {
      note(`${label}: 气泡没打开`);
      return;
    }
    const { letters, muted, solo } = snapshotState;
    console.log(`${label}: M lit=${letters.M?.lit} pressed=${letters.M?.pressed} | ` +
      `S lit=${letters.S?.lit} pressed=${letters.S?.pressed} | server muted=${muted} solo=${solo}`);
    // Highlight and aria-pressed must agree — they diverged before this fix, so a screen
    // reader and a glance told different stories.
    if (String(letters.M?.lit) !== letters.M?.pressed) {
      note(`${label}: M 的高亮(${letters.M?.lit})和 aria-pressed(${letters.M?.pressed})不一致`);
    }
    if (String(letters.S?.lit) !== letters.S?.pressed) {
      note(`${label}: S 的高亮(${letters.S?.lit})和 aria-pressed(${letters.S?.pressed})不一致`);
    }
    const lit = [letters.M?.lit, letters.S?.lit].filter(Boolean).length;
    if (lit !== 1) note(`${label}: 应该恰好一个亮，实际 ${lit} 个`);
    if (String(muted) !== letters.M?.pressed) {
      note(`${label}: 界面上的 M 和接口的 muted 不一致（${letters.M?.pressed} vs ${muted}）`);
    }
    if (String(solo) !== letters.S?.pressed) {
      note(`${label}: 界面上的 S 和接口的 solo 不一致（${letters.S?.pressed} vs ${solo}）`);
    }
    if (muted && solo) note(`${label}: 服务端存了 muted && solo`);
    if (!muted && !solo) note(`${label}: 服务端存了两个都不亮`);
  };

  const clickLetter = async (letter) => {
    await text(`(() => {
      const bubble = document.querySelector(".video-bubble");
      [...bubble.querySelectorAll("button")].find((b) => b.textContent.trim() === "${letter}")?.click();
      return true;
    })()`);
    // Poll the rendered state: a fixed sleep is not enough because the patch is followed by a
    // full timeline reload.
    let previous = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await wait(150);
      const current = await snapshot();
      if (String(current?.letters.M?.pressed) !== null && current) {
        const now = `${current.letters.M?.pressed}${current.letters.S?.pressed}`;
        if (previous !== null && now === previous) return current;
        previous = now;
      }
    }
    return snapshot();
  };

  try {
    await openBubble();
    // The user's own click left this block on S, so this is the live reproduction.
    check("进入时", await snapshot());

    await clickLetter("M");
    const afterM = await snapshot();
    check("按 M 之后", afterM);
    if (afterM?.letters.M?.pressed !== "true") note("按 M 之后 M 应该是亮的");

    await clickLetter("S");
    const afterS = await snapshot();
    check("按 S 之后", afterS);
    if (afterS?.letters.S?.pressed !== "true") note("按 S 之后 S 应该是亮的");
    if (afterS?.letters.M?.pressed !== "false") note("按 S 之后 M 不应该还亮着");

    // Pressing S again must land on M, not on "neither".
    await clickLetter("S");
    const afterS2 = await snapshot();
    check("再按一次 S 之后", afterS2);
    if (afterS2?.letters.M?.pressed !== "true") note("再按一次 S 之后应该回到 M");

    // M is a destination, not a toggle: pressing it while it is already lit must not move
    // anything. If it did, the request would be `muted:false` and the block would land on
    // the one state the bubble cannot draw.
    await clickLetter("M");
    const afterM2 = await snapshot();
    check("在 M 上再按一次 M", afterM2);
    if (afterM2?.letters.M?.pressed !== "true" || afterM2?.letters.S?.pressed !== "false") {
      note("在 M 上再按 M 应该是原地不动");
    }
  } finally {
    const back = target.solo
      ? { solo: true }
      : { muted: true, solo: false };
    await text(`(async () => {
      await fetch("/api/video/items/${target.id}", {
        method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify(${JSON.stringify(back)}),
      });
      return true;
    })()`);
    await wait(500);
    const state = await text(`(async () => {
      const body = await (await fetch("/api/versions/${VERSION}/video/timeline")).json();
      const row = body.items.find((item) => item.id === "${target.id}");
      return { muted: row.muted, solo: row.solo };
    })()`);
    console.log("restored:", JSON.stringify(state));
    if (state.muted !== target.muted || state.solo !== target.solo) {
      note(`没有还原：${JSON.stringify(target)} -> ${JSON.stringify(state)}`);
    }
  }

  await shot(out);
  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: M 和 S 恰好一个亮，高亮与 aria-pressed 一致，按 S 两次回到 M");
}
