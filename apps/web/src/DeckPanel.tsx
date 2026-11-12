/**
 * Deck panel: generate topic pages, proofread them, drop them into the box (P4, §19.16).
 *
 * **A page is a topic, not a stretch of transcript.** The panel asks the server to plan pages
 * by where the conversation turns, and each page carries 2–4 sub-topics rather than the
 * words that were spoken. The earlier version cut the transcript at pauses and put the
 * verbatim text on the slide, which produced a 60-page teleprompter for a 61-minute episode.
 *
 * **The transcript was the wrong unit and the pause was the wrong boundary.** Sentence gaps
 * in real speech are 320 ms at the median, so "break at a pause" degenerates into "break at
 * the length cap" — a page every minute, mid-sentence, whether or not anything changed. The
 * model is asked only where a topic starts (by line number); the timestamps are read back off
 * the transcript, because a model writing 00:12:34 produces a plausible wrong number.
 *
 * **Proofreading is side by side, not a form.** Left is the transcript stretch the page came
 * from, right is the editable title and points. Without the left pane the user has to remember
 * what was said three minutes ago to know whether the page is right.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Deck, type DeckSlide, type Sentence } from "./api";
import { formatTime } from "./format";

interface Props {
  projectId: string;
  versionId: string;
  /** Sentences of the current version, for the left-hand source pane. */
  sentences: Sentence[];
  /** Where the playhead is, so the panel can open on the page being heard. */
  positionMs: number;
  onNotice: (message: string) => void;
  /** Seek the audio playhead to a source sentence. */
  onSeek: (ms: number) => void;
  /**
   * Create a slide block on the timeline covering this page.
   *
   * The deck id travels with the call because the block stores *which deck* it plays, not
   * which page: a block spans a stretch, and the pages inside it are whatever the deck says
   * at render time. That is what keeps the block correct after pages are reordered.
   */
  onInsertBlock: (deckId: string, startMs: number, endMs: number) => void;
}

const LAYOUTS: { id: string; label: string }[] = [
  { id: "title_body", label: "标题 + 正文" },
  { id: "title", label: "只有标题（章节页）" },
  { id: "quote", label: "引文" },
];

