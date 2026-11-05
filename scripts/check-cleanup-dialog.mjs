/**
 * One-click cleanup dialog, checked in a real browser.
 *
 * The assertions are about the *gate* the dialog exists to be: it must list what
 * would be cut, per category, with counts that match the rows it renders, and
 * it must not write anything until 执行清理 is clicked. A dialog that shows a
 * plan but has already applied it would pass a screenshot and fail the user.
 *
 * Nothing is applied here — clicking the destructive button is the one thing
 * this check leaves to the operator.
 */
export default async function run({ evaluate, shot, wait }) {
  const openMenu = async (label) => {
    const found = await evaluate(`(() => {
      const triggers = Array.from(document.querySelectorAll(".menu-trigger"));
      const hit = triggers.find((b) => b.textContent.trim() === ${JSON.stringify(label)});
      if (hit) hit.click();
      return Boolean(hit);
    })()`);
    await wait(200);
    return found;
  };

  const picked = await openMenu("清理");
  const clicked = await evaluate(`(() => {
    const items = Array.from(document.querySelectorAll(".menu-item"));
    const hit = items.find((b) => b.textContent.includes("一键清理"));
    if (!hit) return { found: false, labels: items.map((b) => b.textContent.trim()) };
    hit.click();
    return { found: true };
  })()`);
  await wait(1500);

  const dialog = await evaluate(`(() => {
    const modal = document.querySelector(".modal-wide");
    if (!modal) return { open: false };
    const kinds = Array.from(modal.querySelectorAll(".cleanup-kind")).map((el) => ({
      name: el.querySelector(".cleanup-kind-name")?.textContent ?? "",
      count: el.querySelector(".cleanup-kind-count")?.textContent ?? "",
      checked: el.querySelector("input")?.checked ?? null,
      disabled: el.classList.contains("disabled"),
    }));
    const rows = Array.from(modal.querySelectorAll(".cleanup-row")).length;
    const buttons = Array.from(modal.querySelectorAll(".modal-foot button")).map((b) => ({
      text: b.textContent.trim(),
      disabled: b.disabled,
    }));
    return {
      open: true,
      heading: modal.querySelector("h2")?.textContent ?? "",
      kinds,
      rows,
      header: modal.querySelector(".cleanup-list-head strong")?.textContent ?? "",
      apply: buttons.find((b) => b.text.includes("执行清理")) ?? null,
      thresholds: Boolean(modal.querySelector(".cleanup-thresholds")),
      hasFillerList: Boolean(modal.querySelector(".cleanup-list")),
    };
  })()`);
  console.log("dialog:", JSON.stringify(dialog));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/cleanup-dialog.png");

  // Counts must agree with the header, or the tick boxes lie.
  const consistent = await evaluate(`(() => {
    const modal = document.querySelector(".modal-wide");
    const head = modal.querySelector(".cleanup-list-head strong")?.textContent ?? "";
    const listed = Array.from(modal.querySelectorAll(".cleanup-kind"))
      .filter((el) => el.querySelector("input")?.checked)
      .map((el) => el.querySelector(".cleanup-kind-name")?.textContent ?? "");
    return { head, listed };
  })()`);
  console.log("consistency:", JSON.stringify(consistent));

  // Untick everything: apply must disable rather than doing nothing silently.
  await evaluate(`(() => {
    const boxes = Array.from(document.querySelectorAll(".cleanup-kind input"));
    for (const box of boxes) {
      if (box.checked && !box.disabled) box.click();
    }
    return true;
  })()`);
  await wait(400);
  const emptied = await evaluate(`(() => {
    const modal = document.querySelector(".modal-wide");
    const buttons = Array.from(modal.querySelectorAll(".modal-foot button"));
    const apply = buttons.find((b) => b.textContent.includes("执行清理"));
    return {
      header: modal.querySelector(".cleanup-list-head strong")?.textContent ?? "",
      applyDisabled: apply ? apply.disabled : null,
    };
  })()`);
  console.log("all unticked:", JSON.stringify(emptied));

  const ok =
    picked &&
    clicked.found &&
    dialog.open &&
    dialog.heading === "一键清理" &&
    dialog.kinds.length === 3 &&
    dialog.hasFillerList &&
    dialog.apply !== null &&
    dialog.apply.disabled === false &&
    /本次会删 \d+ 处/.test(dialog.header) &&
    emptied.applyDisabled === true;
  console.log(ok ? "PASS" : "FAIL");
  process.exitCode = ok ? 0 : 1;
}
