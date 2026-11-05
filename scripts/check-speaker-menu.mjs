/**
 * Opens the speaker menu in the real workbench and checks it stays on screen.
 *
 * The menu is absolutely positioned inside the sentence header; if it overflows
 * the transcript column the rename field becomes unclickable, which is the kind
 * of thing only a real layout can tell you.
 */
export default async function run({ evaluate, click, shot, wait }) {
  await click(".sentence .speaker-name");
  await wait(250);

  const menu = await evaluate(`(() => {
    const el = document.querySelector(".speaker-menu");
    if (!el) return { present: false };
    const r = el.getBoundingClientRect();
    const col = document.querySelector(".sidebar").getBoundingClientRect();
    return {
      present: true,
      items: Array.from(el.querySelectorAll("button")).map((b) => b.textContent.trim()),
      insideColumn: r.left >= col.left && r.right <= col.right + 1,
      insideViewport: r.top >= 0 && r.bottom <= window.innerHeight && r.right <= window.innerWidth,
      width: Math.round(r.width),
    };
  })()`);
  console.log("speaker menu:", JSON.stringify(menu));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/live-speaker.png");

  // Rename through the real form.
  await evaluate(`(() => {
    const items = Array.from(document.querySelectorAll(".speaker-menu button"));
    items.find((b) => b.textContent.includes("重命名"))?.click();
  })()`);
  await wait(200);
  await evaluate(`(() => {
    const input = document.querySelector(".speaker-menu input");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(input, "主持人 阿豆");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  })()`);
  await wait(150);
  await click(".speaker-menu-row button[type=submit]");
  await wait(1500);

  const renamed = await evaluate(`(() => Array.from(document.querySelectorAll(".speaker-name")).map((b) => b.textContent.replace("…", "").trim()))()`);
  console.log("names after rename:", JSON.stringify(renamed));

  const ok =
    menu.present && menu.insideViewport && renamed.some((n) => n.includes("阿豆"));
  console.log(ok ? "PASS" : "FAIL");
  process.exitCode = ok ? 0 : 1;
}
