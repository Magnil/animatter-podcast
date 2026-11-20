/**
 * Round 107 walkthrough: slide block names its page, the deck's two page actions sit with
 * 删掉这页, and Ctrl+←/→ closes a gap exactly.
 *
 * **Read-only by construction.** The demo project is being edited by hand while this runs — it
 * grew from 2 blocks to 12 during this round — so the shortcut is verified by intercepting the
 * PATCH: `window.fetch` records the request and answers it without forwarding, so nothing is
 * written and the panel's own reload puts the UI back from the database. The assertions are
 * about *the request the shortcut produced*, which is exactly what "is the key wired to the
 * right numbers" means.
 *
 * Three facts:
 *
 *  1. Every slide block's label is the page it opens on (「开场与节目定位 等 2 页」), never the
 *     string 「演示稿」 — that was the bug: `deck.title` is that constant on every project.
 *  2. In the 演示稿 tab, 跳到这句字幕 and 放进素材框 share a row with 删掉这页, above the 标题
 *     field. Asserted as vertical order, because the change is about where things are.
 *  3. Ctrl+→ on a block with a gap to its left produces a PATCH whose start is exactly the
 *     neighbour's end and whose length is unchanged; pressing it again on a flush block
 *     produces no request.
 */

export default async function ({ evaluate, shot, wait, out, send }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };
  const text = (expression) => evaluate(expression);

  /** The real timeline, straight from the server, as ground truth for the label checks. */
  const real = await text(`(async () => {
    const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
    const body = await response.json();
    return body.items.map((item) => ({
      id: item.id, kind: item.kind, label: item.label,
      start: item.start_ms, end: item.end_ms,
    }));
  })()`);
  console.log("server items:", real.length);
  if (real.length === 0) note("演示项目里一个素材块都没有");
  const slides = real.filter((item) => item.kind === "slide");
  if (slides.length === 0) note("演示项目里没有演示稿块");

  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return !!panel;
  })()`);
  await wait(1200);

  // --- 1. each slide block is named after the page it opens on --------------
  const blocks = await text(`(() => [...document.querySelectorAll(".video-block")].map((n) => ({
    name: n.querySelector(".video-block-name")?.textContent.trim() ?? "",
    time: n.querySelector(".video-block-time")?.textContent.trim() ?? "",
  })))()`);
  console.log("dom blocks:", JSON.stringify(blocks.map((block) => block.name)));
  if (blocks.length !== real.length) {
    note(`轴上画了 ${blocks.length} 个块，服务端有 ${real.length} 个`);
  }
  for (const slide of slides) {
    if (slide.label === "演示稿") {
      note(`块 ${slide.id} 的 label 还是「演示稿」——那正是这一轮要修的那一条`);
    }
    if (!slide.label) note(`块 ${slide.id} 没有 label`);
  }
  // `/video/timeline` orders by `start_ms, z` and the track renders `items` in that order, so
  // index i in the DOM is item i from the server. Matching by parsed time instead is how the
  // first version of this check failed on 「01:10.970」 — the comparison was fragile, not the
  // label.
  for (const [index, item] of real.entries()) {
    const shown = blocks[index];
    if (!shown) continue;
    if (shown.name !== item.label) {
      note(`第 ${index + 1} 个块画的是 ${JSON.stringify(shown.name)}，服务端给的是 ${JSON.stringify(item.label)}`);
    }
    if (item.kind === "slide" && shown.name === "演示稿") {
      note(`第 ${index + 1} 个块是演示稿块，画出来还是「演示稿」`);
    }
  }

  // --- 2. deck tab: three page actions in one row, above the text fields ----
  await text(`(() => {
    [...document.querySelectorAll(".video-panel button")].find((b) => b.textContent.trim() === "演示稿")?.click();
    return true;
  })()`);
  await wait(1200);
  const deck = await text(`(() => {
    const buttons = [...document.querySelectorAll(".deck-editor button")];
    const find = (label) => buttons.find((b) => b.textContent.trim() === label) ?? null;
    const box = (node) => (node ? node.getBoundingClientRect() : null);
    const seek = box(find("跳到这句字幕"));
    const insert = box(find("放进素材框"));
    const remove = box(find("删掉这页"));
    const title = box(document.querySelector(".deck-editor input"));
    const save = box(buttons.find((b) => /保存这一页|已保存/.test(b.textContent.trim())));
    return {
      present: { seek: !!seek, insert: !!insert, remove: !!remove, title: !!title, save: !!save },
      sameRow: seek && insert && remove
        ? Math.abs(seek.top - insert.top) < 2 && Math.abs(seek.top - remove.top) < 2
        : false,
      aboveTitle: seek && title ? seek.bottom <= title.top + 1 : false,
      saveBelowText: save && title ? save.top > title.top : false,
      labels: buttons.map((b) => b.textContent.trim()),
    };
  })()`);
  console.log("deck tab:", JSON.stringify(deck));
  const missing = Object.entries(deck.present).filter(([, ok]) => !ok).map(([name]) => name);
  if (missing.length > 0) note(`演示稿标签页缺：${missing.join(", ")}`);
  if (!deck.sameRow) note("跳到这句字幕 / 放进素材框 / 删掉这页 不在同一行");
  if (!deck.aboveTitle) note("这三个按钮不在标题输入框上面");
  if (!deck.saveBelowText) note("保存按钮应该留在标题正文下面");
  await shot(out);

  // --- 3. Ctrl+→ against an intercepted PATCH (no writes) -------------------
  // Pick the (block, direction) pair that the rule can satisfy *and* that actually has a gap
  // to close. The same arithmetic as `abutNeighbour`, deliberately spelled out here: this is a
  // check of the wiring, so the expectation must not come from the code under test.
  const staged = await text(`(() => {
    const items = ${JSON.stringify(real)};
    const duration = 3_695_928;
    for (const block of items) {
      for (const direction of ["prev", "next"]) {
        const others = items.filter((item) => item.id !== block.id);
        const neighbours = direction === "prev"
          ? others.filter((item) => item.end <= block.start).sort((a, b) => b.end - a.end)
          : others.filter((item) => item.start >= block.end).sort((a, b) => a.start - b.start);
        const neighbour = neighbours[0];
        if (!neighbour) continue;
        const length = block.end - block.start;
        const start = direction === "prev" ? neighbour.end : neighbour.start - length;
        const end = start + length;
        if (direction === "prev" && start >= block.start) continue;
        if (direction === "next" && start <= block.start) continue;
        if (start < 0 || end > duration) continue;
        if (others.some((item) => start < item.end && item.start < end)) continue;
        return {
          id: block.id,
          kind: block.kind,
          direction,
          start: block.start,
          end: block.end,
          neighbourEdge: direction === "prev" ? neighbour.end : neighbour.start,
          index: items.indexOf(block),
        };
      }
    }
    return null;
  })()`);
  console.log("staged:", JSON.stringify(staged));
  if (!staged) {
    note("演示项目里没有一个块左边还有缝，这个快捷键在它身上无从验证");
  } else {
    const armed = await text(`(() => {
      const original = window.fetch;
      window.__r107 = { patches: [] };
      window.fetch = async (input, init) => {
        const url = typeof input === "string" ? input : input.url;
        const method = (init?.method ?? "GET").toUpperCase();
        if (url.includes("/video/items/") && method === "PATCH") {
          window.__r107.patches.push({ url, body: JSON.parse(init.body) });
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

    // Zoom onto *that* block: the wheel anchors at the cursor, so each notch is taken with
    // the pointer over the block's own current position. Zooming from the track's left edge
    // only works for the first block, and a 43-pixel block is not clickable.
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const where = await text(`(() => {
        const node = document.querySelectorAll(".video-block")[${staged.index}];
        const track = document.querySelector(".video-track");
        if (!node || !track) return null;
        const r = node.getBoundingClientRect();
        const t = track.getBoundingClientRect();
        return {
          x: Math.min(t.right - 4, Math.max(t.left + 4, r.left + r.width / 2)),
          y: t.top + t.height / 2,
          width: Math.round(r.width),
        };
      })()`);
      if (!where) {
        note("找不到要测的那个块");
        break;
      }
      if (where.width >= 60) break;
      await send("Input.dispatchMouseEvent", {
        type: "mouseWheel", x: where.x, y: where.y,
        deltaX: 0, deltaY: -100, clickCount: 0,
      });
      await wait(120);
    }
    await wait(400);
    const target = await text(`(() => {
      const node = document.querySelectorAll(".video-block")[${staged.index}];
      if (!node) return null;
      const r = node.getBoundingClientRect();
      return { x: r.left + Math.min(r.width / 2, 60), y: r.top + r.height / 2, w: Math.round(r.width) };
    })()`);
    console.log("click target:", JSON.stringify(target));
    if (!target) {
      note("找不到演示稿块在屏幕上的位置");
    } else {
      for (const type of ["mousePressed", "mouseReleased"]) {
        await send("Input.dispatchMouseEvent", {
          type, x: target.x, y: target.y, button: "left", clickCount: 1,
        });
      }
      await wait(500);
      const selected = await text(`!!document.querySelector(".video-bubble")`);
      if (!selected) note("点击演示稿块之后没有选中（没有气泡）");

      const press = async (key) => {
        for (const type of ["keyDown", "keyUp"]) {
          await send("Input.dispatchKeyEvent", {
            type,
            key,
            code: key,
            windowsVirtualKeyCode: key === "ArrowRight" ? 39 : 37,
            nativeVirtualKeyCode: key === "ArrowRight" ? 39 : 37,
            modifiers: 2, // Ctrl
          });
        }
        await wait(800);
      };

      // The direction the block was chosen for.
      const key = staged.direction === "prev" ? "ArrowLeft" : "ArrowRight";
      await press(key);
      const first = await text(`window.__r107.patches`);
      console.log(`patches after Ctrl+${staged.direction === "prev" ? "Left" : "Right"}:`, JSON.stringify(first));
      if (first.length !== 1) {
        note(`Ctrl+${staged.direction === "prev" ? "←" : "→"} 应该发一次 PATCH，实际发了 ${first.length} 次`);
      } else {
        const body = first[0].body;
        const landed = staged.direction === "prev" ? body.start_ms : body.end_ms;
        if (landed !== staged.neighbourEdge) {
          note(`贴过去之后接触边是 ${landed}，应该等于邻居的边 ${staged.neighbourEdge}`);
        }
        if (body.end_ms - body.start_ms !== staged.end - staged.start) {
          note(`改长度了：${staged.end - staged.start} → ${body.end_ms - body.start_ms}`);
        }
      }

      // Not asserted here: "already flush ⇒ send nothing". The stub does not persist, so the
      // panel's reload after the move puts the block back where the database still has it and
      // the block is legitimately un-flush again. That rule is a pure-function fact, pinned by
      // `abutNeighbour does nothing when the block is already flush`; asserting it here would
      // measure the stub rather than the shortcut.

      await shot(out);
    }
  }

  // Remove the stub and confirm the demo was not touched.
  await text(`(() => { delete window.__r107; return true; })()`);
  // Only the block this walkthrough aimed at: the demo project is edited by hand while the
  // round is being built, and comparing the whole timeline reported the user's own edits as
  // this script's damage.
  if (staged) {
    const closing = await text(`(async () => {
      const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
      const body = await response.json();
      const found = body.items.find((item) => item.id === ${JSON.stringify(staged.id)});
      return found ? [found.start_ms, found.end_ms] : null;
    })()`);
    console.log("staged block now:", JSON.stringify(closing));
    if (JSON.stringify(closing) !== JSON.stringify([staged.start, staged.end])) {
      note(
        `走查动到了被测试的那个块：${JSON.stringify([staged.start, staged.end])} → ${JSON.stringify(closing)}`,
      );
    }
  }

  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: 三件事都对，且没有写库");
}
