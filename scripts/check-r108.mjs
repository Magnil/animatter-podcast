/**
 * The click landed on the neighbouring block: the point was computed as `r.left + 60`, and a
 * block whose left edge had scrolled off the track put that point inside the block before it.
 * Clamp to the *visible* part of the block, and verify the selection instead of assuming it.
 */

export default async function ({ evaluate, shot, wait, out, send }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };
  const text = (expression) => evaluate(expression);

  const real = await text(`(async () => {
    const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
    const body = await response.json();
    return body.items.map((item) => ({
      id: item.id, kind: item.kind, label: item.label,
      start: item.start_ms, end: item.end_ms,
      muted: item.muted, has_audio: item.has_audio,
    }));
  })()`);
  console.log("blocks:", real.length, "with sound:", real.filter((item) => item.has_audio).length);

  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return true;
  })()`);
  await wait(1000);

  // --- 3 first: the hint line is pure text, no clicking needed -------------
  const hint = await text(`(() => {
    const node = document.querySelector(".video-track-hint");
    return node ? node.textContent.replace(/\\s+/g, " ").trim() : null;
  })()`);
  console.log("hint:", JSON.stringify(hint));
  if (hint === null) {
    note("轨道下面没有说明行");
  } else {
    if (/拖块两端|拖块身|滚轮缩放/.test(hint)) {
      note(`说明行还在讲拖动和缩放：${JSON.stringify(hint)}`);
    }
    if (!/Ctrl/.test(hint) || !/Shift/.test(hint)) {
      note(`说明行没讲两个快捷键：${JSON.stringify(hint)}`);
    }
  }

  const bubbleState = () =>
    text(`(() => {
      const node = document.querySelector(".video-bubble");
      if (!node) return null;
      const button = [...node.querySelectorAll("button")].find((b) => b.textContent.trim() === "M");
      const remove = [...node.querySelectorAll("button")].find((b) => b.textContent.trim() === "删除素材");
      return {
        name: node.querySelector(".video-bubble-name")?.textContent.trim() ?? "",
        hasM: !!button,
        pressed: button ? button.getAttribute("aria-pressed") : null,
        leftOfRemove: button && remove
          ? button.getBoundingClientRect().left < remove.getBoundingClientRect().left
          : false,
        sameRow: button && remove
          ? Math.abs(button.getBoundingClientRect().top - remove.getBoundingClientRect().top) < 2
          : false,
      };
    })()`);

  /** Where to click for block `index`: the middle of its *visible* part, clamped to the track. */
  const pointFor = (index) =>
    text(`(() => {
      const node = document.querySelectorAll(".video-block")[${index}];
      const track = document.querySelector(".video-track");
      if (!node || !track) return null;
      const r = node.getBoundingClientRect();
      const t = track.getBoundingClientRect();
      const left = Math.max(r.left, t.left + 2);
      const right = Math.min(r.right, t.right - 2);
      if (right - left < 4) return null;
      return { x: left + Math.min((right - left) / 2, 60), y: r.top + r.height / 2, width: Math.round(right - left) };
    })()`);

  /**
   * Zoom until the block is wide enough to hit, then click it — and **check** the selection.
   *
   * Both shortcut keys are silent when nothing is selected, so a click that landed on the
   * neighbouring block produced "0 PATCHes" with nothing to explain it. The bubble's own name
   * is the proof of which block is selected.
   */
  const selectBlock = async (index) => {
    // The zoom window is shared with the audio waveform, so a previous selection may have left
    // this block outside it. 全部 is the app's own reset — a deliberate target, not a wheel.
    await text(`(() => {
      const button = [...document.querySelectorAll(".waveform-bar button")]
        .find((b) => b.textContent.trim() === "全部");
      if (button) button.click();
      return !!button;
    })()`);
    await wait(350);
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const where = await pointFor(index);
      if (where === null) return null;
      if (where.width >= 70) break;
      await send("Input.dispatchMouseEvent", {
        type: "mouseWheel", x: where.x, y: where.y, deltaX: 0, deltaY: -100, clickCount: 0,
      });
      await wait(110);
    }
    await wait(300);
    const where = await pointFor(index);
    if (where === null) return null;
    for (const type of ["mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", {
        type, x: where.x, y: where.y, button: "left", clickCount: 1,
      });
    }
    await wait(450);
    return where;
  };

  // --- 1. the M button ----------------------------------------------------
  const loudIndex = real.findIndex((item) => item.has_audio);
  if (loudIndex < 0) {
    note("演示项目里没有带声音的素材块——M 按钮无从验证");
  } else if ((await selectBlock(loudIndex)) === null) {
    note("找不到带声音的块在屏幕上的位置");
  } else {
    const bubble = await bubbleState();
    console.log("bubble on an audible block:", JSON.stringify(bubble));
    if (bubble === null) {
      note("点块之后没有气泡");
    } else {
      if (bubble.name !== real[loudIndex].label) {
        note(`点中的是 ${JSON.stringify(bubble.name)}，应该是 ${JSON.stringify(real[loudIndex].label)}`);
      }
      if (!bubble.hasM) note(`带声音的块（${bubble.name}）上应该有 M 按钮`);
      if (bubble.pressed !== "true") {
        note(`M 的初始状态应是按下（静音），实际 aria-pressed=${bubble.pressed}`);
      }
      if (!bubble.leftOfRemove) note("M 应该在 删除素材 左边");
      if (!bubble.sameRow) note("M 和 删除素材 不在同一行");
    }
    await shot(out);
  }

  // A block without sound must not offer M at all.
  const silentIndex = real.findIndex((item) => !item.has_audio);
  if (silentIndex >= 0 && (await selectBlock(silentIndex)) !== null) {
    const silent = await bubbleState();
    console.log("bubble on a silent block:", JSON.stringify(silent));
    if (silent === null) {
      note("点没有声音的块之后没有气泡");
    } else {
      if (silent.name !== real[silentIndex].label) {
        note(`点中的是 ${JSON.stringify(silent.name)}，应该是 ${JSON.stringify(real[silentIndex].label)}`);
      }
      if (silent.hasM) note(`没有声音的块（${silent.name}）不该有 M 按钮`);
    }
  }

  // --- 2. Shift+→ stretches, Ctrl+→ does not ------------------------------
  // The path from the block's end to its neighbour must be clear: a third block inside that
  // gap is exactly what `stretchToNeighbour` refuses (it would trim the middle block away), so
  // staging on such a block tests the guard rather than the key.
  const staged = await text(`(() => {
    const items = ${JSON.stringify(real)};
    for (const block of items) {
      const right = items
        .filter((item) => item.id !== block.id && item.start >= block.end)
        .sort((a, b) => a.start - b.start)[0];
      if (!right || right.start - block.end <= 1) continue;
      const blocked = items.some(
        (item) =>
          item.id !== block.id &&
          item.id !== right.id &&
          block.end < item.end &&
          item.start < right.start,
      );
      if (blocked) continue;
      return {
        id: block.id, label: block.label, start: block.start, end: block.end,
        rightStart: right.start, index: items.indexOf(block),
      };
    }
    return null;
  })()`);
  console.log("staged:", JSON.stringify(staged));
  if (staged === null) {
    note("没有一个块右边还有缝，Shift+→ 无从验证");
  } else if ((await selectBlock(staged.index)) === null) {
    note("找不到要拉伸的那个块");
  } else {
    const chosen = await bubbleState();
    console.log("selected before pressing:", JSON.stringify(chosen));
    if (chosen === null) {
      note("按下快捷键之前没有选中任何块");
    } else if (chosen.name !== staged.label) {
      note(`选中的是 ${JSON.stringify(chosen.name)}，应该是 ${JSON.stringify(staged.label)}`);
    } else {
      const armed = await text(`(() => {
        const original = window.fetch;
        window.__r108 = { patches: [] };
        window.fetch = async (input, init) => {
          const url = typeof input === "string" ? input : input.url;
          const method = (init?.method ?? "GET").toUpperCase();
          if (url.includes("/video/items/") && method === "PATCH") {
            window.__r108.patches.push({ url, body: JSON.parse(init.body) });
            const id = url.split("/video/items/")[1];
            const known = ${JSON.stringify(real)}.find((item) => item.id === id);
            return new Response(JSON.stringify({ ...known, duration_ms: known.end - known.start }),
              { status: 200, headers: { "content-type": "application/json" } });
          }
          return original(input, init);
        };
        return true;
      })()`);
      if (!armed) note("装不上 PATCH 桩");

      const press = async (key, modifiers) => {
        for (const type of ["keyDown", "keyUp"]) {
          await send("Input.dispatchKeyEvent", {
            type,
            key,
            code: key,
            windowsVirtualKeyCode: key === "ArrowRight" ? 39 : 37,
            nativeVirtualKeyCode: key === "ArrowRight" ? 39 : 37,
            modifiers,
          });
        }
        await wait(800);
      };

      // Shift = 8 in CDP's modifier bits.
      await press("ArrowRight", 8);
      const stretched = await text(`window.__r108.patches`);
      console.log("patches after Shift+Right:", JSON.stringify(stretched));
      if (stretched.length !== 1) {
        note(`Shift+→ 应该发一次 PATCH，实际发了 ${stretched.length} 次`);
      } else {
        const body = stretched[0].body;
        if (body.start_ms !== staged.start) {
          note(`Shift+→ 不该动起点：${staged.start} -> ${body.start_ms}`);
        }
        if (body.end_ms !== staged.rightStart) {
          note(`Shift+→ 的终点是 ${body.end_ms}，应该贴到后一个块的起点 ${staged.rightStart}`);
        }
        if (body.end_ms - body.start_ms === staged.end - staged.start) {
          note("Shift+→ 没改变长度——那和 Ctrl+→ 就没区别了");
        }
      }

      // Ctrl = 2. Same block, same side: the length must come back unchanged.
      await press("ArrowRight", 2);
      const moved = await text(`window.__r108.patches`);
      console.log("patches after Ctrl+Right:", JSON.stringify(moved));
      if (moved.length !== 2) {
        note(`Ctrl+→ 应该再发一次 PATCH，累计 ${moved.length} 次`);
      } else {
        const body = moved[1].body;
        if (body.end_ms - body.start_ms !== staged.end - staged.start) {
          note(`Ctrl+→ 改了长度：${staged.end - staged.start} -> ${body.end_ms - body.start_ms}`);
        }
      }
      await shot(out);
    }
  }

  await text(`(() => { delete window.__r108; return true; })()`);

  // Nothing was written *by this script*. The project itself is being edited by hand while
  // the walkthrough runs (36 → 37 blocks during one run), so comparing the whole timeline would
  // report the user's own work as damage. What can be asserted is that every PATCH the script
  // caught was intercepted, and that the block it aimed at still has the edges the server had.
  const closing = await text(`(async () => {
    const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
    const body = await response.json();
    const staged = ${JSON.stringify(staged === null ? null : { id: staged.id, start: staged.start, end: staged.end })};
    const found = staged === null ? null : body.items.find((item) => item.id === staged.id);
    return {
      staged: found ? [found.start_ms, found.end_ms] : null,
      unmuted: body.items.filter((item) => !item.muted).length,
    };
  })()`);
  console.log("closing:", JSON.stringify(closing));
  if (closing.unmuted !== real.filter((item) => !item.muted).length) {
    note(`有块的静音状态被改了：${real.filter((item) => !item.muted).length} -> ${closing.unmuted}`);
  }
  if (staged !== null && JSON.stringify(closing.staged) !== JSON.stringify([staged.start, staged.end])) {
    note(`被测试的那个块被改动了：${JSON.stringify([staged.start, staged.end])} -> ${JSON.stringify(closing.staged)}`);
  }

  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: 三件事都对，且没有写库");
}