export function DeckPanel({
  projectId,
  versionId,
  sentences,
  positionMs,
  onNotice,
  onSeek,
  onInsertBlock,
}: Props): React.JSX.Element {
  const [deck, setDeck] = useState<Deck | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // ``plan`` asks the model where the topics turn; ``transcript`` is the free fallback and is
  // also what the server silently produces when no key is configured. Kept as state because
  // 「重新生成」 has to repeat whatever the user last chose, including "cheap and instant".
  const [mode, setMode] = useState<"plan" | "transcript">("plan");
  const [draftTitle, setDraftTitle] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [dirty, setDirty] = useState(false);
  // Bumped after every edit so the <img> reloads instead of showing the cached old page.
  const [revision, setRevision] = useState(0);
  // Chinese-capable families, for the two typeface pickers. Empty until the first fetch, which
  // is also when the selects disable themselves rather than offering an empty list.
  const [fontFamilies, setFontFamilies] = useState<string[]>([]);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const next = await api.deck(projectId);
      // A stale response must not overwrite a newer one: generating while the panel is open
      // fires both this and the generate call, and they can land out of order.
      if (seq !== loadSeq.current) return;
      setDeck(next);
      setSelectedId((current) => {
        if (current && next?.slides.some((slide) => slide.id === current)) return current;
        return next?.slides[0]?.id ?? null;
      });
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  }, [onNotice, projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // Fetched once, and again only if the panel is remounted: the set of families on the
    // machine does not change while the user is editing a deck.
    void api
      .deckFonts(projectId)
      .then((list) => setFontFamilies(list.families))
      .catch(() => setFontFamilies([]));
  }, [projectId]);

  const slides = deck?.slides ?? [];
  const selected = useMemo(
    () => slides.find((slide) => slide.id === selectedId) ?? null,
    [slides, selectedId],
  );

  // The draft follows the selection; it does not follow every keystroke back into the deck.
  useEffect(() => {
    setDraftTitle(selected?.title ?? "");
    setDraftBody(selected?.body ?? "");
    setDirty(false);
  }, [selected?.id, selected?.title, selected?.body]);

  /** The page the playhead is inside, so opening the panel lands where the user is. */
  const activeId = useMemo(() => {
    let found: string | null = null;
    for (const slide of slides) {
      if (slide.start_ms <= positionMs) found = slide.id;
      else break;
    }
    return found;
  }, [positionMs, slides]);

  const doGenerate = async (nextMode: "plan" | "transcript" = mode): Promise<void> => {
    setMode(nextMode);
    setBusy(true);
    try {
      const next = await api.generateDeck(versionId, { replace: true, mode: nextMode });
      setDeck(next);
      setSelectedId(next.slides[0]?.id ?? null);
      setRevision((value) => value + 1);
      // Say which path produced the pages. A fallback deck looks exactly like a planned one
      // and is a different product: silently returning it is how 「生成的还是一字不差的字幕」
      // comes back after a round that was supposed to fix it.
      const how = next.source === "model" ? "按话题分段" : "按字幕停顿分页（未用模型）";
      onNotice(
        next.warnings.length
          ? `${how}，${next.slides.length} 页。${next.warnings[0]}`
          : `${how}，${next.slides.length} 页`,
      );
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doSave = async (): Promise<void> => {
    if (!selected) return;
    setBusy(true);
    try {
      const updated = await api.updateSlide(selected.id, {
        title: draftTitle,
        body: draftBody,
      });
      setDeck((current) =>
        current === null
          ? current
          : {
              ...current,
              slides: current.slides.map((slide) =>
                slide.id === updated.id ? { ...slide, ...updated } : slide,
              ),
            },
      );
      setDirty(false);
      // The server invalidates the PNG; the URL is identical, so the browser must be told
      // to fetch it again. Without this the preview keeps showing the old page forever.
      setRevision((value) => value + 1);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doLayout = async (layout: string): Promise<void> => {
    if (!selected) return;
    try {
      await api.updateSlide(selected.id, { layout });
      setDeck((current) =>
        current === null
          ? current
          : {
              ...current,
              slides: current.slides.map((slide) =>
                slide.id === selected.id ? { ...slide, layout } : slide,
              ),
            },
      );
      setRevision((value) => value + 1);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const doMove = async (delta: number): Promise<void> => {
    if (!selected) return;
    try {
      const ordered = await api.moveSlide(selected.id, selected.ord + delta);
      if (Array.isArray(ordered) && ordered.length) {
        setDeck((current) => (current === null ? current : { ...current, slides: ordered }));
      }
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * Set one of the deck's two typefaces.
   *
   * The whole deck at once, not the selected page: type is what makes a deck read as one
   * document, and a page-by-page font would be visible immediately as a mistake. The server
   * drops every cached PNG, so the preview is told to re-fetch — the URL is unchanged and the
   * browser would otherwise keep showing the old face.
   */
  const doFont = async (role: "title" | "body", family: string): Promise<void> => {
    setBusy(true);
    try {
      const next = await api.setDeckFonts(projectId, {
        [role === "title" ? "title_font" : "body_font"]: family,
      });
      setDeck((current) => (current === null ? current : { ...next }));
      setRevision((value) => value + 1);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async (): Promise<void> => {
    if (!selected) return;
    try {
      await api.deleteSlide(selected.id);
      await load();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * The end of a page's stretch is the start of the next one.
   *
   * Deliberately the same rule the renderer uses (`decks.spans_of`): a page runs until the
   * next page starts, so inserting a block does not need to know every page's end.
   */
  const spanOf = (slide: DeckSlide): { start: number; end: number } => {
    const index = slides.findIndex((item) => item.id === slide.id);
    const next = index >= 0 ? slides[index + 1] : undefined;
    return {
      start: slide.start_ms,
      end: next?.start_ms ?? Math.max(slide.start_ms + 5000, positionMs + 5000),
    };
  };

  const sourceText = useMemo(() => {
    if (!selected) return "";
    const start = slides.findIndex((slide) => slide.id === selected.id);
    const next = slides[start + 1];
    const from = selected.source_ord;
    const to = next ? next.source_ord : Number.POSITIVE_INFINITY;
    return sentences
      .filter((sentence) => sentence.ord >= from && sentence.ord < to)
      .map((sentence) => sentence.text)
      .join("");
  }, [selected, sentences, slides]);

  if (deck === null) {
    return (
      <div className="deck-panel">
        <p className="muted">
          还没有演示稿。按话题转折分段，每页写这一段讲了哪几个小话题——和 PPT 一样，
          不是逐字稿。要调用一次模型，一小时的节目大约一分钟。
        </p>
        <div className="deck-row">
          <button type="button" disabled={busy} onClick={() => void doGenerate("plan")}>
            {busy ? "生成中…" : "按话题生成演示稿"}
          </button>
          <button
            type="button"
            className="ghost small"
            disabled={busy}
            title="不调用模型：按字幕的停顿和时长切分，正文就是字幕原文。免费、秒出，但翻页不看话题。"
            onClick={() => void doGenerate("transcript")}
          >
            按字幕分页（免费）
          </button>
        </div>
      </div>
    );
  }

  /**
   * The options for one picker: the current family first, then everything else.
   *
   * The current family is put first even if it is not in the list, because it can be absent
   * for a real reason — the deck was set on another machine, or in 字由 a font that has since
   * been deleted. A select whose value matches no option renders as blank, which looks like
   * "no font" rather than "that font is gone".
   */
  const fontOptions = (current: string): string[] => {
    const head = current ? [current] : [];
    return [...head, ...fontFamilies.filter((name) => name !== current)];
  };

  return (
    <div className="deck-panel">
      <div className="deck-toolbar">
        <span>
          {slides.length} 页
          <span className="deck-source-tag">
            {deck.source === "model" ? "按话题分段" : "按字幕停顿分页"}
          </span>
          {activeId && activeId !== selectedId ? (
            <button
              type="button"
              className="ghost small"
              onClick={() => setSelectedId(activeId)}
            >
              跳到播放位置这一页
            </button>
          ) : null}
        </span>
        <button type="button" className="ghost small" disabled={busy} onClick={() => void load()}>
          刷新
        </button>
        <button type="button" className="ghost small" disabled={busy} onClick={() => void doGenerate(mode)}>
          {busy ? "生成中…" : "重新生成"}
        </button>
      </div>

      {/*
        Two pickers, one row: the title and the body are set separately because they are
        different kinds of text, and a deck where the title shrinks to the body's size to
        avoid a second wrap will not look like a title. Deck-wide, like a master slide.
      */}
      <div className="deck-fonts">
        <label className="field">
          <span>标题字体</span>
          <select
            value={deck.title_font}
            disabled={busy || fontFamilies.length === 0}
            aria-label="标题字体"
            onChange={(event) => void doFont("title", event.target.value)}
          >
            {fontOptions(deck.title_font).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>正文字体</span>
          <select
            value={deck.body_font}
            disabled={busy || fontFamilies.length === 0}
            aria-label="正文字体"
            onChange={(event) => void doFont("body", event.target.value)}
          >
            {fontOptions(deck.body_font).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <span className="deck-font-hint">
          只列出能画中文的字体。整份演示稿一起换，不用一页页改。
        </span>
      </div>

      {deck.warnings.length ? (
        <ul className="deck-warnings">
          {deck.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}

      <div className="deck-body">
        <ol className="deck-list">
          {slides.map((slide) => (
            <li key={slide.id}>
              <button
                type="button"
                className={slide.id === selectedId ? "active" : ""}
                onClick={() => setSelectedId(slide.id)}
              >
                <span className="deck-list-ord">{slide.ord + 1}</span>
                <span className="deck-list-title">{slide.title || "（无标题）"}</span>
                <span className="deck-list-time">{formatTime(slide.start_ms)}</span>
              </button>
            </li>
          ))}
        </ol>

        {selected ? (
          <div className="deck-editor">
            <img
              className="deck-preview"
              alt={`第 ${selected.ord + 1} 页预览`}
              src={`${api.slideImageUrl(selected.image_url, 720, 405)}&r=${revision}`}
            />

            <div className="deck-row">
              <label className="field">
                <span>版式</span>
                <select value={selected.layout} onChange={(event) => void doLayout(event.target.value)}>
                  {LAYOUTS.map((layout) => (
                    <option key={layout.id} value={layout.id}>
                      {layout.label}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="ghost small"
                disabled={selected.ord === 0}
                title="改演示稿的页序。成片里每页仍按它自己的时间点播放——时间点来自字幕，挪页不会挪字幕。"
                onClick={() => void doMove(-1)}
              >
                上移
              </button>
              <button
                type="button"
                className="ghost small"
                disabled={selected.ord === slides.length - 1}
                title="改演示稿的页序。成片里每页仍按它自己的时间点播放——时间点来自字幕，挪页不会挪字幕。"
                onClick={() => void doMove(1)}
              >
                下移
              </button>
              {/*
                These two belong with 版式/上移/下移, not with 保存: they act on the *page's
                place in the film* (where it is heard, which stretch it covers), while 保存
                belongs to the two text fields below it. They used to sit under the textarea,
                one row away from the page-order buttons they are the natural sequel to, and
                a reader looking for "put this page into the box" had to scan past 版式,
                上移, 下移 and a destructive button to find it.
              */}
              <button type="button" className="ghost small" onClick={() => onSeek(selected.start_ms)}>
                跳到这句字幕
              </button>
              <button
                type="button"
                className="ghost small"
                onClick={() => {
                  const span = spanOf(selected);
                  onInsertBlock(deck.id, span.start, span.end);
                }}
              >
                放进素材框
              </button>
              <button
                type="button"
                className="ghost small push-end"
                onClick={() => void doDelete()}
              >
                删掉这页
              </button>
            </div>

            <label className="field">
              <span>标题</span>
              <input value={draftTitle} onChange={(event) => { setDraftTitle(event.target.value); setDirty(true); }} />
            </label>
            <label className="field">
              <span>
                正文
                <em className="deck-body-hint">一行一个要点，行首的 ・ 可留可去</em>
              </span>
              <textarea
                rows={5}
                value={draftBody}
                onChange={(event) => { setDraftBody(event.target.value); setDirty(true); }}
              />
            </label>

            <div className="deck-row">
              <button type="button" disabled={busy || !dirty} onClick={() => void doSave()}>
                {busy ? "保存中…" : dirty ? "保存这一页" : "已保存"}
              </button>
            </div>

            <details className="deck-source" open>
              <summary>这一页覆盖的字幕原文</summary>
              <p>{sourceText || "（找不到对应的字幕，可能这一页的句子已经被删掉了）"}</p>
            </details>
          </div>
        ) : null}
      </div>
    </div>
  );
}
