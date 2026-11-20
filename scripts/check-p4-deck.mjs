/**
 * P4 walkthrough: enable the video area, expand the deck panel, page through it, and check
 * the panel is showing real data rather than a placeholder.
 *
 * Two things this pins down that a screenshot cannot:
 *
 * * the preview `<img>` actually *loaded* (`naturalWidth > 0`) — a 404 looks identical to a
 *   blank slide in a picture;
 * * the editor's title matches the page that is selected — the earlier bug was the preview
 *   staying on the previous page after an edit.
 */
export default async function run({ evaluate, shot, wait, out }) {
  // 设置 → 显示视频区. It starts off, and it is a menu item, not a persisted preference.
  await evaluate(`(() => {
    const menu = [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "设置");
    menu?.click();
    return true;
  })()`);
  await wait(300);
  const toggled = await evaluate(`(() => {
    const item = [...document.querySelectorAll("button, [role=menuitem]")].find(
      (b) => b.textContent.includes("显示视频区"),
    );
    item?.click();
    return !!item;
  })()`);
  console.log("toggled video area:", toggled);
  await wait(900);

  const opened = await evaluate(`(() => {
    const details = [...document.querySelectorAll("details")].find(
      (item) => item.querySelector("summary")?.textContent?.includes("演示稿"),
    );
    if (!details) return { found: false };
    details.open = true;
    return { found: true, open: details.open };
  })()`);
  console.log("deck details:", JSON.stringify(opened));

  await wait(1500);

  const panel = await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".deck-list li button")];
    const first = rows[0];
    const img = document.querySelector(".deck-preview");
    return {
      pages: rows.length,
      firstTitle: first?.querySelector(".deck-list-title")?.textContent ?? null,
      firstTime: first?.querySelector(".deck-list-time")?.textContent ?? null,
      previewSrc: img ? img.getAttribute("src") : null,
      previewLoaded: img ? img.naturalWidth > 0 : false,
      previewSize: img ? [img.naturalWidth, img.naturalHeight] : null,
    };
  })()`);
  console.log("panel:", JSON.stringify(panel));
  await shot(out);

  // Select the third page and check the editor and the preview follow it.
  await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".deck-list li button")];
    rows[2]?.click();
    return rows.length;
  })()`);
  await wait(1800);

  const third = await evaluate(`(() => {
    const active = document.querySelector(".deck-list li button.active");
    const title = document.querySelector(".deck-editor input");
    const img = document.querySelector(".deck-preview");
    return {
      activeTitle: active?.querySelector(".deck-list-title")?.textContent ?? null,
      editorTitle: title ? title.value : null,
      previewLoaded: img ? img.naturalWidth > 0 : false,
      previewSize: img ? [img.naturalWidth, img.naturalHeight] : null,
      sourceChars: (document.querySelector(".deck-source p")?.textContent ?? "").length,
    };
  })()`);
  console.log("third page:", JSON.stringify(third));
  await evaluate(`(() => {
    document.querySelector(".video-deck-section")?.scrollIntoView({ block: "start" });
    return true;
  })()`);
  await wait(400);
  await shot(out.replace(/\.png$/, "-page3.png"));
}
