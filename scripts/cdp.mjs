/**
 * Minimal Chrome DevTools Protocol driver.
 *
 * Used to check the workbench for real: computed layout, clicks on words, and
 * screenshots of states that only exist after interaction. `node --test` cannot
 * do this, and a plain `--screenshot` of a URL cannot click anything.
 *
 * Node 22+ ships a global `WebSocket`, so no dependency is needed.
 *
 * Usage:
 *   node scripts/cdp.mjs <url> <out.png> [script.js]
 *
 * The optional third argument is a JS module whose default export receives
 * `{ evaluate, click, shot, wait }` and runs inside the page context.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9333;

const [, , url = "http://localhost:5173/", out = "shot.png", script] = process.argv;

const profile = mkdtempSync(join(tmpdir(), "cdp-"));

const chrome = spawn(
  CHROME,
  [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--window-size=1440,900",
    "about:blank",
  ],
  { stdio: "ignore" },
);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Wait until the debugging endpoint answers. */
async function endpoint() {
  for (let i = 0; i < 100; i += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const targets = await response.json();
      const page = targets.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      // not up yet
    }
    await sleep(100);
  }
  throw new Error("chrome did not expose a page target");
}

const wsUrl = await endpoint();
const ws = new WebSocket(wsUrl);
await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = reject;
});

let nextId = 1;
const pending = new Map();
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  const waiter = pending.get(message.id);
  if (!waiter) return;
  pending.delete(message.id);
  if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
  else waiter.resolve(message.result);
};

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

/** Evaluate an expression in the page and return its value. */
async function evaluate(expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description ?? "evaluation failed");
  }
  return result.result.value;
}

/** Click the first element matching `selector`, at its centre. */
async function click(selector) {
  const box = await evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`);
  if (!box) throw new Error(`no element for ${selector}`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type,
      x: box.x,
      y: box.y,
      button: "left",
      clickCount: 1,
    });
  }
  await sleep(150);
}

/** Screenshot the viewport. */
async function shot(path) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(path, Buffer.from(data, "base64"));
}

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1440,
  height: 900,
  deviceScaleFactor: 1.5,
  mobile: false,
});
await send("Page.navigate", { url });
await sleep(2500);

/**
 * Real mouse input, not a synthetic event.
 *
 * Worth having: React's `setPointerCapture` throws for a pointer id the browser
 * never saw, so a hand-built `PointerEvent` can make a drag look broken when it
 * is only the harness. Anything about drag *timing* must use this.
 */
async function mouse(type, x, y) {
  await send("Input.dispatchMouseEvent", {
    type,
    x,
    y,
    button: "left",
    buttons: type === "mouseReleased" ? 0 : 1,
    clickCount: 1,
    pointerType: "mouse",
  });
}

try {
  if (script) {
    const module = await import(new URL(script, `file://${process.cwd()}/`).href);
    await module.default({ evaluate, click, shot, wait: sleep, mouse, send, out });
  } else {
    await shot(out);
  }
} finally {
  ws.close();
  chrome.kill();
  await sleep(200);
  rmSync(profile, { recursive: true, force: true });
}
