/**
 * Sidebar hotword panel: adding a term must not wipe the episode's others.
 *
 * ``POST /media/{id}/hotwords`` *replaces* the per-episode list — that is what
 * outline extraction needs. So a naïve "add" that posts only the new term
 * silently deletes every existing one. The client wraps it as read-modify-write;
 * this check proves the wrapper held, by adding a second term and asserting the
 * first is still there, then removing it again and leaving the list as found.
 */
export default async function run({ evaluate, shot, wait }) {
  const readPanel = () =>
    evaluate(`(() => {
      const panel = document.querySelector(".hotword-panel");
      if (!panel) return { open: false };
      const tabs = Array.from(panel.querySelectorAll(".tab")).map((t) => t.textContent.trim());
      return {
        open: true,
        tabs,
        chips: Array.from(panel.querySelectorAll(".chip")).map((c) =>
          c.textContent.replace("×", "").trim(),
        ),
        summary: panel.querySelector(".panel-head .muted")?.textContent.trim() ?? null,
      };
    })()`);

  // Make sure the episode tab is the active one.
  await evaluate(`(() => {
    const tabs = Array.from(document.querySelectorAll(".hotword-panel .tab"));
    const episode = tabs.find((t) => t.textContent.includes("本期"));
    if (episode) episode.click();
    return true;
  })()`);
  await wait(250);
  const before = await readPanel();
  console.log("before:", JSON.stringify(before));

  const typeAndAdd = async (text) => {
    await evaluate(`(() => {
      const input = document.querySelector(".hotword-panel .hotword-input input");
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
      setter.call(input, ${JSON.stringify(text)});
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    })()`);
    await wait(120);
    await evaluate(`(() => {
      const buttons = Array.from(document.querySelectorAll(".hotword-panel .hotword-input button"));
      const add = buttons.find((b) => b.textContent.trim() === "添加");
      if (add) add.click();
      return true;
    })()`);
    await wait(600);
  };

  // Keep a copy of the seeded term so the removal below restores the state.
  await typeAndAdd("测试用临时词");
  const added = await readPanel();
  console.log("after add:", JSON.stringify(added));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/hotword-panel.png");

  const previousKept = before.chips.every((chip) => added.chips.includes(chip));
  const newPresent = added.chips.includes("测试用临时词");

  await evaluate(`(() => {
    const chips = Array.from(document.querySelectorAll(".hotword-panel .chip"));
    const target = chips.find((c) => c.textContent.includes("测试用临时词"));
    const button = target ? target.querySelector("button") : null;
    if (button) button.click();
    return true;
  })()`);
  await wait(700);
  const removed = await readPanel();
  console.log("after remove:", JSON.stringify(removed));

  const restored =
    !removed.chips.includes("测试用临时词") &&
    before.chips.every((chip) => removed.chips.includes(chip));

  const ok = before.open && newPresent && previousKept && restored;
  console.log(ok ? "PASS" : "FAIL");
  process.exitCode = ok ? 0 : 1;
}
