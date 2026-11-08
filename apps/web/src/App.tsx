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
  type RenderState,
  type ScriptFormat,
  type Sentence,
  type SpeakerInfo,
  type Token,
  type Version,
} from "./api";
import { loadAllSentences } from "./transcriptLoad";
import { isTerminal, percentOf, pollDelayMs, renderEta } from "./renderProgress";
import { HistoryPanel } from "./HistoryPanel";
import { NewProjectDialog } from "./NewProjectDialog";
import { SubtitlePanel } from "./SubtitlePanel";
import { ScriptPanel } from "./ScriptPanel";
import { CleanupDialog } from "./CleanupDialog";
import { HotwordPanel } from "./HotwordPanel";
import { MenuBar, type Menu } from "./MenuBar";
import { SentenceList, findActiveSentence, tokenRangeOfSentences, type TokenSelection } from "./SentenceList";
import { SettingsDialog, type SettingsSection } from "./SettingsDialog";
import { Waveform, type EdgeHandle, type Marker } from "./Waveform";
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
  const [edl, setEdl] = useState<EdlState | null>(null);
  const [rendered, setRendered] = useState<{ url: string; durationMs: number } | null>(null);
  const [showOutput, setShowOutput] = useState(false);
  /** Set while an export is encoding in the background; the button shows it. */
  const [exporting, setExporting] = useState<{ id: string; percent: number } | null>(null);

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
  const playback = usePlayback(audioUrl, boot.version.duration_ms, deletedSpans);

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
  const withinSentence = useMemo(() => {
    if (!selection) return null;
    const from = Math.min(selection.from, selection.to);
    const to = Math.max(selection.from, selection.to);
    return (
      boot.sentences.find((s) => from >= s.token_start && to <= s.token_end) ?? null
    );
  }, [boot.sentences, selection]);

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

  /**
   * Poll a render to completion.
   *
   * The first response is "queued": the endpoint now hands the encode to a
   * worker thread and returns, because a 105-minute export takes about twelve
   * minutes and no HTTP request should sit there for it. Polling is what turns
   * that silence into a percentage — the old synchronous endpoint just froze the
   * button, which is why it was reported as 「点击后无反应」.
   */
  const pollRender = useCallback(
    async (first: RenderState): Promise<RenderState> => {
      let state = first;
      const started = Date.now();
      let lastShown = -1;
      while (!isTerminal(state.status)) {
        const percent = percentOf(state.progress);
        if (percent !== lastShown) {
          lastShown = percent;
          setExporting({ id: state.id, percent });
          // Wording is deliberate: 「正在」 with a live percentage is the signal
          // that the click landed. Before this, twelve minutes of encoding
          // produced no feedback whatsoever.
          say(
            `正在导出成片… ${percent}%（${renderEta(Date.now() - started, state.progress)}）`,
          );
        }
        await new Promise((resolve) => setTimeout(resolve, pollDelayMs(Date.now() - started)));
        state = await api.renderStatus(state.id);
      }
      if (state.status === "failed") throw new Error(state.error ?? "渲染失败");
      return state;
    },
    [say],
  );

  const renderExport = useCallback(async () => {
    const lossless = settings?.export_format === "wav";
    let queued: RenderState | null = null;
    // Only enqueueing counts as "busy". The encode runs for minutes on a long
    // episode; holding the busy flag through it would freeze the whole editor,
    // which is the same complaint wearing a different coat.
    await run("导出成片", async () => {
      queued = await api.render(boot.version.id, { lossless });
    });
    if (queued === null) return; // queueing failed; `run` already reported it

    const start: RenderState = queued;
    if (isTerminal(start.status)) {
      if (start.status === "done") {
        setRendered({ url: start.url, durationMs: start.duration_ms });
        setShowOutput(true);
      }
      return;
    }
    try {
      const result = await pollRender(start);
      setRendered({ url: result.url, durationMs: result.duration_ms });
      setShowOutput(true);
      say(`成片已导出 ${formatClock(result.duration_ms)}，可在下方试听`);
    } catch (caught) {
      setNotice(describe(caught));
    } finally {
      setExporting(null);
    }
  }, [boot.version.id, pollRender, run, say, settings?.export_format]);

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
        label: "清理",
        items: [
          {
            id: "one-click",
            label: "一键清理…",
            hint: "口癖 / 静音 / 漏字",
            onSelect: () => setCleanupOpen(true),
          },
          "separator",
          {
            id: "restore-filler",
            label: `还原口癖词${cleanupCounts.filler ? `（${cleanupCounts.filler}）` : ""}`,
            disabled: !cleanupCounts.filler,
            onSelect: () =>
              void run("还原口癖词", async () => {
                const result = await api.restoreCleanup(boot.version.id, { source: "filler" });
                setNotice(`已还原 ${result.restored} 处口癖词`);
                await onReload();
                await reloadCleanupCounts();
              }),
          },
          {
            id: "restore-silence",
            label: `还原静音${cleanupCounts.silence ? `（${cleanupCounts.silence}）` : ""}`,
            disabled: !cleanupCounts.silence,
            onSelect: () =>
              void run("还原静音", async () => {
                const result = await api.restoreCleanup(boot.version.id, { source: "silence" });
                setNotice(`已还原 ${result.restored} 处静音`);
                await onReload();
                await reloadCleanupCounts();
              }),
          },
          {
            id: "restore-disfluency",
            label: `还原未转写语流${cleanupCounts.disfluency ? `（${cleanupCounts.disfluency}）` : ""}`,
            disabled: !cleanupCounts.disfluency,
            onSelect: () =>
              void run("还原未转写语流", async () => {
                const result = await api.restoreCleanup(boot.version.id, { source: "disfluency" });
                setNotice(`已还原 ${result.restored} 处未转写语流`);
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
            label: "一键清理设置…",
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
            hint: "概述 + 时间轴目录 + 签名",
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
            label: exporting ? `导出成片音频… ${exporting.percent}%（进行中）` : "导出成片音频…",
            hint:
              edl && edl.deletions.length === 0
                ? "先删除要剪掉的内容"
                : !edl
                  ? "正在计算剪辑方案…"
                  : settings?.export_format === "wav"
                    ? "WAV 无损"
                    : "M4A",
            disabled: exporting !== null || !edl || edl.deletions.length === 0,
            onSelect: () => void renderExport(),
          },
          "separator",
          {
            id: "fmt-m4a",
            label: "格式：M4A（有损）",
            checked: settings?.export_format === "m4a",
            onSelect: () => {
              if (!settings) return;
              setSettings({ ...settings, export_format: "m4a" });
              void api.saveSettings({ export_format: "m4a" }).catch(() => undefined);
            },
          },
          {
            id: "fmt-wav",
            label: "格式：WAV（无损）",
            checked: settings?.export_format === "wav",
            onSelect: () => {
              if (!settings) return;
              setSettings({ ...settings, export_format: "wav" });
              void api.saveSettings({ export_format: "wav" }).catch(() => undefined);
            },
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
      renderExport,
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
  const tokenCount = selection ? Math.abs(selection.to - selection.from) : 0;

  const selectionSummary = (() => {
    if (sentenceSelection.size > 0) {
      const ords = [...sentenceSelection].sort((a, b) => a - b);
      const contiguous = ords.every((ord, i) => i === 0 || ord === ords[i - 1]! + 1);
      const label = contiguous
        ? `第 ${ords[0]! + 1}–${ords[ords.length - 1]! + 1} 句`
        : `第 ${ords.map((o) => o + 1).join("、")} 句`;
      return `已选 ${sentenceSelection.size} 句（${label}）`;
    }
    if (tokenCount > 0) {
      return `已选 ${tokenCount} 个词${withinSentence ? `（第 ${withinSentence.ord + 1} 句）` : "（跨句）"}`;
    }
    return "点词选中，拖选连续词；选中后弹出「删除 / 修正」";
  })();

  return (
    <div className="workbench">
      <MenuBar menus={menus} status={`${boot.projectName} · v${boot.version.version}`} />

      <div className="bench-body">
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
              onSeek={playback.seek}
              onDeleteWords={() => void deleteRange()}
              onCorrectWord={(proposed) => void applyTextEdit(proposed)}
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
          <div className="stage-top">
            <span className="clock">{formatTime(playback.positionMs)}</span>
            <span className="muted small">
              {boot.version.sentence_count} 句 · {boot.version.token_count} 词 ·{" "}
              {formatDuration(boot.version.duration_ms)}
            </span>
            {edl ? (
              <span className="muted small">
                成片 {formatClock(edl.output_duration_ms)} / 原始{" "}
                {formatClock(edl.original_duration_ms)} · 删除{" "}
                {formatClock(edl.removed_ms)}
                {edl.segment_count > 1 ? ` · ${edl.segment_count} 段` : ""}
                {edl.clamps.length > 0 ? ` · ${edl.clamps.length} 处吸附` : ""}
              </span>
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
            onSeek={playback.seek}
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
            <button
              type="button"
              disabled={busy || exporting !== null || !edl || edl.deletions.length === 0}
              onClick={() => void renderExport()}
              // A disabled button with no reason is indistinguishable from a
              // broken one — that is how "导出成片 点击后无反应" was reported.
              title={
                exporting
                  ? `正在导出 ${exporting.percent}%`
                  : !edl
                    ? "正在计算剪辑方案…"
                    : edl.deletions.length === 0
                      ? "还没有删除任何内容：先删掉要剪掉的段落，再导出成片"
                      : "把当前的删除方案渲染成一个音频文件"
              }
            >
              {exporting ? `导出中 ${exporting.percent}%` : "导出成片"}
            </button>
            {rendered ? (
              <button type="button" className="ghost" onClick={() => setShowOutput((v) => !v)}>
                {showOutput ? "收起成片" : "试听成片"}
              </button>
            ) : null}
          </div>

          <div className="actionbar">
            <span className="muted small">{selectionSummary}</span>
          </div>

          {notice ? <div className="notice">{notice}</div> : null}

          {rendered && showOutput ? (
            <div className="output-player">
              <audio src={rendered.url} controls style={{ width: "100%" }} />
              <p className="muted small">
                成片时长 {formatClock(rendered.durationMs)}。播放位置相对于成片，不是原始音频。
              </p>
            </div>
          ) : null}
        </section>
      </div>

      <CleanupDialog
        open={cleanupOpen}
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

/**
 * Hand a blob to the browser as a download.
 *
 * Shared by every "导出" action: without it the file only exists in the app's
 * data directory, which is how "点保存项目时没有弹出文件下载" happened.
 */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoking immediately can cancel the download in some browsers; one minute
  // is generous and the blob is small relative to the audio it describes.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
