/**
 * 设置 → 一键清理, checked in a real browser.
 *
 * The settings page is where the two decisions that change what cleanup does
 * live: the 口癖 list and the silence thresholds. A textarea that does not round
 * trip, or a numeric field whose PATCH is rejected by the server's range check,
 * looks fine in a screenshot and fails on save — so this drives an actual edit,
 * reads the value back off the server, and restores it.
 */
export default async function run({ evaluate, shot, wait }) {
  const openSection = async (menuLabel, itemLabel) => {
    await evaluate(`(() => {
      const triggers = Array.from(document.querySelectorAll(".menu-trigger"));
      const hit = triggers.find((b) => b.textContent.trim() === ${JSON.stringify(menuLabel)});
      if (hit) hit.click();
      return Boolean(hit);
    })()`);
    await wait(200);
    const clicked = await evaluate(`(() => {
      const items = Array.from(document.querySelectorAll(".menu-item"));
      const hit = items.find((b) => b.textContent.includes(${JSON.stringify(itemLabel)}));
      if (hit) hit.click();
      return Boolean(hit);
    })()`);
    await wait(900);
    return clicked;
  };

  const readSection = () =>
    evaluate(`(() => {
      const section = document.querySelector(".settings-cleanup");
      if (!section) return { present: false };
      const textarea = section.querySelector("textarea");
      const numbers = Array.from(section.querySelectorAll("input[type=number]")).map((i) => ({
        value: i.value,
        min: i.min,
        max: i.max,
      }));
      return {
        present: true,
        heading: section.querySelector("h3")?.textContent ?? "",
        fillerLines: textarea ? textarea.value.split("\\n").filter(Boolean).length : 0,
        hasFillerPhrase: textarea ? textarea.value.includes("嗯") : false,
        numbers,
        subhead: section.querySelector(".settings-subhead")?.textContent ?? "",
      };
    })()`);

  const opened = await openSection("设置", "一键清理设置");
  const section = await readSection();
  console.log("section:", JSON.stringify(section));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/settings-cleanup.png");

  // Edit the threshold through the real input, save, and read it back.
  const patched = await evaluate(`(() => {
    const section = document.querySelector(".settings-cleanup");
    const inputs = Array.from(section.querySelectorAll("input[type=number]"));
    const target = inputs[0];
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(target, "450");
    target.dispatchEvent(new Event("input", { bubbles: true }));
    return target.value;
  })()`);
  await wait(200);
  await evaluate(`(() => {
    const buttons = Array.from(document.querySelectorAll(".modal-foot button"));
    const save = buttons.find((b) => b.textContent.trim() === "保存");
    if (save) save.click();
    return Boolean(save);
  })()`);
  await wait(900);

  const stored = await evaluate(
    `fetch("/api/settings").then((r) => r.json()).then((s) => s.cleanup_min_silence_ms)`,
  );
  console.log("patched:", patched, "stored:", stored);

  // Put it back so the check leaves no trace.
  await evaluate(
    `fetch("/api/settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values: { cleanup_min_silence_ms: 300 } }) }).then((r) => r.json()).then((s) => s.cleanup_min_silence_ms)`,
  );

  const ok =
    opened &&
    section.present &&
    section.heading === "一键清理" &&
    section.fillerLines > 40 &&
    section.hasFillerPhrase &&
    section.numbers.length === 5 &&
    section.subhead === "静音检测器" &&
    String(stored) === "450";
  console.log(ok ? "PASS" : "FAIL");
  process.exitCode = ok ? 0 : 1;
}
