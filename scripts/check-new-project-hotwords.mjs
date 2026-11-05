/**
 * New-project dialog: hotwords must be editable *before* the transcription.
 *
 * The reported bug was a workflow inversion — the "转写时启用热词" checkbox
 * existed, but the list it referred to could only be edited after the run, when
 * it was too late to influence recognition. So the assertion is not "a checkbox
 * exists" but "ticking it reveals an editor, and terms entered there survive
 * into a draft the create path will submit".
 *
 * Nothing is transcribed here: this checks the dialog's state, not the ASR.
 */
export default async function run({ evaluate, shot, wait }) {
  const openDialog = async () => {
    await evaluate(`(() => {
      const triggers = Array.from(document.querySelectorAll(".menu-trigger"));
      const project = triggers.find((b) => b.textContent.trim() === "项目");
      if (project) project.click();
      return true;
    })()`);
    await wait(200);
    await evaluate(`(() => {
      const items = Array.from(document.querySelectorAll(".menu-item"));
      const hit = items.find((b) => b.textContent.includes("新建项目"));
      if (hit) hit.click();
      return true;
    })()`);
    await wait(500);
  };

  const readDialog = () =>
    evaluate(`(() => {
      const modal = document.querySelector(".modal-narrow");
      if (!modal) return { open: false };
      const checkbox = Array.from(modal.querySelectorAll("label.checkbox")).find((l) =>
        l.textContent.includes("转写时启用热词"),
      );
      const input = checkbox ? checkbox.querySelector("input") : null;
      const editor = modal.querySelector(".new-project-hotwords");
      const tabs = editor
        ? Array.from(editor.querySelectorAll(".tab")).map((t) => t.textContent.trim())
        : [];
      const chips = editor
        ? Array.from(editor.querySelectorAll(".chip")).map((c) => c.textContent.replace("×", "").trim())
        : [];
      const note = editor ? editor.querySelector(".section-note")?.textContent.trim() : null;
      return {
        open: true,
        checked: input ? input.checked : null,
        editorVisible: Boolean(editor),
        tabs,
        chips,
        note,
      };
    })()`);

  await openDialog();
  const initial = await readDialog();
  console.log("initial:", JSON.stringify(initial));

  // Ticking the box is the whole point of the report. The dialog mirrors the
  // saved 设置 value on open, so it can start either way — normalise to off
  // first, then prove that ticking reveals the editor.
  await evaluate(`(() => {
    const modal = document.querySelector(".modal-narrow");
    const label = Array.from(modal.querySelectorAll("label.checkbox")).find((l) =>
      l.textContent.includes("转写时启用热词"),
    );
    const input = label.querySelector("input");
    if (input.checked) input.click();
    return input.checked;
  })()`);
  await wait(300);
  const hidden = await readDialog();
  console.log("unchecked:", JSON.stringify(hidden));

  await evaluate(`(() => {
    const modal = document.querySelector(".modal-narrow");
    const label = Array.from(modal.querySelectorAll("label.checkbox")).find((l) =>
      l.textContent.includes("转写时启用热词"),
    );
    label.querySelector("input").click();
    return true;
  })()`);
  await wait(400);
  const revealed = await readDialog();
  console.log("after check:", JSON.stringify(revealed));

  // Type a term into the episode tab and confirm it lands in the draft.
  await evaluate(`(() => {
    const modal = document.querySelector(".modal-narrow");
    const input = modal.querySelector(".hotword-input input");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(input, "马小褂");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`);
  await wait(150);
  await evaluate(`(() => {
    const modal = document.querySelector(".modal-narrow");
    const buttons = Array.from(modal.querySelectorAll(".hotword-input button"));
    const add = buttons.find((b) => b.textContent.trim() === "添加");
    if (add) add.click();
    return true;
  })()`);
  await wait(400);
  const withTerm = await readDialog();
  console.log("after add:", JSON.stringify(withTerm));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/new-project-hotwords.png");

  // Global tab must be reachable and already contain the shared glossary.
  await evaluate(`(() => {
    const modal = document.querySelector(".modal-narrow");
    const tabs = Array.from(modal.querySelectorAll(".tab"));
    const global = tabs.find((t) => t.textContent.includes("全局"));
    if (global) global.click();
    return true;
  })()`);
  await wait(300);
  const globalTab = await readDialog();
  console.log("global tab:", JSON.stringify(globalTab));

  const ok =
    initial.open &&
    hidden.editorVisible === false &&
    revealed.editorVisible === true &&
    withTerm.chips.includes("马小褂") &&
    withTerm.note.includes("3") &&
    globalTab.chips.includes("漫物质");
  console.log(ok ? "PASS" : "FAIL");
  process.exitCode = ok ? 0 : 1;
}
