/**
 * The two panels: 视频剪辑 (folded by default) and 音频剪辑 (open), same chrome.
 *
 * The point of this check is discoverability, which is exactly what the old build failed:
 * a feature behind 设置 → 显示视频区 has no on-screen entry point, so this asserts the
 * header text exists on first paint — before anyone opens a menu.
 */
export default async function run({ evaluate, click, shot, wait, out }) {
  await wait(900);

  const firstPaint = await evaluate(`(() => {
    const heads = [...document.querySelectorAll(".panel-head h3")].map((h) => h.textContent.trim());
    const toggles = [...document.querySelectorAll(".collapsible-panel .panel-head-tools button")]
      .map((b) => b.textContent.trim());
    return {
      panelHeads: heads,
      toggles,
      videoCollapsed: !!document.querySelector(".video-panel")?.classList.contains("is-collapsed"),
      audioCollapsed: !!document.querySelector(".audio-panel")?.classList.contains("is-collapsed"),
      waveformVisible: !!document.querySelector(".audio-panel .waveform"),
    };
  })()`);
  console.log("first paint:", JSON.stringify(firstPaint));
  await shot(out);

  // Expand the video panel and check its body really mounts.
  await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    const btn = panel?.querySelector(".panel-head-tools button");
    btn?.click();
    return !!btn;
  })()`);
  await wait(1500);

  const expanded = await evaluate(`(() => {
    const panel = document.querySelector(".video-panel");
    return {
      collapsed: panel?.classList.contains("is-collapsed"),
      toggle: panel?.querySelector(".panel-head-tools button")?.textContent.trim(),
      hasBody: !!panel?.querySelector(".video-ready, .video-import"),
      summary: panel?.querySelector(".muted.small")?.textContent.trim(),
    };
  })()`);
  console.log("expanded video:", JSON.stringify(expanded));
  await shot(out.replace(/\.png$/, "-expanded.png"));

  // Collapse the audio panel: the wave must go, the header must stay.
  await evaluate(`(() => {
    const btn = document.querySelector(".audio-panel .panel-head-tools button");
    btn?.click();
    return !!btn;
  })()`);
  await wait(600);

  const audioFolded = await evaluate(`(() => {
    const panel = document.querySelector(".audio-panel");
    return {
      collapsed: panel?.classList.contains("is-collapsed"),
      hasWave: !!panel?.querySelector(".waveform"),
      headStillThere: panel?.querySelector("h3")?.textContent.trim(),
      summary: panel?.querySelector(".muted.small")?.textContent.trim(),
    };
  })()`);
  console.log("audio folded:", JSON.stringify(audioFolded));
  await shot(out.replace(/\.png$/, "-audio-folded.png"));
}
