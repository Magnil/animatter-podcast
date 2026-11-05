/**
 * Drives the workbench through a real edit and screenshots each state.
 *
 * Assertions are on *computed* values, not pixels: "the sidebar is 420px wide"
 * and "this word has the `revised` class" are the facts that break, and a
 * screenshot alone cannot tell a reviewer which of them regressed.
 */
export default async function run({ evaluate, click, shot, wait }) {
  const layout = await evaluate(`(() => {
    const sidebar = document.querySelector(".sidebar");
    const grid = document.querySelector(".bench-body");
    return {
      sidebar: sidebar ? Math.round(sidebar.getBoundingClientRect().width) : null,
      columns: grid ? getComputedStyle(grid).gridTemplateColumns : null,
      words: document.querySelectorAll(".token").length,
      sentences: document.querySelectorAll(".sentence").length,
    };
  })()`);
  console.log("layout:", JSON.stringify(layout));

  // Select one word: the bubble must appear under it.
  await click(".sentence .token");
  await wait(200);
  const bubble = await evaluate(`(() => {
    const b = document.querySelector(".word-bubble");
    if (!b) return { present: false };
    const r = b.getBoundingClientRect();
    const word = document.querySelector(".token.selected").getBoundingClientRect();
    return {
      present: true,
      buttons: Array.from(b.querySelectorAll("button")).map((x) => x.textContent.trim()),
      offscreen: r.left < 0 || r.right > window.innerWidth || r.top < 0,
      aboveOrBelow: r.top >= word.bottom ? "below" : "above",
    };
  })()`);
  console.log("bubble:", JSON.stringify(bubble));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/live-select.png");

  process.exitCode = bubble.present && !bubble.offscreen ? 0 : 1;
}
