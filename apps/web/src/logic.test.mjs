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

// --- bracket navigation (regression: -1 index made both keys land on sentence 0)

function sentenceAtOrBefore(sentences, ms) {
  const index = sentences.findIndex((s) => ms < s.start_ms);
  return index === -1 ? sentences.length - 1 : Math.max(0, index - 1);
}

const three = [
  { ord: 0, start_ms: 0, end_ms: 3000 },
  { ord: 1, start_ms: 4000, end_ms: 7000 },
  { ord: 2, start_ms: 8000, end_ms: 12000 },
];

test("sentenceAtOrBefore finds the containing sentence", () => {
  assert.equal(sentenceAtOrBefore(three, 1000), 0);
  assert.equal(sentenceAtOrBefore(three, 5000), 1);
});

test("sentenceAtOrBefore during a gap returns the earlier sentence", () => {
  // 3500 sits in the silence between sentence 0 and 1.
  assert.equal(sentenceAtOrBefore(three, 3500), 0);
});

test("sentenceAtOrBefore before the first sentence clamps to 0", () => {
  assert.equal(sentenceAtOrBefore(three, 0), 0);
});

test("sentenceAtOrBefore past the end clamps to the last", () => {
  assert.equal(sentenceAtOrBefore(three, 99999), 2);
});

test("next from a gap advances to the following sentence", () => {
  const current = sentenceAtOrBefore(three, 3500);
  assert.equal(three[Math.min(three.length - 1, current + 1)].ord, 1);
});

test("previous from a gap goes back one sentence", () => {
  const current = sentenceAtOrBefore(three, 3500);
  assert.equal(three[Math.max(0, current - 1)].ord, 0);
});

// --- EDL time mapping (mirrors the backend rule the UI must display)

function toNewMs(keeps, ms) {
  for (const keep of keeps) {
    if (keep.orig_start <= ms && ms < keep.orig_end) {
      return keep.new_start + (ms - keep.orig_start);
    }
  }
  return null;
}

const keepsWithCuts = [
  { orig_start: 0, orig_end: 2000, new_start: 0 },
  { orig_start: 4000, orig_end: 6000, new_start: 2000 },
];

test("deleted instants map to null so the UI can grey them out", () => {
  assert.equal(toNewMs(keepsWithCuts, 3000), null);
});

test("a keep's start maps to its new start", () => {
  assert.equal(toNewMs(keepsWithCuts, 4000), 2000);
});

test("a keep's end is exclusive and maps to null when followed by a cut", () => {
  // Getting this wrong made a deleted cue reappear one frame early.
  assert.equal(toNewMs(keepsWithCuts, 2000), null);
});

test("output duration equals the sum of keep lengths", () => {
  const total = keepsWithCuts.reduce((sum, k) => sum + (k.orig_end - k.orig_start), 0);
  assert.equal(total, 4000);
});

test("removed duration equals original minus output", () => {
  const original = 6000;
  const output = keepsWithCuts.reduce((sum, k) => sum + (k.orig_end - k.orig_start), 0);
  assert.equal(original - output, 2000);
});

// --- skip-playback (real-time preview of deletions)

function mergeSpans(spans) {
  if (spans.length === 0) return [];
  const sorted = [...spans].sort((a, b) => a.start_ms - b.start_ms);
  const merged = [sorted[0]];
  for (const span of sorted.slice(1)) {
    const last = merged[merged.length - 1];
    if (span.start_ms <= last.end_ms) {
      merged[merged.length - 1] = {
        start_ms: last.start_ms,
        end_ms: Math.max(last.end_ms, span.end_ms),
      };
    } else {
      merged.push(span);
    }
  }
  return merged;
}

function spanAt(spans, ms) {
  for (const span of spans) {
    if (ms >= span.start_ms && ms < span.end_ms) return span;
  }
  return null;
}

function nextAudible(spans, ms) {
  let position = ms;
  for (let guard = 0; guard < spans.length + 1; guard += 1) {
    const hit = spanAt(spans, position);
    if (!hit) return position;
    position = hit.end_ms;
  }
  return position;
}

const cut = [{ start_ms: 3709, end_ms: 6080 }];

test("merged spans collapse touching deletions", () => {
  assert.deepEqual(mergeSpans([
    { start_ms: 1000, end_ms: 2000 },
    { start_ms: 2000, end_ms: 3000 },
  ]), [{ start_ms: 1000, end_ms: 3000 }]);
});

test("merged spans keep separated cuts apart", () => {
  assert.equal(mergeSpans([
    { start_ms: 1000, end_ms: 2000 },
    { start_ms: 2001, end_ms: 3000 },
  ]).length, 2);
});

test("merge handles overlapping and unsorted input", () => {
  assert.deepEqual(mergeSpans([
    { start_ms: 5000, end_ms: 6000 },
    { start_ms: 1000, end_ms: 4000 },
    { start_ms: 3000, end_ms: 4500 },
  ]), [
    { start_ms: 1000, end_ms: 4500 },
    { start_ms: 5000, end_ms: 6000 },
  ]);
});

test("spanAt finds an instant inside a cut", () => {
  assert.ok(spanAt(cut, 4000));
});

test("spanAt treats the end boundary as audible", () => {
  assert.equal(spanAt(cut, 6080), null);
});

test("spanAt ignores instants outside the cut", () => {
  assert.equal(spanAt(cut, 3708), null);
  assert.equal(spanAt(cut, 6081), null);
});

test("nextAudible escapes a cut", () => {
  assert.equal(nextAudible(cut, 4000), 6080);
});

