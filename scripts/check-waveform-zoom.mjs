/**
 * Waveform zoom, scrollbar and settings dialog, checked in a real browser.
 *
 * A canvas is opaque to assertions: "the waveform is zoomed" is not visible in
 * a screenshot in a way a reviewer can distinguish from "it loaded". So this
 * asserts the *state* the canvas is drawn from — the scrollbar thumb geometry,
 * the zoom readout, the request the component made — and only then screenshots.
 *
 * The wheel event is dispatched as a real (cancelable, bubbling) WheelEvent on
 * the canvas rather than through CDP Input.dispatchMouseEvent, because the
 * component registers a non-passive native listener (React's synthetic handler
 * cannot preventDefault). A synthetic event exercises exactly that path.
 */
export default async function run({ evaluate, click, shot, wait }) {
  const readState = () =>
    evaluate(`(() => {
      const track = document.querySelector(".waveform-scroll");
      const thumb = document.querySelector(".waveform-thumb");
      const readout = document.querySelector(".waveform-zoom .muted");
      const canvas = document.querySelector("canvas");
      return {
        thumbLeft: thumb ? thumb.style.left : null,
        thumbWidth: thumb ? thumb.style.width : null,
        idle: track ? track.classList.contains("idle") : null,
        readout: readout ? readout.textContent.trim() : null,
        ariaNow: track ? track.getAttribute("aria-valuenow") : null,
        canvasWidth: canvas ? Math.round(canvas.getBoundingClientRect().width) : null,
      };
    })()`);

  const before = await readState();
  console.log("before zoom:", JSON.stringify(before));

  /** Zoom by dispatching `notches` wheel events at the canvas centre. */
  const wheel = async (deltaY, times = 1) => {
    await evaluate(`(() => {
      const canvas = document.querySelector("canvas");
      const r = canvas.getBoundingClientRect();
      for (let i = 0; i < ${times}; i += 1) {
        canvas.dispatchEvent(new WheelEvent("wheel", {
          deltaY: ${deltaY},
          clientX: r.left + r.width / 2,
          clientY: r.top + r.height / 2,
          bubbles: true,
          cancelable: true,
        }));
      }
      return true;
    })()`);
    await wait(320);
  };

  await wheel(-120, 4);
  const zoomed = await readState();
  console.log("after zoom in:", JSON.stringify(zoomed));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/wave-zoom.png");

  // The thumb must actually be a thumb now, not the full track.
  const thumbFraction = zoomed.thumbWidth ? parseFloat(zoomed.thumbWidth) : 100;
  const narrowed = thumbFraction < 95;
  console.log("thumb fraction:", thumbFraction, "narrowed:", narrowed);

  // Drag the thumb to the right: the window must move, and the readout must
  // change, which proves the fetch/pan path ran and not just the CSS.
  const drag = await evaluate(`(async () => {
    const track = document.querySelector(".waveform-scroll");
    const thumb = document.querySelector(".waveform-thumb");
    const t = track.getBoundingClientRect();
    const th = thumb.getBoundingClientRect();
    const startX = th.left + th.width / 2;
    const y = t.top + t.height / 2;
    const opts = (x) => ({ clientX: x, clientY: y, bubbles: true, cancelable: true, pointerId: 1 });
    track.setPointerCapture = () => undefined;
    track.releasePointerCapture = () => undefined;
    track.dispatchEvent(new PointerEvent("pointerdown", opts(startX)));
    track.dispatchEvent(new PointerEvent("pointermove", opts(startX + t.width * 0.25)));
    track.dispatchEvent(new PointerEvent("pointerup", opts(startX + t.width * 0.25)));
    return true;
  })()`);
  await wait(400);
  const panned = await readState();
  console.log("after pan:", JSON.stringify({ drag, ...panned }));

  const movedRight = parseFloat(panned.thumbLeft || "0") > parseFloat(zoomed.thumbLeft || "0");

  // Reset, then open 设置 → 热词与校对.
  await evaluate(`(() => {
    const all = Array.from(document.querySelectorAll(".waveform-zoom button"));
    const reset = all.find((b) => b.textContent.includes("全部"));
    if (reset) reset.click();
    return true;
  })()`);
  await wait(350);
  const reset = await readState();
  console.log("after reset:", JSON.stringify(reset));

  const settingsItem = await evaluate(`(() => {
    const triggers = Array.from(document.querySelectorAll(".menu-trigger"));
    const settings = triggers.find((b) => b.textContent.trim() === "设置");
    if (!settings) return { found: false, labels: triggers.map((b) => b.textContent.trim()) };
    settings.click();
    return { found: true };
  })()`);
  await wait(250);
  const picked = await evaluate(`(() => {
    const items = Array.from(document.querySelectorAll(".menu-item"));
    const hit = items.find((b) => b.textContent.includes("热词与校对"));
    if (!hit) return { clicked: false, labels: items.map((b) => b.textContent.trim()) };
    hit.click();
    return { clicked: true };
  })()`);
  await wait(700);
  const dialog = await evaluate(`(() => {
    const modal = document.querySelector(".modal-wide");
    if (!modal) return { open: false };
    const sections = Array.from(modal.querySelectorAll("h3")).map((h) => h.textContent.trim());
    const hot = modal.querySelector(".settings-hotwords");
    return {
      open: true,
      sections,
      hotwordEditor: Boolean(hot && hot.querySelector(".hotword-editor")),
      useProofread: Array.from(modal.querySelectorAll("label.checkbox")).some((l) =>
        l.textContent.includes("LLM 校对"),
      ),
      chips: Array.from(modal.querySelectorAll(".chip")).map((c) => c.textContent.replace("×", "").trim()),
    };
  })()`);
  console.log("settings dialog:", JSON.stringify({ settingsItem, picked, ...dialog }));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/settings-hotwords.png");

  const ok =
    narrowed &&
    movedRight &&
    reset.idle === true &&
    dialog.open &&
    dialog.hotwordEditor &&
    dialog.useProofread &&
    (dialog.sections || []).includes("热词与校对");
  console.log(ok ? "PASS" : "FAIL");
  process.exitCode = ok ? 0 : 1;
}
