// Walk the video panel and check the two things P0-3 changed in the DOM:
//   1. a still appears on its own after the playhead stops (no button press),
//   2. while a new frame is composing the previous one stays on screen, dimmed, rather than
//      the box going blank.
//
// The clock conversion (P0-1) cannot be shown here: this app loads the most recent project,
// and the two projects with deletions have no video template. Its direction is pinned by
// `apps/web/src/timeMap.test.mjs` and by `.tmp/research/p0clock.py`, which showed the two
// instants produce two different pictures (1202 ms apart) on the M3 project.
//
// Run:
//   node scripts/cdp.mjs http://127.0.0.1:8317 .tmp/p0/panel.png scripts/check-p0-preview.mjs

export default async ({ evaluate, shot, wait, out }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const click = async (selector) => {
    await evaluate(`
      (() => {
        const node = document.querySelector(${JSON.stringify(selector)});
        if (!node) return false;
        node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        return true;
      })()
    `);
  };
  const state = () =>
    evaluate(`
      (() => {
        const img = document.querySelector(".video-preview-box img");
        const box = document.querySelector(".video-preview-box");
        const note = document.querySelector(".edl-actions .muted");
        const placeholder = document.querySelector(".video-preview-box p");
        return {
          hasImg: Boolean(img),
          src: img ? img.getAttribute("src") : null,
          stale: img ? img.className : null,
          busy: box ? box.getAttribute("aria-busy") : null,
          placeholder: placeholder ? placeholder.textContent : null,
          note: note ? note.textContent : null,
        };
      })()
    `);

  console.log("panel present?", await evaluate(`Boolean(document.querySelector(".video-panel"))`));

  // The panel starts folded; open it.
  await click(".video-panel .panel-head button");
  await sleep(600);
  await shot(out);

  console.log("before any seek:", JSON.stringify(await state()));

  // Let the automatic preview fire: the effect runs 300 ms after the playhead stops, and the
  // still itself takes ~0.65 s. Nothing here presses 生成静帧.
  await sleep(3000);
  await shot(out);
  const afterAuto = await state();
  console.log("after auto preview:", JSON.stringify(afterAuto));

  // Seek somewhere else through the transport, then watch the transitional state.
  // `.sentence-time` is the button that seeks ("选中这一句并跳到这里"); the pick box next to
  // it only toggles selection and never moves the playhead.
  await evaluate(`
    (() => {
      const times = document.querySelectorAll(".sentence-list .sentence-time");
      const target = times[Math.min(20, times.length - 1)];
      if (!target) return false;
      target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      return true;
    })()
  `);
  await sleep(400);
  const during = await state();
  console.log("while recomposing:", JSON.stringify(during));
  await shot(out);

  await sleep(3000);
  const afterSeek = await state();
  console.log("after seek:", JSON.stringify(afterSeek));
  await shot(out);

  console.log(JSON.stringify({
    autoRanWithoutButton: Boolean(afterAuto.src),
    srcChangedOnSeek: Boolean(afterSeek.src) && afterSeek.src !== afterAuto.src,
    placeholderBeforeFirstFrame: afterAuto.placeholder,
    dimmedWhileBusy: during.stale === "is-stale" || during.busy === "true",
  }, null, 2));
};
