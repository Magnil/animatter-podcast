/**
 * Paging through a whole transcript.
 *
 * The server pages sentences by design — a three-hour episode is ~40k tokens and
 * no single response should carry all of them. What went wrong was the client:
 * it asked for one page and then treated that page as the transcript. The status
 * bar printed `version.sentence_count` (1074, from the database) while the list
 * ended at sentence 399 (40 minutes into a 105-minute file), so the app looked
 * like transcription had stopped early. It had not: the data was always there.
 *
 * This module is the "fetch until done" loop, with the fetch injected so it can
 * be tested without a browser or a server.
 */

export interface TranscriptPage<Token> {
  sentences: { token_start: number; token_end: number }[];
  tokens: Token[];
}

export interface PageSource<Page> {
  (from: number, limit: number): Promise<Page>;
}

/** Sentences per request. Matches the server's own cap, so nothing is truncated. */
export const PAGE_SIZE = 500;

/**
 * Fetch every sentence, in order, concatenating tokens.
 *
 * `totalSentences` comes from the version row. It is the loop bound *and* the
 * consistency check: if the server ever returns fewer than it promises we stop
 * and say so rather than silently rendering a short list.
 *
 * A page that returns no sentences ends the loop even when `totalSentences`
 * says otherwise — that is a server/client disagreement, not a reason to spin.
 */
export async function loadAllSentences<Page extends TranscriptPage<unknown>>(
  source: PageSource<Page>,
  totalSentences: number,
  onPage?: (loadedSentences: number, totalSentences: number) => void,
): Promise<Page> {
  const sentences: Page["sentences"] = [];
  let tokens: unknown[] = [];
  let first: Page | null = null;

  for (let from = 0; ; from += PAGE_SIZE) {
    const page = await source(from, PAGE_SIZE);
    if (first === null) first = page;
    if (page.sentences.length === 0) break;
    sentences.push(...page.sentences);
    tokens = tokens.concat(page.tokens);
    onPage?.(sentences.length, totalSentences);
    if (sentences.length >= totalSentences) break;
  }

  if (first === null) throw new Error("没有取到转写内容");
  return { ...first, sentences, tokens: tokens as Page["tokens"] };
}
