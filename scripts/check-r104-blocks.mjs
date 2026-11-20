/**
 * Round 104 walkthrough: the selected-block row is gone, blocks name themselves, the
 * preview fills its column, and a slide block shows the page it starts inside.
 *
 * Four facts, each computed from the live page rather than asserted from memory:
 *
 *  1. **No selected-block row.** The panel must not contain 「选中 …」,
 *     「拖一个素材到下面的视频轴上可以加进画廊」 or a 删掉这个块 button outside the orphan
 *     list. It must contain a bubble instead, anchored above the selected block.
 *  2. **Blocks say what and when.** Every block shows the asset's own name (or the deck's
 *     title) and its own `start–end`; the two numbers read off the blocks are compared with
 *     the same blocks' rectangles on screen, so a wrong label/time pairing cannot pass.
 *  3. **The preview fills the column.** The box's width equals `.video-stage-main`'s width,
 *     its height is width × 9/16, and the still inside covers it — measured against the
 *     image's natural size so a 16:9 still has no letterbox.
 *  4. **The still at 01:10.970 is 开场与节目定位.** Read out of the page DOM after asking
 *     for the still, by comparing the rendered preview against the deck's own PNG for that
 *     page — a same-size pixel comparison of the box region.
 */

export default async function ({ evaluate, shot, wait, out, send }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };
  const text = (expression) => evaluate(expression);

  // Every step that touches the track gets a before/after snapshot from the server. Two runs
  // in a row ended with the slide block ending at 141700 instead of 198850, and a screenshot
  // cannot say which click did it.
  const state = async (label) => {
    const items = await text(`(async () => {
      const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
      const body = await response.json();
      return body.items.map((item) => [item.start_ms, item.end_ms]);
    })()`);
    console.log("state:", label, JSON.stringify(items));
  };

  // --- 0. open the video panel ---------------------------------------------
  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return !!panel;
  })()`);
  await wait(900);

  await state("opened");
  const opening = await text(`(async () => {
    const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
    const body = await response.json();
    return body.items.map((item) => [item.id, item.start_ms, item.end_ms]);
  })()`);

  // --- 1. the old row is gone, nothing replaced it in the flow --------------
  const row = await text(`(() => {
    const panel = document.querySelector(".video-panel");
    const flat = (panel?.textContent ?? "").replace(/\\s+/g, " ");
    const buttons = [...(panel?.querySelectorAll("button") ?? [])].map((b) => b.textContent.trim());
    return {
      hasSelectedLine: /选中\\s*\\d/.test(flat),
      hasDragHint: flat.includes("拖一个素材到下面的视频轴上可以加进画廊"),
      hasDeleteButton: buttons.some((t) => t === "删掉这个块"),
      hasOldFitRow: !!panel?.querySelector(".video-selected"),
      hasBubbleBeforeSelect: !!panel?.querySelector(".video-bubble"),
    };
  })()`);
  console.log("row:", JSON.stringify(row));
  if (row.hasSelectedLine) note("面板里还有「选中 …」那一行");
  if (row.hasDragHint) note("面板里还有「拖一个素材…可以加进画廊」那句提示");
  if (row.hasDeleteButton) note("面板里还有「删掉这个块」按钮");
  if (row.hasOldFitRow) note("面板里还有 .video-selected 那一块");
  if (row.hasBubbleBeforeSelect) note("什么都没选就出现了气泡");

  // --- 2. names and ranges, read off the blocks ----------------------------
  const blocks = await text(`(() => {
    const track = document.querySelector(".video-track");
    const trackRect = track.getBoundingClientRect();
    return [...document.querySelectorAll(".video-block")].map((node) => {
      const r = node.getBoundingClientRect();
      return {
        name: node.querySelector(".video-block-name")?.textContent.trim() ?? "",
        time: node.querySelector(".video-block-time")?.textContent.trim() ?? "",
        title: node.getAttribute("title") ?? "",
        left: Math.round(r.left - trackRect.left),
        width: Math.round(r.width),
      };
    });
  })()`);
  console.log("blocks:", JSON.stringify(blocks));
  if (blocks.length !== 2) note(`素材块数量是 ${blocks.length}，演示项目里应该是 2`);
  const names = blocks.map((b) => b.name);
  if (!names.includes("开场片头.mp4")) note(`没有块显示素材原名「开场片头.mp4」，读到的是 ${JSON.stringify(names)}`);
  if (!names.includes("演示稿")) note(`没有块显示演示稿标题，读到的是 ${JSON.stringify(names)}`);
  for (const block of blocks) {
    if (!/^\d{2}:\d{2}\.\d{3}–\d{2}:\d{2}\.\d{3}$/.test(block.time)) {
      note(`块的时间不是「起–止」：${JSON.stringify(block.time)}`);
    }
  }
  // The media block covers the first 70.970 s and the slide block starts there. Sorted by
  // x, the ranges must be 00:00.000–01:10.970 then 01:10.970–03:18.850.
  const byX = [...blocks].sort((a, b) => a.left - b.left);
  if (byX[0]?.time !== "00:00.000–01:10.970") note(`第一个块的时间是 ${JSON.stringify(byX[0]?.time)}`);
  if (byX[1]?.time !== "01:10.970–03:18.850") note(`第二个块的时间是 ${JSON.stringify(byX[1]?.time)}`);

  // --- 3. the preview fills its column ------------------------------------
  await text(`(() => {
    [...document.querySelectorAll(".video-panel button")].find((b) => b.textContent.trim() === "生成静帧")?.click();
    return true;
  })()`);
  for (let i = 0; i < 40; i += 1) {
    await wait(500);
    const ready = await text(`!!document.querySelector(".video-preview-box img")?.getAttribute("src")`);
    if (ready) break;
  }
  const preview = await text(`(() => {
    const box = document.querySelector(".video-preview-box");
    const main = document.querySelector(".video-stage-main");
    const img = box?.querySelector("img");
    if (!box || !main) return { found: false };
    const b = box.getBoundingClientRect();
    const m = main.getBoundingClientRect();
    return {
      found: true,
      // The outer box and its *content* box: the frame has a 1px border on each side, so the
      // picture is 2px narrower than the frame and that is correct — comparing against the
      // border box would call a correctly filling image a failure.
      boxW: Math.round(b.width),
      boxH: Math.round(b.height),
      innerW: box.clientWidth,
      innerH: box.clientHeight,
      mainW: Math.round(m.width),
      ratio: +(b.width / b.height).toFixed(3),
      hasImg: !!img,
      imgW: img ? Math.round(img.getBoundingClientRect().width) : 0,
      imgH: img ? Math.round(img.getBoundingClientRect().height) : 0,
      naturalW: img?.naturalWidth ?? 0,
      naturalH: img?.naturalHeight ?? 0,
    };
  })()`);
  console.log("preview:", JSON.stringify(preview));
  if (!preview.found) note("找不到 .video-preview-box / .video-stage-main");
  if (!preview.hasImg) note("预览框里没有图");
  if (preview.boxW !== preview.mainW) {
    note(`预览框宽 ${preview.boxW} ≠ 栏宽 ${preview.mainW}，没有铺满`);
  }
  if (Math.abs(preview.ratio - 16 / 9) > 0.02) {
    note(`预览框宽高比 ${preview.ratio}，应该是 16:9（1.778）`);
  }
  if (preview.imgW !== preview.innerW || preview.imgH !== preview.innerH) {
    note(
      `图 ${preview.imgW}×${preview.imgH} 没铺满框的内容区 ${preview.innerW}×${preview.innerH}`,
    );
  }
  await shot(out);

  await state("after still");

  // --- 4. zoom in far enough to click the slide block ---------------------
  const zoom = await text(`(() => {
    const track = document.querySelector(".video-track");
    const r = track.getBoundingClientRect();
    return { x: r.left + 8, y: r.top + r.height / 2, w: r.width };
  })()`);
  // The anchor sits at the very left of the track; 14 notches of ZOOM_STEP (1.25) takes the
  // 61-minute window down to ~2.5 minutes, which makes every block hundreds of px wide.
  for (let i = 0; i < 14; i += 1) {
    await send("Input.dispatchMouseEvent", {
      type: "mouseWheel",
      x: zoom.x,
      y: zoom.y,
      deltaX: 0,
      deltaY: -100,
      clickCount: 0,
    });
    await wait(90);
  }
  await wait(400);
  // Pan to the slide block: shift+wheel scrolls the shared window.
  const slide = await text(`(() => {
    const nodes = [...document.querySelectorAll(".video-block")];
    const wanted = nodes.find((n) => (n.querySelector(".video-block-name")?.textContent ?? "").includes("演示稿"))
      ?? nodes[nodes.length - 1];
    if (!wanted) return null;
    const r = wanted.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: Math.round(r.width) };
  })()`);
  console.log("slide block on screen:", JSON.stringify(slide));
  if (!slide || slide.w < 20) note(`演示稿块在屏幕上只有 ${slide?.w ?? 0}px 宽，缩小过头了`);
  await shot(out);

  await state("after zoom");

  // --- 5. select it: the bubble appears above the block -------------------
  await send("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: slide.x,
    y: slide.y,
    button: "left",
    clickCount: 1,
  });
  await send("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: slide.x,
    y: slide.y,
    button: "left",
    clickCount: 1,
  });
  await wait(400);

  await state("after click");

  const bubble = await text(`(() => {
    const node = document.querySelector(".video-bubble");
    if (!node) return { found: false };
    const r = node.getBoundingClientRect();
    const block = document.querySelector(".video-block.is-selected, .video-block:hover");
    const wrap = document.querySelector(".video-track-wrap").getBoundingClientRect();
    const track = document.querySelector(".video-track").getBoundingClientRect();
    const box = node.querySelector("select");
    return {
      found: true,
      name: node.querySelector(".video-bubble-name")?.textContent.trim() ?? "",
      options: box ? [...box.options].map((o) => o.textContent.trim()) : [],
      value: box?.value ?? "",
      buttons: [...node.querySelectorAll("button")].map((b) => b.textContent.trim()),
      insideWrap: r.left >= wrap.left - 1 && r.right <= wrap.right + 1,
      aboveTrack: r.bottom <= track.top + 1,
      zIndex: getComputedStyle(node).zIndex,
    };
  })()`);
  console.log("bubble:", JSON.stringify(bubble));
  if (!bubble.found) note("选中素材块后没有出现气泡");
  if (bubble.name !== "演示稿") note(`气泡里的名字是 ${JSON.stringify(bubble.name)}，应该是「演示稿」`);
  if (bubble.options.length !== 4) note(`气泡的填充下拉有 ${bubble.options.length} 项，应该 4 项`);
  if (!bubble.buttons.includes("删除素材")) note(`气泡里没有「删除素材」按钮：${JSON.stringify(bubble.buttons)}`);
  if (bubble.found && !bubble.insideWrap) note("气泡超出了视频轴的范围");
  if (bubble.found && !bubble.aboveTrack) note("气泡没有画在视频轴上方");
  await shot(out);

  // --- 6. the fit dropdown really writes ----------------------------------
  const before = bubble.value;
  const nextFit = before === "cover" ? "contain" : "cover";
  await text(`(() => {
    const box = document.querySelector(".video-bubble select");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
    setter.call(box, ${JSON.stringify(nextFit)});
    box.dispatchEvent(new Event("change", { bubbles: true }));
    return box.value;
  })()`);
  await wait(700);
  const afterFit = await text(`document.querySelector(".video-bubble select")?.value ?? null`);
  console.log("fit:", before, "->", afterFit);
  if (afterFit !== nextFit) note(`填充改成 ${nextFit} 之后气泡里读到的是 ${afterFit}`);

  // Put it back, so re-running the walkthrough starts from the demo's own state.
  await text(`(() => {
    const box = document.querySelector(".video-bubble select");
    if (!box) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
    setter.call(box, ${JSON.stringify(before)});
    box.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  })()`);
  await wait(500);

  await state("after fit");

  // --- 7. the still at 01:10.970 is 开场与节目定位 -------------------------
  // The block starts at 70 970 ms, inside page 1 (which begins at 5 100 ms). Request the
  // still at the block's start and compare it with the deck's own PNG for that page: same
  // pixels in the media box means the right page, and a comparison of *sizes* alone would
  // pass for any page (all pages are 1440×810).
  const still = await text(`(async () => {
    const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/preview?at_ms=70970");
    if (!response.ok) return { ok: false, status: response.status };
    const body = await response.json();
    return { ok: true, url: body.url, at: body.at_ms };
  })()`);
  console.log("still:", JSON.stringify(still));
  if (!still.ok) note(`生成 01:10.970 的静帧失败：${still.status}`);

  const same = await text(`(async () => {
    const url = ${JSON.stringify(still.url ?? "")} + "?project_id=prj_1m6v2m8anfpyqq3w8qt";
    const load = (src) => new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error("load failed"));
      image.src = src;
    });
    const [stillImage, pageImage] = await Promise.all([
      load(url),
      load("/api/projects/prj_1m6v2m8anfpyqq3w8qt/decks/deck_1m74pw3kpzpwst5swm8/slides/sld_1m76jprnem4rs7jekdj/image?width=1440&height=810"),
    ]);
    const crop = (image, x, y, w, h) => {
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const context = canvas.getContext("2d");
      context.drawImage(image, x, y, w, h, 0, 0, w, h);
      return context.getImageData(0, 0, w, h).data;
    };
    // The media box's rect, from the template. Comparing outside it would compare the plate,
    // which is identical for every page.
    const box = { x: 240, y: 209, w: 1440, h: 810 };
    const a = crop(stillImage, box.x, box.y, box.w, box.h);
    const b = crop(pageImage, 0, 0, box.w, box.h);
    if (a.length !== b.length) return { compared: false };
    let diff = 0;
    for (let i = 0; i < a.length; i += 4) {
      if (Math.abs(a[i] - b[i]) > 24 || Math.abs(a[i + 1] - b[i + 1]) > 24 || Math.abs(a[i + 2] - b[i + 2]) > 24) diff += 1;
    }
    return { compared: true, diff, total: a.length / 4, ratio: diff / (a.length / 4) };
  })()`);
  console.log("still vs page:", JSON.stringify(same));
  if (!same.compared) note("静帧与页面 PNG 尺寸不一致，无法比较");
  else if (same.ratio > 0.02) {
    note(`01:10.970 的静帧和「开场与节目定位」页面有 ${(same.ratio * 100).toFixed(1)}% 的像素不同，说明显示的是别的页`);
  }

  await state("after still 2");

  // --- 8. the bubble's 删除素材 really deletes -------------------------------
  // Back to the full view first, so the track is wide enough to aim a drop at.
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 900,
    deviceScaleFactor: 1.5,
    mobile: false,
  });
  await wait(500);
  await text(`(() => {
    [...document.querySelectorAll(".video-panel button")].find((b) => b.textContent.trim() === "素材库")?.click();
    return true;
  })()`);
  await wait(700);

  const blocksBefore = await text(`document.querySelectorAll(".video-block").length`);
  const staged = await text(`(() => {
    const item = document.querySelector('.asset-item[data-asset-id]');
    return item ? item.getAttribute("data-asset-id") : null;
  })()`);
  console.log("delete test: blocks before =", blocksBefore, "asset =", staged);
  if (blocksBefore !== 2) note(`准备测试删除时素材块是 ${blocksBefore} 个，应该是 2 个`);
  if (!staged) note("素材库里找不到可拖的素材");

  if (staged) {
    // Back to the timeline the block will land on, then a real drag-and-drop through CDP:
    // a synthetic `drop` event does not set `dataTransfer`, and the handler reads it.
    await text(`(() => {
      [...document.querySelectorAll(".video-tabs button, .video-panel button")]
        .find((b) => b.textContent.trim() === "模板")?.click();
      return true;
    })()`);
    await wait(600);
    const drop = await text(`(() => {
      const track = document.querySelector(".video-track");
      const r = track.getBoundingClientRect();
      return { x: r.left + r.width * 0.85, y: r.top + r.height / 2 };
    })()`);
    const dragData = {
      items: [{ mimeType: "text/plain", data: staged }],
      dragOperationsMask: 1,
    };
    for (const type of ["dragEnter", "dragOver", "drop"]) {
      await send("Input.dispatchDragEvent", { type, x: drop.x, y: drop.y, data: dragData });
      await wait(160);
    }
    await wait(1500);
    const afterDrop = await text(`document.querySelectorAll(".video-block").length`);
    console.log("delete test: blocks after drop =", afterDrop);
    if (afterDrop !== blocksBefore + 1) {
      note(`拖一个素材到轴上之后应该有 ${blocksBefore + 1} 个块，实际 ${afterDrop} 个`);
    } else {
      // Select the new block, then delete it through the bubble.
      const target = await text(`(() => {
        const nodes = [...document.querySelectorAll(".video-block")];
        const node = nodes[nodes.length - 1];
        const r = node.getBoundingClientRect();
        return { x: r.left + Math.min(r.width / 2, 40), y: r.top + r.height / 2 };
      })()`);
      await send("Input.dispatchMouseEvent", {
        type: "mousePressed",
        x: target.x,
        y: target.y,
        button: "left",
        clickCount: 1,
      });
      await send("Input.dispatchMouseEvent", {
        type: "mouseReleased",
        x: target.x,
        y: target.y,
        button: "left",
        clickCount: 1,
      });
      await wait(400);
      const armed = await text(`(() => {
        const bubble = document.querySelector(".video-bubble");
        const button = [...(bubble?.querySelectorAll("button") ?? [])].find((b) => b.textContent.trim() === "删除素材");
        if (!button) return false;
        button.click();
        return true;
      })()`);
      if (!armed) note("选中新块之后气泡里没有「删除素材」");
      await wait(1600);
      const afterDelete = await text(`document.querySelectorAll(".video-block").length`);
      console.log("delete test: blocks after delete =", afterDelete);
      if (afterDelete !== blocksBefore) {
        note(`点了「删除素材」之后是 ${afterDelete} 个块，应该回到 ${blocksBefore} 个`);
      }
      // Counting is not enough: a drop that resizes a neighbour also leaves 2 blocks. The
      // ranges have to come back byte-identical, or this walkthrough silently edits the demo.
      const server = await text(`(async () => {
        const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
        const body = await response.json();
        return body.items.map((item) => [item.id, item.start_ms, item.end_ms]);
      })()`);
      if (server.length !== blocksBefore) note(`服务端还有 ${server.length} 个块，应该 ${blocksBefore} 个`);
      if (JSON.stringify(server) !== JSON.stringify(opening)) {
        note(
          `删完之后服务端的块和开始时不一样了：${JSON.stringify(opening)} → ${JSON.stringify(server)}`,
        );
      }
      await shot(out);
    }
  }

  await state("after delete test");

  // --- 9. the library lists the names the files had -------------------------
  await text(`(() => {
    [...document.querySelectorAll(".video-tabs button, .video-panel button")]
      .find((b) => b.textContent.trim() === "素材库")?.click();
    return true;
  })()`);
  await wait(900);
  const library = await text(`(() => {
    const nodes = [...document.querySelectorAll(".asset-name")];
    const names = nodes.map((n) => n.textContent.trim());
    // A name that is in the DOM but clipped to 「主…」 is not usable. Measured as the width
    // actually available to the text, and as whether the longest name overflows.
    const widths = nodes.map((n) => Math.round(n.getBoundingClientRect().width));
    const clipped = nodes
      .filter((n) => n.scrollWidth > n.clientWidth + 1)
      .map((n) => n.textContent.trim());
    return {
      names,
      widths,
      clipped,
      // A name that is 16 hex characters plus an extension is the hash the import used to
      // show. It is allowed only for assets imported before original_name existed, so the
      // check is on the ones this walkthrough re-imported.
      hashLike: names.filter((n) => /^[0-9a-f]{16}\.[a-z0-9]+$/.test(n)),
    };
  })()`);
  console.log("library:", JSON.stringify(library));
  for (const wanted of ["开场片头.mp4", "插画-白色少女.jpg", "主持人与嘉宾合照.jpg", "节目封面.png"]) {
    if (!library.names.includes(wanted)) {
      note(`素材库里没有「${wanted}」，读到的是 ${JSON.stringify(library.names)}`);
    }
  }
  if (library.names.length > 0 && library.hashLike.length > 1) {
    note(`素材库里还有 ${library.hashLike.length} 个哈希文件名：${JSON.stringify(library.hashLike)}`);
  }
  // 60px is about 7 CJK characters at 0.78rem — enough to tell 插画-白色少女 from 主持人与嘉宾合照.
  const narrowest = Math.min(...library.widths);
  if (library.names.length > 0 && narrowest < 60) {
    note(`素材名最窄只有 ${narrowest}px，读不出名字（宽度：${JSON.stringify(library.widths)}）`);
  }
  if (library.clipped.length > 0) {
    note(`有 ${library.clipped.length} 个素材名被截断了：${JSON.stringify(library.clipped)}`);
  }
  await shot(out);

  // --- 10. the preview still fills the column when the window narrows -------
  // The item was 「随着宽度改变自适应改变」: at 1100px the panel folds to one column, so the
  // stage is much wider than the 480px measured above. Anything with a `max-height` (or a
  // fixed width) would show up here and not at 1440.
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1100,
    height: 900,
    deviceScaleFactor: 1.5,
    mobile: false,
  });
  await wait(700);
  const narrow = await text(`(() => {
    const box = document.querySelector(".video-preview-box");
    const main = document.querySelector(".video-stage-main");
    const img = box?.querySelector("img");
    if (!box || !main) return { found: false };
    const b = box.getBoundingClientRect();
    const m = main.getBoundingClientRect();
    return {
      found: true,
      boxW: Math.round(b.width),
      boxH: Math.round(b.height),
      innerW: box.clientWidth,
      innerH: box.clientHeight,
      mainW: Math.round(m.width),
      ratio: +(b.width / b.height).toFixed(3),
      imgW: img ? Math.round(img.getBoundingClientRect().width) : 0,
      imgH: img ? Math.round(img.getBoundingClientRect().height) : 0,
    };
  })()`);
  console.log("preview @1100:", JSON.stringify(narrow));
  if (!narrow.found) note("窄窗口下找不到预览框");
  if (narrow.found && narrow.boxW !== narrow.mainW) {
    note(`窄窗口下预览框宽 ${narrow.boxW} ≠ 栏宽 ${narrow.mainW}`);
  }
  if (narrow.found && Math.abs(narrow.ratio - 16 / 9) > 0.02) {
    note(`窄窗口下预览框宽高比 ${narrow.ratio}，应该 16:9`);
  }
  if (narrow.found && (narrow.imgW !== narrow.innerW || narrow.imgH !== narrow.innerH)) {
    note(`窄窗口下图 ${narrow.imgW}×${narrow.imgH} 没铺满 ${narrow.innerW}×${narrow.innerH}`);
  }
  await shot(out);

  const closing = await text(`(async () => {
    const response = await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline");
    const body = await response.json();
    return body.items.map((item) => [item.id, item.start_ms, item.end_ms]);
  })()`);
  console.log("state: closing", JSON.stringify(closing));
  if (JSON.stringify(closing) !== JSON.stringify(opening)) {
    note(`走查结束时演示项目的素材块被改动了：${JSON.stringify(opening)} → ${JSON.stringify(closing)}`);
  }

  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: 四件事都对");
}