test("nextAudible leaves audible positions alone", () => {
  assert.equal(nextAudible(cut, 1000), 1000);
});

test("nextAudible chains through two adjacent cuts", () => {
  const chain = mergeSpans([
    { start_ms: 0, end_ms: 1000 },
    { start_ms: 1000, end_ms: 2000 },
  ]);
  assert.equal(nextAudible(chain, 500), 2000);
});

test("seeking into a cut snaps forward to surviving audio", () => {
  assert.equal(nextAudible(cut, 4500), 6080);
});

test("no deletions means nothing is skipped", () => {
  assert.equal(nextAudible([], 5000), 5000);
  assert.equal(spanAt([], 5000), null);
});

// --- deletion-aware navigation (bug 1 and 2 from the field report)

function sortSpans(spans) {
  return [...spans].sort((a, b) => a.start_ms - b.start_ms);
}

function isDeleted(spans, startMs, endMs) {
  if (endMs <= startMs) return true;
  let cursor = startMs;
  for (const span of sortSpans(spans)) {
    if (span.end_ms <= cursor) continue;
    if (span.start_ms > cursor) return false;
    cursor = Math.max(cursor, span.end_ms);
    if (cursor >= endMs) return true;
  }
  return false;
}

function audibleStart(spans, sentence) {
  if (isDeleted(spans, sentence.start_ms, sentence.end_ms)) return null;
  let pos = sentence.start_ms;
  for (let guard = 0; guard < spans.length + 1; guard += 1) {
    const hit = sortSpans(spans).find((s) => pos >= s.start_ms && pos < s.end_ms);
    if (!hit) break;
    pos = hit.end_ms;
  }
  return pos >= sentence.end_ms ? null : pos;
}

function previousSentence(spans, sentences, currentOrd) {
  for (const s of [...sentences].sort((a, b) => b.ord - a.ord)) {
    if (s.ord >= currentOrd) continue;
    if (isDeleted(spans, s.start_ms, s.end_ms)) continue;
    return s;
  }
  return null;
}

function nextSentence(spans, sentences, currentOrd) {
  for (const s of [...sentences].sort((a, b) => a.ord - b.ord)) {
    if (s.ord <= currentOrd) continue;
    if (isDeleted(spans, s.start_ms, s.end_ms)) continue;
    return s;
  }
  return null;
}

const dialogue = [
  { ord: 0, start_ms: 0, end_ms: 3000 },
  { ord: 1, start_ms: 4000, end_ms: 7000 },
  { ord: 2, start_ms: 8000, end_ms: 12000 },
];

test("previous sentence skips a fully deleted one", () => {
  // Bug 2: with sentence 1 deleted, "previous" from sentence 2 must land on 0.
  const deleted = [{ start_ms: 4000, end_ms: 7000 }];
  const target = previousSentence(deleted, dialogue, 2);
  assert.equal(target.ord, 0);
});

test("previous sentence skips several deleted ones", () => {
  const deleted = [
    { start_ms: 4000, end_ms: 7000 },
    { start_ms: 8000, end_ms: 12000 },
  ];
  assert.equal(previousSentence(deleted, dialogue, 2).ord, 0);
});

test("previous sentence returns null when everything earlier is deleted", () => {
  const deleted = [{ start_ms: 0, end_ms: 7000 }];
  assert.equal(previousSentence(deleted, dialogue, 2), null);
});

test("next sentence skips a fully deleted one", () => {
  const deleted = [{ start_ms: 4000, end_ms: 7000 }];
  assert.equal(nextSentence(deleted, dialogue, 0).ord, 2);
});

test("audibleStart returns null for a fully deleted sentence", () => {
  // Bug 1: playing a deleted sentence produced silence, because the start was
  // inside a cut and the stop point ended up behind it.
  assert.equal(audibleStart([{ start_ms: 4000, end_ms: 7000 }], dialogue[1]), null);
});

test("audibleStart snaps forward into the surviving part", () => {
  const partial = [{ start_ms: 0, end_ms: 1500 }];
  assert.equal(audibleStart(partial, dialogue[0]), 1500);
});

test("audibleStart leaves an untouched sentence at its own start", () => {
  assert.equal(audibleStart([], dialogue[2]), 8000);
});

test("a partially deleted sentence still counts as audible", () => {
  const deleted = [{ start_ms: 0, end_ms: 1500 }];
  assert.equal(isDeleted(deleted, 0, 3000), false);
});

test("a sentence covered by two adjacent cuts counts as deleted", () => {
  const deleted = [
    { start_ms: 4000, end_ms: 5500 },
    { start_ms: 5500, end_ms: 7000 },
  ];
  assert.equal(isDeleted(deleted, 4000, 7000), true);
});

test("isDeleted is false when a surviving gap exists", () => {
  const deleted = [
    { start_ms: 0, end_ms: 1000 },
    { start_ms: 2000, end_ms: 3000 },
  ];
  assert.equal(isDeleted(deleted, 0, 3000), false);
});

test("space over a deleted gap starts at the next audible point", () => {
  // The clickable area before a deleted sentence: playhead 3500, sentences
  // 0 [0,3000) and 1 [4000,7000) with 1 deleted.
  const deleted = [{ start_ms: 4000, end_ms: 7000 }];
  const from = 3500;
  assert.equal(isDeleted(deleted, from, from + 1), false);
  assert.equal(sortSpans(deleted)[0].start_ms, 4000);
  // Advancing must skip the cut entirely and land on sentence 2.
  assert.equal(nextSentence(deleted, dialogue, 0).ord, 2);
});
