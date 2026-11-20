// 走查：热词 / 操作记录两个面板能收起，收起后字幕区铺满左侧。
//
// 检查四件事：
//   1. 两个面板各有「收起」按钮，点击后按钮变成「展开」，标题还在
//   2. 收起后正文消失（热词编辑器和操作记录列表都不在了）
//   3. 字幕区（.transcript-pane / .sentence-list）变高了——这才是收起的意义
//   4. 字幕区自己没有「收起」按钮，标题是「字幕」

const measure = `(() => {
  const px = (node) => (node ? Math.round(node.getBoundingClientRect().height) : null);
  const panel = (name) => {
    const h3 = [...document.querySelectorAll(".panel h3")].find((n) => n.textContent.trim() === name);
    return h3 ? h3.closest(".panel") : null;
  };
  const hot = panel("热词");
  const his = panel("操作记录");
  const rail = document.querySelector(".sentence-rail");
  const list = document.querySelector(".sentence-list");
  return {
    hotword: {
      exists: Boolean(hot),
      collapsed: hot ? hot.classList.contains("is-collapsed") : null,
      height: px(hot),
      button: hot ? (hot.querySelector(".panel-head button") || {}).textContent || null : null,
      hasBody: hot ? Boolean(hot.querySelector(".hotword-editor, input")) : null,
    },
    history: {
      exists: Boolean(his),
      collapsed: his ? his.classList.contains("is-collapsed") : null,
      height: px(his),
      button: his ? ([...his.querySelectorAll(".panel-head button")].slice(-1)[0] || {}).textContent || null : null,
      hasBody: his ? Boolean(his.querySelector(".history-list, .muted")) : null,
    },
    rail: {
      title: rail ? (rail.querySelector(".rail-title") || {}).textContent || null : null,
      buttons: rail ? [...rail.querySelectorAll("button")].map((b) => b.textContent.trim()) : null,
      hasCollapse: rail
        ? [...rail.querySelectorAll("button")].some((b) => /收起|展开/.test(b.textContent))
        : null,
      railHeight: px(rail),
    },
    listHeight: px(list),
  };
})()`;

const clickPanelButton = (name, label) => `(() => {
  const h3 = [...document.querySelectorAll(".panel h3")].find((n) => n.textContent.trim() === ${JSON.stringify(name)});
  if (!h3) return null;
  const panel = h3.closest(".panel");
  const b = [...panel.querySelectorAll(".panel-head button")].find(
    (n) => n.textContent.trim() === ${JSON.stringify(label)},
  );
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
})()`;

export default async ({ evaluate, mouse, wait, shot, out }) => {
  await wait(3500);
  const result = { initial: await evaluate(measure) };
  await shot(`${out}-1-before`);

  // 收起热词
  const hotBtn = await evaluate(clickPanelButton("热词", "收起"));
  if (hotBtn) {
    await mouse("mousePressed", hotBtn.x, hotBtn.y);
    await mouse("mouseReleased", hotBtn.x, hotBtn.y);
    await wait(700);
  }
  result.afterHotword = await evaluate(measure);
  await shot(`${out}-2-hotword-collapsed`);

  // 收起操作记录
  const hisBtn = await evaluate(clickPanelButton("操作记录", "收起"));
  if (hisBtn) {
    await mouse("mousePressed", hisBtn.x, hisBtn.y);
    await mouse("mouseReleased", hisBtn.x, hisBtn.y);
    await wait(700);
  }
  result.afterBoth = await evaluate(measure);
  await shot(`${out}-3-both-collapsed`);

  // 展开热词，确认能回来
  const hotExpand = await evaluate(clickPanelButton("热词", "展开"));
  if (hotExpand) {
    await mouse("mousePressed", hotExpand.x, hotExpand.y);
    await mouse("mouseReleased", hotExpand.x, hotExpand.y);
    await wait(700);
  }
  result.afterExpand = await evaluate(measure);
  await shot(`${out}-4-expanded`);

  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
