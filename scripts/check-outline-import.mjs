/**
 * 新建项目 → 从大纲导入热词, and 保存项目 → a real download plus its path.
 *
 * Both were reported as missing features by a user who then could not tell
 * whether they existed at all, so this drives the actual clicks:
 *
 * 1. Outline import only matters before the transcription runs, and at that
 *    moment the project does not exist. The old dialog could only type terms by
 *    hand; the endpoint backing the new one is stateless for exactly this case.
 *    Assertion: paste → 提取候选 → candidates appear → drop one → 加入本期热词 →
 *    it lands in the episode draft, and the dropped one does not.
 * 2. 保存项目 must reach the browser's download path. A headless browser cannot
 *    show a Downloads folder, so this asserts what the notice says (a real
 *    absolute path the 载入 dialog can read back) *and* that the blob anchor
 *    was created with `download` set — the two halves of the bug.
 *
 * Nothing is transcribed and nothing is created.
 */
export default async function run({ evaluate, shot, wait }) {
  const openNewProject = async () => {
    await evaluate(`(() => {
      const trigger = Array.from(document.querySelectorAll(".menu-trigger")).find(
        (b) => b.textContent.trim() === "项目",
      );
      if (trigger) trigger.click();
      return Boolean(trigger);
    })()`);
    await wait(200);
    await evaluate(`(() => {
      const item = Array.from(document.querySelectorAll(".menu-item")).find((b) =>
        b.textContent.includes("新建项目"),
      );
      if (item) item.click();
      return Boolean(item);
    })()`);
    await wait(500);
  };

  /** Choose an element inside the modal and dispatch a real input event. */
  const typeInto = async (selector, value) => {
    const ok = await evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return false;
      const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, ${JSON.stringify(value)});
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    })()`);
    await wait(120);
    return ok;
  };

  const clickText = async (selector, text) => {
    const ok = await evaluate(`(() => {
      const hit = Array.from(document.querySelectorAll(${JSON.stringify(selector)})).find(
        (b) => b.textContent.trim() === ${JSON.stringify(text)},
      );
      if (!hit) return false;
      hit.click();
      return true;
    })()`);
    await wait(150);
    return ok;
  };

  await openNewProject();

  // The hotword editor only appears once 转写时启用热词 is on.
  await evaluate(`(() => {
    const modal = document.querySelector(".modal-narrow");
    const label = Array.from(modal.querySelectorAll("label.checkbox")).find((l) =>
      l.textContent.includes("转写时启用热词"),
    );
    if (label && !label.querySelector("input").checked) label.querySelector("input").click();
    return true;
  })()`);
  await wait(300);

  const opened = await clickText(".outline-import > button", "从大纲导入热词…");
  console.log("outline button:", opened);
  await typeInto(".outline-import textarea", "本期嘉宾：马小褂。我们聊《漫物质》，也聊播客剪辑。主持人漫无日。");

  const hasExtract = await clickText(".outline-import-actions button", "提取候选");
  console.log("extract clicked:", hasExtract);
  // Extraction is a real LLM round trip; give it room, then wait for candidates.
  let candidates = [];
  for (let i = 0; i < 40; i += 1) {
    await wait(500);
    candidates = await evaluate(`(() => {
      const rows = Array.from(document.querySelectorAll(".outline-candidates .chip-toggle"));
      return rows.map((r) => r.textContent.replace("已有", "").trim());
    })()`);
    if (candidates.length > 0) break;
  }
  console.log("candidates:", JSON.stringify(candidates));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/outline-import-candidates.png");

  // Drop one candidate: the review step is the point, an all-or-nothing import
  // would defeat it.
  const dropped = candidates[0];
  await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll(".outline-candidates .chip-toggle"));
    const first = rows[0];
    if (!first) return false;
    first.querySelector("input").click();
    return true;
  })()`);
  await wait(200);
  const keptCount = await evaluate(`(() => {
    const on = document.querySelectorAll(".outline-candidates .chip-toggle.on").length;
    return on;
  })()`);

  const accepted = await evaluate(`(() => {
    const btn = Array.from(document.querySelectorAll(".outline-candidates button")).find((b) =>
      b.textContent.startsWith("加入本期热词"),
    );
    if (!btn) return null;
    btn.click();
    return btn.textContent.trim();
  })()`);
  await wait(500);
  const draft = await evaluate(`(() => {
    const editor = document.querySelector(".new-project-hotwords");
    if (!editor) return [];
    // The outline block has no .hotword-input; scope to the editor's own chips.
    const host = editor.querySelector(".hotword-editor");
    return host ? Array.from(host.querySelectorAll(".chips > .chip")).map((c) => c.textContent.replace("×", "").trim()) : [];
  })()`);
  console.log("accepted:", accepted, "kept:", keptCount, "draft:", JSON.stringify(draft));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/outline-import-draft.png");

  const imported = draft.length >= 2;
  const droppedAbsent = dropped ? !draft.includes(dropped) : false;

  // Close the dialog without creating anything.
  await clickText(".modal-foot button", "取消");
  await wait(400);

  // --- 保存项目 ---------------------------------------------------------
  // Intercept the anchor click so the assertion is about the *page* creating a
  // download, not about a headless browser's file handling.
  await evaluate(`(() => {
    window.__downloads = [];
    const original = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function patched() {
      if (this.download) {
        window.__downloads.push({ name: this.download, href: this.href.slice(0, 5) });
        return undefined;
      }
      return original.call(this);
    };
    return true;
  })()`);

  await evaluate(`(() => {
    const trigger = Array.from(document.querySelectorAll(".menu-trigger")).find(
      (b) => b.textContent.trim() === "项目",
    );
    if (trigger) trigger.click();
    return true;
  })()`);
  await wait(200);
  await evaluate(`(() => {
    const item = Array.from(document.querySelectorAll(".menu-item")).find((b) =>
      b.textContent.includes("保存项目"),
    );
    if (item) item.click();
    return Boolean(item);
  })()`);
  await wait(3000);

  const downloads = await evaluate(`(() => window.__downloads ?? [] )()`);
  const notice = await evaluate(`(() => {
    const el = document.querySelector(".notice");
    return el ? el.textContent.trim() : null;
  })()`);
  console.log("downloads:", JSON.stringify(downloads));
  console.log("save notice:", JSON.stringify(notice));

  // The notice must carry a real Windows path (Chinese project names included),
  // which is what 载入项目 pre-fills.
  const absolute = typeof notice === "string" && /[A-Za-z]:[\\/]/.test(notice);
  const downloaded = Array.isArray(downloads) && downloads.length === 1 && downloads[0].name.endsWith(".project");

  console.log(imported ? "import: PASS" : "import: FAIL");
  console.log(droppedAbsent ? "review: PASS" : "review: FAIL");
  console.log(downloaded ? "download: PASS" : "download: FAIL");
  console.log(absolute ? "path: PASS" : "path: FAIL");

  const ok = imported && droppedAbsent && downloaded && absolute;
  process.exitCode = ok ? 0 : 1;
}
