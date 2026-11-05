/**
 * Transcript pane: word-level editing, direct manipulation.
 *
 * Design rules this file encodes (AGENT.md §6.3, §7.3):
 *
 * * **The unit is the ASR word.** Clicking a word selects that word; dragging
 *   extends by whole words. There is no character selection any more, because
 *   per-character times never existed — they were interpolated, and every
 *   unequal-length edit then moved interior boundaries that the user had been
 *   listening to.
 * * **Editing is in place, not in a form.** Selecting text raises a small
 *   bubble with 删除 / 修正 next to the selection, the way rescript does. The old
 *   "改文字" panel was a second place to look and a second state to keep in
 *   sync; both are gone.
 * * **State is visible.** A deleted word is red + struck through + pale red. A
 *   word a human rewrote is blue + pale blue. Both survive a reload because they
 *   are properties of the token, not of the session.
 * * **Sentence structure lives one click away.** 另起一句 / 并入上句 / 并入下句 sit
 *   in a rail at the left-middle of this pane, so the operation is next to the
 *   text it acts on instead of in a bar across the room.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Sentence, SpeakerInfo, Token } from "./api";
import { charsPerSecond, formatTime } from "./format";
import {
  bubbleCenter,
  describeSelection,
  isSingleWord,
  selectionRange,
  tokenRangeOfSentences,
  wordState,
  type TokenRange,
} from "./transcript";

export type TokenSelection = TokenRange;

interface Props {
  sentences: Sentence[];
  tokens: Token[];
  positionMs: number;
  selection: TokenRange | null;
  sentenceSelection: Set<number>;
  speakers: SpeakerInfo[];
  onSelectionChange: (selection: TokenRange | null) => void;
  onSentenceSelectionChange: (selection: Set<number>) => void;
  onPlaySentence: (sentence: Sentence) => void;
  onSeek: (ms: number) => void;
  /** Cut the selected words out of the audio. */
  onDeleteWords: () => void;
  /** Rewrite one word's text. Only ever called for a single-word selection. */
  onCorrectWord: (proposed: string) => void;
  onBoundary: (action: "new_sentence" | "to_prev" | "to_next" | "merge") => void;
  onAssignSpeaker: (localId: number | null, sentenceOrds: number[]) => void;
  onCreateSpeaker: (sentenceOrds: number[]) => void;
  onRenameSpeaker: (localId: number, name: string) => void;
  /** Time ranges already deleted, so those words render struck through. */
  deletedRanges: { start_ms: number; end_ms: number }[];
  busy: boolean;
}

const CPS_WARN_ZH = 9;

const BUBBLE_WIDTH = 168;
const BUBBLE_HEIGHT = 40;

