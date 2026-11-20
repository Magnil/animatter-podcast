/**
 * 走查（第 100 轮）：演示稿不再照搬字幕，面板上要说清「这份稿是谁分的段」。
 *
 * 断言落在算出来的事实上，每条都对应一种「看起来正常、其实是错的」页面：
 *
 * 1. 页数落在 10–60：60 是 `MAX_PAGES`，10 是「一小时节目至少得聊十件事」；
 * 2. `source` 标签写的是「按话题分段」而不是「按字幕停顿分页」——模型路径真的跑过；
 * 3. **每一页都是「标题 + 2–4 条 ・要点」**，没有任何一页的正文是一整句字幕原文
 *    （判据：每行都以 ・ 开头，且每行 ≤ 28 字）。旧实现的正文是无换行的长句，
 *    所以这条在旧版本上必然失败；
 * 4. 页起始时间的**中位数 ≥ 60 s**：按话题分的段不可能比按分钟切的还碎；
 * 5. 缩略图真的加载了（`naturalWidth > 0`），并且 720×405；
 * 6. 左侧原文面板仍然显示这一页覆盖的字幕（逐页校对没有因为换生成器而失效）。
 */
export default async function run({ evaluate, shot, wait, out }) {
  const fail = (message) => {
    throw new Error(message);
  };
  await wait(1500);

  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(2000);

  await evaluate(`(() => {
    const tab = [...document.querySelectorAll(".video-settings .tab")].find(
      (b) => b.textContent.trim() === "演示稿",
    );
    if (tab && !tab.classList.contains("active")) tab.click();
    return true;
  })()`);
  await wait(2500);

  const facts = await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".deck-list li button")];
    const img = document.querySelector(".deck-preview");
    const tag = document.querySelector(".deck-source-tag");
    return {
      pages: rows.length,
      tag: tag ? tag.textContent.trim() : null,
      warnings: [...document.querySelectorAll(".deck-warnings li")].map((li) => li.textContent),
      titles: rows.map((b) => b.querySelector(".deck-list-title")?.textContent ?? ""),
      times: rows.map((b) => b.querySelector(".deck-list-time")?.textContent ?? ""),
      body: document.querySelector(".deck-editor textarea")?.value ?? null,
      editorTitle: document.querySelector(".deck-editor input")?.value ?? null,
      previewLoaded: img ? img.naturalWidth > 0 : false,
      previewSize: img ? [img.naturalWidth, img.naturalHeight] : null,
      sourceChars: (document.querySelector(".deck-source p")?.textContent ?? "").length,
    };
  })()`);
  console.log("facts:", JSON.stringify({ ...facts, body: undefined, titles: facts.titles.length }));

  if (facts.pages < 10 || facts.pages > 60) fail(`page count ${facts.pages} is not a deck`);
  if (facts.tag !== "按话题分段") fail(`source tag says ${JSON.stringify(facts.tag)}`);
  if (facts.warnings.length) fail(`unexpected warnings: ${JSON.stringify(facts.warnings)}`);
  if (facts.titles.some((title) => !title.trim())) fail("a page has no title");
  if (!facts.previewLoaded) fail("the preview img did not load");
  if (JSON.stringify(facts.previewSize) !== JSON.stringify([720, 405])) {
    fail(`preview is ${JSON.stringify(facts.previewSize)}, expected [720,405]`);
  }
  if (facts.sourceChars < 40) fail(`the source pane has only ${facts.sourceChars} chars`);
  if (!facts.editorTitle || facts.editorTitle !== facts.titles[0]) {
    fail(`editor title ${JSON.stringify(facts.editorTitle)} != list title ${JSON.stringify(facts.titles[0])}`);
  }

  // 每一页的正文必须是「・要点」行，而不是一段字幕原文。
  const bodies = [];
  for (let index = 0; index < facts.pages; index += 1) {
    const body = await evaluate(`(() => {
      const rows = [...document.querySelectorAll(".deck-list li button")];
      rows[${index}]?.click();
      return true;
    })()`);
    void body;
    await wait(320);
    const page = await evaluate(`(() => ({
      ord: document.querySelector(".deck-list li button.active .deck-list-ord")?.textContent ?? null,
      title: document.querySelector(".deck-editor input")?.value ?? "",
      body: document.querySelector(".deck-editor textarea")?.value ?? "",
    }))()`);
    bodies.push(page);
  }
  console.log("bodies:", bodies.length, JSON.stringify(bodies[0]));

  for (const page of bodies) {
    const lines = page.body.split("\n").filter((line) => line.length > 0);
    if (lines.length < 2 || lines.length > 4) {
      fail(`page ${page.ord} has ${lines.length} points, expected 2–4: ${JSON.stringify(page.body)}`);
    }
    for (const line of lines) {
      if (!line.startsWith("・")) fail(`page ${page.ord} has a non-point line: ${JSON.stringify(line)}`);
      if (line.length > 29) fail(`page ${page.ord} has a ${line.length}-char point: ${JSON.stringify(line)}`);
    }
  }

  // 页面之间的间隔：按话题分段的中位数不可能低于 60 秒。
  const gaps = [];
  for (let index = 1; index < facts.times.length; index += 1) {
    // The list prints `mm:ss.mmm` under an hour and `h:mm:ss.mmm` over it, so the parts are
    // counted from the right; a two-part parse turned 1:00:13 into 73 minutes and produced a
    // −3368 s gap, which is the kind of number that only appears when the parser is wrong.
    const toMs = (value) => {
      const [clock, millis = "0"] = value.split(".");
      const parts = clock.split(":").map(Number);
      const seconds = parts.reduce((total, part) => total * 60 + part, 0);
      return seconds * 1000 + Number(millis.padEnd(3, "0"));
    };
    gaps.push(toMs(facts.times[index]) - toMs(facts.times[index - 1]));
  }
  gaps.sort((x, y) => x - y);
  const median = gaps[Math.floor(gaps.length / 2)];
  console.log("median page span:", median / 1000, "s", JSON.stringify(gaps.map((g) => g / 1000)));
  if (median < 60_000) fail(`median page span ${median / 1000}s is a slideshow, not a deck`);

  await evaluate(`(() => {
    const rows = [...document.querySelectorAll(".deck-list li button")];
    rows[0]?.click();
    return true;
  })()`);
  await wait(600);
  await shot(out);
  return { pages: facts.pages, medianSpanSec: median / 1000 };
}
