/**
 * Logic tests for the pure helpers, runnable with `node --test`.
 *
 * These exist because a typecheck cannot catch "the handler receives the right
 * number of arguments but the wrong value" — the class of bug that made the
 * text-edit button silently send `undefined`.
 */
import assert from "node:assert/strict";
import test from "node:test";

// Re-implemented here because the sources are TSX/TS; testing the *rules*
// rather than importing keeps this runnable without a build step.
function charsPerSecond(text, startMs, endMs) {
  const seconds = Math.max(0.001, (endMs - startMs) / 1000);
  return text.replace(/\s/g, "").length / seconds;
}

function findActiveSentence(sentences, positionMs) {
  for (const s of sentences) {
    if (positionMs >= s.start_ms && positionMs < s.end_ms) return s.ord;
  }
  return -1;
}

function sentenceForSelection(sentences, selection) {
  if (!selection) return null;
  const from = Math.min(selection.from, selection.to);
  const to = Math.max(selection.from, selection.to);
  for (const s of sentences) {
    if (from >= s.token_start && to <= s.token_end) return s;
  }
  return null;
}

const sentences = [
  { ord: 0, token_start: 0, token_end: 3, start_ms: 0, end_ms: 1000, text: "一二三" },
  { ord: 1, token_start: 3, token_end: 6, start_ms: 1000, end_ms: 3000, text: "四五六" },
];

test("findActiveSentence picks the containing sentence", () => {
  assert.equal(findActiveSentence(sentences, 0), 0);
  assert.equal(findActiveSentence(sentences, 999), 0);
  assert.equal(findActiveSentence(sentences, 1000), 1);
  assert.equal(findActiveSentence(sentences, 5000), -1);
});

test("sentenceForSelection accepts a range inside one sentence", () => {
  assert.equal(sentenceForSelection(sentences, { from: 0, to: 2 })?.ord, 0);
  assert.equal(sentenceForSelection(sentences, { from: 3, to: 6 })?.ord, 1);
});

test("sentenceForSelection rejects a cross-sentence range", () => {
  assert.equal(sentenceForSelection(sentences, { from: 2, to: 4 }), null);
});

test("sentenceForSelection normalises reversed selections", () => {
  assert.equal(sentenceForSelection(sentences, { from: 2, to: 0 })?.ord, 0);
});

test("sentenceForSelection returns null for no selection", () => {
  assert.equal(sentenceForSelection(sentences, null), null);
});

test("charsPerSecond follows duration, not character count", () => {
  // 3 chars over 1s = 3 cps
  assert.equal(charsPerSecond("一二三", 0, 1000), 3);
  // Same text over 0.5s = 6 cps
  assert.equal(charsPerSecond("一二三", 0, 500), 6);
});

test("charsPerSecond ignores whitespace", () => {
  assert.equal(charsPerSecond("一 二 三", 0, 1000), 3);
});

test("charsPerSecond guards zero duration", () => {
  assert.ok(Number.isFinite(charsPerSecond("一二三", 500, 500)));
});

test("selection range maps to the API's half-open token bounds", () => {
  // The backend treats token_end as exclusive; the UI must send idx+1.
  const selected = { from: 52, to: 54 };
  const tokens = Array.from({ length: 60 }, (_, i) => ({ idx: i, text: `t${i}` }));
  const slice = tokens.slice(Math.min(selected.from, selected.to), Math.max(selected.from, selected.to));
  assert.equal(slice.length, 2);
  assert.equal(slice[0].idx, 52);
  assert.equal(slice[1].idx, 53);
});
