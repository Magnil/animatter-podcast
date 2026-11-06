/**
 * 工作台侧边栏的「从大纲自动提取」必须真的把词写进本期热词表。
 *
 * The old flow called the suggest endpoint, refetched the (unchanged) list, and
 * announced "从大纲提取了 N 个热词". Suggest only *proposes* — nothing is written
 * until the accept call — so the notice was true about the extraction and false
 * about the result: the chips stayed empty and the terms never reached the next
 * transcription. This drives the panel for real and asserts the chip list grows.
 *
 * Runs against the verification project, so the replaced list is throwaway.
 */
export default async function run({ evaluate, shot, wait }) {
  const readPanel = () =>
    evaluate(`(() => {
      const panel = document.querySelector(".hotword-panel");
      if (!panel) return { present: false };
      const episodeTab = Array.from(panel.querySelectorAll(".tab")).find((t) =>
        t.textContent.includes("本期"),
      );
      const chips = Array.from(panel.querySelectorAll(".chips > .chip")).map((c) =>
        c.textContent.replace("×", "").trim(),
      );
      return {
        present: true,
        tab: episodeTab ? episodeTab.textContent.trim() : null,
        chips,
        notice: document.querySelector(".notice")?.textContent.trim() ?? null,
      };
    })()`);

  const before = await readPanel();
  console.log("before:", JSON.stringify(before));

  // Episode tab, then open the outline box.
  await evaluate(`(() => {
    const panel = document.querySelector(".hotword-panel");
    const tab = Array.from(panel.querySelectorAll(".tab")).find((t) => t.textContent.includes("本期"));
    if (tab) tab.click();
    return Boolean(tab);
  })()`);
  await wait(200);

  await evaluate(`(() => {
    const btn = document.querySelector(".hotword-outline > button");
    if (btn) btn.click();
    return Boolean(btn);
  })()`);
  await wait(300);

  await evaluate(`(() => {
    const area = document.querySelector(".hotword-outline textarea");
    if (!area) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    setter.call(area, "本期嘉宾：马小褂。聊《漫物质》与播客剪辑，主持人漫无日。");
    area.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`);
  await wait(200);

  await evaluate(`(() => {
    const btn = Array.from(document.querySelectorAll(".hotword-outline button")).find(
      (b) => b.textContent.trim() === "提取热词",
    );
    if (btn) btn.click();
    return Boolean(btn);
  })()`);

  // Two round trips: save outline, extract, then accept.
  let after = null;
  for (let i = 0; i < 50; i += 1) {
    await wait(500);
    after = await readPanel();
    if (after.chips.length > before.chips.length) break;
  }
  console.log("after:", JSON.stringify(after));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/workbench-outline-extract.png");

  const grew = after !== null && after.chips.length > before.chips.length;
  const persisted = after !== null && ["马小褂", "漫物质"].filter((w) => after.chips.includes(w)).length >= 1;
  console.log(grew ? "chips: PASS" : "chips: FAIL");
  console.log(persisted ? "terms: PASS" : "terms: FAIL");
  process.exitCode = grew && persisted ? 0 : 1;
}
