/**
 * Title length, counted the way a Chinese editor counts it.
 *
 * The rule is 「汉字 + 全角标点 ≤ 22，半角字母数字按字符计」, so "MV" costs two
 * characters and "：" costs one. This has to match the server's
 * `core/script.py::count_title_chars` exactly — the panel shows the number next
 * to each candidate, and a display that disagrees with the filter is worse than
 * no display: it invites the user to pick a title the backend has already
 * decided is too long.
 *
 * Whitespace is free (it occupies no cell), and zero-width joiners / variation
 * selectors belong to the character before them rather than counting twice.
 */

/** Code points that take up no cell of their own. */
const ZERO_WIDTH = new Set(["\u200b", "\u200c", "\u200d", "\ufe0e", "\ufe0f", "\u20e3"]);

export function countTitleChars(title: string): number {
  let total = 0;
  for (const char of title) {
    if (/\s/.test(char)) continue;
    if (ZERO_WIDTH.has(char)) continue;
    total += 1;
  }
  return total;
}

/** The cap the prompt and the backend both enforce. */
export const MAX_TITLE_CHARS = 22;
