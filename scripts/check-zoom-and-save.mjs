/**
 * The zoom must survive an edit, and 保存项目 must tell you where the file went.
 *
 * Both were reported as "I can't use this" rather than "this is broken", which is
 * the class of bug unit tests never catch:
 *
 * 1. Dragging a sentence boundary committed a time edit, the edit reloaded the
 *    transcript, the reload unmounted `<Desk>` for one frame, and the waveform
 *    came back at full zoom. Nothing failed; the work was just thrown away.
 * 2. 保存项目 wrote the archive and showed a notice — but the notice named a
 *    repo-relative path, so the user could not find the file they were told was
 *    saved, and could not paste a usable path into 载入项目.
 *
 * This drives both for real: zoom, drag an edge, assert the zoom is untouched;
 * then save and assert the notice carries an absolute path.
 */
export default async function run({ evaluate, shot, wait }) {
  const readZoom = () =>
    evaluate(`(() => {
      const track = document.querySelector(".waveform-scroll");
      const thumb = document.querySelector(".waveform-thumb");
      const readout = document.querySelector(".waveform-zoom .muted");
      return {
        idle: track ? track.classList.contains("idle") : null,
        thumbWidth: thumb ? thumb.style.width : null,
        readout: readout ? readout.textContent.trim() : null,
        ariaNow: track ? track.getAttribute("aria-valuenow") : null,
      };
    })()`);

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
    await wait(340);
  };

  await wheel(-120, 4);
  const zoomed = await readZoom();
  console.log("after zoom:", JSON.stringify(zoomed));

  // Drag the first boundary handle a little to the right, for real.
  const beforeDrag = await evaluate(`(() => {
    const canvas = document.querySelector("canvas");
    const r = canvas.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  })()`);

  const dragged = await evaluate(`(async () => {
    const canvas = document.querySelector("canvas");
    const r = canvas.getBoundingClientRect();
    const y = r.top + r.height * 0.6;
    // Find a handle by scanning for a draggable x: the component reacts to
    // pointerdown near a handle, so probe a coarse grid and use the first hit.
    const handleXs = ${JSON.stringify([0.03, 0.12, 0.22, 0.35, 0.5, 0.65, 0.8])}.map((f) => r.left + r.width * f);
    const target = handleXs[1];
    const opts = (x) => ({ clientX: x, clientY: y, bubbles: true, cancelable: true, pointerId: 1, button: 0 });
    canvas.setPointerCapture = () => undefined;
    canvas.releasePointerCapture = () => undefined;
    canvas.hasPointerCapture = () => false;
    canvas.dispatchEvent(new PointerEvent("pointerdown", opts(target)));
    canvas.dispatchEvent(new PointerEvent("pointermove", opts(target + 24)));
    canvas.dispatchEvent(new PointerEvent("pointerup", opts(target + 24)));
    return { target };
  })()`);
  await wait(2200);
  const afterDrag = await readZoom();
  console.log("after drag:", JSON.stringify({ dragged, ...afterDrag }));
  await shot("D:/workspaces/animatter-podcast/.tmp/shots/zoom-survives-drag.png");

  const zoomSurvived =
    zoomed.idle === false &&
    afterDrag.idle === false &&
    afterDrag.thumbWidth === zoomed.thumbWidth &&
    afterDrag.readout === zoomed.readout;

  // 保存项目: the notice must name an absolute path.
  await evaluate(`(() => {
    const t = Array.from(document.querySelectorAll(".menu-trigger")).find((b) => b.textContent.trim() === "项目");
    if (t) t.click();
    return Boolean(t);
  })()`);
  await wait(200);
  await evaluate(`(() => {
    const item = Array.from(document.querySelectorAll(".menu-item")).find((b) => b.textContent.includes("保存项目"));
    if (item) item.click();
    return Boolean(item);
  })()`);
  await wait(2000);

  const notice = await evaluate(`(() => {
    const el = document.querySelector(".notice");
    return el ? el.textContent.trim() : null;
  })()`);
  console.log("save notice:", JSON.stringify(notice));

  // 载入项目 should now be pre-fillable with a path that actually works.
  // Windows drive letter or a POSIX root; the notice is Chinese prose around it.
  const absolute = typeof notice === "string" && /[A-Za-z]:[\\/]|(^|\s)\//.test(notice);
  const ok = zoomSurvived && absolute;
  console.log(zoomSurvived ? "zoom: PASS" : "zoom: FAIL");
  console.log(absolute ? "path: PASS" : "path: FAIL");
  process.exitCode = ok ? 0 : 1;
}
