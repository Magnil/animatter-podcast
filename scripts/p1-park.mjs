// Park the playhead at a chosen **output** time, precisely, through the UI the user uses.
//
// Clicking a sentence sets the playhead to that sentence's original time, which is then mapped to
// output time — so a sentence click is precise to the *sentence*, not to the millisecond (measured:
// asked 68 970, landed 70 970, exactly 2 000 ms out). Three iterations of "scroll, click the row
// nearest, correct by the residual" only ever converge to the next sentence boundary, which is not
// good enough for parking 5 s before a block boundary on a film whose blocks are 20–90 s long.
//
// So this drives the control that is exact: **the waveform canvas**, whose pointer-down handler
// seeks to `msFromClientX(clientX)`. A real `Input.dispatchMouseEvent` is the same path a user's
// click takes.
//
// Two things can steal a waveform click, and both are handled by retrying at a different x:
//
//  * **an edge handle** — a click within `HANDLE_HIT_PX` (6 px) of a sentence boundary starts a
//    drag instead of a seek, and the playhead does not move at all;
//  * **the caption band** — the top 22 px of the canvas opens a caption instead of seeking, so the
//    click has to land below it.
//
// Exported so the playback walkthrough can reuse it.

const JITTER = [0, 7, -9, 14, -18, 25, -30];

/**
 * Seek the playhead to `wantMs` of output time by clicking the waveform.
 *
 * Returns `{ landed, error }` where `error = wantMs - landed`. `total` is the film's output
 * duration; the default waveform view is the whole file, so x maps linearly onto time.
 */
export async function parkAtOutputMs({ evaluate, send }, wantMs, { total = 3_695_928 } = {}) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const box = await evaluate(`
    (() => {
      const canvas = document.querySelector(".waveform canvas");
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      return { x: rect.left, y: rect.top, w: rect.width, h: rect.height };
    })()
  `);
  if (!box) return { landed: null, error: "no waveform canvas" };

  let landed = null;
  for (const offset of JITTER) {
    const target = Math.min(total - 200, Math.max(200, wantMs + offset));
    // Half the canvas height: below the 22 px caption band, and on no handle row in particular.
    const x = box.x + Math.min(box.w - 2, Math.max(2, (target / total) * box.w));
    const y = box.y + box.h * 0.6;
    await send("Input.dispatchMouseEvent", {
      type: "mousePressed",
      x,
      y,
      button: "left",
      buttons: 1,
      clickCount: 1,
      pointerType: "mouse",
    });
    await send("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x,
      y,
      button: "left",
      buttons: 0,
      clickCount: 1,
      pointerType: "mouse",
    });
    await sleep(700);
    landed = await evaluate(`
      (() => {
        const stage = document.querySelector(".dom-stage");
        return stage ? Math.round(Number(stage.dataset.atMs)) : null;
      })()
    `);
    if (landed !== null && Math.abs(landed - wantMs) < 600) break;
  }
  return { landed, error: landed === null ? null : wantMs - landed };
}
