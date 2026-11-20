// Look at the stage, not just at its numbers. (§19.32 / §19.32)
//
// The consistency gate compares pixels against the film, which is the right check for correctness —
// but it captures the stage by clipping a rect, so it never shows whether the panel is laid out
// sensibly. This just expands 视频剪辑 and screenshots the whole page.
//
// Run: node scripts/cdp.mjs http://localhost:5173/ .tmp/p2/look.png scripts/check-p2-look.mjs

export default async ({ evaluate, send, out, shot }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  await send("Emulation.setDeviceMetricsOverride", {
    width: 2200,
    height: 1500,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await sleep(400);
  await evaluate(`
    (() => {
      const panel = document.querySelector(".video-panel");
      if (!panel || !panel.classList.contains("is-collapsed")) return false;
      [...panel.querySelectorAll("button")]
        .find((node) => (node.textContent || "").trim() === "展开")
        ?.click();
      return true;
    })()
  `);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await evaluate(`Boolean(document.querySelector(".dom-stage"))`)) break;
    await sleep(500);
  }
  // The stage is below the audio panel; bring it into view so the screenshot shows it.
  await evaluate(`document.querySelector(".dom-stage")?.scrollIntoView({ block: "center" })`);
  await sleep(800);
  const info = await evaluate(`
    (() => {
      const stage = document.querySelector(".dom-stage");
      const deck = document.querySelector(".layer-deck");
      const rect = stage?.getBoundingClientRect();
      return {
        layers: stage ? stage.children.length : 0,
        deckSlots: deck ? deck.querySelectorAll(".layer-media").length : 0,
        deckFront: deck?.dataset.deckFront ?? null,
        videos: document.querySelectorAll(".dom-stage video").length,
        stageBox: rect ? { w: Math.round(rect.width), h: Math.round(rect.height) } : null,
      };
    })()
  `);
  console.log(JSON.stringify(info));
  await shot(out);
};
