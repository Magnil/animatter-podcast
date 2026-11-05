/**
 * Tests for the transcript pane's pure helpers.
 *
 * These import the real module rather than a re-implementation (Node strips the
 * TypeScript natively), so a renamed field cannot pass here and break in the app.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  bubbleCenter,
  describeSelection,
  findActiveSentence,
  isSingleWord,
  selectionRange,
  tokenRangeOfSentences,
  wordState,
} from "./transcript.ts";

const words = [
  { idx: 0, start_ms: 0, end_ms: 400 },
  { idx: 1, start_ms: 400, end_ms: 900 },
  { idx: 2, start_ms: 900, end_ms: 1300 },
];

test("wordState reports kept when nothing overlaps", () => {
  assert.equal(wordState(words[1], []), "kept");
  assert.equal(wordState(words[1], [{ start_ms: 900, end_ms: 1300 }]), "kept");
});

test("wordState reports deleted when one span covers the whole word", () => {
  assert.equal(wordState(words[1], [{ start_ms: 300, end_ms: 1000 }]), "deleted");
});

test("wordState reports deleted when adjacent cuts together cover the word", () => {
  // This is the case a per-span test got wrong: deleting two neighbouring words
  // produces two half-open cuts that meet inside a third word.
  assert.equal(
    wordState(words[1], [
      { start_ms: 400, end_ms: 600 },
      { start_ms: 600, end_ms: 900 },
    ]),
    "deleted",
  );
});

test("wordState reports partial when audio survives inside the word", () => {
  assert.equal(wordState(words[1], [{ start_ms: 400, end_ms: 600 }]), "partial");
  assert.equal(
    wordState(words[1], [
      { start_ms: 400, end_ms: 500 },
      { start_ms: 700, end_ms: 900 },
    ]),
    "partial",
  );
});

test("wordState ignores a zero-length word", () => {
  assert.equal(wordState({ idx: 0, start_ms: 500, end_ms: 500 }, [{ start_ms: 0, end_ms: 900 }]), "kept");
});

test("selectionRange normalises a reversed drag", () => {
  assert.deepEqual(selectionRange({ from: 5, to: 2 }), { from: 2, to: 5 });
  assert.deepEqual(selectionRange({ from: 2, to: 5 }), { from: 2, to: 5 });
});

test("selectionRange rejects an empty selection", () => {
  assert.equal(selectionRange({ from: 3, to: 3 }), null);
  assert.equal(selectionRange(null), null);
});

test("isSingleWord is the guard that keeps corrections 1:1", () => {
  assert.equal(isSingleWord({ from: 3, to: 4 }), true);
  assert.equal(isSingleWord({ from: 3, to: 5 }), false);
  assert.equal(isSingleWord(null), false);
});

test("bubbleCenter keeps the whole bubble inside the viewport", () => {
  // The bubble is centred with translateX(-50%), so the bounds are inset by half
  // its width — clamping the left edge instead is what put a bubble near the
  // margin half off screen.
  assert.equal(bubbleCenter(500, 168, 1000), 500);
  assert.equal(bubbleCenter(980, 168, 1000), 1000 - 8 - 84);
  assert.equal(bubbleCenter(4, 168, 1000), 8 + 84);
});

test("bubbleCenter centres on a viewport narrower than the bubble", () => {
  // Bounds invert (min 92 > max 8); it must centre, not park at an arbitrary edge.
  assert.equal(bubbleCenter(50, 168, 100), 50);
});

test("describeSelection names the single word", () => {
  const tokens = [{ text: "播客" }, { text: "剪辑" }];
  assert.equal(describeSelection(tokens, { from: 0, to: 1 }), "已选 1 个词「播客」");
  assert.equal(describeSelection(tokens, { from: 0, to: 2 }), "已选 2 个词「播客剪辑」");
});

test("describeSelection truncates long selections", () => {
  const tokens = [{ text: "一".repeat(40) }];
  const label = describeSelection(tokens, { from: 0, to: 1 });
  assert.ok(label.includes("…"), label);
  assert.ok(label.length < 40, label);
});

test("describeSelection explains the empty state", () => {
  assert.match(describeSelection([], null), /点击或拖选/);
});

test("findActiveSentence picks the containing sentence only", () => {
  const sentences = [
    { ord: 0, start_ms: 0, end_ms: 1000 },
    { ord: 1, start_ms: 1000, end_ms: 2000 },
  ];
  assert.equal(findActiveSentence(sentences, 0), 0);
  assert.equal(findActiveSentence(sentences, 999), 0);
  assert.equal(findActiveSentence(sentences, 1000), 1);
  assert.equal(findActiveSentence(sentences, 2000), -1);
});

test("tokenRangeOfSentences spans from the first to the last picked sentence", () => {
  const sentences = [
    { ord: 0, token_start: 0, token_end: 4 },
    { ord: 1, token_start: 4, token_end: 9 },
    { ord: 2, token_start: 9, token_end: 12 },
  ];
  assert.deepEqual(tokenRangeOfSentences(sentences, new Set([0, 2])), { from: 0, to: 12 });
  assert.deepEqual(tokenRangeOfSentences(sentences, new Set([1])), { from: 4, to: 9 });
  assert.equal(tokenRangeOfSentences(sentences, new Set()), null);
});
