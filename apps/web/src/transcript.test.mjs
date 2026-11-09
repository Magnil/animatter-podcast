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
  findActiveSentence,
  hiddenSpans,
  isSingleWord,
  selectionRange,
  speakerConflict,
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

// --- speaker conflicts --------------------------------------------------
// The server refuses these outright; these tests are about knowing *before*
// the click, and about the message naming the real reason instead of pointing
// at the selection.

const dialogue = [
  { token_start: 0, token_end: 4, speaker_id: 0 },
  { token_start: 4, token_end: 8, speaker_id: 1 },
  { token_start: 8, token_end: 12, speaker_id: 0 },
];

test("merging two different speakers is reported, and says what to do", () => {
  const reason = speakerConflict(dialogue, { from: 0, to: 8 }, "merge");
  assert.match(reason, /不同说话人/);
  assert.match(reason, /取消.*标注/, "拒绝必须给出下一步");
});

test("merging within one speaker is allowed", () => {
  assert.equal(speakerConflict(dialogue, { from: 0, to: 4 }, "merge"), null);
});

test("merging unannotated sentences is allowed", () => {
  const bare = dialogue.map((s) => ({ ...s, speaker_id: null }));
  assert.equal(speakerConflict(bare, { from: 0, to: 8 }, "merge"), null);
});

test("one annotated and one unannotated sentence can still merge", () => {
  const mixed = [
    { token_start: 0, token_end: 4, speaker_id: null },
    { token_start: 4, token_end: 8, speaker_id: 1 },
  ];
  assert.equal(speakerConflict(mixed, { from: 0, to: 8 }, "merge"), null);
});

test("joining a whole sentence upward checks the neighbour too", () => {
  // Selection is exactly sentence 1 (speaker 1); its neighbour above is speaker 0.
  assert.match(speakerConflict(dialogue, { from: 4, to: 8 }, "to_prev"), /不同说话人/);
  // Sentence 2 (speaker 0) joined up into sentence 1 (speaker 1) — also refused.
  assert.match(speakerConflict(dialogue, { from: 8, to: 12 }, "to_prev"), /不同说话人/);
});

test("joining within the same speaker is allowed", () => {
  const same = [
    { token_start: 0, token_end: 4, speaker_id: 0 },
    { token_start: 4, token_end: 8, speaker_id: 0 },
  ];
  assert.equal(speakerConflict(same, { from: 4, to: 8 }, "to_prev"), null);
});

test("moving part of a sentence to another speaker's sentence is refused", () => {
  // Half of sentence 1 moves up into sentence 0.
  assert.match(speakerConflict(dialogue, { from: 4, to: 6 }, "to_prev"), /归属/);
});

test("splitting is never a speaker conflict", () => {
  // A split keeps both halves with their original speaker, so it cannot
  // misattribute anything and must not be gated.
  assert.equal(speakerConflict(dialogue, { from: 2, to: 6 }, "new_sentence"), null);
});

test("an empty selection reports nothing", () => {
  assert.equal(speakerConflict(dialogue, null, "merge"), null);
  assert.equal(speakerConflict(dialogue, { from: 4, to: 4 }, "merge"), null);
});

test("the first sentence joining up has no neighbour and is not blocked", () => {
  assert.equal(speakerConflict(dialogue, { from: 0, to: 4 }, "to_prev"), null);
});

test("joining is only about the two adjacent sentences", () => {
  // A multi-sentence selection is not a join; the server rejects it for a
  // different reason ("selection spans more than one sentence") and this
  // helper must not claim a speaker conflict it cannot see.
  const many = [
    { token_start: 0, token_end: 4, speaker_id: 0 },
    { token_start: 4, token_end: 8, speaker_id: 1 },
    { token_start: 8, token_end: 12, speaker_id: 0 },
  ];
  assert.equal(speakerConflict(many, { from: 0, to: 12 }, "to_prev"), null);
});

test('hiddenSpans picks out only the hidden words', () => {
  const tokens = [
    { idx: 0, start_ms: 0, end_ms: 400, hidden: true },
    { idx: 1, start_ms: 400, end_ms: 900 },
    { idx: 2, start_ms: 900, end_ms: 1300, hidden: true },
  ];
  assert.deepEqual(hiddenSpans(tokens), [
    { start_ms: 0, end_ms: 400 },
    { start_ms: 900, end_ms: 1300 },
  ]);
});

test('hiddenSpans merges adjacent hidden words into one mark', () => {
  // Two filler words scraped out back to back are contiguous in time; two lines
  // 1 px apart look like one thick misaligned line, so they are one span.
  const tokens = [
    { idx: 0, start_ms: 0, end_ms: 400, hidden: true },
    { idx: 1, start_ms: 400, end_ms: 900, hidden: true },
    { idx: 2, start_ms: 900, end_ms: 1300 },
  ];
  assert.deepEqual(hiddenSpans(tokens), [{ start_ms: 0, end_ms: 900 }]);
});

test('hiddenSpans merges across the silent gap between words', () => {
  // Real transcripts have a 100-200 ms gap between words. Merging only when the
  // times touch drew two lines for one selected run of seven words.
  const tokens = [
    { idx: 0, start_ms: 0, end_ms: 400, hidden: true },
    { idx: 1, start_ms: 560, end_ms: 900, hidden: true },
  ];
  assert.deepEqual(hiddenSpans(tokens), [{ start_ms: 0, end_ms: 900 }]);
});

test('hiddenSpans keeps an audible word between two hidden ones', () => {
  const tokens = [
    { idx: 0, start_ms: 0, end_ms: 400, hidden: true },
    { idx: 1, start_ms: 400, end_ms: 900 },
    { idx: 2, start_ms: 900, end_ms: 1300, hidden: true },
  ];
  assert.equal(hiddenSpans(tokens).length, 2);
});

test('hiddenSpans ignores zero-length hidden tokens', () => {
  // There is no audio at that instant, so a line would point at nothing.
  const tokens = [{ idx: 0, start_ms: 400, end_ms: 400, hidden: true }];
  assert.deepEqual(hiddenSpans(tokens), []);
});

test('hiddenSpans returns nothing when nothing is hidden', () => {
  assert.deepEqual(hiddenSpans(words), []);
});
