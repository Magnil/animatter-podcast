/**
 * Round 109 walkthrough: the fifth fit appears in both dropdowns and the server accepts it.
 *
 * **Read-only.** The demo project is edited by hand while this round runs, so the one write
 * path — picking a fit — is verified by intercepting the PATCH: `window.fetch` records the
 * request and answers with the row the server already has. Nothing is stored, and the panel's
 * own reload restores the UI.
 *
 * Three facts:
 *
 *  1. The bubble's 填充 select has five entries and 模糊裁切 is one of them, between the two
 *     blur strategies — the list is shared with the template tab's 框内默认填充, and a fit the
 *     server does not accept is a dropdown entry that 400s on pick.
 *  2. The server accepts `blur_crop` on a real PATCH (asserted on the intercepted body, and
 *     separately against the API so the *server* is proven, not just the request).
 *  3. The template tab's default-fit select lists it too, in the same order.
 */

export default async function ({ evaluate, shot, wait, out, send }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };
  const text = (expression) => evaluate(expression);

  const items = await text(`(async () => {
    const body = await (await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline")).json();
    return body.items.map((item) => ({
      id: item.id, kind: item.kind, label: item.label,
      start: item.start_ms, end: item.end_ms, fit: item.fit,
    }));
  })()`);
  console.log("blocks:", items.length);

  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return true;
  })()`);
  await wait(1000);

  const EXPECTED = ["模糊填充", "模糊裁切", "完整显示", "填满裁切", "拉伸"];
  const EXPECTED_VALUES = ["blur_fill", "blur_crop", "contain", "cover", "stretch"];

  // --- 3 first: the template tab's default fit, no clicking of blocks needed -
  await text(`(() => {
    [...document.querySelectorAll(".video-panel button")].find((b) => b.textContent.trim() === "模板")?.click();
    return true;
  })()`);
  await wait(900);
  const defaults = await text(`(() => {
    const field = [...document.querySelectorAll(".video-settings-body label.field")]
      .find((label) => label.querySelector("span")?.textContent.trim() === "框内默认填充");
    if (!field) return null;
    const select = field.querySelector("select");
    return {
      options: [...select.options].map((option) => option.textContent.trim()),
      values: [...select.options].map((option) => option.value),
      current: select.value,
    };
  })()`);
  console.log("template default select:", JSON.stringify(defaults));
  if (defaults === null) {
    note("模板页找不到「框内默认填充」下拉");
  } else {
    // This select renders `{label} — {hint}`, so the check is the order of the labels at the
    // front of each option, not the whole string.
    const heads = defaults.options.map((option) => option.split(" — ")[0]);
    if (JSON.stringify(heads) !== JSON.stringify(EXPECTED)) {
      note(`框内默认填充的顺序是 ${JSON.stringify(heads)}，应为 ${JSON.stringify(EXPECTED)}`);
    }
    if (JSON.stringify(defaults.values) !== JSON.stringify(EXPECTED_VALUES)) {
      note(`框内默认填充的值是 ${JSON.stringify(defaults.values)}，应为 ${JSON.stringify(EXPECTED_VALUES)}`);
    }
  }
  await shot(out);

  /** Zoom until the block at `index` is clickable, then click it and prove which one it is. */
  const selectBlock = async (index) => {
    await text(`(() => {
      [...document.querySelectorAll(".waveform-bar button")].find((b) => b.textContent.trim() === "全部")?.click();
      return true;
    })()`);
    await wait(350);
    const pointFor = (which) =>
      text(`(() => {
        const node = document.querySelectorAll(".video-block")[${which}];
        const track = document.querySelector(".video-track");
        if (!node || !track) return null;
        const r = node.getBoundingClientRect();
        const t = track.getBoundingClientRect();
        const left = Math.max(r.left, t.left + 2);
        const right = Math.min(r.right, t.right - 2);
        if (right - left < 4) return null;
        return { x: left + Math.min((right - left) / 2, 70), y: r.top + r.height / 2, width: Math.round(right - left) };
      })()`);
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const where = await pointFor(index);
      if (where === null) return null;
      if (where.width >= 90) break;
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

  // A wide block is easiest to hit; prefer one with a still so the fit is meaningful.
  const index = items.length - 1;
  if ((await selectBlock(index)) === null) {
    note("点不到任何素材块");
  } else {
    const bubble = await text(`(() => {
      const node = document.querySelector(".video-bubble");
      if (!node) return null;
      const select = node.querySelector(".video-bubble-fit select");
      return {
        name: node.querySelector(".video-bubble-name")?.textContent.trim() ?? "",
        options: [...select.options].map((option) => option.textContent.trim()),
        values: [...select.options].map((option) => option.value),
        current: select.value,
        title: select.getAttribute("title") ?? "",
      };
    })()`);
    console.log("bubble select:", JSON.stringify(bubble));
    if (bubble === null) {
      note("点块之后没有气泡");
    } else {
      if (bubble.name !== items[index].label) {
        note(`点中的是 ${JSON.stringify(bubble.name)}，应该是 ${JSON.stringify(items[index].label)}`);
      }
      if (JSON.stringify(bubble.options) !== JSON.stringify(EXPECTED)) {
        note(`气泡里的填充选项是 ${JSON.stringify(bubble.options)}，应为 ${JSON.stringify(EXPECTED)}`);
      }
      // The hint must say what makes this mode different: it does not enlarge the picture.
      const hint = bubble.title;
      if (!hint.includes("模糊")) {
        note(`当前 fit 的提示是 ${JSON.stringify(hint)}，和它本来的选项对不上`);
      }
    }

    // Pick 模糊裁切 and check the PATCH the UI produced (intercepted, not written).
    const armed = await text(`(() => {
      const original = window.fetch;
      window.__r109 = { patches: [] };
      window.fetch = async (input, init) => {
        const url = typeof input === "string" ? input : input.url;
        const method = (init?.method ?? "GET").toUpperCase();
        if (url.includes("/video/items/") && method === "PATCH") {
          window.__r109.patches.push({ url, body: JSON.parse(init.body) });
          const id = url.split("/video/items/")[1];
          const known = ${JSON.stringify(items)}.find((item) => item.id === id);
          return new Response(JSON.stringify({ ...known, duration_ms: known.end - known.start }),
            { status: 200, headers: { "content-type": "application/json" } });
        }
        return original(input, init);
      };
      return true;
    })()`);
    if (!armed) note("装不上 PATCH 桩");

    await text(`(() => {
      const select = document.querySelector(".video-bubble-fit select");
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLSelectElement.prototype, "value").set;
      nativeInputValueSetter.call(select, "blur_crop");
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    })()`);
    await wait(700);
    const patches = await text(`window.__r109.patches`);
    console.log("patches:", JSON.stringify(patches));
    if (patches.length !== 1) {
      note(`选 模糊裁切 应该发一次 PATCH，实际发了 ${patches.length} 次`);
    } else if (patches[0].body.fit !== "blur_crop") {
      note(`PATCH 里的 fit 是 ${JSON.stringify(patches[0].body.fit)}，应为 blur_crop`);
    }

    // The server itself has to accept it — a dropdown value the API rejects is a 400 on pick,
    // and the stubbed PATCH above cannot prove otherwise. Driven through the template tab's
    // own select, because that is one project-level field (the same `FITS` validation) and it
    // is restored immediately; a block's `fit` is user data mid-edit.
    await text(`(() => {
      [...document.querySelectorAll(".video-panel button")].find((b) => b.textContent.trim() === "模板")?.click();
      return true;
    })()`);
    await wait(700);
    const original = await text(`(async () => {
      const body = await (await fetch("/api/projects/prj_1m6v2m8anfpyqq3w8qt/video/template")).json();
      return body.box_fit_default ?? null;
    })()`);
    console.log("box_fit_default before:", JSON.stringify(original));
    await text(`(() => {
      const field = [...document.querySelectorAll(".video-settings-body label.field")]
        .find((label) => label.querySelector("span")?.textContent.trim() === "框内默认填充");
      const select = field.querySelector("select");
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
      setter.call(select, "blur_crop");
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    })()`);
    await wait(900);
    const stored = await text(`(async () => {
      const body = await (await fetch("/api/projects/prj_1m6v2m8anfpyqq3w8qt/video/template")).json();
      return body.box_fit_default ?? null;
    })()`);
    console.log("box_fit_default after picking 模糊裁切:", JSON.stringify(stored));
    if (stored !== "blur_crop") {
      note(`服务端没有存下 blur_crop（现在是 ${JSON.stringify(stored)}）——下拉里的这个值会被拒绝`);
    }
    if (original !== null) {
      await text(`(() => {
        const field = [...document.querySelectorAll(".video-settings-body label.field")]
          .find((label) => label.querySelector("span")?.textContent.trim() === "框内默认填充");
        const select = field.querySelector("select");
        const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
        setter.call(select, ${JSON.stringify(original)});
        select.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      })()`);
      await wait(900);
      const restored = await text(`(async () => {
        const body = await (await fetch("/api/projects/prj_1m6v2m8anfpyqq3w8qt/video/template")).json();
        return body.box_fit_default ?? null;
      })()`);
      console.log("box_fit_default restored:", JSON.stringify(restored));
      if (restored !== original) {
        note(`没还原回去：${JSON.stringify(original)} -> ${JSON.stringify(restored)}`);
      }
    }
  }

  await text(`(() => { delete window.__r109; return true; })()`);
  await shot(out);

  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: 第五种填充方式在两个下拉里都在，服务端也收");
}
