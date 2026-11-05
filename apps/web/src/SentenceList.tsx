/**
 * Sentence list with two kinds of selection.
 *
 * The editor needs both granularities, and they mean different things:
 *
 * * **Character selection** (`{from, to}` over tokens) — re-draw a boundary or
 *   fix a word. Token cells render as individual clickable spans rather than a
 *   contenteditable block (AGENT.md §7.3): every edit reduces to a token-range
 *   operation, and DOM selection ranges do not map onto that model reliably.
 * * **Sentence selection** (`Set<ord>`) — act on whole sentences: merge into a
 *   neighbour, or delete several at once.
 *
 * They are separate state rather than one derived from the other, because that
 * is what makes "select 5 sentences and delete" two clicks instead of a
 * 300-character drag. Starting one clears the other, so the action bar always
 * has exactly one unambiguous target.
 *
 * Text repair stays selection-driven: the backend locates `original` verbatim in
 * the token stream, and punctuation lives in `punc_after`, so a whole-sentence
 * string with punctuation could never be located.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Sentence, Token } from "./api";
import { charsPerSecond, formatTime } from "./format";

export interface TokenSelection {
  from: number;
  to: number;
}

interface Props {
  sentences: Sentence[];
  tokens: Token[];
  positionMs: number;
  selection: TokenSelection | null;
  sentenceSelection: Set<number>;
  onSelectionChange: (selection: TokenSelection | null) => void;
  onSentenceSelectionChange: (selection: Set<number>) => void;
  onPlaySentence: (sentence: Sentence) => void;
  onSeek: (ms: number) => void;
  onEditSelection: (proposed: string) => void;
  /** Time ranges already deleted, so those sentences render struck through. */
  deletedRanges: { start_ms: number; end_ms: number }[];
}

const CPS_WARN_ZH = 9;

