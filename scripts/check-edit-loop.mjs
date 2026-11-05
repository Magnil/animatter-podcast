/**
 * End-to-end check of the word-level editing loop against the live backend.
 *
 * It exercises the exact path a user takes — click a word, click 删除 — and then
 * reads the *rendered* state back, because the interesting failures are
 * "the cut happened but the transcript still looks untouched" and "the
 * correction was saved but the word is not blue".
 */
export default async function run({ evaluate, click, shot, wait }) {
  const before = await evaluate(`(() => {
    const words = Array.from(document.querySelectorAll(".sentence .token"));
    const target = words[4];
    return {
      total: words.length,
      text: target.textContent,
      start: Array.from(words).findIndex((w) => w === target),
    };
  })()`);
  console.log("before:", JSON.stringify(before));

  // Select word #4 and cut it.
  await evaluate(`(() => {
    const words = document.querySelectorAll(".sentence .token");
    words[4].dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  })()`);
  await wait(200);
  await click(".word-bubble .bubble-cut");
  await wait(1800);

  const afterCut = await evaluate(`(() => {
    const cut = Array.from(document.querySelectorAll(".token.cut"));
    const first = cut[0];
    const style = first ? getComputedStyle(first) : null;
    return {
      cutCount: cut.length,
      cutText: first ? first.textContent : null,
      colour: style ? style.color : null,
      decoration: style ? style.textDecorationLine : null,
      removedTag: Boolean(document.querySelector(".removed-tag")),
      header: document.querySelector(".stage-top .muted")?.textContent ?? null,
    };
  })()`);
  console.log("after cut:", JSON.stringify(afterCut));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/live-cut.png");

  // Now correct the word before it.
  await evaluate(`(() => {
    const words = document.querySelectorAll(".sentence .token");
    words[3].dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  })()`);
  await wait(200);
  await click(".word-bubble .bubble-fix");
  await wait(200);
  await evaluate(`(() => {
    const input = document.querySelector(".bubble-form input");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(input, "示范");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  })()`);
  await wait(150);
  await click(".bubble-form button[type=submit]");
  await wait(1800);

  const afterFix = await evaluate(`(() => {
    const revised = Array.from(document.querySelectorAll(".token.revised"));
    const first = revised[0];
    const style = first ? getComputedStyle(first) : null;
    return {
      revisedCount: revised.length,
      revisedText: first ? first.textContent : null,
      colour: style ? style.color : null,
      weight: style ? style.fontWeight : null,
      background: style ? style.backgroundColor : null,
    };
  })()`);
  console.log("after fix:", JSON.stringify(afterFix));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/live-fix.png");

  const ok =
    afterCut.cutCount === 1 &&
    afterCut.decoration.includes("line-through") &&
    afterFix.revisedCount === 1 &&
    afterFix.revisedText?.includes("示范");
  console.log(ok ? "PASS" : "FAIL");
  process.exitCode = ok ? 0 : 1;
}
