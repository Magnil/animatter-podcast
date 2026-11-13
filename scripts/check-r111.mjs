/**
 * Round 111 walkthrough: the S button and the placement menu, driven through the real UI.
 *
 * **Writes, then restores.** Unlike the last two rounds this one has to click, because both
 * features only exist in the bubble. Every change is reverted at the end from an `evaluate`-
 * captured snapshot of the block's original flags, so the project is left exactly as it was.
 *
 * Facts asserted:
 *   1. A block with sound shows **both** M and S; a still/voiceless block shows neither.
 *   2. S starts unpressed; pressing it stores `solo=true, muted=false` — the server's exclusive
 *      rule is visible from the UI, not just in the API tests.
 *   3. Pressing M afterwards clears solo.
 *   4. The placement select lists exactly 素材框 / 全屏, defaults to 素材框, is the **first** select
 *      in the bubble, and neither select has a text label in front of it.
 *   5. Picking 全屏 stores `placement=full`.
 */

export default async function ({ evaluate, shot, wait, out, send }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };
  const text = (expression) => evaluate(expression);

  // The video panel remembers being collapsed, and every selector below lives inside it.
  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return true;
  })()`);
  await wait(900);

  const items = await text(`(async () => {
    const body = await (await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline")).json();
    return body.items.map((item) => ({
      id: item.id, label: item.label, has_audio: item.has_audio,
      muted: item.muted, solo: item.solo, placement: item.placement,
      start: item.start_ms, end: item.end_ms,
    }));
  })()`);
  const blocks = await text(`document.querySelectorAll(".video-block").length`);
  console.log(`.video-block in the DOM: ${blocks}`);
  if (blocks !== items.length) {
    note(`时间轴上画出了 ${blocks} 个块，接口里有 ${items.length} 个——面板可能还是收起的`);
  }

  const audible = items.filter((item) => item.has_audio);
  const silent = items.filter((item) => !item.has_audio);
  console.log(`blocks: ${items.length}, with sound: ${audible.length}, without: ${silent.length}`);
  if (audible.length === 0 || silent.length === 0) {
    note("这条时间轴上需要同时有「有声音」和「没声音」的块，否则两种按钮状态测不全");
  }

  /** Click the block at `index` after zooming to 全部, then read the bubble. */
  const openBubble = async (index) => {
    await text(`(() => {
      [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "全部")?.click();
      return true;
    })()`);
    await wait(350);
    const where = await text(`(() => {
      const node = document.querySelectorAll(".video-block")[${index}];
      const track = document.querySelector(".video-track");
      if (!node || !track) return null;
      const r = node.getBoundingClientRect();
      const t = track.getBoundingClientRect();
      const left = Math.max(r.left, t.left + 2);
      const right = Math.min(r.right, t.right - 2);
      if (right - left < 4) return null;
      return { x: left + Math.min((right - left) / 2, 60), y: r.top + r.height / 2 };
    })()`);
    if (where === null) return null;
    for (const type of ["mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", { type, x: where.x, y: where.y, button: "left", clickCount: 1 });
    }
    await wait(450);
    return readBubble();
  };

  const readBubble = async () =>
    text(`(() => {
      const bubble = document.querySelector(".video-bubble");
      if (!bubble) return null;
      const buttons = [...bubble.querySelectorAll("button")].map((node) => ({
        text: node.textContent.trim(),
        pressed: node.getAttribute("aria-pressed"),
      }));
      const selects = [...bubble.querySelectorAll("select")].map((node) => ({
        value: node.value,
        options: [...node.options].map((option) => option.textContent.trim()),
        label: node.closest("label")?.querySelector("span")?.textContent.trim() ?? null,
      }));
      return { name: bubble.querySelector(".video-bubble-name")?.textContent.trim() ?? "", buttons, selects };
    })()`);

  /**
   * Click a letter, then wait for the **rendered** state to change.
   *
   * A fixed sleep is not enough: the first run read the bubble 700 ms after the S click and
   * still saw the old state, so the following M click used a stale item and sent
   * `muted=false` — a no-op under the exclusivity rule — and the script reported a feature bug
   * that was really its own race. Polling what the browser actually drew is the honest wait.
   */
  const clickLetter = async (letter, until) => {
    await text(`(() => {
      const bubble = document.querySelector(".video-bubble");
      const node = [...bubble.querySelectorAll("button")].find((b) => b.textContent.trim() === "${letter}");
      node?.click();
      return true;
    })()`);
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await wait(150);
      const current = await readBubble();
      const pressed = current?.buttons.find((b) => b.text === letter)?.pressed;
      if (pressed === until) return current;
    }
    return readBubble();
  };

  const readState = async (id) =>
    text(`(async () => {
      const body = await (await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline")).json();
      const found = body.items.find((item) => item.id === "${id}");
      return found ? { muted: found.muted, solo: found.solo, placement: found.placement } : null;
    })()`);

  // --- 1. the buttons exist only where they can do something ---------------
  const silentIndex = items.indexOf(silent[0]);
  const silentBubble = await openBubble(silentIndex);
  if (silentBubble === null) {
    note("点不到没有声音的块");
  } else {
    console.log("silent block bubble:", JSON.stringify(silentBubble));
    const letters = silentBubble.buttons.map((b) => b.text).filter((t) => t === "M" || t === "S");
    if (letters.length !== 0) {
      note(`没有声音的块上不该出现 ${letters.join("/")}`);
    }
  }

  if (audible.length > 0) {
    const index = items.indexOf(audible[0]);
    const before = await readState(audible[0].id);
    console.log("audible block before:", JSON.stringify(before));
    const bubble = await openBubble(index);
    if (bubble === null) {
      note("点不到有声音的块");
    } else {
      console.log("audible block bubble:", JSON.stringify(bubble));
      const labels = bubble.buttons.map((b) => b.text);
      for (const letter of ["M", "S"]) {
        if (!labels.includes(letter)) note(`气泡里缺少 ${letter} 按钮：${JSON.stringify(labels)}`);
      }

      // --- 2/3/4: the selects and the exclusivity -------------------------
      if (bubble.selects.length !== 2) {
        note(`气泡里应该有两个下拉（展现方式 + 填充方式），实际 ${bubble.selects.length} 个`);
      } else {
        const [placement, fit] = bubble.selects;
        if (JSON.stringify(placement.options) !== JSON.stringify(["素材框", "全屏"])) {
          note(`第一个下拉应该是 素材框/全屏，实际 ${JSON.stringify(placement.options)}`);
        }
        if (placement.value !== (before.placement || "box")) {
          note(`展现方式初值应该反映当前设置：${placement.value} vs ${before.placement}`);
        }
        if (placement.label !== null) {
          note(`展现方式的 select 前面不该有文字标签，实际有「${placement.label}」`);
        }
        if (fit.label !== null) {
          note(`填充方式的 select 前面不该有文字标签，实际有「${fit.label}」`);
        }
      }

      // Press S: solo on, muted cleared, by the server's own rule.
      const afterS = await clickLetter("S", "true");
      const stateS = await readState(audible[0].id);
      console.log("after S:", JSON.stringify(stateS));
      const pressedS = afterS?.buttons.find((b) => b.text === "S")?.pressed;
      if (stateS === null || stateS.solo !== true) {
        note(`按 S 之后应该 solo=true，实际 ${JSON.stringify(stateS)}`);
      }
      if (stateS !== null && stateS.muted !== false) {
        note(`按 S 之后 muted 应该被清掉，实际 ${stateS.muted}`);
      }
      if (pressedS !== "true") {
        note(`S 按钮按下后 aria-pressed 应该是 "true"，实际 ${pressedS}`);
      }

      // Press M: solo cleared.
      const afterM = await clickLetter("M", "true");
      const stateM = await readState(audible[0].id);
      console.log("after M:", JSON.stringify(stateM));
      const pressedM = afterM?.buttons.find((b) => b.text === "M")?.pressed;
      if (stateM === null || stateM.solo !== false) {
        note(`按 M 之后应该 solo=false，实际 ${JSON.stringify(stateM)}`);
      }
      if (stateM !== null && stateM.muted !== true) {
        note(`按 M 之后 muted 应该是 true，实际 ${stateM.muted}`);
      }
      if (pressedM !== "true") {
        note(`M 按钮按下后 aria-pressed 应该是 "true"，实际 ${pressedM}`);
      }

      // --- 5. 全屏 --------------------------------------------------------
      await text(`(() => {
        const select = document.querySelector(".video-bubble select");
        const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
        setter.call(select, "full");
        select.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      })()`);
      await wait(800);
      const stateFull = await readState(audible[0].id);
      console.log("after 全屏:", JSON.stringify(stateFull));
      if (stateFull === null || stateFull.placement !== "full") {
        note(`选 全屏 之后应该 placement=full，实际 ${JSON.stringify(stateFull)}`);
      }

      // Restore everything this script touched.
      if (before !== null) {
        await text(`(async () => {
          await fetch("/api/video/items/${audible[0].id}", {
            method: "PATCH",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              placement: ${JSON.stringify(before.placement)},
              solo: ${JSON.stringify(before.solo)},
              muted: ${JSON.stringify(before.muted)},
            }),
          });
          return true;
        })()`);
        await wait(600);
        const restored = await readState(audible[0].id);
        console.log("restored:", JSON.stringify(restored));
        if (JSON.stringify(restored) !== JSON.stringify(before)) {
          note(`没有还原：${JSON.stringify(before)} -> ${JSON.stringify(restored)}`);
        }
      }
    }
  }

  await shot(out);
  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: S 只出现在有声音的块上，与 M 互斥，展现方式两个选项可用且没有文字标签");
}
