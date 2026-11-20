/**
 * P3 走查：视频区里的头像位绑定。
 *
 * 1. 打开项目，展开视频区
 * 2. 展开「版式」详情
 * 3. 把「彼方」这个头像位绑到一个说话人，截图
 * 4. 读回下拉框的实际值和时长文案
 */
export default async function run({ evaluate, shot, wait, out }) {
  const result = { steps: [] };

  const clickByText = async (selector, text) => {
    await evaluate(`(() => {
      const nodes = [...document.querySelectorAll(${JSON.stringify(selector)})];
      const hit = nodes.find((n) => n.textContent && n.textContent.includes(${JSON.stringify(text)}));
      if (!hit) throw new Error("no node matching " + ${JSON.stringify(text)});
      hit.click();
      return true;
    })()`);
    return true;
  };

  // The video area is collapsed by default; open it from the settings menu.
  await evaluate(`(() => {
    const buttons = [...document.querySelectorAll("button, [role=menuitem]")];
    const found = buttons.find((b) => b.textContent && b.textContent.trim() === "设置");
    if (found) found.click();
    return Boolean(found);
  })()`);
  await wait(300);
  await clickByText('[role="menuitem"], button', "显示视频区");
  await wait(900);

  result.steps.push(
    await evaluate(`(() => {
      const summary = [...document.querySelectorAll("summary")].find((s) =>
        s.textContent.includes("版式"),
      );
      if (summary && summary.parentElement && !summary.parentElement.open) summary.click();
      return {
        detailsOpen: summary ? summary.parentElement.open : null,
        rows: document.querySelectorAll(".video-avatar-row").length,
        text: summary ? summary.textContent.trim().slice(0, 120) : null,
      };
    })()`),
  );
  await wait(400);

  // Bind the first row to whatever speaker is on offer.
  result.steps.push(
    await evaluate(`(() => {
      const row = document.querySelector(".video-avatar-row");
      if (!row) return { bound: false, reason: "no row" };
      const select = row.querySelector("select");
      const options = [...select.options].map((o) => ({ value: o.value, text: o.textContent }));
      if (options.length < 2) return { bound: false, reason: "no speakers", options };
      select.value = options[1].value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return { bound: true, chose: options[1], options };
    })()`),
  );
  await wait(2500);

  result.steps.push(
    await evaluate(`(() => ({
      rows: [...document.querySelectorAll(".video-avatar-row")].map((row) => ({
        name: row.querySelector(".video-avatar-name")?.textContent?.trim(),
        speaker: row.querySelector("select")?.selectedOptions?.[0]?.textContent,
        talk: row.querySelector(".video-avatar-talk")?.textContent,
      })),
    }))`),
  );

  await shot(out);
  return result;
}
