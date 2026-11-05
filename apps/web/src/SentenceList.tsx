/**
 * Sentence list with character-level selection.
 *
 * Tokens render as individual clickable cells rather than a contenteditable
 * block (AGENT.md §7.3): every edit reduces to a token-range operation, and DOM
 * selection ranges do not map onto that model reliably.
 *
 * Text repair is **selection-driven**, not sentence-driven. The backend locates
 * the `original` string verbatim in the token stream, and punctuation lives in
 * `punc_after` rather than in the token text — so a whole-sentence string with
 * punctuation can never be located. Requiring an explicit character selection
 * keeps every request locatable by construction.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Sentence, Token } from "./api";
import { charsPerSecond, formatTime } from "./format";

export interface Selection {
  from: number;
  to: number;
}

interface Props {
  sentences: Sentence[];
  tokens: Token[];
  positionMs: number;
  selection: Selection | null;
  onSelectionChange: (selection: Selection | null) => void;
  onPlaySentence: (sentence: Sentence) => void;
  onSeek: (ms: number) => void;
  /** Replace the current selection with `proposed` (the range comes from `selection`). */
  onEditSelection: (proposed: string) => void;
}

const CPS_WARN_ZH = 9;

export function SentenceList({
  sentences,
  tokens,
  positionMs,
  selection,
  onSelectionChange,
  onPlaySentence,
  onSeek,
  onEditSelection,
}: Props): React.JSX.Element {
  const dragAnchor = useRef<number | null>(null);
  const [editing, setEditing] = useState<{ original: string; value: string } | null>(null);

  const activeOrd = findActiveSentence(sentences, positionMs);

  const low = selection ? Math.min(selection.from, selection.to) : -1;
  const high = selection ? Math.max(selection.from, selection.to) : -1;

  const isSelected = useCallback(
    (idx: number): boolean => selection !== null && idx >= low && idx < high,
    [selection, low, high],
  );

  const selectedText = selection
    ? tokens
        .slice(low, high)
        .map((token) => token.text)
        .join("")
    : "";

  const handleTokenDown = (idx: number, shift: boolean): void => {
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

        return (
          <div
            key={sentence.id}
            role="listitem"
            className={`sentence${isActive ? " active" : ""}`}
            data-ord={sentence.ord}
          >
            <div className="sentence-head">
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
                  if (value.trim() && value !== original) {
                    onEditSelection(value);
                  }
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

/** The sentence fully containing the selection, or null when it crosses a boundary. */
export function sentenceForSelection(
  sentences: Sentence[],
  selection: Selection | null,
): Sentence | null {
  if (!selection) return null;
  const from = Math.min(selection.from, selection.to);
  const to = Math.max(selection.from, selection.to);
  for (const sentence of sentences) {
    if (from >= sentence.token_start && to <= sentence.token_end) return sentence;
  }
  return null;
}
