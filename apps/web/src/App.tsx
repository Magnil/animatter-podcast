/**
 * Correction desk shell.
 *
 * Layout follows AGENTS.md §7.2: waveform on top, sentence list below, action bar
 * for the current selection. Keyboard shortcuts are the primary workflow —
 * mouse-only proof-reading of a two-hour episode is unbearable.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AppSettings,
  type HotwordItem,
  type ScriptFormat,
  type Sentence,
  type SpeakerInfo,
  type Token,
  type Version,
} from "./api";
import { loadAllSentences } from "./transcriptLoad";
import { hiddenSpans as hiddenWordSpans, tokenAt } from "./transcript";
import { videoExportEntry } from "./videoExport";
import { AudioExportDialog } from "./AudioExportDialog";
import { VideoExportDialog } from "./VideoExportDialog";
import { HistoryPanel } from "./HistoryPanel";
import { NewProjectDialog } from "./NewProjectDialog";
import { SubtitlePanel } from "./SubtitlePanel";
import { ScriptPanel } from "./ScriptPanel";
import { CleanupDialog, type BatchAction } from "./CleanupDialog";
import { HotwordPanel } from "./HotwordPanel";
import { CollapsiblePanel } from "./CollapsiblePanel";
import { useTimelineView } from "./timelineView";
import { MenuBar, type Menu } from "./MenuBar";
import { SentenceList, findActiveSentence, tokenRangeOfSentences, type TokenSelection } from "./SentenceList";
import { SettingsDialog, type SettingsSection } from "./SettingsDialog";
import { Waveform, type EdgeHandle, type Marker } from "./Waveform";
import { VideoPanel } from "./VideoPanel";
import {
  audibleStart,
  audibleStop,
  insideDeletion as isInsideDeletion,
  nextAudible,
  nextBoundary,
  nextSentence,
  previousSentence,
  sentenceAtOrBefore,
} from "./navigation";
import { saveBlob } from "./download";
import { usePlayback } from "./usePlayback";
import { formatDuration, formatTime } from "./format";

interface Boot {
  projectId: string;
  projectName: string;
  mediaId: string;
  version: Version;
  sentences: Sentence[];
  tokens: Token[];
}

interface EdlState {
  original_duration_ms: number;
  output_duration_ms: number;
  removed_ms: number;
  segment_count: number;
  keeps: { orig_start: number; orig_end: number; new_start: number }[];
  deletions: { start_ms: number; end_ms: number; source: string }[];
  clamps: { requested: number; applied: number; edge: string; reason: string }[];
  signature: string;
}

export function App(): React.JSX.Element {
  const [boot, setBoot] = useState<Boot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selection, setSelection] = useState<TokenSelection | null>(null);
  const [sentenceSelection, setSentenceSelection] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("models");
  // Bumped after every successful edit so the history panel refetches.
  const [historyRevision, setHistoryRevision] = useState(0);
  // Mirrors `boot` so `load` can tell "first load" from "reload after an edit"
  // without taking `boot` as a dependency (which would rebuild the callback and
  // re-trigger the mount effect on every reload).
  const bootRef = useRef<Boot | null>(null);
  bootRef.current = boot;

  const load = useCallback(async (projectId?: string) => {
    // Only the *first* load blanks the screen. A reload after an edit must keep
    // the workbench mounted: unmounting it throws away every piece of local
    // state inside — the waveform's zoom window and scroll position above all.
    // That is why dragging a sentence boundary used to snap the waveform back to
    // full zoom: the drag committed a time edit, the edit called reload, and the
    // reload replaced `<Desk>` with "加载中…" for a frame, resetting the view the
    // user had just set up. Rescript keeps zoom in its own component state and
    // never remounts the timeline, so the same drag leaves the view untouched.
    if (bootRef.current === null) setLoading(true);
    setError(null);
    try {
      const projects = await api.listProjects();
      // A requested project wins; otherwise fall back to the most recent one.
      const first = projectId
        ? projects.items.find((item) => item.id === projectId) ?? projects.items[0]
        : projects.items[0];
      if (!first) {
        setError("还没有项目。先用命令行创建并转写一期播客：\n\n" +
          "  animatter transcribe samples/speech.wav --project \"第 1 期\"");
        setLoading(false);
        return;
      }

      const versions = await api.listVersions(first.id);
      const version = versions[0];
      if (!version) {
        setError(`项目「${first.name}」还没有转写结果。先用命令行转写。`);
        setLoading(false);
        return;
      }

      // The server pages by design; the client must page too. Asking for one
      // page and calling it the transcript is why a fully transcribed 105-minute
      // episode displayed 40 minutes of it while the status bar — which reads
      // the database's own count — said 1074 sentences.
      const transcript = await loadAllSentences(
        (from, limit) => api.transcript(version.id, from, limit),
        version.sentence_count,
      );
      // The media id is not on the version payload; the audio route needs it,
      // so resolve it from the media list.
      const media = await fetch(`/api/projects/${first.id}/media`).then(
        (response) => response.json() as Promise<{ id: string }[]>,
      );
      const mediaId = media[0]?.id;
      if (!mediaId) {
        setError("这个项目没有登记音频文件。");
        setLoading(false);
        return;
      }

      setBoot({
        projectId: first.id,
        projectName: first.name,
        mediaId,
        version: transcript.version,
        sentences: transcript.sentences,
        tokens: transcript.tokens,
      });
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !boot) return <main className="boot">加载中…</main>;
  if (error && !boot) {
    return (
      <main className="boot error-text" style={{ whiteSpace: "pre-wrap" }}>
        {error}
      </main>
    );
  }
  if (!boot) return <main className="boot">没有可显示的内容</main>;

  return (
    <Desk
      boot={boot}
      notice={notice}
      busy={busy}
      selection={selection}
      sentenceSelection={sentenceSelection}
      settingsOpen={settingsOpen}
      setSettingsOpen={setSettingsOpen}
      settingsSection={settingsSection}
      setSettingsSection={setSettingsSection}
      onSettingsSaved={() => setHistoryRevision((v) => v + 1)}
      historyRevision={historyRevision}
      setNotice={setNotice}
      onSelectionChange={setSelection}
      onSentenceSelectionChange={setSentenceSelection}
      setBusy={setBusy}
      onReload={load}
      setBoot={setBoot}
    />
  );
}

interface DeskProps {
  boot: Boot;
  notice: string | null;
  busy: boolean;
  selection: TokenSelection | null;
  sentenceSelection: Set<number>;
  setNotice: (value: string | null) => void;
  onSelectionChange: (selection: TokenSelection | null) => void;
  onSentenceSelectionChange: (selection: Set<number>) => void;
  settingsOpen: boolean;
  setSettingsOpen: (value: boolean) => void;
  settingsSection: SettingsSection;
  setSettingsSection: (section: SettingsSection) => void;
  onSettingsSaved: (settings: AppSettings) => void;
  historyRevision: number;
  setBusy: (value: boolean) => void;
  onReload: (projectId?: string) => Promise<void>;
  setBoot: (boot: Boot) => void;
}

function Desk({
  boot,
  notice,
  busy,
  selection,
  sentenceSelection,
  settingsOpen,
  setSettingsOpen,
  settingsSection,
  setSettingsSection,
  onSettingsSaved,
  historyRevision,
  setNotice,
  onSelectionChange,
  onSentenceSelectionChange,
  setBusy,
  onReload,
}: DeskProps): React.JSX.Element {
  const audioUrl = useMemo(
    () => api.audioUrl(boot.projectId, boot.mediaId),
    [boot.projectId, boot.mediaId],
  );
  /**
   * One zoom/scroll window for both timelines.
   *
   * The video track draws the same milliseconds as the waveform above it, so they have to
   * agree on the window: independent windows put the same instant at two different x
   * positions, and "where do I drop this asset" turns into guesswork.
   */
  const timeline = useTimelineView(boot.projectId, boot.mediaId, boot.version.duration_ms);
  /**
   * Whether the whole left column is showing.
   *
   * One flag for all three panels, not three: 「热词 / 字幕 / 操作记录」 are one work area
   * behind one border, and folding it answers "do I need the transcript right now" —
   * a per-panel memory would let the column come back in a shape nobody asked for.
   * Not persisted, for the same reason the individual panels are not.
   */
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [edl, setEdl] = useState<EdlState | null>(null);
  const [rendered, setRendered] = useState<{ url: string; durationMs: number } | null>(null);
  const [showOutput, setShowOutput] = useState(false);
  /** Which export panel is open. One at a time: both deliver a file, and two dialogs on top
   * of each other is a way to start an export you then cannot see. */
  const [videoExportOpen, setVideoExportOpen] = useState(false);
  const [audioExportOpen, setAudioExportOpen] = useState(false);
  /**
   * Whether a film export is running, reported up by `VideoExportDialog`.
   *
   * The progress itself stays in the panel — this is only what the menu item needs to say
   * 「正在导出成片视频…」 and stay disabled. Lifting the whole progress state up here would make
   * the shell re-render on every poll of an 18-minute encode.
   */
  const [videoExportRunning, setVideoExportRunning] = useState(false);
  /**
   * The audio export's subtitle style, which the 字幕 tab edits and the *film* export uses.
   *
   * It lives here, not in `VideoPanel`, because the two consumers are now in two places: the
   * tab renders the two sliders, and the 导出 menu renders the entry that encodes with them.
   * Leaving the numbers in the panel and exporting with the renderer's defaults would
   * silently ignore the slider — the user nudges 字号 to 72, the still frame obeys, and the
   * film comes back at 52.
   *
   * Seeded from `GET /video/subtitle-style` rather than hard-coded, so it does not drift the
   * moment the ASS style changes. A failed fetch is ignored: 52/56 are the same defaults the
   * renderer falls back to.
   */
  const [fontSize, setFontSize] = useState(52);
  const [marginV, setMarginV] = useState(56);
  useEffect(() => {
    void api
      .videoSubtitleStyle()
      .then((style) => {
        setFontSize(style.font_size);
        setMarginV(style.margin_v);
      })
      .catch(() => undefined);
  }, []);

  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [loadOpen, setLoadOpen] = useState(false);
  const [loadPath, setLoadPath] = useState("");
  // Where the last 保存项目 put the file. The 载入 dialog pre-fills from it so
  // "save, then load it back" needs no copying and no guesswork.
  const [lastArchive, setLastArchive] = useState<{ path: string; filename: string } | null>(null);
  const [showSubtitles, setShowSubtitles] = useState(false);
  // Which generation the 导出 menu asked for. Kept as state rather than two
  // booleans so opening the panel means "generate this", not "open a dialog".
  const [scriptFormat, setScriptFormat] = useState<ScriptFormat>("shownotes");
  const [showScript, setShowScript] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  // Which batch action the 处理 menu asked for. Kept as one state rather than
  // five booleans so "the dialog is open" and "what it does" cannot disagree.
  const [batchAction, setBatchAction] = useState<BatchAction>("delete-all");

  const openBatch = useCallback((action: BatchAction) => {
    setBatchAction(action);
    setCleanupOpen(true);
  }, []);
  const [episodeHotwords, setEpisodeHotwords] = useState<HotwordItem[]>([]);
  const [globalHotwords, setGlobalHotwords] = useState<HotwordItem[]>([]);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [speakers, setSpeakers] = useState<SpeakerInfo[]>([]);
  // How many cleanups of each kind are in effect, for the 清理 menu labels.
  const [cleanupCounts, setCleanupCounts] = useState<Record<string, number>>({});

  const reloadCleanupCounts = useCallback(async () => {
    try {
      const result = await api.cleanupCounts(boot.version.id);
      setCleanupCounts(result.counts);
    } catch {
      // Decorative: a failed count must not block editing.
      setCleanupCounts({});
    }
  }, [boot.version.id]);

  useEffect(() => {
    void reloadCleanupCounts();
  }, [reloadCleanupCounts]);

  const reloadSpeakers = useCallback(async () => {
    try {
      setSpeakers(await api.speakers(boot.projectId));
    } catch {
      // Speaker metadata is decorative: a failure must never block editing.
      setSpeakers([]);
    }
  }, [boot.projectId]);

  useEffect(() => {
    void reloadSpeakers();
  }, [reloadSpeakers]);

  const reloadHotwords = useCallback(async () => {
    try {
      const [episode, global] = await Promise.all([
        api.episodeHotwords(boot.projectId, boot.mediaId),
        api.globalHotwords(),
      ]);
      setEpisodeHotwords(episode);
      setGlobalHotwords(global);
    } catch {
      // A hotword failure must never block editing.
      setEpisodeHotwords([]);
      setGlobalHotwords([]);
    }
  }, [boot.mediaId, boot.projectId]);

  useEffect(() => {
    void reloadHotwords();
  }, [reloadHotwords]);

  useEffect(() => {
    void api.settings().then(setSettings).catch(() => setSettings(null));
  }, []);

  // Playback skips deleted spans, so a cut is audible immediately — no render.
  const deletedSpans = useMemo(
    () => (edl?.deletions ?? []).map((d) => ({ start_ms: d.start_ms, end_ms: d.end_ms })),
    [edl],
  );
  // Words hidden from the subtitles but still audible. Their own marks on the
  // waveform, because otherwise the only trace of a hide is the 「已隐去 N」 tag in
  // the sentence header — invisible from the timeline, where the user is looking
  // when they ask "what did I actually change".
  const hiddenSpans = useMemo(() => hiddenWordSpans(boot.tokens), [boot.tokens]);
  const playback = usePlayback(audioUrl, boot.version.duration_ms, deletedSpans, (issue) =>
    setNotice(issue.message),
  );

  // The EDL is derived on the server from the deletion set; refetch whenever the
  // transcript reloads, because a text edit can change token timings.
  useEffect(() => {
    let cancelled = false;
    void api
      .edl(boot.version.id)
      .then((payload) => {
        if (!cancelled) setEdl(payload);
      })
      .catch(() => {
        if (!cancelled) setEdl(null);
      });
    return () => {
      cancelled = true;
    };
  }, [boot.version.id, boot.tokens]);
  const activeOrd = findActiveSentence(boot.sentences, playback.positionMs);

  const markers: Marker[] = useMemo(
    () =>
      boot.sentences.map((sentence) => ({
        start_ms: sentence.start_ms,
        end_ms: sentence.end_ms,
        active: sentence.ord === activeOrd,
      })),
    [boot.sentences, activeOrd],
  );

  // Only sentence-edge tokens are draggable. Interior tokens are not: moving one
  // would leave the sentence's own bounds stale, and there is no sensible fix
  // for that in a waveform drag.
  const handles: EdgeHandle[] = useMemo(
    () =>
      boot.sentences.flatMap((sentence) => {
        const first = boot.tokens[sentence.token_start];
        const last = boot.tokens[sentence.token_end - 1];
        if (!first || !last) return [];
        return [
          { tokenIndex: first.idx, edge: "start" as const, ms: first.start_ms, sentenceOrd: sentence.ord },
          { tokenIndex: last.idx, edge: "end" as const, ms: last.end_ms, sentenceOrd: sentence.ord },
        ];
      }),
    [boot.sentences, boot.tokens],
  );

  /**
   * Run one workspace action with the busy flag, and a fallback notice.
   *
   * The fallback is only a fallback: an action that says something specific
   * ("saved to D:/...", "restored 12 fillers") wins. It used to not: `run` wrote
   * `「label」完成` *after* the action returned, so every detailed message was
   * replaced a moment later by "保存项目 完成". That is how a save that told you
   * its path ended up telling you nothing — the user's complaint was accurate
   * and the message was being overwritten, not missing.
   */
  const noticeClaimed = useRef(false);
  const say = useCallback((message: string | null) => {
    noticeClaimed.current = true;
    setNotice(message);
  }, [setNotice]);

  const run = useCallback(
    async (label: string, action: () => Promise<void>) => {
      setBusy(true);
      noticeClaimed.current = false;
      setNotice(null);
      try {
        await action();
        if (!noticeClaimed.current) setNotice(`${label} 完成`);
      } catch (caught) {
        setNotice(describe(caught));
      } finally {
        setBusy(false);
      }
    },
    [setBusy, setNotice],
  );

  const playSentence = useCallback(
    (sentence: Sentence) => {
      const startMs = audibleStart(deletedSpans, sentence);
      if (startMs === null) {
        setNotice(`第 ${sentence.ord + 1} 句已整句删除，已跳到下一句`);
        const fallback = nextSentence(deletedSpans, boot.sentences, sentence.ord);
        if (fallback) {
          const fallbackStart = audibleStart(deletedSpans, fallback);
          if (fallbackStart !== null) {
            const stop = audibleStop(deletedSpans, fallbackStart, fallback.end_ms);
            playback.playRange(fallbackStart, stop ?? fallback.end_ms);
          }
        }
        return;
      }
      const stop = audibleStop(deletedSpans, startMs, sentence.end_ms);
      playback.playRange(startMs, stop ?? sentence.end_ms);
    },
    [boot.sentences, deletedSpans, playback, setNotice],
  );

  /** Sentence the playhead sits in, ignoring deletions. */
  const currentSentence = useCallback(
    () => sentenceAtOrBefore(boot.sentences, playback.positionMs),
    [boot.sentences, playback.positionMs],
  );

  const playPrevious = useCallback(() => {
    const current = currentSentence();
    if (!current) {
      // Before the first sentence: nothing earlier exists.
      return;
    }

    const currentStart = audibleStart(deletedSpans, current);
    // Rewind inside the current sentence first, but only if the playhead is
    // actually past its audible start; otherwise "previous" must step back.
    if (currentStart !== null && playback.positionMs - currentStart > 1200) {
      playback.seek(currentStart);
      return;
    }

    const target = previousSentence(deletedSpans, boot.sentences, current.ord);
    if (!target) {
      // Every earlier sentence is deleted: go to the top of surviving audio.
      const top = nextAudible(deletedSpans, 0);
      playback.seek(top);
      return;
    }
    const targetStart = audibleStart(deletedSpans, target);
    if (targetStart === null) return;
    playback.seek(targetStart);
  }, [boot.sentences, currentSentence, deletedSpans, playback]);

  const playNext = useCallback(() => {
    const current = currentSentence();
    const fromOrd = current ? current.ord : -1;
    const target = nextSentence(deletedSpans, boot.sentences, fromOrd);
    if (!target) return;
    const targetStart = audibleStart(deletedSpans, target);
    if (targetStart === null) return;
    playback.seek(targetStart);
  }, [boot.sentences, currentSentence, deletedSpans, playback]);

  /**
   * A click on the waveform: move the playhead, and point the transcript at it.
   *
   * The two halves of the screen have to agree about *when* it is. Before this,
   * clicking the timeline moved the audio and left the word selection wherever
   * it happened to be — so 「删除选中」 acted on a word the user had clicked
   * twenty seconds of audio ago, which is the worst possible combination: a
   * destructive action aimed by a stale pointer.
   *
   * Three things this deliberately does not do:
   *
   * * **It does not tick the sentence checkbox.** Ticking it would make
   *   「删除选中」 and the boundary buttons act on the whole sentence, silently
   *   escalating a "show me where I am" click into a sentence-sized operation.
   *   The active sentence is already marked by the playhead and the `.active`
   *   highlight; that is the right amount of signal.
   * * **It does not fall back to the nearest word.** A click in the 100–200 ms
   *   pause between two words clears the selection instead, because the honest
   *   answer is "no word is playing here".
   * * **It does not seek to the click time when that time is deleted.** `seek`
   *   snaps forward out of deletions, and the return value is used for the
   *   lookup — otherwise clicking inside a cut span would highlight the words
   *   that were removed rather than the audio that actually plays.
   *
   * It *does* clear any ticked sentences, because that is not a new rule: word
   * selection and sentence selection are exclusive everywhere else, and leaving
   * a sentence ticked here would mean the highlight shows one word while
   * 「删除选中」 acts on a whole sentence the user cannot see is still selected.
   */
  const seekAndPoint = useCallback(
    (ms: number) => {
      const landed = playback.seek(ms);
      const index = tokenAt(boot.tokens, landed);
      onSentenceSelectionChange(new Set());
      onSelectionChange(index < 0 ? null : { from: index, to: index + 1 });
    },
    [boot.tokens, onSelectionChange, onSentenceSelectionChange, playback],
  );

  /** Token range covered by the current selection, whichever kind it is. */
  const selectionRange = useCallback((): { from: number; to: number } | null => {
    if (sentenceSelection.size > 0) {
      return tokenRangeOfSentences(boot.sentences, sentenceSelection);
    }
    if (!selection) return null;
    return {
      from: Math.min(selection.from, selection.to),
      to: Math.max(selection.from, selection.to),
    };
  }, [boot.sentences, sentenceSelection, selection]);

  const applyBoundary = useCallback(
    async (action: "new_sentence" | "merge" | "to_prev" | "to_next") => {
      // Both kinds of selection resolve to one token range; the rail buttons are
      // enabled for either, so this must not require the word selection alone.
      const range = selectionRange();
      if (!range) return;
      await run("边界调整", async () => {
        await api.boundary(boot.version.id, action, range.from, range.to);
        await onReload();
        onSelectionChange(null);
        onSentenceSelectionChange(new Set());
      });
    },
    [boot.version.id, onReload, onSelectionChange, onSentenceSelectionChange, run, selectionRange],
  );

  const commitEdge = useCallback(
    async (handle: EdgeHandle, newMs: number) => {
      await run("调整边界", async () => {
        const result = await api.adjustEdge(boot.version.id, handle.tokenIndex, handle.edge, newMs);
        await onReload();
        if (result.clamped && result.reason) setNotice(`已调整到 ${result.start_ms}–${result.end_ms}（${result.reason}）`);
      });
    },
    [boot.version.id, onReload, run, setNotice],
  );

  const hasSelection = sentenceSelection.size > 0 || selection !== null;

  const deleteRange = useCallback(async () => {
    const range = selectionRange();
    if (!range) return;
    const ids = Array.from({ length: range.to - range.from }, (_, i) => range.from + i);
    await run("删除", async () => {
      await api.addDeletion(boot.version.id, { token_ids: ids });
      await onReload();
      onSelectionChange(null);
      onSentenceSelectionChange(new Set());
    });
  }, [boot.version.id, onReload, onSelectionChange, onSentenceSelectionChange, run, selectionRange]);

  /**
   * Take the selected words back out of the deletion set — the bubble's 「还」.
   *
   * Word-scoped, not row-scoped. A sentence delete covers dozens of words in one
   * deletion row; restoring the row because one word in it was clicked would
   * bring back audio the user never pointed at.
   */
  const restoreRange = useCallback(async () => {
    const range = selectionRange();
    if (!range) return;
    const ids = Array.from({ length: range.to - range.from }, (_, i) => range.from + i);
    await run("还原", async () => {
      await api.restoreWords(boot.version.id, ids);
      await onReload();
      onSelectionChange(null);
      onSentenceSelectionChange(new Set());
    });
  }, [boot.version.id, onReload, onSelectionChange, onSentenceSelectionChange, run, selectionRange]);

  /**
   * Hide the selected words from the text, leaving the audio alone.
   *
   * `hidden` is a toggle, not a one-way switch: the same button un-hides, which
   * is the only way back short of the history panel. A selection that mixes
   * hidden and visible words becomes hidden — "make these disappear" is the
   * request, and a mixed outcome would look like the click did nothing.
   */
  const hideRange = useCallback(
    async (hidden: boolean) => {
      const range = selectionRange();
      if (!range) return;
      await run(hidden ? "隐去" : "恢复文字", async () => {
        await api.hideWords(boot.version.id, range.from, range.to, hidden);
        await onReload();
        onSelectionChange(null);
        onSentenceSelectionChange(new Set());
      });
    },
    [boot.version.id, onReload, onSelectionChange, onSentenceSelectionChange, run, selectionRange],
  );

  const undoDeletion = useCallback(async () => {
    const rows = await api.deletions(boot.version.id);
    const last = rows[rows.length - 1];
    if (!last) return;
    await run("恢复删除", async () => {
      await api.removeDeletion(last.id);
      await onReload();
    });
  }, [boot.version.id, onReload, run]);

  const assignSpeaker = useCallback(
    async (localId: number | null, ords: number[]) => {
      if (ords.length === 0) return;
      await run(localId === null ? "清除标注" : "指派说话人", async () => {
        await api.assignSpeaker(boot.version.id, ords, localId);
        await onReload();
        await reloadSpeakers();
      });
    },
    [boot.version.id, onReload, reloadSpeakers, run],
  );

  const createSpeaker = useCallback(
    async (ords: number[]) => {
      await run("新建说话人", async () => {
        const created = await api.createSpeaker(boot.projectId, "");
        await reloadSpeakers();
        // Create-then-assign in one click: a speaker nobody is attached to is
        // useless, and the user opened the menu from a sentence they meant.
        if (ords.length > 0) {
          await api.assignSpeaker(boot.version.id, ords, created.local_id);
          await onReload();
          await reloadSpeakers();
        }
      });
    },
    [boot.projectId, boot.version.id, onReload, reloadSpeakers, run],
  );

  const renameSpeaker = useCallback(
    async (localId: number, name: string) => {
      await run("重命名说话人", async () => {
        await api.renameSpeaker(boot.projectId, localId, name);
        await reloadSpeakers();
      });
    },
    [boot.projectId, reloadSpeakers, run],
  );

  const saveArchive = useCallback(async () => {
    await run("保存项目", async () => {
      const result = await api.saveAndDownloadArchive(boot.projectId);
      saveBlob(result.blob, result.filename);

      setLastArchive({ path: result.path, filename: result.filename });
      say(
        `已保存并下载 ${result.filename}（${result.sentence_count} 句 · ${result.op_count} 条操作 · ${result.deletion_count} 处删除）。` +
          `原件在 ${result.path}，「载入项目」里已经填好这个路径。`,
      );
    });
  }, [boot.projectId, run, say]);

  /**
   * Hand the Audacity label track to the browser.
   *
   * This is the escape hatch for "I need to cut this episode today": the word
   * timeline leaves the app as a plain label file, and the actual editing
   * happens in Audacity. It is therefore *not* gated on having deletions —
   * 导出成片音频 is, and that is precisely the wall this route exists to go
   * around.
   */
  const downloadLabels = useCallback(async () => {
    await run("导出 Audacity 标签", async () => {
      const result = await api.downloadLabels(boot.version.id);
      saveBlob(result.blob, result.filename);
      say(
        `已导出 ${result.lines} 条标签到 ${result.filename}。` +
          `在 Audacity 里打开原始音频，然后 文件 > 导入 > 标签 选中这个文件。`,
      );
    });
  }, [boot.version.id, run, say]);

  const loadArchive = useCallback(async () => {
    if (!loadPath.trim()) return;
    await run("载入项目", async () => {
      const result = await api.loadArchive(loadPath.trim());
      setLoadOpen(false);
      say(
        result.media_relinked
          ? `已载入「${result.project_name}」（${result.sentence_count} 句）`
          : `已载入「${result.project_name}」，但音频未找到：${result.warning}`,
      );
      await onReload(result.project_id);
    });
  }, [loadPath, onReload, run, say]);

  // The filler pair is mutually exclusive: once one of them has been applied the
  // other has nothing left to act on. Both states are derived from the counts the
  // menu already loads, so this costs no extra request. `?? 0` because the record
  // is empty until the first load lands.
  const fillerHidden = (cleanupCounts["hidden_filler"] ?? 0) > 0;
  const fillerDeleted = (cleanupCounts["filler"] ?? 0) > 0;

  const menus: Menu[] = useMemo(
    () => [
      {
        id: "project",
        label: "项目",
        items: [
          { id: "new", label: "新建项目…", onSelect: () => setNewProjectOpen(true) },
          {
            id: "save",
            label: "保存项目…",
            hint: ".project",
            disabled: busy,
            onSelect: () => void saveArchive(),
          },
          {
            id: "load",
            label: "载入项目…",
            onSelect: () => {
              setLoadPath(lastArchive?.path ?? "");
              setLoadOpen(true);
            },
          },
          "separator",
          { id: "reload", label: "重新载入", hint: "Ctrl+R", onSelect: () => void onReload() },
          {
            id: "undo",
            label: "撤销最近一次编辑",
            hint: "Ctrl+Z",
            onSelect: () =>
              void run("撤销", async () => {
                await api.undo(boot.version.id);
                await onReload();
              }),
          },
        ],
      },
      {
        id: "cleanup",
        label: "处理",
        items: [
          // The actions come first and are scoped: the dialog a menu item opens
          // is already that action. This replaced a single "一键清理" that asked
          // for the scope inside the dialog — one button doing two very different
          // things (cut audio / only edit text) depending on a toggle.
          //
          // The filler pair is mutually exclusive and says so by greying out: a
          // word is either deleted or hidden, never both. The hint is the reason,
          // not a count — "已隐去 183 处" on a *delete* item reads as a threat.
          {
            id: "delete-filler",
            label: "删除口癖词…",
            hint: fillerHidden ? "已隐去的口癖词要先还原" : "剪掉音频",
            disabled: fillerHidden,
            onSelect: () => openBatch("delete-filler"),
          },
          {
            id: "hide-filler",
            label: "隐去口癖词…",
            hint: fillerDeleted ? "已删除的口癖词要先还原" : "只改字幕",
            disabled: fillerDeleted,
            onSelect: () => openBatch("hide-filler"),
          },
          {
            id: "delete-silence",
            label: "删除静音…",
            onSelect: () => openBatch("delete-silence"),
          },
          {
            id: "delete-disfluency",
            label: "删除未转写语流…",
            onSelect: () => openBatch("delete-disfluency"),
          },
          "separator",
          {
            id: "delete-all",
            label: "删除全部三类…",
            hint: "口癖 / 静音 / 漏字",
            onSelect: () => openBatch("delete-all"),
          },
          "separator",
          // Restores are per-kind and say what they take back. The filler pair is
          // split because deleting and hiding are different acts: one comes back
          // with its audio, the other with its text.
          {
            id: "restore-deleted-filler",
            label: `还原已删除口癖词${cleanupCounts.filler ? `（${cleanupCounts.filler}）` : ""}`,
            disabled: !cleanupCounts.filler,
            onSelect: () =>
              void run("还原已删除口癖词", async () => {
                const result = await api.restoreCleanup(boot.version.id, { source: "filler" });
                say(`已还原 ${result.restored} 处已删除口癖词`);
                await onReload();
                await reloadCleanupCounts();
              }),
          },
          {
            id: "restore-hidden-filler",
            label: `还原已隐去口癖词${
              cleanupCounts.hidden_filler ? `（${cleanupCounts.hidden_filler}）` : ""
            }`,
            disabled: !cleanupCounts.hidden_filler,
            onSelect: () =>
              void run("还原已隐去口癖词", async () => {
                const result = await api.restoreCleanup(boot.version.id, { source: "hidden_filler" });
                say(`已还原 ${result.restored} 处已隐去口癖词`);
                await onReload();
                await reloadCleanupCounts();
              }),
          },
          {
            id: "restore-silence",
            label: `还原已删除静音${cleanupCounts.silence ? `（${cleanupCounts.silence}）` : ""}`,
            disabled: !cleanupCounts.silence,
            onSelect: () =>
              void run("还原已删除静音", async () => {
                const result = await api.restoreCleanup(boot.version.id, { source: "silence" });
                say(`已还原 ${result.restored} 处已删除静音`);
                await onReload();
                await reloadCleanupCounts();
              }),
          },
          {
            id: "restore-disfluency",
            label: `还原已删除未转写语流${
              cleanupCounts.disfluency ? `（${cleanupCounts.disfluency}）` : ""
            }`,
            disabled: !cleanupCounts.disfluency,
            onSelect: () =>
              void run("还原已删除未转写语流", async () => {
                const result = await api.restoreCleanup(boot.version.id, { source: "disfluency" });
                say(`已还原 ${result.restored} 处已删除未转写语流`);
                await onReload();
                await reloadCleanupCounts();
              }),
          },
        ],
      },
      {
        id: "settings",
        label: "设置",
        items: [
          {
            id: "open",
            label: "模型…",
            onSelect: () => {
              setSettingsSection("models");
              setSettingsOpen(true);
            },
          },
          {
            id: "hotword-settings",
            label: "热词与校对…",
            onSelect: () => {
              setSettingsSection("hotwords");
              setSettingsOpen(true);
            },
          },
          {
            id: "cleanup-settings",
            label: "一键删除与隐去…",
            onSelect: () => {
              setSettingsSection("cleanup");
              setSettingsOpen(true);
            },
          },
          {
            id: "text-settings",
            label: "文本处理…",
            hint: "模型参数 / ShowNotes 签名 / 提示词",
            onSelect: () => {
              setSettingsSection("text");
              setSettingsOpen(true);
            },
          },
          {
            id: "align-settings",
            label: "时间轴对齐…",
            hint: "整条时间轴整体偏移 ±1000ms，可撤销",
            onSelect: () => {
              setSettingsSection("align");
              setSettingsOpen(true);
            },
          },
          "separator",
          {
            id: "hotwords",
            label: "转写时启用热词",
            checked: settings?.use_hotwords ?? false,
            onSelect: () => {
              if (!settings) return;
              const next = { ...settings, use_hotwords: !settings.use_hotwords };
              setSettings(next);
              void api.saveSettings({ use_hotwords: next.use_hotwords }).catch(() => undefined);
            },
          },
          {
            id: "snap",
            label: "切点吸附到静音",
            checked: settings?.snap_to_silence ?? true,
            onSelect: () => {
              if (!settings) return;
              const next = { ...settings, snap_to_silence: !settings.snap_to_silence };
              setSettings(next);
              void api.saveSettings({ snap_to_silence: next.snap_to_silence }).catch(() => undefined);
              void onReload();
            },
          },
          "separator",
        ],
      },
      {
        id: "export",
        label: "导出",
        items: [
          {
            id: "subtitles",
            label: "字幕…",
            hint: "SRT / VTT / TXT",
            onSelect: () => setShowSubtitles(true),
          },
          {
            id: "labels",
            label: "Audacity 标签…",
            hint: "词级标签 .txt，可直接在 Audacity 剪辑",
            disabled: busy,
            onSelect: () => void downloadLabels(),
          },
          "separator",
          // ShowNotes and 文稿 share a group because they are the same act —
          // publishing the episode's text — even though one is a summary and
          // the other is a rewrite. Audio sits below, separated, because it is
          // a different deliverable with a long runtime.
          {
            id: "shownotes",
            label: "ShowNotes…",
            hint: "备选标题 + 两段概述 + 时间轴 + 签名",
            disabled: busy,
            onSelect: () => {
              setScriptFormat("shownotes");
              setShowScript(true);
            },
          },
          {
            id: "script",
            label: "文稿…",
            hint: "对话式 / 文章式",
            disabled: busy,
            onSelect: () => {
              setScriptFormat(
                settings?.script_format === "article" ? "article" : "dialogue",
              );
              setShowScript(true);
            },
          },
          "separator",
          {
            id: "render",
            label: "导出成片音频…",
            // Empty edit exports the whole episode. That is the normal end of a
            // proofreading-only session (第 110 轮): subtitle work is a deliverable on its
            // own, and requiring a cut first made the menu item dead for anyone who never
            // wanted one.
            hint: !edl ? "正在计算剪辑方案…" : "文件名、文件夹、格式",
            disabled: !edl,
            // A panel rather than an immediate export (round 135): the user asked to name the
            // file and choose the folder, which a menu item cannot ask for. The format moved
            // in as well — it is the third thing about the same file, not a global preference.
            onSelect: () => setAudioExportOpen(true),
          },
          {
            // The film export used to be a button inside `视频剪辑` (round 102). It is a
            // delivery, and every other delivery is in this menu — so it comes here, right
            // under the audio export, which is the order the work happens in.
            //
            // Since round 135 the item opens a panel: a 900 MB film needs a name, a folder and
            // the two encoding options, and all three used to be decided *for* the user (or
            // hidden one level up as menu toggles sitting next to the action itself).
            id: "render-video",
            ...videoExportEntry({
              hasEdl: edl !== null,
              exporting: videoExportRunning,
            }),
            onSelect: () => setVideoExportOpen(true),
          },
          "separator",
          {
            id: "sig-settings",
            label: "文本处理设置…",
            hint: "模型参数、签名、提示词",
            onSelect: () => {
              setSettingsSection("text");
              setSettingsOpen(true);
            },
          },
        ],
      },
    ],
    [
      boot.version.id,
      cleanupCounts,
      edl,
      lastArchive,
      onReload,
      reloadCleanupCounts,
      run,
      setSettingsOpen,
      setSettingsSection,
      settings,
    ],
  );
  /**
   * Rewrite one word.
   *
   * The backend accepts any length, because at word level the replacement is
   * 1 token → 1 token and both edges are real observations. The only guard left
   * is readability: a four-character word stretched over 90 ms is fine, but a
   * twenty-character sentence crammed into 90 ms is not, and the user cannot
   * hear that from the transcript, so say it.
   */
  const applyTextEdit = useCallback(
    async (proposed: string) => {
      if (!selection) return;
      const from = Math.min(selection.from, selection.to);
      const to = Math.max(selection.from, selection.to);
      const token = boot.tokens[from];
      const spokenMs = token ? token.end_ms - token.start_ms : 0;
      if (token && spokenMs > 0) {
        const cps = proposed.replace(/\s/g, "").length / (spokenMs / 1000);
        if (cps > 14) {
          setNotice(`注意：「${proposed}」要塞进 ${spokenMs}ms，语速会明显快于原音。`);
        }
      }
      await run("文字修改", async () => {
        await api.editText(boot.version.id, from, to, proposed);
        await onReload();
        onSelectionChange(null);
      });
    },
    [boot.tokens, boot.version.id, onReload, onSelectionChange, run, selection, setNotice],
  );

  // Keyboard: space plays the active sentence, brackets move by sentence.
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;

      // Never steal keys while typing.
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
      // A focused button already handles Space/Enter as "click"; stealing it
      // would fire both the button action and playback.
      if (target && target.tagName === "BUTTON" && (event.code === "Space" || event.key === "Enter")) {
        return;
      }
      // IME composition swallows bracket keys in Chinese input.
      if (event.isComposing) return;

      if (event.code === "Space") {
        event.preventDefault();
        if (playback.playing) {
          playback.pause();
          return;
        }
        const found = sentenceAtOrBefore(boot.sentences, playback.positionMs);
        const sentence = found
          ? boot.sentences.find((item) => item.ord === found.ord) ?? null
          : null;
        const audible = nextAudible(deletedSpans, playback.positionMs);
        const resumeHere =
          sentence !== null &&
          !isInsideDeletion(deletedSpans, audible) &&
          audible < sentence.end_ms - 50;

        if (resumeHere) {
          // Resume inside the current sentence rather than restarting it.
          playback.play();
        } else if (sentence) {
          // The playhead sits in deleted audio, or past the end of the sentence:
          // start from the next position that still has sound.
          playSentence(sentence);
        } else {
          // No sentence covers this position (leading silence): play from the
          // first surviving instant instead of from a deleted one.
          const from = nextAudible(deletedSpans, audible);
          playback.playRange(from, nextBoundary(deletedSpans, from, boot.version.duration_ms));
        }
      } else if (event.key === "[" || event.code === "BracketLeft") {
        event.preventDefault();
        playPrevious();
      } else if (event.key === "]" || event.code === "BracketRight") {
        event.preventDefault();
        playNext();
      } else if (event.key === "Escape") {
        onSelectionChange(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [boot.sentences, onSelectionChange, playNext, playPrevious, playSentence, playback, sentenceAtOrBefore]);

  // Derived enablement. Each action needs a different kind of target, and
  // getting this wrong is how a button ends up silently doing nothing.
  //
  // There is deliberately no "已选 N 个词「…」" readout here any more. The word
  // being acted on is already highlighted in the transcript, and the label
  // repeated it in a bar that re-flowed to a second line whenever a long word
  // was picked — so the panel under the cursor grew and shrank with the
  // selection, which is worse than saying nothing. What is *not* visible on
  // its own is a cross-speaker conflict, and that is still rendered, in the
  // rail next to the buttons it disables.

  return (
    <div className="workbench">
      <MenuBar menus={menus} status={`${boot.projectName} · v${boot.version.version}`} />

      <div className={`bench-body${drawerOpen ? "" : " is-drawer-closed"}`}>
        {/*
          The handle sits on the sidebar's right border, not in the toolbar: it collapses
          the column it is attached to, and it stays put in both states so a closed drawer
          can be reopened without knowing a shortcut. The panels inside already collapse
          one at a time; this folds the whole column when the transcript is not what you
          are working on (laying out video, exporting, settings).
        */}
        <button
          type="button"
          className="drawer-handle"
          aria-expanded={drawerOpen}
          aria-label={drawerOpen ? "收起左侧工作区" : "展开左侧工作区"}
          title={drawerOpen ? "收起左侧工作区" : "展开左侧工作区"}
          onClick={() => setDrawerOpen((value) => !value)}
        >
          {drawerOpen ? "‹" : "›"}
        </button>
        <aside className="sidebar">
          <HotwordPanel
            projectId={boot.projectId}
            mediaId={boot.mediaId}
            episodeHotwords={episodeHotwords}
            globalHotwords={globalHotwords}
            onChanged={reloadHotwords}
            onNotice={setNotice}
          />
          <div className="sidebar-grow">
            <SentenceList
              sentences={boot.sentences}
              tokens={boot.tokens}
              positionMs={playback.positionMs}
              selection={selection}
              sentenceSelection={sentenceSelection}
              speakers={speakers}
              onSelectionChange={onSelectionChange}
              onSentenceSelectionChange={onSentenceSelectionChange}
              onPlaySentence={playSentence}
              playing={playback.playing}
              onPause={playback.pause}
              onSeek={playback.seek}
              onDeleteWords={() => void deleteRange()}
              onRestoreWords={() => void restoreRange()}
              onCorrectWord={(proposed) => void applyTextEdit(proposed)}
              onHideWords={(hidden) => void hideRange(hidden)}
              onBoundary={(action) => void applyBoundary(action)}
              onAssignSpeaker={(localId, ords) => void assignSpeaker(localId, ords)}
              onCreateSpeaker={(ords) => void createSpeaker(ords)}
              onRenameSpeaker={(localId, name) => void renameSpeaker(localId, name)}
              deletedRanges={edl?.deletions ?? []}
              busy={busy}
            />
          </div>
          <HistoryPanel
            versionId={boot.version.id}
            revision={historyRevision}
            onUndone={onReload}
            onNotice={setNotice}
          />
        </aside>

        <section className="stage">
          {/*
            Two peer panels with identical chrome: title on the left, summary plus a
            收起/展开 button on the right. The video area used to live behind
            设置 → 显示视频区, which left a whole feature with no on-screen entry point —
            the only way to find it was to open a menu and guess. Folded by default, so it
            costs one row, and its summary answers "is this project using video?" without
            opening anything.
          */}
          <VideoPanel
            projectId={boot.projectId}
            projectName={boot.projectName}
            versionId={boot.version.id}
            positionMs={playback.positionMs}
            keeps={edl?.keeps ?? []}
            playing={playback.playing}
            sentences={boot.sentences}
            fontSize={fontSize}
            marginV={marginV}
            onFontSize={setFontSize}
            onMarginV={setMarginV}
            onSeek={playback.seek}
            onNotice={setNotice}
            view={timeline.view}
            onZoom={timeline.zoomAt}
            onPan={timeline.panTo}
          />

          <CollapsiblePanel
            title="音频剪辑"
            className="audio-panel"
            summary={
              edl ? (
                <>
                  成片 {formatClock(edl.output_duration_ms)} / 原始{" "}
                  {formatClock(edl.original_duration_ms)}
                  {edl.removed_ms > 0 ? ` · 删除 ${formatClock(edl.removed_ms)}` : ""}
                </>
              ) : null
            }
          >
            <div className="stage-top">
              <span className="clock">{formatTime(playback.positionMs)}</span>
              <span className="muted small">
                {boot.version.sentence_count} 句 · {boot.version.token_count} 词 ·{" "}
                {formatDuration(boot.version.duration_ms)}
              </span>
              {edl && edl.segment_count > 1 ? (
                <span className="muted small">{edl.segment_count} 段</span>
              ) : null}
              {edl && edl.clamps.length > 0 ? (
                <span className="muted small">{edl.clamps.length} 处吸附</span>
              ) : null}
            </div>

            <Waveform
              projectId={boot.projectId}
              mediaId={boot.mediaId}
              durationMs={boot.version.duration_ms}
              positionMs={playback.positionMs}
              markers={markers}
              handles={handles}
              deletions={edl?.deletions ?? []}
              hidden={hiddenSpans}
              captions={boot.sentences.map((sentence) => ({
                start_ms: sentence.start_ms,
                end_ms: sentence.end_ms,
                text: sentence.text,
                ord: sentence.ord,
              }))}
              activeOrd={activeOrd}
              onCaptionClick={(ord) => {
                const sentence = boot.sentences.find((item) => item.ord === ord);
                if (sentence) playSentence(sentence);
              }}
              onEdgeCommit={(handle, ms) => void commitEdge(handle, ms)}
              onSeek={seekAndPoint}
              view={timeline.view}
              total={timeline.total}
              onZoom={timeline.zoomAt}
              onPan={timeline.panTo}
              onReset={timeline.reset}
              onFollowPlayhead={timeline.followPlayhead}
            />

            <div className="transport">
              <button type="button" onClick={playback.playing ? playback.pause : playback.play}>
                {playback.playing ? "⏸ 暂停" : "▶ 播放"}
              </button>
              <button type="button" className="ghost" onClick={playPrevious} title="上一句（[）">
                ⟨ 上一句
              </button>
              <button type="button" className="ghost" onClick={playNext} title="下一句（]）">
                下一句 ⟩
              </button>
              <span className="transport-gap" />
              <button
                type="button"
                className="danger"
                disabled={!hasSelection || busy}
                onClick={() => void deleteRange()}
                title="删除选区（可多句整句删除）"
              >
                删除选中
              </button>
              <button
                type="button"
                className="ghost"
                disabled={busy || (edl?.deletions.length ?? 0) === 0}
                onClick={() => void undoDeletion()}
              >
                恢复删除
              </button>
              {/*
                导出成片 lives in the 导出 menu now. The transport keeps only the actions
                that act on the selection; a button that produces a file belongs with the
                other file-producing actions, and having it here *and* in the menu is how
                the two drifted apart). 试听成片 stays — it plays, it does not deliver.
              */}
              {rendered ? (
                <button type="button" className="ghost" onClick={() => setShowOutput((v) => !v)}>
                  {showOutput ? "收起成片" : "试听成片"}
                </button>
              ) : null}
            </div>

            {rendered && showOutput ? (
              <div className="output-player">
                <audio src={rendered.url} controls style={{ width: "100%" }} />
                <p className="muted small">
                  成片时长 {formatClock(rendered.durationMs)}。播放位置相对于成片，不是原始音频。
                </p>
              </div>
            ) : null}
          </CollapsiblePanel>

          {/*
            The notice stays at stage level, *outside* both panels. Both of them report into
            it — importing a PSD, binding a speaker, exporting a film — and putting it inside
            音频剪辑 meant a message raised from the video panel could land below the fold,
            which is the same as no message at all.
          */}
          {notice ? <div className="notice">{notice}</div> : null}
        </section>
      </div>

      <CleanupDialog
        open={cleanupOpen}
        action={batchAction}
        versionId={boot.version.id}
        onClose={() => setCleanupOpen(false)}
        onNotice={setNotice}
        onApplied={async () => {
          await onReload();
          await reloadCleanupCounts();
        }}
      />

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={onSettingsSaved}
        projectId={boot.projectId}
        mediaId={boot.mediaId}
        section={settingsSection}
        onHotwordsChanged={() => void reloadHotwords()}
        versionId={boot.version.id}
        onTranscriptChanged={onReload}
        onNotice={setNotice}
      />

      <VideoExportDialog
        open={videoExportOpen}
        episode={boot.projectName}
        versionId={boot.version.id}
        fontSize={fontSize}
        marginV={marginV}
        faster={settings?.video_faster ?? false}
        fastEncode={settings?.video_fast_encode ?? false}
        onClose={() => setVideoExportOpen(false)}
        onNotice={setNotice}
        onSettingsChanged={(values) => {
          if (settings) setSettings({ ...settings, ...values });
        }}
        onRunningChanged={setVideoExportRunning}
      />

      <AudioExportDialog
        open={audioExportOpen}
        episode={boot.projectName}
        versionId={boot.version.id}
        format={settings?.export_format ?? "m4a"}
        onFormatChanged={(format) => {
          if (settings) setSettings({ ...settings, export_format: format });
        }}
        onClose={() => setAudioExportOpen(false)}
        onNotice={setNotice}
        onExported={(url, durationMs) => {
          setRendered({ url, durationMs });
          setShowOutput(true);
        }}
      />

      <NewProjectDialog
        open={newProjectOpen}
        onClose={() => setNewProjectOpen(false)}
        onCreated={(projectId) => void onReload(projectId)}
        onNotice={setNotice}
      />

      {loadOpen ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setLoadOpen(false)}>
          <div
            className="modal modal-narrow"
            role="dialog"
            aria-modal="true"
            aria-label="载入项目"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header className="modal-head">
              <h2>载入项目</h2>
              <button type="button" className="ghost" onClick={() => setLoadOpen(false)} aria-label="关闭">
                ↗
              </button>
            </header>
            <div className="modal-body modal-single">
              <label className="field">
                <span>
                  .project 文件路径
                  <em className="hint">载入会新建一个项目，不会覆盖当前项目</em>
                </span>
                <input
                  autoFocus
                  value={loadPath}
                  placeholder="D:/podcast/S02E07.project"
                  onChange={(event) => setLoadPath(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void loadArchive();
                  }}
                />
              </label>
              <p className="section-note">
                存档包含文稿、全部编辑、操作记录与热词；音频按哈希引用，移动过也能恢复文稿。
              </p>
            </div>
            <footer className="modal-foot">
              <button type="button" className="ghost" onClick={() => setLoadOpen(false)}>
                取消
              </button>
              <button type="button" disabled={!loadPath.trim() || busy} onClick={() => void loadArchive()}>
                载入
              </button>
            </footer>
          </div>
        </div>
      ) : null}

      {showSubtitles ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setShowSubtitles(false)}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="字幕导出"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header className="modal-head">
              <h2>字幕导出</h2>
              <button type="button" className="ghost" onClick={() => setShowSubtitles(false)} aria-label="关闭">
                ✕
              </button>
            </header>
            <div className="modal-body modal-single">
              <SubtitlePanel
                versionId={boot.version.id}
                hasDeletions={(edl?.deletions.length ?? 0) > 0}
                onNotice={setNotice}
              />
            </div>
          </div>
        </div>
      ) : null}

      {showScript ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setShowScript(false)}>
          <div
            className="modal modal-wide"
            role="dialog"
            aria-modal="true"
            aria-label="ShowNotes / 文稿"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header className="modal-head">
              <h2>ShowNotes / 文稿</h2>
              <button type="button" className="ghost" onClick={() => setShowScript(false)} aria-label="关闭">
                ✕
              </button>
            </header>
            <div className="modal-body modal-single">
              <ScriptPanel
                versionId={boot.version.id}
                hasDeletions={(edl?.deletions.length ?? 0) > 0}
                onNotice={setNotice}
                initialFormat={scriptFormat}
                onFormatChange={(next) => {
                  setScriptFormat(next);
                  // Only 文稿 has a style to remember; ShowNotes would inherit it
                  // and then reopen 文稿 in the wrong mode next time.
                  if (next === "shownotes" || !settings) return;
                  setSettings({ ...settings, script_format: next });
                  void api.saveSettings({ script_format: next }).catch(() => undefined);
                }}
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** mm:ss for the EDL panel, where sub-second precision is noise. */
function formatClock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** Turn any thrown value into something a person can read. */
function describe(caught: unknown): string {
  if (caught instanceof Error) return caught.message;
  return String(caught);
}

