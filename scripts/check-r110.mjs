/**
 * Round 110 walkthrough: the export entries no longer require a cut.
 *
 * **Read-only.** And this round it can be: the demo project is *itself* in the state the
 * request is about — every deletion has been undone, so the real EDL comes back with
 * `deletions: []` (verified against the API in step 0, not assumed). That is a stronger check
 * than substituting a fake response, because it is the user's own timeline.
 *
 * The first version of this script stubbed the EDL to empty and then reloaded the page, which
 * erased the stub; the follow-up tried `addScriptToEvaluateOnNewDocument` and asserted the stub
 * had done work — and it correctly reported `rewrote: 0`, i.e. "there was nothing to rewrite".
 * That failure was right: the project already had no deletions. Both are gone now in favour of
 * measuring the real thing.
 *
 * Facts asserted:
 *
 *  1. The API says this version has **zero** deletions (otherwise the rest proves nothing).
 *  2. Both 导出 entries are enabled, and neither carries a 「先删除」 hint.
 *  3. The old wording appears nowhere in the document.
 */

export default async function ({ evaluate, shot, wait, out }) {
  const failures = [];
  const note = (message) => {
    failures.push(message);
    console.log("FAIL:", message);
  };

  // --- 0. the project really is unedited -----------------------------------
  const deletions = await evaluate(`(async () => {
    const body = await (await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/edl")).json();
    return {
      count: (body.deletions || []).length,
      removed_ms: body.removed_ms,
      keeps: (body.keeps || []).length,
      output_ms: body.output_duration_ms,
      original_ms: body.original_duration_ms,
    };
  })()`);
  console.log("real EDL:", JSON.stringify(deletions));
  if (deletions.count !== 0) {
    note(`这一版现在有 ${deletions.count} 处删除，测不到「一次都没删」的状态`);
  }
  if (deletions.removed_ms !== 0) {
    note(`removed_ms=${deletions.removed_ms}，应该为 0`);
  }
  if (deletions.keeps !== 1 || deletions.output_ms !== deletions.original_ms) {
    note(`空剪辑应该是一条 keep 且成片时长 == 原始时长，实际 keeps=${deletions.keeps}`);
  }

  // --- 1. both entries, read from the open menu ----------------------------
  await evaluate(`(() => {
    const button = [...document.querySelectorAll("button")].find(
      (node) => node.textContent.trim() === "导出" && !node.closest(".modal"),
    );
    button?.click();
    return true;
  })()`);
  await wait(400);

  const entries = await evaluate(`(() => {
    const items = [...document.querySelectorAll("button")];
    const find = (needle) =>
      items.find((node) => (node.textContent || "").trim().startsWith(needle)) ?? null;
    const view = (node) =>
      node === null
        ? null
        : {
            text: node.textContent.trim(),
            disabled: node.hasAttribute("disabled"),
            title: node.getAttribute("title") || "",
          };
    return { audio: view(find("导出成片音频")), film: view(find("导出成片视频")) };
  })()`);
  console.log("export menu:", JSON.stringify(entries));

  if (entries.audio === null || entries.film === null) {
    note("导出菜单里找不到两个导出项");
  } else {
    for (const [name, entry] of [["成片音频", entries.audio], ["成片视频", entries.film]]) {
      if (entry.disabled) {
        note(`${name} 在零删除状态下应该是可点的，实际是灰的：${entry.text}`);
      }
      if (entry.title.includes("先删除")) {
        note(`${name} 的提示还写着「先删除」：${entry.title}`);
      }
    }
    // The hint must carry the real fact instead of a precondition.
    if (!/1080p/.test(entries.film.text) && !/1080p/.test(entries.film.title)) {
      note(`成片视频的说明里没有 1080p 这条事实：${entries.film.text} / ${entries.film.title}`);
    }
    if (!/M4A|WAV/.test(entries.audio.text) && !/M4A|WAV/.test(entries.audio.title)) {
      note(`成片音频的说明里没有格式：${entries.audio.text} / ${entries.audio.title}`);
    }
  }

  await shot(out);

  // --- 2. the old wording is gone ------------------------------------------
  const leftover = await evaluate(`(() => {
    const html = document.documentElement.outerHTML;
    return ["先删除要剪掉的内容", "先导出成片音频"].filter((phrase) => html.includes(phrase));
  })()`);
  if (leftover.length > 0) note(`页面上还残留旧说明：${JSON.stringify(leftover)}`);

  const sourceHits = await evaluate(`(async () => {
    // The strings also lived in source comments; only the ones that reach the screen matter,
    // but a stale hint string is cheap to look for in the bundle the page is running.
    const text = await (await fetch(location.href)).text();
    return ["先删除要剪掉的内容"].filter((phrase) => text.includes(phrase));
  })()`).catch(() => []);
  if (sourceHits.length > 0) note(`页面 HTML 里还有残留：${JSON.stringify(sourceHits)}`);

  if (failures.length > 0) {
    throw new Error(`${failures.length} 项不符合预期：\n- ${failures.join("\n- ")}`);
  }
  console.log("OK: 零删除状态下两个导出项都能点，旧说明已消失");
}
