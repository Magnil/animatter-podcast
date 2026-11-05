/**
 * Correction desk shell.
 *
 * Layout follows AGENT.md §7.2: waveform on top, sentence list below, action bar
 * for the current selection. Keyboard shortcuts are the primary workflow —
 * mouse-only proof-reading of a two-hour episode is unbearable.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  RequestFailed,
  type Sentence,
  type Token,
  type Version,
} from "./api";
import { SentenceList, findActiveSentence, sentenceForSelection, type Selection } from "./SentenceList";
import { Waveform, type Marker } from "./Waveform";
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

export function App(): React.JSX.Element {
  const [boot, setBoot] = useState<Boot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const projects = await api.listProjects();
      const first = projects.items[0];
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

      const transcript = await api.transcript(version.id);
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

  if (loading) return <main className="boot">加载中…</main>;
  if (error) return <main className="boot error-text" style={{ whiteSpace: "pre-wrap" }}>{error}</main>;
  if (!boot) return <main className="boot">没有可显示的内容</main>;

  return (
    <Desk
      boot={boot}
      notice={notice}
      busy={busy}
      selection={selection}
      setNotice={setNotice}
      onSelectionChange={setSelection}
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
  selection: Selection | null;
  setNotice: (value: string | null) => void;
  onSelectionChange: (selection: Selection | null) => void;
  setBusy: (value: boolean) => void;
  onReload: () => Promise<void>;
  setBoot: (boot: Boot) => void;
}

function Desk({
  boot,
  notice,
  busy,
  selection,
  setNotice,
  onSelectionChange,
  setBusy,
  onReload,
}: DeskProps): React.JSX.Element {
  const audioUrl = useMemo(
    () => api.audioUrl(boot.projectId, boot.mediaId),
    [boot.projectId, boot.mediaId],
  );
  const playback = usePlayback(audioUrl, boot.version.duration_ms);
  const activeOrd = findActiveSentence(boot.sentences, playback.positionMs);
  const withinSentence = sentenceForSelection(boot.sentences, selection);

  const markers: Marker[] = useMemo(
    () =>
      boot.sentences.map((sentence) => ({
        start_ms: sentence.start_ms,
        end_ms: sentence.end_ms,
        active: sentence.ord === activeOrd,
      })),
    [boot.sentences, activeOrd],
  );

  const run = useCallback(
    async (label: string, action: () => Promise<void>) => {
      setBusy(true);
      setNotice(null);
      try {
        await action();
        setNotice(`${label} 完成`);
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
      playback.playRange(sentence.start_ms, sentence.end_ms);
    },
    [playback],
  );

  const playPrevious = useCallback(() => {
    const index = boot.sentences.findIndex((s) => s.ord === activeOrd);
    const target = boot.sentences[Math.max(0, index - 1)] ?? boot.sentences[0];
    if (target) playSentence(target);
  }, [activeOrd, boot.sentences, playSentence]);

  const playNext = useCallback(() => {
    const index = boot.sentences.findIndex((s) => s.ord === activeOrd);
    const target = boot.sentences[Math.min(boot.sentences.length - 1, index + 1)];
    if (target) playSentence(target);
  }, [activeOrd, boot.sentences, playSentence]);

  const applyBoundary = useCallback(
    async (action: "new_sentence" | "merge" | "to_prev" | "to_next") => {
      if (!selection) return;
      const from = Math.min(selection.from, selection.to);
      const to = Math.max(selection.from, selection.to);
      await run("边界调整", async () => {
        await api.boundary(boot.version.id, action, from, to);
        await onReload();
        onSelectionChange(null);
      });
    },
    [boot.version.id, onReload, onSelectionChange, run, selection],
  );

  const applyTextEdit = useCallback(
    async (proposed: string) => {
      if (!selection) return;
      const from = Math.min(selection.from, selection.to);
      const to = Math.max(selection.from, selection.to);
      await run("文字修改", async () => {
        await api.editText(boot.version.id, from, to, proposed);
        await onReload();
        onSelectionChange(null);
      });
    },
    [boot.version.id, onReload, onSelectionChange, run, selection],
  );

  // Keyboard: space plays the active sentence, brackets move by sentence.
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;

      if (event.code === "Space") {
        event.preventDefault();
        if (playback.playing) playback.pause();
        else if (activeOrd >= 0) {
          const sentence = boot.sentences.find((s) => s.ord === activeOrd);
          if (sentence) playSentence(sentence);
        } else playback.play();
      } else if (event.key === "[") {
        event.preventDefault();
        playPrevious();
      } else if (event.key === "]") {
        event.preventDefault();
        playNext();
      } else if (event.key === "Escape") {
        onSelectionChange(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeOrd, boot.sentences, onSelectionChange, playNext, playPrevious, playSentence, playback]);

  const selectionCount = selection
    ? Math.abs(selection.to - selection.from)
    : 0;

  return (
    <main className="desk">
      <header className="topbar">
        <div className="title">
          <strong>{boot.projectName}</strong>
          <span className="muted">
            v{boot.version.version} · {boot.version.sentence_count} 句 ·{" "}
            {boot.version.token_count} 字 · {formatDuration(boot.version.duration_ms)}
          </span>
        </div>
        <div className="transport">
          <span className="clock">{formatTime(playback.positionMs)}</span>
          <button type="button" onClick={playback.playing ? playback.pause : playback.play}>
            {playback.playing ? "⏸ 暂停" : "▶ 播放"}
          </button>
          <button type="button" className="ghost" onClick={playPrevious} title="上一句（[）">
            ⟨ 上一句
          </button>
          <button type="button" className="ghost" onClick={playNext} title="下一句（]）">
            下一句 ⟩
          </button>
        </div>
      </header>

      <Waveform
        projectId={boot.projectId}
        mediaId={boot.mediaId}
        durationMs={boot.version.duration_ms}
        positionMs={playback.positionMs}
        markers={markers}
        onSeek={playback.seek}
      />

      <div className="actionbar">
        <span className="muted">
          {selectionCount > 0
            ? `已选 ${selectionCount} 字${withinSentence ? `（第 ${withinSentence.ord + 1} 句）` : "（跨句）"}`
            : "选中文字后可以重划句边界"}
        </span>
        <div className="actions">
          <button
            type="button"
            disabled={!selection || busy}
            onClick={() => void applyBoundary("new_sentence")}
          >
            另起一句
          </button>
          <button
            type="button"
            disabled={!selection || busy}
            onClick={() => void applyBoundary("to_prev")}
          >
            并入上句
          </button>
          <button
            type="button"
            disabled={!selection || busy}
            onClick={() => void applyBoundary("to_next")}
          >
            并入下句
          </button>
          <button
            type="button"
            disabled={!selection || busy}
            onClick={() => void applyBoundary("merge")}
          >
            合并选区
          </button>
          <button
            type="button"
            className="ghost"
            disabled={busy}
            onClick={() =>
              void run("撤销", async () => {
                await api.undo(boot.version.id);
                await onReload();
              })
            }
          >
            撤销
          </button>
        </div>
      </div>

      {notice ? <div className="notice">{notice}</div> : null}

      <SentenceList
        sentences={boot.sentences}
        tokens={boot.tokens}
        positionMs={playback.positionMs}
        selection={selection}
        onSelectionChange={onSelectionChange}
        onPlaySentence={playSentence}
        onSeek={playback.seek}
        onEditSelection={applyTextEdit}
      />
    </main>
  );
}

function describe(caught: unknown): string {
  if (caught instanceof RequestFailed) {
    return caught.detail.hint
      ? `${caught.detail.message}\n${caught.detail.hint}`
      : caught.detail.message;
  }
  return caught instanceof Error ? caught.message : String(caught);
}
