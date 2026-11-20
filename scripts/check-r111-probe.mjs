/**
 * Same probe, but reading the **buttons** after the click instead of the truncated HTML.
 *
 * The PATCH lands (`{"solo":true}`) and the server agrees, so the question is now purely "does
 * the bubble re-render". This prints each button's `aria-pressed` before and after, plus the
 * `solo` field of the item the panel is holding.
 */

export default async function ({ evaluate, shot, wait, out, send }) {
  const text = (expression) => evaluate(expression);

  await text(`(() => {
    const panel = document.querySelector(".video-panel");
    if (panel && panel.classList.contains("is-collapsed")) {
      [...panel.querySelectorAll("button")].find((b) => b.textContent.trim() === "展开")?.click();
    }
    return true;
  })()`);
  await wait(900);

  const items = await text(`(async () => {
    const body = await (await fetch("/api/versions/ver_1m6v2q5javqm1j82xsg/video/timeline")).json();
    return body.items.map((item) => ({ id: item.id, has_audio: item.has_audio, muted: item.muted,
      solo: item.solo, placement: item.placement }));
  })()`);
  const audible = items.find((item) => item.has_audio);
  const index = items.indexOf(audible);

  const readButtons = () => text(`(() => {
    const bubble = document.querySelector(".video-bubble");
    if (!bubble) return { bubble: false };
    return {
      bubble: true,
      name: bubble.querySelector(".video-bubble-name")?.textContent.trim() ?? "",
      buttons: [...bubble.querySelectorAll("button")].map((b) => ({ t: b.textContent.trim(), p: b.getAttribute("aria-pressed") })),
      selects: [...bubble.querySelectorAll("select")].map((s) => s.value),
    };
  })()`);

  const where = await text(`(() => {
    const node = document.querySelectorAll(".video-block")[${index}];
    const track = document.querySelector(".video-track");
    const r = node.getBoundingClientRect();
    const t = track.getBoundingClientRect();
    const left = Math.max(r.left, t.left + 2);
    const right = Math.min(r.right, t.right - 2);
    return { x: left + Math.min((right - left) / 2, 60), y: r.top + r.height / 2 };
  })()`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type, x: where.x, y: where.y, button: "left", clickCount: 1 });
  }
  await wait(600);
  console.log("before:", JSON.stringify(await readButtons()));

  await text(`(() => {
    [...document.querySelector(".video-bubble").querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "S")?.click();
    return true;
  })()`);
  for (const delay of [200, 600, 1500, 3000]) {
    await wait(delay === 200 ? 200 : delay - 200);
    console.log(`after ${delay}ms:`, JSON.stringify(await readButtons()));
  }

  // Restore.
  await text(`(async () => {
    await fetch("/api/video/items/${audible.id}", {
      method: "PATCH", headers: { "content-type": "application/json" },
      body: JSON.stringify({ solo: false, muted: ${JSON.stringify(audible.muted)} }),
    });
    return true;
  })()`);
  await shot(out);
}
