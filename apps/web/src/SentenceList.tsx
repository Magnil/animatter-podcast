/**
 * Transcript pane: word-level editing, direct manipulation.
 *
 * Design rules this file encodes (AGENTS.md §6.3, §7.3):
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
  selectionRange,
  speakerConflict,
  tokenRangeOfSentences,
  wordState,
  type TokenRange,
} from "./transcript";
import { FOLLOW_GRACE_MS, planWindow, scrollOffsetFor } from "./virtualList";

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
  /** Rewrite the selection's text as one word. */
  onCorrectWord: (proposed: string) => void;
  /** Hide the selected words from the text without touching the audio. */
  onHideWords: (hidden: boolean) => void;
  onBoundary: (action: "new_sentence" | "to_prev" | "to_next" | "merge") => void;
  onAssignSpeaker: (localId: number | null, sentenceOrds: number[]) => void;
  onCreateSpeaker: (sentenceOrds: number[]) => void;
  onRenameSpeaker: (localId: number, name: string) => void;
  /** Time ranges already deleted, so those words render struck through. */
  deletedRanges: { start_ms: number; end_ms: number }[];
  busy: boolean;
}

const CPS_WARN_ZH = 9;

//: Bubble geometry, used to keep it on screen. Two widths because the bubble has
//: two shapes: three one-character buttons (~112 px), or one text field. Measuring
//: the DOM would be the alternative, but the bubble is positioned in the same
//: render that creates it, so there is nothing to measure yet on the first paint.
const BUBBLE_WIDTH = 112;
const BUBBLE_WIDTH_FORM = 232;
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
  onHideWords,
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

  // ---- Windowing -----------------------------------------------------------
  // Only the rows near the viewport are mounted. The full list is ~25k nodes and
  // one word click cost 650 ms to paint; slicing the list took it to 199 ms.
  const listRef = useRef<HTMLDivElement | null>(null);
  /** Measured row heights, keyed by sentence ord. Survives scrolling by design. */
  const heights = useRef<Map<number, number>>(new Map());
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);
  /** When the reader last scrolled by hand, so playhead-following can stand down. */
  const userScrolledAt = useRef(0);

  const plan = useMemo(
    () => planWindow(sentences.length, heights.current, scrollTop, viewportHeight),
    // `scrollTop`/`viewportHeight` decide the window; a re-render from any other
    // state must not walk the height map again.
    [sentences.length, scrollTop, viewportHeight],
  );

  /**
   * Keep the scroll position after the mounted rows change.
   *
   * Windowing replaces the mounted slice, so the browser recomputes where the
   * content sits. The spacer above already accounts for the skipped rows, which
   * is what keeps `scrollTop` meaningful — but React applies the new spacer and
   * the new rows in one commit, so the scroller can briefly clamp `scrollTop`
   * to the old (shorter) content. Restoring it synchronously after layout is
   * what stops the list from snapping to the top on every scroll.
   */
  useLayoutEffect(() => {
    const element = listRef.current;
    if (!element) return;
    if (Math.abs(element.scrollTop - scrollTop) > 1) element.scrollTop = scrollTop;
  }, [plan.start, plan.end, scrollTop]);

  // Measure the scroll viewport, including when the sidebar is resized.
  useEffect(() => {
    const element = listRef.current;
    if (!element) return;
    const measure = (): void => setViewportHeight(element.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  /**
   * Record a row's real height after it renders.
   *
   * Rows are 77–235 px depending on how many lines the text takes, so a fixed
   * row height would make the scrollbar jump by a whole row whenever a tall one
   * scrolled past. Called from a layout effect on each mounted row.
   */
  const measureRow = useCallback((ord: number, element: HTMLDivElement | null): void => {
    if (!element) return;
    const height = element.getBoundingClientRect().height;
    if (height <= 0) return;
    const previous = heights.current.get(ord);
    if (previous !== undefined && Math.abs(previous - height) < 1) return;
    heights.current.set(ord, height);
  }, []);

  /**
   * Scroll a row into view, mounting it first if needed.
   *
   * With every row mounted this was `scrollIntoView()`. It cannot be any more:
   * the target row may not exist. Setting `scrollTop` from the height model
   * makes the window planner mount it on the next render, so one call suffices.
   */
  const revealOrd = useCallback(
    (ord: number): void => {
      const element = listRef.current;
      if (!element) return;
      const target = scrollOffsetFor(ord, heights.current, element.clientHeight);
      if (Math.abs(element.scrollTop - target) < 2) return;
      element.scrollTop = target;
      setScrollTop(target);
    },
    [],
  );

  /**
   * Follow the playhead — but not while the reader is scrolling.
   *
   * The sentence being spoken must be on screen, or the transcript and the audio
   * drift apart. It must not, however, yank the list back every time a sentence
   * changes while someone is reading somewhere else; the windowed list can only
   * be read by scrolling, so fighting the scrollbar would make it unusable. A
   * short grace period after a manual scroll is enough to tell the two apart.
   */
  const revealedOrd = useRef<number | null>(null);
  useEffect(() => {
    if (activeOrd < 0 || revealedOrd.current === activeOrd) return;
    revealedOrd.current = activeOrd;
    if (performance.now() - userScrolledAt.current < FOLLOW_GRACE_MS) return;
    revealOrd(activeOrd);
  }, [activeOrd, revealOrd]);

  const range = selectionRange(selection);

  const isSelected = useCallback(
    (idx: number): boolean => range !== null && idx >= range.from && idx < range.to,
    [range],
  );

  /**
   * Text of the whole selection, used to pre-fill the correction box.
   *
   * A multi-word selection is corrected as **one** word: the user dragged over
   * e.g. «句级标签» and wants to write what it should have said. The token count
   * shrinks to one on the server (the replacement text becomes a single token),
   * which is exactly the "merge several words into one corrected word" the
   * feature was asked for.
   */
  const selectedText = useMemo(() => {
    if (!range) return "";
    return tokens
      .slice(range.from, range.to)
      .map((token) => token.text)
      .join("");
  }, [range, tokens]);

  /** Every word in the selection already hidden → the button un-hides. */
  const allHidden = useMemo(() => {
    if (!range) return false;
    const picked = tokens.slice(range.from, range.to);
    return picked.length > 0 && picked.every((token) => token.hidden);
  }, [range, tokens]);

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

  // Word-operation shortcuts. Del / Enter / Backspace act on the current word
  // selection, which is the same target the bubble's three buttons act on —
  // the shortcuts exist because reaching for the mouse to hit a 20 px button
  // 200 times an hour is the slowest part of proofreading.
  //
  // Not handled here: Space and brackets, which the app-level handler owns.
  // Deliberately *not* swallowed when there is no selection, either: a stray
  // Backspace with nothing selected should do nothing, not silently hide the
  // word under the playhead.
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.isComposing) return;
      // Never steal a key while the correction box (or any other field) is
      // focused: Backspace there means "delete a character".
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
      if (range === null || busy) return;
      // Let a focused button keep its native Enter/Space activation.
      if (target && target.tagName === "BUTTON" && event.key === "Enter") return;

      if (event.key === "Delete") {
        event.preventDefault();
        setCorrecting(null);
        onDeleteWords();
      } else if (event.key === "Enter") {
        // Multi-word selections are corrected as one word (see selectedText).
        event.preventDefault();
        setCorrecting(selectedText);
      } else if (event.key === "Backspace") {
        // Backspace, not Delete, hides: the two keys sit next to each other and
        // "remove the text but keep the sound" is the softer of the two, which
        // is the softer of the two keys.
        event.preventDefault();
        setCorrecting(null);
        onHideWords(!allHidden);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [allHidden, busy, onDeleteWords, onHideWords, range, selectedText]);

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
    const natural = correcting === null ? BUBBLE_WIDTH : BUBBLE_WIDTH_FORM;
    const width = viewportWidth ? Math.min(natural, viewportWidth - 16) : natural;
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
  }, [anchor, correcting, range]);

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

  // Boundary actions that would put words under the wrong speaker are disabled.
  // The server refuses them anyway; offering a button that always fails is just
  // a slower way of saying no. Names which direction is blocked, because "can't
  // join" is not actionable when only one of the two directions is refused.
  const joinUpConflict = speakerConflict(sentences, rangeForRail, "to_prev");
  const joinDownConflict = speakerConflict(sentences, rangeForRail, "to_next");
  const mergeConflict = speakerConflict(sentences, rangeForRail, "merge");
  const railConflict =
    mergeConflict ??
    (joinUpConflict !== null && joinDownConflict !== null
      ? "上下相邻的两句都属于别的说话人，先取消其中一句的标注"
      : joinUpConflict !== null
        ? "并入上句会跨说话人，先取消其中一句的标注"
        : joinDownConflict !== null
          ? "并入下句会跨说话人，先取消其中一句的标注"
          : null);

  return (
    <div className="transcript-pane">
      <div className="sentence-rail" role="toolbar" aria-label="句子边界">
        <span className="rail-title">句边界</span>        <button
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
          disabled={!canJoin || busy || joinUpConflict !== null}
          onClick={() => onBoundary("to_prev")}
          title={joinUpConflict ?? "把选区并入上一句"}
        >
          并入上句
        </button>
        <button
          type="button"
          className="ghost"
          disabled={!canJoin || busy || joinDownConflict !== null}
          onClick={() => onBoundary("to_next")}
          title={joinDownConflict ?? "把选区并入下一句"}
        >
          并入下句
        </button>
        <button
          type="button"
          className="ghost"
          disabled={!canMerge || busy || mergeConflict !== null}
          onClick={() => onBoundary("merge")}
          title={mergeConflict ?? "把选区涉及的所有句子合并成一句"}
        >
          合并句子
        </button>
        {/* Always rendered, even when there is nothing to say.
 
            Two heights are what made this rail feel jumpy: the word-count
            readout (gone) and this line. It only appears when a boundary action
            would cross speakers — which is most clicks — so the rail flipped
            between one and two rows constantly. Reserving the row keeps the
            panel still; the text still appears and disappears, the geometry no
            longer does. */}
        <span className="rail-conflict" role="status">
          {railConflict ?? ""}
        </span>
      </div>

      <div
        className="sentence-list"
        role="list"
        ref={listRef}
        onScroll={(event) => {
          userScrolledAt.current = performance.now();
          setScrollTop(event.currentTarget.scrollTop);
        }}
      >
        {/* Height of the rows above the window. Without it the scroller would
            only be as tall as the mounted slice, and the scrollbar would sit at
            the bottom on the first screenful. */}
        {plan.paddingTop > 0 ? <div style={{ height: plan.paddingTop }} aria-hidden="true" /> : null}
        {sentences.slice(plan.start, plan.end).map((sentence) => {
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
          const hiddenCount = slice.filter((token) => token.hidden).length;

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
              // The height model needs real heights: rows run 77–235 px with
              // seven common values, so a constant would make the scrollbar
              // jump by a line whenever a tall sentence scrolled past.
              ref={(element) => measureRow(sentence.ord, element)}
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
                  {hiddenCount > 0 ? (
                    <span
                      className="hidden-tag"
                      title={`${hiddenCount} 个词只在字幕里隐去，音频还在`}
                    >
                      已隐去 {hiddenCount}
                    </span>
                  ) : null}
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
                        token.hidden ? "hidden-word" : "",
                        state === "deleted" ? "cut" : "",
                        state === "partial" ? "partly-cut" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      role="button"
                      tabIndex={0}
                      title={`${formatTime(token.start_ms)} – ${formatTime(token.end_ms)}${
                        token.derived ? "（时间为插值估算）" : ""
                      }${token.hidden ? "（字幕里已隐去，音频仍在）" : ""}`}
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
        {plan.paddingBottom > 0 ? (
          <div style={{ height: plan.paddingBottom }} aria-hidden="true" />
        ) : null}
      </div>

      {bubbleStyle && range ? (
        <div className="word-bubble" style={bubbleStyle} role="toolbar" aria-label="选中文字的操作">
          {correcting === null ? (
            <>
              <button
                type="button"
                className="bubble-cut"
                disabled={busy}
                aria-label="删除"
                onClick={() => {
                  setCorrecting(null);
                  onDeleteWords();
                }}
                title="把选中的词从音频中「删除」（DEL）"
              >
                删
              </button>
              <button
                type="button"
                className="bubble-fix"
                disabled={busy}
                aria-label="修正"
                onClick={() => setCorrecting(selectedText)}
                title={
                  range.to - range.from === 1
                    ? "「修正」这个词而时间不变（Enter）"
                    : `把选中的 ${range.to - range.from} 个词「修正」成一个词而时间范围不变（Enter）`
                }
              >
                修
              </button>
              <button
                type="button"
                className="bubble-hide"
                disabled={busy}
                aria-label={allHidden ? "取消隐去" : "隐去"}
                onClick={() => {
                  setCorrecting(null);
                  onHideWords(!allHidden);
                }}
                title={
                  allHidden
                    ? "把这些词放回字幕，音频本来就没动（Backspace）"
                    : "只在字幕中「隐去」这些词而音频不变（Backspace）"
                }
              >
                {/* 显 rather than 隐 when every selected word is already hidden:
                    the button would restore, and a control that lies about what
                    it does is how a toggle gets clicked twice. Counterpart of 隐,
                    same one-character rule. */}
                {allHidden ? "显" : "隐"}
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
