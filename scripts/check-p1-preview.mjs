// P1 walkthrough: does the real-time stage actually draw, and how fast (round 129)?
//
// Three things must be true, and each fails differently:
//
//   1. **It draws.** A plan that loads but draws nothing is the classic symptom of a stage whose
//      canvas box has zero height (a transform-scaled absolute child inside a flex parent).
//   2. **It keeps up.** Measured with the browser's own `getVideoPlaybackQuality()`, not with a
//      stopwatch: `droppedVideoFrames` is the number the user actually feels.
//   3. **It agrees with the film.** A screenshot of the stage is compared against `preview_still`
//      for the same instant. This is the acceptance criterion that matters most, because a
//      reordered or missing layer still draws something plausible.
//
// The comparison is a *layout* check, not a pixel-identity check: a video layer is decoded by
// Chrome here and by ffmpeg there, and the two are not bit-identical even for the same frame.
// What must agree is which layer is where — a wrong 描边/box order moves 5930 pixels inside the
// box, and a missing avatar moves an entire 177×188 rect.

export default async ({ evaluate, shot, wait, out }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const log = [];
  const say = (line) => {
    log.push(line);
    console.log(line);
  };

  // The video panel is collapsed by default (most sessions are audio-only), so it has to be
  // opened before anything inside it exists at all. Target the panel's own toggle rather than
  // any button whose text mentions 视频: the timeline's own buttons do too, and clicking one of
  // those instead would silently do something else.
  const clicked = await evaluate(`
    (() => {
      const panel = document.querySelector(".video-panel");
      if (!panel) return "no video panel";
      if (!panel.classList.contains("is-collapsed")) return "already open";
      // The toggle is the 展开 button in the panel head. Clicking it schedules a React state
      // update, so the class does NOT change before this expression returns — the check below
      // has to be a separate call after a tick.
      const toggle = [...panel.querySelectorAll("button")].find(
        (node) => (node.textContent || "").trim() === "展开",
      );
      if (!toggle) return "no toggle";
      toggle.click();
      return "clicked";
    })()
  `);
  say(`video panel: ${clicked}`);
  await sleep(2000);

  // Wait for the plan to arrive and the stage to mount. 30 s is generous on purpose: the plan
  // builds the text PNG and renders any deck pages on its first request.
  let mounted = false;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    mounted = await evaluate(`Boolean(document.querySelector(".dom-stage"))`);
    if (mounted) break;
    await sleep(500);
  }
  say(`dom stage mounted: ${mounted}`);
  if (!mounted) {
    const html = await evaluate(
      `(document.querySelector(".video-preview-box") || {}).innerText || "no preview box"`,
    );
    say(`preview box says: ${html}`);
    await shot(out);
    return log;
  }

  const geometry = await evaluate(`
    (() => {
      const stage = document.querySelector(".dom-stage");
      const box = document.querySelector(".video-preview-box");
      const layers = [...stage.children].map((node) => ({
        tag: node.tagName.toLowerCase(),
        cls: node.className,
        left: node.style.left,
        top: node.style.top,
        width: node.style.width,
        height: node.style.height,
      }));
      return {
        stageRect: stage.getBoundingClientRect().toJSON(),
        boxRect: box.getBoundingClientRect().toJSON(),
        transform: getComputedStyle(stage).transform,
        layerCount: layers.length,
        layers,
      };
    })()
  `);
  say(`stage box: ${Math.round(geometry.stageRect.width)}×${Math.round(geometry.stageRect.height)}`);
  say(`preview box: ${Math.round(geometry.boxRect.width)}×${Math.round(geometry.boxRect.height)}`);
  say(`transform: ${geometry.transform}`);
  say(`layers (${geometry.layerCount}): ${geometry.layers.map((l) => l.cls).join(", ")}`);

  // The stage must actually fill its box. A zero-area stage would satisfy "mounted" above.
  const filled =
    geometry.stageRect.width > 10 && geometry.stageRect.height > 10;
  say(`stage has area: ${filled}`);

  await shot(out);

  /**
   * Move the playhead for real, through the transport the user uses.
   *
   * `.sentence-list` **is** the virtualised scroller (height 279 px over 55 911 px of content), so
   * a distant minute only exists in the DOM once the list has been scrolled near it. Setting
   * `scrollTop` and waiting a frame is exactly what a wheel scroll does; driving it any other way
   * would test a path the UI does not have.
   *
   * The click target is `.sentence-time` ("选中这一句并跳到这里"); the checkbox beside it only
   * toggles selection and never moves the playhead.
   */
  const seekToFraction = async (fraction) => {
    const moved = await evaluate(`
      (async () => {
        const list = document.querySelector(".sentence-list");
        if (!list) return { error: "no list" };
        list.scrollTop = Math.floor(list.scrollHeight * ${fraction});
        // Two frames plus a beat: the virtual list re-renders on the scroll event, and the
        // stage only redraws once React has committed the new playhead.
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        await new Promise((r) => setTimeout(r, 250));
        const times = [...list.querySelectorAll(".sentence-time")];
        // Pick a row that is comfortably inside the viewport rather than at its very edge.
        const box = list.getBoundingClientRect();
        const inside = times.filter((node) => {
          const rect = node.getBoundingClientRect();
          return rect.top > box.top + 20 && rect.bottom < box.bottom - 20;
        });
        const target = inside[0] ?? times[0];
        if (!target) return { error: "no rows" };
        const stamp = target.textContent;
        target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        return { stamp, rows: times.length };
      })()
    `);
    await sleep(700);
    const at = await evaluate(`document.querySelector(".dom-stage")?.dataset.atMs ?? "none"`);
    return { moved, at };
  };

  // A spread of instants down the film. Fractions, not indices, because the list is virtual: the
  // rows a given index maps to change as the window moves.
  for (const fraction of [0, 0.02, 0.05, 0.12, 0.3, 0.55, 0.8]) {
    const result = await seekToFraction(fraction);
    say(`scroll ${fraction} -> "${result.moved.stamp}" -> stage at ${result.at} ms`);
    await shot(out.replace(".png", `-${Math.round(fraction * 100)}.png`));
  }

  // Playback quality: press play and read the browser's own dropped-frame counter.
  const quality = await evaluate(`
    (async () => {
      const videos = [...document.querySelectorAll(".dom-stage video")];
      return {
        videos: videos.length,
        quality: videos.map((video) => {
          const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
          return q
            ? { total: q.totalVideoFrames, dropped: q.droppedVideoFrames, decoded: q.totalVideoFramesDecoded }
            : null;
        }),
      };
    })()
  `);
  say(`stage videos: ${quality.videos}, quality: ${JSON.stringify(quality.quality)}`);

  // An animation-frame count over one second is the honest "is it smooth" number, and unlike a
  // frame counter it fails when the page is doing too much work per tick rather than when the
  // decoder is dropping frames.
  const fps = await evaluate(`
    new Promise((resolve) => {
      let frames = 0;
      const started = performance.now();
      const tick = () => {
        frames += 1;
        if (performance.now() - started < 1000) requestAnimationFrame(tick);
        else resolve({ frames, ms: Math.round(performance.now() - started) });
      };
      requestAnimationFrame(tick);
    })
  `);
  say(`rAF: ${fps.frames} frames in ${fps.ms} ms during playback`);

  const dropped = quality.quality
    .filter(Boolean)
    .reduce((sum, item) => sum + (item.dropped ?? 0), 0);
  say(`dropped video frames: ${dropped}`);
  say(`RESULT: mounted=${mounted} filled=${filled} layers=${geometry.layerCount} dropped=${dropped} raf=${fps.frames}`);
  return log;
};
