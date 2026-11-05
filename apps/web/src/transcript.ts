/**
 * Pure helpers for the transcript pane.
 *
 * Kept out of the TSX so `node --test` can exercise them without a build step:
 * the bug class these guard against ("a handler receives the right number of
 * arguments but the wrong value") is invisible to the type checker.
 */

export interface Span {
  start_ms: number;
  end_ms: number;
}

export interface TokenLike {
  idx: number;
  start_ms: number;
  end_ms: number;
}

export interface SentenceLike {
  ord: number;
  start_ms: number;
  end_ms: number;
}

export interface TokenRange {
  from: number;
  to: number;
}

/**
 * How much of one word the deletion set covers.
 *
 * ``deleted`` means the whole word is gone — the case the editor paints red and
 * strikes through. ``partial`` only happens after a boundary drag nibbled at a
 * word's edge; striking the whole word out would then claim audio was removed
 * when part of it is still audible, so it gets its own milder style.
 */
export type WordState = "kept" | "partial" | "deleted";

export function wordState(token: TokenLike, spans: Span[]): WordState {
  if (token.end_ms <= token.start_ms) {
    // A zero-length token has no audio to remove; calling it deleted would be a
    // lie about a word nobody can hear either way.
    return "kept";
  }
  // Union the overlapping spans first. Two adjacent cuts that *together* cover a
  // word are the common case after deleting neighbouring words, and testing each
  // span on its own would report "partial" while no audio survives.
  const overlapping = spans
    .filter((span) => span.end_ms > token.start_ms && span.start_ms < token.end_ms)
    .map((span) => ({
      start_ms: Math.max(span.start_ms, token.start_ms),
      end_ms: Math.min(span.end_ms, token.end_ms),
    }))
    .sort((a, b) => a.start_ms - b.start_ms);

  if (overlapping.length === 0) return "kept";

  let cursor = token.start_ms;
  for (const span of overlapping) {
    if (span.start_ms > cursor) return "partial";
    cursor = Math.max(cursor, span.end_ms);
    if (cursor >= token.end_ms) return "deleted";
  }
  return "partial";
}

/** Normalise a possibly-reversed selection; `null` when it is empty. */
export function selectionRange(selection: TokenRange | null): TokenRange | null {
  if (!selection) return null;
  const from = Math.min(selection.from, selection.to);
  const to = Math.max(selection.from, selection.to);
  return to > from ? { from, to } : null;
}

export function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

/**
 * Horizontal **centre** for the floating bubble, kept inside the viewport.
 *
 * The bubble is positioned with ``left`` + ``translateX(-50%)``, so the value
 * returned here is the centre, not the left edge. Clamping the left edge and
 * then shifting by half the width is what pushed the bubble for a word near the
 * left margin half off screen.
 */
export function bubbleCenter(
  preferredCenter: number,
  width: number,
  viewportWidth: number,
  margin = 8,
): number {
  const half = width / 2;
  const min = margin + half;
  const max = viewportWidth - margin - half;
  // A viewport narrower than the bubble: centre it rather than inverting the
  // clamp bounds (which would park it at an arbitrary negative offset).
  if (max < min) return viewportWidth / 2;
  return clamp(preferredCenter, min, max);
}

export function findActiveSentence(sentences: SentenceLike[], positionMs: number): number {
  for (const sentence of sentences) {
    if (positionMs >= sentence.start_ms && positionMs < sentence.end_ms) return sentence.ord;
  }
  return -1;
}

/** Token range covered by a set of selected sentences. */
export function tokenRangeOfSentences(
  sentences: { ord: number; token_start: number; token_end: number }[],
  selected: Set<number>,
): TokenRange | null {
  const picked = sentences.filter((sentence) => selected.has(sentence.ord));
  if (picked.length === 0) return null;
  return {
    from: Math.min(...picked.map((sentence) => sentence.token_start)),
    to: Math.max(...picked.map((sentence) => sentence.token_end)),
  };
}

/**
 * A selection is a whole word when it covers exactly one token.
 *
 * This is the guard that keeps corrections 1:1. Rewriting several words at once
 * has to redistribute the window's interior boundaries, which is precisely how
 * a correction used to nudge a cut point the user had been listening for.
 */
export function isSingleWord(range: TokenRange | null): boolean {
  return range !== null && range.to - range.from === 1;
}

/** One-line description of the current selection, for the toolbar. */
export function describeSelection(
  words: { text: string }[],
  range: TokenRange | null,
): string {
  if (!range) return "点击或拖选文字，选中整词后弹出「删除 / 修正」";
  const picked = words.slice(range.from, range.to);
  if (picked.length === 0) return "没有选中文字";
  const text = picked.map((word) => word.text).join("");
  const shown = text.length > 18 ? `${text.slice(0, 18)}…` : text;
  if (picked.length === 1) return `已选 1 个词「${shown}」`;
  return `已选 ${picked.length} 个词「${shown}」`;
}
