/**
 * Navigation over a timeline that contains deletions.
 *
 * The rule this module exists to enforce: **navigation must only ever land on
 * audio that still exists**. Before it existed, two bugs came from ignoring that:
 *
 * * Deleting the previous sentence and pressing `[` did nothing, because the
 *   cursor was already at the start of a deleted span (seek is a no-op there).
 * * Clicking the silence before a deleted sentence and pressing space produced
 *   silence, because the computed stop point was behind the start point.
 *
 * Both are the same mistake in different clothes: treating "previous sentence"
 * and "next sentence" as list indices rather than as positions in surviving audio.
 */

export interface Span {
  start_ms: number;
  end_ms: number;
}

export interface SentenceLike {
  ord: number;
  start_ms: number;
  end_ms: number;
}

/** True when `[start, end)` is entirely covered by deleted spans. */
export function isDeleted(spans: Span[], startMs: number, endMs: number): boolean {
  if (endMs <= startMs) return true;
  let cursor = startMs;
  for (const span of sortSpans(spans)) {
    if (span.end_ms <= cursor) continue;
    // A surviving gap means part of the range is audible.
    if (span.start_ms > cursor) return false;
    cursor = Math.max(cursor, span.end_ms);
    if (cursor >= endMs) return true;
  }
  return false;
}

export function sortSpans(spans: Span[]): Span[] {
  return [...spans].sort((a, b) => a.start_ms - b.start_ms);
}

/** First position at or after `ms` that is not inside a deleted span. */
export function nextAudible(spans: Span[], ms: number): number {
  let position = ms;
  // Bounded loop: each iteration strictly advances past one span.
  for (let guard = 0; guard < spans.length + 1; guard += 1) {
    const hit = sortSpans(spans).find((s) => position >= s.start_ms && position < s.end_ms);
    if (!hit) return position;
    position = hit.end_ms;
  }
  return position;
}

/** True when any deleted span starts at or before `ms` and ends after it. */
export function insideDeletion(spans: Span[], ms: number): boolean {
  return spans.some((s) => ms >= s.start_ms && ms < s.end_ms);
}

/**
 * Sentences that still have audio, in order.
 *
 * A partially deleted sentence stays in the list: the user may still want to
 * hear its surviving parts, and dropping it would make navigation skip content
 * that is actually there.
 */
export function audibleSentences(
  sentences: SentenceLike[],
  spans: Span[],
): SentenceLike[] {
  return sentences.filter((s) => !isDeleted(spans, s.start_ms, s.end_ms));
}

/**
 * The sentence to play next, skipping anything fully deleted.
 * Returns `null` when every later sentence is gone.
 */
export function nextSentence(
  spans: Span[],
  sentences: SentenceLike[],
  currentOrd: number,
): SentenceLike | null {
  const ordered = [...sentences].sort((a, b) => a.ord - b.ord);
  for (const sentence of ordered) {
    if (sentence.ord <= currentOrd) continue;
    if (isDeleted(spans, sentence.start_ms, sentence.end_ms)) continue;
    return sentence;
  }
  return null;
}

/** The sentence to play before `currentOrd`, skipping anything fully deleted. */
export function previousSentence(
  spans: Span[],
  sentences: SentenceLike[],
  currentOrd: number,
): SentenceLike | null {
  const ordered = [...sentences].sort((a, b) => b.ord - a.ord);
  for (const sentence of ordered) {
    if (sentence.ord >= currentOrd) continue;
    if (isDeleted(spans, sentence.start_ms, sentence.end_ms)) continue;
    return sentence;
  }
  return null;
}

/** Sentence containing `ms`, or the one immediately before it. */
export function sentenceAtOrBefore(sentences: SentenceLike[], ms: number): SentenceLike | null {
  let best: SentenceLike | null = null;
  for (const sentence of sentences) {
    if (sentence.start_ms <= ms) {
      if (best === null || sentence.start_ms > best.start_ms) best = sentence;
    }
  }
  return best;
}

/**
 * Where playback should actually start for `sentence`.
 *
 * Returns `null` when the sentence has no surviving audio. When only part of it
 * survives, the start is snapped forward into the first surviving run.
 */
export function audibleStart(spans: Span[], sentence: SentenceLike): number | null {
  if (isDeleted(spans, sentence.start_ms, sentence.end_ms)) return null;
  const start = nextAudible(spans, sentence.start_ms);
  if (start >= sentence.end_ms) return null;
  return start;
}

/**
 * Stop point for "play this sentence", or `null` for "play until the natural end".
 *
 * The stop must be after the start; otherwise `playRange` pauses immediately and
 * the command looks broken — which is exactly what happened when the sentence
 * began inside a deletion.
 */
export function audibleStop(spans: Span[], startMs: number, endMs: number): number | null {
  if (endMs <= startMs) return null;
  // If a deletion begins inside the sentence, stopping there is the honest end.
  const cut = sortSpans(spans).find((s) => s.start_ms > startMs && s.start_ms < endMs);
  if (cut) return cut.start_ms > startMs ? cut.start_ms : null;
  return endMs;
}

/**
 * End of the surviving run that starts at `fromMs`.
 *
 * Used when playback starts in an area no sentence covers (leading silence, or
 * the gap left by a deletion): play until the next deletion or the media ends,
 * rather than guessing a sentence.
 */
export function nextBoundary(spans: Span[], fromMs: number, durationMs: number): number {
  const cut = sortSpans(spans).find((s) => s.start_ms > fromMs);
  const end = cut ? cut.start_ms : durationMs;
  return end > fromMs ? end : durationMs;
}