export function SentenceList({
  sentences,
  tokens,
  positionMs,
  selection,
  sentenceSelection,
  speakers,
  onSelectionChange,
  onSentenceSelectionChange,
  onPlaySentence,
  onSeek,
  onDeleteWords,
  onCorrectWord,
  onBoundary,
  onAssignSpeaker,
  onCreateSpeaker,
  onRenameSpeaker,
  deletedRanges,
  busy,
}: Props): React.JSX.Element {
  const dragAnchor = useRef<number | null>(null);
  const sentenceAnchor = useRef<number | null>(null);

  const [anchor, setAnchor] = useState<{ left: number; top: number; bottom: number } | null>(null);
  const [correcting, setCorrecting] = useState<string | null>(null);
  const [menuOrd, setMenuOrd] = useState<number | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);

  const activeOrd = findActiveSentence(sentences, positionMs);
  const range = selectionRange(selection);
  const single = isSingleWord(range);

  const isSelected = useCallback(
    (idx: number): boolean => range !== null && idx >= range.from && idx < range.to,
    [range],
  );

  const selectedWord = useMemo(
    () => (single && range ? tokens[range.from] ?? null : null),
    [single, range, tokens],
  );

  const rangeOfSentence = useCallback(
    (sentence: Sentence): TokenRange => ({ from: sentence.token_start, to: sentence.token_end }),
    [],
  );

  const speakerOf = useCallback(
    (sentence: Sentence): SpeakerInfo | null =>
      sentence.speaker_id === null
        ? null
        : speakers.find((speaker) => speaker.local_id === sentence.speaker_id) ?? null,
    [speakers],
  );

  /** Anchor the bubble on the word the pointer just touched. */
  const pointAt = (target: HTMLElement): void => {
    const rect = target.getBoundingClientRect();
    setAnchor({ left: rect.left + rect.width / 2, top: rect.top, bottom: rect.bottom });
  };

  const selectWords = (from: number, to: number, target: HTMLElement): void => {
    const low = Math.min(from, to);
    const high = Math.max(from, to);
    onSelectionChange({ from: low, to: high });
    pointAt(target);
  };

  const handleWordDown = (idx: number, event: React.MouseEvent<HTMLElement>): void => {
    // Word work and sentence work are exclusive: the rail has one unambiguous
    // target or it has none.
    if (sentenceSelection.size > 0) onSentenceSelectionChange(new Set());
    setMenuOrd(null);
    setCorrecting(null);

    if (event.shiftKey && range) {
      selectWords(range.from, idx + 1, event.currentTarget);
      return;
    }
    dragAnchor.current = idx;
    selectWords(idx, idx + 1, event.currentTarget);
  };

  const handleWordEnter = (idx: number, event: React.MouseEvent<HTMLElement>): void => {
    if (dragAnchor.current === null) return;
    selectWords(dragAnchor.current, idx + 1, event.currentTarget);
  };

  /** Sentence checkbox: click = one, Shift = range, Ctrl/Cmd = toggle. */
  const handleSentenceClick = (ord: number, event: React.MouseEvent<HTMLButtonElement>): void => {
    onSelectionChange(null);
    setCorrecting(null);

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
    // Selecting the sentence is structural, not textual, but it must still be
    // obvious *which* text belongs to the sentence — hence a token selection
    // covering it in addition to the checkbox state.
    onSentenceSelectionChange(new Set([ord]));
  };

  useEffect(() => {
    const clear = (): void => {
      dragAnchor.current = null;
    };
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      setCorrecting(null);
      setMenuOrd(null);
      setRenaming(null);
    };
    window.addEventListener("mouseup", clear);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mouseup", clear);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  // A new target invalidates any open editor; keeping it open would apply the
  // typed text to a different word than the one it was opened on.
  useEffect(() => {
    setCorrecting(null);
  }, [range?.from, range?.to]);

  /**
   * Bubble position.
   *
   * Below the word by default: the row above the words is the sentence header
   * (time, speaker, tags), and a bubble that covers it hides the very context
   * the user is deciding in. Only when there is no room below does it flip up.
   */
  const bubbleStyle = useMemo((): React.CSSProperties | null => {
    if (!anchor || !range) return null;
    const viewportWidth = typeof window === "undefined" ? 0 : window.innerWidth;
    const viewportHeight = typeof window === "undefined" ? 0 : window.innerHeight;
    const width = viewportWidth ? Math.min(BUBBLE_WIDTH, viewportWidth - 16) : BUBBLE_WIDTH;
    const center = viewportWidth ? bubbleCenter(anchor.left, width, viewportWidth) : anchor.left;
    const below = anchor.bottom + 6;
    const flipUp = viewportHeight > 0 && below + BUBBLE_HEIGHT > viewportHeight - 8;
    return {
      position: "fixed",
      left: center,
      top: flipUp ? Math.max(8, anchor.top - BUBBLE_HEIGHT - 6) : below,
      width,
      transform: "translateX(-50%)",
    };
  }, [anchor, range]);

  const rangeForRail = useMemo(
    () =>
      sentenceSelection.size > 0
        ? tokenRangeOfSentences(sentences, sentenceSelection)
        : range,
    [range, sentenceSelection, sentences],
  );

  /**
   * Which sentences a speaker action applies to.
   *
   * Precedence is deliberate: an explicit multi-sentence pick wins, then the
   * sentence the menu was opened on. Using the *current word selection* here
   * would silently annotate a different sentence than the one the user clicked
   * "…" on.
   */
  const menuTargets = useCallback(
    (ord: number): number[] =>
      sentenceSelection.size > 0 ? [...sentenceSelection].sort((a, b) => a - b) : [ord],
    [sentenceSelection],
  );

  const canSplit = rangeForRail !== null && sentenceSelection.size === 0;
  const canJoin = rangeForRail !== null;
  const canMerge = sentenceSelection.size >= 1 || rangeForRail !== null;

  return (
    <div className="transcript-pane">
      <div className="sentence-rail" role="toolbar" aria-label="句子边界">
        <span className="rail-title">句边界</span>
        <button
          type="button"
          className="ghost"
          disabled={!canSplit || busy}
          onClick={() => onBoundary("new_sentence")}
          title="把选中的词变成独立的一句"
        >
          另起一句
        </button>
        <button
          type="button"
          className="ghost"
          disabled={!canJoin || busy}
          onClick={() => onBoundary("to_prev")}
          title="把选区并入上一句"
        >
          并入上句
        </button>
        <button
          type="button"
          className="ghost"
          disabled={!canJoin || busy}
          onClick={() => onBoundary("to_next")}
          title="把选区并入下一句"
        >
          并入下句
        </button>
        <button
          type="button"
          className="ghost"
          disabled={!canMerge || busy}
          onClick={() => onBoundary("merge")}
          title="把选区涉及的所有句子合并成一句"
        >
          合并句子
        </button>
        <span className="rail-hint">{describeSelection(tokens, range)}</span>
      </div>

      <div className="sentence-list" role="list">
        {sentences.map((sentence) => {
          const slice = tokens.slice(sentence.token_start, sentence.token_end);
          const cps = charsPerSecond(sentence.text, sentence.start_ms, sentence.end_ms);
          const tooFast = cps > CPS_WARN_ZH;
          const isActive = sentence.ord === activeOrd;
          const picked = sentenceSelection.has(sentence.ord);
          const speaker = speakerOf(sentence);
          const wholeDeleted = wordState(
            { idx: 0, start_ms: sentence.start_ms, end_ms: sentence.end_ms },
            deletedRanges,
          ) === "deleted";
          const touched = slice.some((token) => token.revised);

          return (
            <div
              key={sentence.id}
              role="listitem"
              className={[
                "sentence",
                isActive ? "active" : "",
                picked ? "picked" : "",
                wholeDeleted ? "removed" : "",
                speaker ? "has-speaker" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              data-ord={sentence.ord}
              style={speaker ? { borderLeftColor: speaker.color } : undefined}
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
                  onClick={() => {
                    onSelectionChange(rangeOfSentence(sentence));
                    onSentenceSelectionChange(new Set());
                    onSeek(sentence.start_ms);
                  }}
                  title="选中这一句并跳到这里"
                >
                  {formatTime(sentence.start_ms)}
                </button>

                <span className="speaker-slot">
                  <button
                    type="button"
                    className="speaker-name"
                    style={speaker ? { color: speaker.color } : undefined}
                    onClick={() => {
                      setRenaming(null);
                      setMenuOrd(menuOrd === sentence.ord ? null : sentence.ord);
                    }}
                    title="改名、指派或新建说话人"
                  >
                    {speaker ? speaker.name : "未标注"}
                    <span className="speaker-dots">…</span>
                  </button>
                  {menuOrd === sentence.ord ? (
                    <SpeakerMenu
                      speakers={speakers}
                      current={sentence.speaker_id}
                      renaming={renaming}
                      onRenameStart={() => setRenaming(`s${sentence.ord}`)}
                      onRenameCancel={() => setRenaming(null)}
                      onRenameCommit={(name) => {
                        const target = speaker;
                        setRenaming(null);
                        setMenuOrd(null);
                        if (target && name.trim()) onRenameSpeaker(target.local_id, name.trim());
                      }}
                      onAssign={(localId) => {
                        setMenuOrd(null);
                        onAssignSpeaker(localId, menuTargets(sentence.ord));
                      }}
                      onCreate={() => {
                        setMenuOrd(null);
                        onCreateSpeaker(menuTargets(sentence.ord));
                      }}
                    />
                  ) : null}
                </span>

                <span className="sentence-meta">
                  {wholeDeleted ? <span className="removed-tag">已删除</span> : null}
                  {touched ? <span className="revised-tag">已修改</span> : null}
                  {tooFast ? (
                    <span className="warn" title={`每秒 ${cps.toFixed(1)} 字，超出阅读速度`}>
                      语速过快
                    </span>
                  ) : null}
                </span>
                <button type="button" className="ghost sentence-play" onClick={() => onPlaySentence(sentence)}>
                  ▶
                </button>
              </div>

              <div className="tokens">
                {slice.map((token) => {
                  const state = wordState(token, deletedRanges);
                  return (
                    <span
                      key={token.idx}
                      data-token={token.idx}
                      className={[
                        "token",
                        isSelected(token.idx) ? "selected" : "",
                        token.derived ? "derived" : "",
                        token.revised ? "revised" : "",
                        state === "deleted" ? "cut" : "",
                        state === "partial" ? "partly-cut" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      role="button"
                      tabIndex={0}
                      title={`${formatTime(token.start_ms)} – ${formatTime(token.end_ms)}${
                        token.derived ? "（时间为插值估算）" : ""
                      }`}
                      onMouseDown={(event) => {
                        event.preventDefault();
                        handleWordDown(token.idx, event);
                      }}
                      onMouseEnter={(event) => handleWordEnter(token.idx, event)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          handleWordDown(token.idx, event as unknown as React.MouseEvent<HTMLElement>);
                        }
                      }}
                    >
                      {token.text}
                      {token.punc_after ? <span className="punct">{token.punc_after}</span> : null}
                    </span>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      {bubbleStyle && range ? (
        <div className="word-bubble" style={bubbleStyle} role="toolbar" aria-label="选中文字的操作">
          {correcting === null ? (
            <>
              <button
                type="button"
                className="bubble-cut"
                disabled={busy}
                onClick={() => {
                  setCorrecting(null);
                  onDeleteWords();
                }}
                title="把选中的词从音频里剪掉"
              >
                ✂ 删除
              </button>
              <button
                type="button"
                className="bubble-fix"
                disabled={busy || !single || selectedWord === null}
                onClick={() => setCorrecting(selectedWord ? selectedWord.text : "")}
                title={
                  single
                    ? "改写这个词（时间不变）"
                    : "一次只能修正一个词：改多个词会移动它们之间的时间点"
                }
              >
                ✎ 修正
              </button>
            </>
          ) : (
            <CorrectForm
              original={correcting}
              onSubmit={(value) => {
                setCorrecting(null);
                const trimmed = value.trim();
                if (trimmed && trimmed !== correcting) onCorrectWord(trimmed);
              }}
              onCancel={() => setCorrecting(null)}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}

interface SpeakerMenuProps {
  speakers: SpeakerInfo[];
  current: number | null;
  renaming: string | null;
  onRenameStart: () => void;
  onRenameCancel: () => void;
  onRenameCommit: (name: string) => void;
  onAssign: (localId: number | null) => void;
  onCreate: () => void;
}

function SpeakerMenu({
  speakers,
  current,
  renaming,
  onRenameStart,
  onRenameCancel,
  onRenameCommit,
  onAssign,
  onCreate,
}: SpeakerMenuProps): React.JSX.Element {
  const [draft, setDraft] = useState("");
  const active = speakers.find((speaker) => speaker.local_id === current) ?? null;

  useLayoutEffect(() => {
    setDraft(active ? active.name : "");
  }, [active, renaming]);

  if (renaming !== null) {
    return (
      <div className="speaker-menu">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onRenameCommit(draft);
          }}
        >
          <input
            autoFocus
            value={draft}
            maxLength={24}
            aria-label="说话人名字"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") onRenameCancel();
            }}
          />
          <div className="speaker-menu-row">
            <button type="submit" disabled={!draft.trim()}>
              保存
            </button>
            <button type="button" className="ghost" onClick={onRenameCancel}>
              取消
            </button>
          </div>
        </form>
      </div>
    );
  }

  return (
    <div className="speaker-menu" role="menu">
      <span className="speaker-menu-title">指派说话人</span>
      {speakers.length === 0 ? (
        <p className="speaker-menu-empty">
          这个模型的转写结果里没有分人。可以先手动建一个，再把句子指过去。
        </p>
      ) : null}
      {speakers.map((speaker) => (
        <button
          key={speaker.local_id}
          type="button"
          role="menuitem"
          className={`speaker-item${speaker.local_id === current ? " current" : ""}`}
          onClick={() => onAssign(speaker.local_id)}
        >
          <span className="speaker-dot" style={{ background: speaker.color }} />
          {speaker.name}
          <span className="speaker-count">{speaker.sentence_count} 句</span>
        </button>
      ))}
      <div className="speaker-menu-divider" />
      {active ? (
        <button type="button" role="menuitem" onClick={onRenameStart}>
          ✎ 重命名「{active.name}」
        </button>
      ) : null}
      <button type="button" role="menuitem" onClick={onCreate}>
        ＋ 新建说话人
      </button>
      {current !== null ? (
        <button type="button" role="menuitem" onClick={() => onAssign(null)}>
          清除标注
        </button>
      ) : null}
    </div>
  );
}

interface CorrectFormProps {
  original: string;
  onSubmit: (value: string) => void;
  onCancel: () => void;
}

function CorrectForm({ original, onSubmit, onCancel }: CorrectFormProps): React.JSX.Element {
  const [value, setValue] = useState(original);
  return (
    <form
      className="bubble-form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit(value);
      }}
    >
      <input
        autoFocus
        value={value}
        aria-label={`把「${original}」改成`}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
        }}
      />
      <button type="submit" disabled={!value.trim()}>
        改
      </button>
    </form>
  );
}

export function findActiveSentence(sentences: Sentence[], positionMs: number): number {
  for (const sentence of sentences) {
    if (positionMs >= sentence.start_ms && positionMs < sentence.end_ms) return sentence.ord;
  }
  return -1;
}

export { tokenRangeOfSentences };
