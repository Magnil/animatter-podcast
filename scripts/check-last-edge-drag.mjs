// 真机验证：把**最后一句的句尾**手柄往右拖，看后端收不收。
//
// 之前的失败全在“抓住哪个手柄”上：从左往右扫会先撞到别的句子。这次从右往左扫，
// 并核对请求里的 token_index——最后一句是 8234，抓到别的不算数。

const patchFetch = `(() => {
  window.__reqs = [];
  if (window.__patched) return true;
  window.__patched = true;
  const orig = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    const body = init && init.body ? String(init.body) : null;
    const res = await orig.call(this, input, init);
    if (url.includes("/edge")) {
      let text = "";
      try { text = await res.clone().text(); } catch { text = "<unreadable>"; }
      window.__reqs.push({ body, status: res.status, text });
    }
    return res;
  };
  return true;
})()`;

const scrollToBottom = `(async () => {
  const list = document.querySelector(".sentence-list");
  list.scrollTop = list.scrollHeight;
  await new Promise((r) => setTimeout(r, 800));
  return true;
})()`;

const clickLastSentence = `(() => {
  const rows = [...document.querySelectorAll(".sentence")];
  const last = rows[rows.length - 1];
  if (!last) return null;
  const t = last.querySelector(".sentence-play") || last.querySelector(".sentence-time");
  t.scrollIntoView({ block: "center" });
  const r = t.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
})()`;

const label = `(() => {
  const n = document.querySelector(".waveform-zoom .muted");
  return n ? n.textContent.trim() : null;
})()`;
const cursor = `(() => document.querySelector("canvas").style.cursor || "")()`;
const reqs = `(() => window.__reqs || [])()`;
const canvasBox = `(() => {
  const c = document.querySelector("canvas");
  const r = c.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
})()`;
const boxOfAria = (aria) => `(() => {
  const b = document.querySelector('button[aria-label=' + JSON.stringify(${JSON.stringify(aria)}) + ']');
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
})()`;

const LAST_TOKEN = 8234;

export default async ({ evaluate, mouse, wait, shot, out }) => {
  const result = { tried: [] };
  await wait(2600);
  await evaluate(patchFetch);

  await evaluate(scrollToBottom);
  const row = await evaluate(clickLastSentence);
  if (row) {
    await mouse("mousePressed", row.x, row.y);
    await mouse("mouseReleased", row.x, row.y);
  }
  await wait(2500);

  // 「+」锚在窗口中心，点击顺序确定，跨度可算；缩放后末尾手柄在可拖区
  const plus = await evaluate(boxOfAria("放大波形"));
  for (let i = 0; i < 20; i += 1) {
    await mouse("mousePressed", plus.x, plus.y);
    await mouse("mouseReleased", plus.x, plus.y);
    await wait(110);
  }
  await wait(700);
  result.label = await evaluate(label);
  await shot(`${out}-1-zoomed`);

  const canvas = await evaluate(canvasBox);
  const midY = canvas.top + canvas.height / 2;

  // 从右往左扫：第一个 ew-resize 就是窗口内最靠右的手柄，也就是最后一句的句尾
  const candidates = [];
  for (let x = Math.round(canvas.left + canvas.width - 2); x > canvas.left + 2; x -= 3) {
    await mouse("mouseMoved", x, midY);
    await wait(18);
    if ((await evaluate(cursor)) === "ew-resize") {
      candidates.push(x);
      // 跳过这个手柄的命中带（约 12px）
      x -= 12;
    }
    if (candidates.length >= 3) break;
  }
  result.candidates = candidates;

  for (const x of candidates) {
    await evaluate("window.__reqs = []");
    const targetX = Math.min(canvas.left + canvas.width - 2, x + 40);
    await mouse("mousePressed", x, midY);
    await wait(140);
    await mouse("mouseMoved", (x + targetX) / 2, midY);
    await wait(140);
    await mouse("mouseMoved", targetX, midY);
    await wait(140);
    await mouse("mouseReleased", targetX, midY);
    await wait(1800);

    const raw = await evaluate(reqs);
    const parsed = raw.map((r) => {
      let body = null;
      let resp = null;
      try { body = JSON.parse(r.body); } catch { body = r.body; }
      try { resp = JSON.parse(r.text); } catch { resp = r.text; }
      return { body, status: r.status, resp };
    });
    const entry = { x, reqs: parsed };
    result.tried.push(entry);
    if (parsed.some((p) => p.body && p.body.token_index === LAST_TOKEN && p.body.edge === "end")) {
      result.hit = entry;
      break;
    }
  }

  await shot(`${out}-2-after`);
  const { writeFileSync } = await import("node:fs");
  writeFileSync(`${out}.json`, JSON.stringify(result, null, 2), "utf8");
  return result;
};
