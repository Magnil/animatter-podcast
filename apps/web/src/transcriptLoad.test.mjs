/**
 * Tests for the transcript paging loop.
 *
 * The bug: the app asked the server for one page of 400 sentences and treated it
 * as the transcript, so a fully transcribed 105-minute episode displayed 40
 * minutes of it while the header printed the database's sentence count. Nothing
 * was missing — the client just never asked for the rest.
 *
 * These tests use a fake page source, so they pin the *loop*: how many requests,
 * from where, and what happens when the server stops early.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { PAGE_SIZE, loadAllSentences } from "./transcriptLoad.ts";

/** Build a fake transcript of `total` sentences with `wordsPer` tokens each. */
function fakeTranscript(total, wordsPer = 3) {
  const sentences = [];
  const tokens = [];
  for (let i = 0; i < total; i += 1) {
    const start = i * wordsPer;
    for (let w = 0; w < wordsPer; w += 1) {
      tokens.push({ idx: start + w, text: `w${start + w}` });
    }
    sentences.push({ ord: i, token_start: start, token_end: start + wordsPer, text: `s${i}` });
  }
  return { sentences, tokens };
}

function pagingSource(transcript, calls = []) {
  return async (from, limit) => {
    calls.push({ from, limit });
    return {
      version: { sentence_count: transcript.sentences.length },
      sentences: transcript.sentences.slice(from, from + limit),
      tokens: transcript.tokens.slice(
        transcript.sentences[from]?.token_start ?? 0,
        transcript.sentences[from + limit]?.token_start ?? transcript.tokens.length,
      ),
    };
  };
}

test("a short transcript is fetched in one page", async () => {
  const transcript = fakeTranscript(120);
  const calls = [];
  const result = await loadAllSentences(pagingSource(transcript, calls), 120);

  assert.equal(result.sentences.length, 120);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { from: 0, limit: PAGE_SIZE });
});

test("a transcript longer than one page is fetched completely", async () => {
  // 1074 sentences is the real episode that exposed this.
  const transcript = fakeTranscript(1074);
  const result = await loadAllSentences(pagingSource(transcript), 1074);

  assert.equal(result.sentences.length, 1074);
  assert.equal(result.sentences.at(-1).ord, 1073);
  // Every token, in order, with no gap between pages: a page boundary that
  // dropped the last token of page 1 would shift every later sentence's words.
  assert.equal(result.tokens.length, 1074 * 3);
  assert.deepEqual(
    result.tokens.map((t) => t.idx),
    Array.from({ length: 1074 * 3 }, (_v, i) => i),
  );
});

test("the final page does not request more than it needs", async () => {
  const transcript = fakeTranscript(600);
  const calls = [];
  await loadAllSentences(pagingSource(transcript, calls), 600);

  assert.deepEqual(
    calls.map((c) => c.from),
    [0, PAGE_SIZE],
  );
});

test("loading reports progress page by page", async () => {
  const transcript = fakeTranscript(1200);
  const seen = [];
  await loadAllSentences(pagingSource(transcript), 1200, (loaded, total) =>
    seen.push([loaded, total]),
  );
  assert.deepEqual(seen, [
    [500, 1200],
    [1000, 1200],
    [1200, 1200],
  ]);
});

test("a server that stops early ends the loop instead of spinning", async () => {
  // total says 900 but the source only has 640: the loop must terminate and
  // return what exists, not hammer the API for pages that will never arrive.
  const transcript = fakeTranscript(640);
  const result = await loadAllSentences(pagingSource(transcript), 900);

  assert.equal(result.sentences.length, 640);
});

test("a completely empty transcript returns its first page rather than throwing", async () => {
  const result = await loadAllSentences(
    async () => ({ version: { sentence_count: 0 }, sentences: [], tokens: [] }),
    0,
  );
  assert.equal(result.sentences.length, 0);
});