export function SentenceList({
  sentences,
  tokens,
  positionMs,
  selection,
  sentenceSelection,
  onSelectionChange,
  onSentenceSelectionChange,
  onPlaySentence,
  onSeek,
  onEditSelection,
  deletedRanges,
}: Props): React.JSX.Element {
  const dragAnchor = useRef<number | null>(null);
  const sentenceAnchor = useRef<number | null>(null);
  const [editing, setEditing] = useState<{ original: string; value: string } | null>(null);

  const activeOrd = findActiveSentence(sentences, positionMs);

  const low = selection ? Math.min(selection.from, selection.to) : -1;
  const high = selection ? Math.max(selection.from, selection.to) : -1;

  const isSelected = useCallback(
    (idx: number): boolean => selection !== null && idx >= low && idx < high,
    [selection, low, high],
  );

  const selectedText = selection
    ? tokens.slice(low, high).map((token) => token.text).join("")
    : "";

  const isDeleted = useCallback(
    (sentence: Sentence): boolean =>
      deletedRanges.some(
        (range) => sentence.start_ms >= range.start_ms && sentence.end_ms <= range.end_ms + 1,
      ),
    [deletedRanges],
  );

  const handleTokenDown = (idx: number, shift: boolean): void => {
    // Character work and sentence work are exclusive.
    if (sentenceSelection.size > 0) onSentenceSelectionChange(new Set());
    if (shift && selection) {
      onSelectionChange({ from: selection.from, to: idx + 1 });
      return;
    }
    dragAnchor.current = idx;
    onSelectionChange({ from: idx, to: idx + 1 });
  };

  const handleTokenEnter = (idx: number): void => {
    if (dragAnchor.current === null) return;
    onSelectionChange({ from: dragAnchor.current, to: idx + 1 });
  };

  /** Sentence gutter: click = one, Shift = range, Ctrl/Cmd = toggle. */
  const handleSentenceClick = (
    ord: number,
    event: React.MouseEvent<HTMLButtonElement>,
  ): void => {
    onSelectionChange(null);

    if (event.shiftKey && sentenceAnchor.current !== null) {
      const start = Math.min(sentenceAnchor.current, ord);
      const end = Math.max(sentenceAnchor.current, ord);
      const next = new Set<number>();
      for (let i = start; i <= end; i += 1) next.add(i);
      onSentenceSelectionChange(next);
      return;
    }

    if (event.ctrlKey || event.metaKey) {
      const next = new Set(sentenceSelection);
      if (next.has(ord)) next.delete(ord);
      else next.add(ord);
      sentenceAnchor.current = ord;
      onSentenceSelectionChange(next);
      return;
    }

    sentenceAnchor.current = ord;
    onSentenceSelectionChange(new Set([ord]));
  };

  useEffect(() => {
    const clear = (): void => {
      dragAnchor.current = null;
    };
    window.addEventListener("mouseup", clear);
    return () => window.removeEventListener("mouseup", clear);
  }, []);

  useEffect(() => {
    setEditing(null);
  }, [selection]);

  return (
    <div className="sentence-list" role="list">
      {sentences.map((sentence) => {
        const slice = tokens.slice(sentence.token_start, sentence.token_end);
        const cps = charsPerSecond(sentence.text, sentence.start_ms, sentence.end_ms);
        const tooFast = cps > CPS_WARN_ZH;
        const isActive = sentence.ord === activeOrd;
        const picked = sentenceSelection.has(sentence.ord);
        const removed = isDeleted(sentence);

        return (
          <div
            key={sentence.id}
            role="listitem"
            className={`sentence${isActive ? " active" : ""}${picked ? " picked" : ""}${
              removed ? " removed" : ""
            }`}
            data-ord={sentence.ord}
          >
            <div className="sentence-head">
              <button
                type="button"
                className="sentence-pick"
                aria-pressed={picked}
                aria-label={`选中第 ${sentence.ord + 1} 句`}
                title="选中整句（Shift 连选，Ctrl 加选）"
                onClick={(event) => handleSentenceClick(sentence.ord, event)}
              >
                {picked ? "☑" : "☐"}
              </button>
              <button
                type="button"
                className="sentence-time"
                onClick={() => onSeek(sentence.start_ms)}
                title="跳到这一句"
              >
                {formatTime(sentence.start_ms)}
              </button>
              <span className="sentence-meta">
                {sentence.speaker_id !== null ? `说话人${sentence.speaker_id + 1} · ` : ""}
                {removed ? <span className="removed-tag">已删除</span> : null}
                {tooFast ? (
                  <span className="warn" title={`每秒 ${cps.toFixed(1)} 字，超出阅读速度`}>
                    语速过快
                  </span>
                ) : null}
              </span>
              <button type="button" className="ghost" onClick={() => onPlaySentence(sentence)}>
                ▶ 听这句
              </button>
            </div>

            <div className="tokens">
              {slice.map((token) => (
                <span
                  key={token.idx}
                  className={`token${isSelected(token.idx) ? " selected" : ""}${
                    token.derived ? " derived" : ""
                  }`}
                  role="button"
                  tabIndex={0}
                  title={`${formatTime(token.start_ms)} – ${formatTime(token.end_ms)}${
                    token.derived ? "（时间为插值估算）" : ""
                  }`}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    handleTokenDown(token.idx, event.shiftKey);
                  }}
                  onMouseEnter={() => handleTokenEnter(token.idx)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      handleTokenDown(token.idx, event.shiftKey);
                    }
                  }}
                >
                  {token.text}
                  {token.punc_after ? <span className="punct">{token.punc_after}</span> : null}
                </span>
              ))}
            </div>

            {editing ? (
              <form
                className="text-edit"
                onSubmit={(event) => {
                  event.preventDefault();
                  const { original, value } = editing;
                  setEditing(null);
                  if (value.trim() && value !== original) onEditSelection(value);
                }}
              >
                <span className="edit-original">「{editing.original}」→</span>
                <input
                  autoFocus
                  value={editing.value}
                  onChange={(event) => setEditing({ ...editing, value: event.target.value })}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setEditing(null);
                  }}
                  aria-label="替换选中的文字"
                />
                <button type="submit">保存</button>
                <button type="button" className="ghost" onClick={() => setEditing(null)}>
                  取消
                </button>
              </form>
            ) : null}
          </div>
        );
      })}

      {selection && !editing ? (
        <div className="selection-bar" role="toolbar" aria-label="选中文字的操作">
          <span className="selection-info">
            已选 {high - low} 字「{selectedText}」
          </span>
          <button
            type="button"
            onClick={() => setEditing({ original: selectedText, value: selectedText })}
          >
            改文字
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function findActiveSentence(sentences: Sentence[], positionMs: number): number {
  for (const sentence of sentences) {
    if (positionMs >= sentence.start_ms && positionMs < sentence.end_ms) return sentence.ord;
  }
  return -1;
}

/** Token range covered by a set of selected sentences. */
export function tokenRangeOfSentences(
  sentences: Sentence[],
  selected: Set<number>,
): { from: number; to: number } | null {
  const picked = sentences.filter((s) => selected.has(s.ord));
  if (picked.length === 0) return null;
  return {
    from: Math.min(...picked.map((s) => s.token_start)),
    to: Math.max(...picked.map((s) => s.token_end)),
  };
}
