/**
 * Tests for the API client's response handling.
 *
 * The bug: `subtitlePreview` is served as `text/plain` (SRT/VTT/TXT — a subtitle
 * file is text by definition), but the shared `request()` helper always called
 * `response.json()`. That threw, the panel's catch swallowed it, and the UI showed
 * 「（没有可导出的字幕）」 with the export button disabled for a version that had
 * 1074 perfectly good sentences. The backend was answering correctly the whole
 * time.
 *
 * `fetch` is stubbed here on purpose: the point is what the client does with a
 * body, not what the server sends.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { RequestFailed, api } from "./api.ts";

/** Replace global fetch for the duration of one call. */
async function withFetch(response, run) {
  const original = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init });
    return response();
  };
  try {
    return { result: await run(), seen };
  } finally {
    globalThis.fetch = original;
  }
}

function textResponse(body, status = 200, contentType = "text/plain; charset=utf-8") {
  return () =>
    new Response(body, { status, headers: { "Content-Type": contentType } });
}

function jsonResponse(body, status = 200) {
  return () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
}

test("subtitlePreview returns the SRT body verbatim", async () => {
  const srt = "1\n00:00:00,000 --> 00:00:02,000\n今天我们来聊剪辑\n\n";
  const { result, seen } = await withFetch(textResponse(srt), () =>
    api.subtitlePreview("ver_1", { fmt: "srt", limit: 20 }),
  );

  assert.equal(result, srt);
  assert.match(seen[0].url, /\/api\/versions\/ver_1\/subtitles\/preview\?/);
  assert.match(seen[0].url, /fmt=srt/);
  assert.match(seen[0].url, /limit=20/);
});

test("a JSON endpoint still parses as JSON", async () => {
  // The fix must not turn every response into text: `edl` and friends still
  // need objects, and silently returning a string would break every caller.
  const { result } = await withFetch(jsonResponse({ signature: "abc", segment_count: 3 }), () =>
    api.edl("ver_1"),
  );
  assert.equal(result.segment_count, 3);
  assert.equal(result.signature, "abc");
});

test("a 404 on a text endpoint still raises the structured error", async () => {
  // Reading text must not lose the AppError shape: the message is the only thing
  // the user ever sees.
  const body = JSON.stringify({
    error: { code: "version_not_found", message: "转写版本不存在", hint: "刷新版本列表。" },
  });
  await assert.rejects(
    () =>
      withFetch(textResponse(body, 404, "application/json"), () =>
        api.subtitlePreview("ver_missing"),
      ),
    (error) => {
      assert.ok(error instanceof RequestFailed);
      assert.equal(error.status, 404);
      assert.equal(error.detail.code, "version_not_found");
      assert.equal(error.detail.message, "转写版本不存在");
      return true;
    },
  );
});

test("an empty subtitle body is empty, not the string 'undefined'", async () => {
  // A version with no cues returns ""; the panel checks truthiness to decide
  // between the preview and 「没有可导出的字幕」, so "undefined" would be a lie.
  const { result } = await withFetch(textResponse(""), () => api.subtitlePreview("ver_1"));
  assert.equal(result, "");
});
