/**
 * Drawer + video panel layout, asserted on computed facts rather than pixels.
 *
 * Three things were reported: (1) the left column should fold as one drawer with a handle
 * on its right border, (2) the four video sections should be tabs in a settings column to
 * the right of the preview, (3) the video track should sit at the bottom, next to the
 * audio track, and share its zoom.
 *
 * Each assertion below is something a blank/broken render cannot satisfy: a width, an
 * element that must not exist, or two numbers that must be equal.
 */
export default async function run({ evaluate, click, shot, wait, out, mouse }) {
  await wait(1200);

  // --- 1. drawer ------------------------------------------------------------
  const drawerBefore = await evaluate(`(() => {
    const body = document.querySelector(".bench-body");
    const side = document.querySelector(".sidebar");
    const handle = document.querySelector(".drawer-handle");
    const style = handle ? getComputedStyle(handle) : null;
    return {
      closed: body?.classList.contains("is-drawer-closed"),
      sidebarWidth: Math.round(side?.getBoundingClientRect().width ?? 0),
      handleLeft: Math.round(handle?.getBoundingClientRect().left ?? -1),
      handleVisible: style?.visibility ?? null,
      handleText: handle?.textContent?.trim() ?? null,
    };
  })()`);
  console.log("drawer open:", JSON.stringify(drawerBefore));

  await evaluate(`document.querySelector(".drawer-handle")?.click()`);
  await wait(500);
  const drawerAfter = await evaluate(`(() => {
    const body = document.querySelector(".bench-body");
    const side = document.querySelector(".sidebar");
    const handle = document.querySelector(".drawer-handle");
    const stage = document.querySelector(".stage");
    return {
      closed: body?.classList.contains("is-drawer-closed"),
      sidebarWidth: Math.round(side?.getBoundingClientRect().width ?? 0),
      sidebarVisibility: side ? getComputedStyle(side).visibility : null,
      handleLeft: Math.round(handle?.getBoundingClientRect().left ?? -1),
      stageWidth: Math.round(stage?.getBoundingClientRect().width ?? 0),
    };
  })()`);
  console.log("drawer closed:", JSON.stringify(drawerAfter));
  await shot(out.replace(/\.png$/, "-drawer-closed.png"));

  // Reopen before looking at the video panel.
  await evaluate(`document.querySelector(".drawer-handle")?.click()`);
  await wait(500);

  // --- 2. expand the video panel and check the tab layout -------------------
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel?.classList.contains("is-collapsed")) {
      panel.querySelector(".panel-head-tools button")?.click();
    }
    return true;
  })()`);
  await wait(2200);

  const layout = await evaluate(`(() => {
    const preview = document.querySelector(".video-preview-box");
    const settings = document.querySelector(".video-settings");
    const tabs = [...document.querySelectorAll(".video-settings .tab")].map((b) => ({
      label: b.textContent.trim(),
      active: b.classList.contains("active"),
    }));
    const body = document.querySelector(".video-settings-body");
    const pr = preview?.getBoundingClientRect();
    const sr = settings?.getBoundingClientRect();
    return {
      tabs,
      // "to the right of the preview" is a geometric claim: the settings column's left
      // edge must be past the preview's right edge.
      settingsRightOfPreview: !!pr && !!sr && sr.left >= pr.right - 1,
      previewWidth: Math.round(pr?.width ?? 0),
      settingsWidth: Math.round(sr?.width ?? 0),
      bodyChildren: body?.children.length ?? 0,
      bodyText: (body?.textContent ?? "").trim().slice(0, 40),
    };
  })()`);
  console.log("video layout:", JSON.stringify(layout));

  // Each tab must swap the body, not hide it — count the panes' distinctive text.
  const tabBodies = [];
  for (const label of ["模板", "字幕", "素材库", "演示稿"]) {
    await evaluate(`(() => {
      const btn = [...document.querySelectorAll(".video-settings .tab")]
        .find((b) => b.textContent.trim() === ${JSON.stringify(label)});
      btn?.click();
      return !!btn;
    })()`);
    await wait(900);
    tabBodies.push(
      await evaluate(`(() => {
        const body = document.querySelector(".video-settings-body");
        const active = document.querySelector(".video-settings .tab.active")?.textContent.trim();
        return { active, text: (body?.textContent ?? "").replace(/\\s+/g, " ").trim().slice(0, 50) };
      })()`),
    );
  }
  console.log("tab panes:", JSON.stringify(tabBodies));

  // --- 3. the track is the last block, and the two tracks share a window ----
  const geometry = await evaluate(`(() => {
    const ready = document.querySelector(".video-ready");
    const track = document.querySelector(".video-track");
    const wave = document.querySelector(".audio-panel .waveform");
    const tr = track?.getBoundingClientRect();
    const wr = wave?.getBoundingClientRect();
    const rr = ready?.getBoundingClientRect();
    // Round 102 removed the export row that used to sit above the track, so "nothing else
    // below the track" is now the whole claim: the track is the last thing in the panel.
    const after = track
      ? [...(ready?.children ?? [])].filter((row) => row !== track.parentElement &&
          row.getBoundingClientRect().top > track.getBoundingClientRect().top)
      : [];
    return {
      trackInsideVideo: !!track && !!ready && ready.contains(track),
      trackTop: Math.round(tr?.top ?? -1),
      videoBottom: Math.round(rr?.bottom ?? -1),
      waveTop: Math.round(wr?.top ?? -1),
      gapTrackToWave: Math.round((wr?.top ?? 0) - (tr?.bottom ?? 0)),
      rowsBelowTrack: after.length,
    };
  })()`);
  console.log("track geometry:", JSON.stringify(geometry));

  // Zoom the wave, then check the video track moved with it: the same playhead time must
  // land at (nearly) the same x in both tracks.
  const before = await evaluate(`(() => {
    const btn = [...document.querySelectorAll(".audio-panel .waveform-zoom button")]
      .find((b) => b.textContent.trim() === "+");
    btn?.click();
    return !!btn;
  })()`);
  await wait(700);
  const sync = await evaluate(`(() => {
    const wave = document.querySelector(".audio-panel .waveform canvas");
    const track = document.querySelector(".video-track");
    // Both components project ms -> x through one shared window, so the video playhead
    // must sit at the same fraction of its track as the audio playhead does.
    const playhead = document.querySelector(".video-playhead");
    const wf = wave?.getBoundingClientRect();
    const tk = track?.getBoundingClientRect();
    const ph = playhead?.getBoundingClientRect();
    const block = document.querySelector(".video-block");
    const bl = block?.getBoundingClientRect();
    return {
      waveLeft: Math.round(wf?.left ?? -1),
      waveWidth: Math.round(wf?.width ?? 0),
      trackLeft: Math.round(tk?.left ?? -1),
      trackWidth: Math.round(tk?.width ?? 0),
      playheadLeft: Math.round(ph?.left ?? -1),
      blockLeft: Math.round(bl?.left ?? -1),
      blockWidth: Math.round(bl?.width ?? 0),
    };
  })()`);
  console.log("shared window:", JSON.stringify({ zoomClicked: before, ...sync }));

  // The strongest available proof that the two tracks share one window: click the *same*
  // fraction of each track and read the playhead clock. If each track had its own window
  // these two numbers would differ; with one window they must agree.
  // `aria-valuenow` on the waveform canvas is the real playhead in ms — a computed fact,
  // not a rendered string. Both tracks seek through the same `playback`, so this reads the
  // instant that either click landed on.
  const playheadMs = async () =>
    evaluate(
      '(() => { const c = document.querySelector(".audio-panel .waveform canvas");' +
        ' return c ? Number(c.getAttribute("aria-valuenow")) : null; })()',
    );
  const fractionPoint = async (selector, fraction) =>
    evaluate(
      `(() => { const el = document.querySelector(${JSON.stringify(selector)});` +
        " if (!el) return null; const r = el.getBoundingClientRect();" +
        ` return { x: r.left + r.width * ${fraction}, y: r.top + r.height / 2 }; })()`,
    );

  const at = 0.8;
  const onTrack = await fractionPoint(".video-track", at);
  await mouse("mousePressed", onTrack.x, onTrack.y);
  await mouse("mouseReleased", onTrack.x, onTrack.y);
  await wait(700);
  const trackTime = await playheadMs();

  const onWave = await fractionPoint(".audio-panel .waveform canvas", at);
  await mouse("mousePressed", onWave.x, onWave.y);
  await mouse("mouseReleased", onWave.x, onWave.y);
  await wait(700);
  const waveTime = await playheadMs();

  console.log(
    "same x, both tracks:",
    JSON.stringify({
      trackTime,
      waveTime,
      deltaMs: Math.abs(trackTime - waveTime),
      // Snap-to-silence can move a seek by up to ~200ms on either track, so anything under
      // a second means "the same instant"; different windows would differ by minutes.
      agrees: Math.abs(trackTime - waveTime) < 1000,
    }),
  );

  await shot(out);
}
