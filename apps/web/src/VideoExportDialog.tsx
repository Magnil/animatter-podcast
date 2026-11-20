/**
 * 视频导出面板（第 135 轮）。
 *
 * 这个面板取代了原来的三件东西：
 *
 * 1. 菜单里那一项点了就直接开跑 —— 文件名和文件夹无法选择，交付永远落在下载目录；
 * 2. 菜单里两个「视频导出：加速 / 编码快一点」开关 —— 它们和「导出成片视频」并排，
 *    看起来像三个平级动作，实际上是「导出」+「导出怎么跑」，混在一起没人分得清；
 * 3. 菜单项上那行「正在导出成片视频… 41%」—— 进度挤在菜单项里，菜单一关就看不见。
 *
 * 现在：一个对话框说清「导出到哪、叫什么、怎么跑」，点「导出」开始，同一个按钮变成
 * 「停止」，下方是进度条。
 *
 * **停止是真的停。** `取消` 会调 `POST /video/exports/{id}/cancel`，服务端让编码线程在
 * 200 ms 内杀掉正在跑的那个 ffmpeg（`media/video._run` 的轮询），终态写成 `cancelled`。
 * 不是在块之间挂个标记——一块要 200 s，那样「停止」会有三分钟没反应。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type VideoExport } from "./api";
import {
  IDLE,
  defaultExportName,
  deliveryNotice,
  isRunning,
  primaryDisabled,
  primaryLabel,
  progressView,
  type ExportState,
} from "./exportPanels";
import { percentOf, pollDelayMs, renderEta } from "./renderProgress";
import { setExportRunning as markExportRunning } from "./mediaDeck";

interface Props {
  open: boolean;
  episode: string;
  versionId: string;
  fontSize: number;
  marginV: number;
  /** The two export costs, as last saved. Editable here and remembered. */
  faster: boolean;
  fastEncode: boolean;
  onClose: () => void;
  onNotice: (message: string) => void;
  onSettingsChanged: (values: { video_faster?: boolean; video_fast_encode?: boolean }) => void;
  /**
   * Reported up so the 导出 menu can say 「正在导出成片视频…」 and stay disabled.
   *
   * Only the boolean goes up: the progress stays here, otherwise the whole shell would
   * re-render on every poll of an 18-minute encode.
   */
  onRunningChanged: (running: boolean) => void;
}

export function VideoExportDialog(props: Props): React.JSX.Element | null {
  const { open, episode, versionId, onClose, onNotice, onRunningChanged } = props;
  const [filename, setFilename] = useState("");
  const [folder, setFolder] = useState("");
  const [faster, setFaster] = useState(props.faster);
  const [fastEncode, setFastEncode] = useState(props.fastEncode);
  const [state, setState] = useState<ExportState>(IDLE);
  const [eta, setEta] = useState("正在估算…");
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  /** Set on unmount / close so a poll loop that outlived the dialog stops writing state. */
  const alive = useRef(true);

  useEffect(() => {
    if (!open) return;
    // Fresh panel each opening: the name defaults to this episode and today's date, and a
    // previous run's progress must not be shown for a job that no longer exists.
    setFilename(defaultExportName(episode, new Date()));
    setFolder("");
    setState(IDLE);
    setError(null);
    setEta("正在估算…");
    setFaster(props.faster);
    setFastEncode(props.fastEncode);
    alive.current = true;
    return () => {
      alive.current = false;
      // Closing the panel does not stop the encode, but the menu must not claim a job is
      // running after the panel that owns it has gone: the flag is only ever set by a panel
      // that is on screen.
      onRunningChanged(false);
    };
  }, [open, episode, props.faster, props.fastEncode, onRunningChanged]);

  const run = useCallback(async () => {
    setError(null);
    // Clear the previous run's terminal state before queueing a new one: otherwise the bar
    // still says 「已停止」 between the click and the first poll, which reads as "the click did
    // nothing". The text fields keep whatever the user typed.
    setState(IDLE);
    setEta("正在估算…");
    const startedAt = Date.now();
    let job: VideoExport;
    try {
      job = await api.exportVideo(versionId, {
        burn_subtitles: true,
        font_size: props.fontSize,
        margin_v: props.marginV,
        faster,
        fast_encode: fastEncode,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      return;
    }
    markExportRunning(true);
    onRunningChanged(true);
    setState({ job: { ...job, note: job.note ?? "" }, stopping: false });
    let lastShown = -1;
    try {
      for (;;) {
        if (job.status === "done") {
          setState({ job: { ...job, progress: 1 }, stopping: false });
          await deliver(job.id, filename, folder, onNotice);
          return;
        }
        if (job.status === "failed") {
          setState({ job, stopping: false });
          return;
        }
        if (job.status === "cancelled") {
          setState({ job, stopping: false });
          return;
        }
        const percent = percentOf(job.progress);
        if (percent !== lastShown) {
          lastShown = percent;
          setEta(renderEta(Date.now() - startedAt, job.progress));
          if (alive.current) setState({ job, stopping: false });
        } else if (alive.current) {
          // Keep the note fresh without re-rendering on every poll: the server publishes it a
          // moment after queueing, once it has counted the chunks and the free memory.
          setState((current) => (current.job === null ? current : { job, stopping: current.stopping }));
        }
        await new Promise((resolve) => setTimeout(resolve, pollDelayMs(Date.now() - startedAt)));
        job = await api.videoExport(job.id);
      }
    } catch (caught) {
      if (alive.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      markExportRunning(false);
      onRunningChanged(false);
      if (alive.current) {
        // Clear the 「正在停止」 flag if the loop ended some other way (a poll error), so the
        // button is not stuck on 「正在停止…」 with nothing stopping.
        setState((current) => (current.stopping ? { job: current.job, stopping: false } : current));
      }
    }
  }, [versionId, props.fontSize, props.marginV, faster, fastEncode, filename, folder, onNotice]);

  const stop = useCallback(async () => {
    const job = state.job;
    if (job === null) return;
    setState({ job, stopping: true });
    try {
      await api.cancelVideoExport(job.id);
    } catch (caught) {
      // A cancel that races the last chunk is not an error worth a red box; the poll loop
      // reports the real outcome either way.
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  }, [state.job, onNotice]);

  const pick = useCallback(async () => {
    setPicking(true);
    try {
      const picked = await api.pickFolder(folder);
      setFolder(picked.path);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPicking(false);
    }
  }, [folder]);

  const remember = useCallback(
    (next: { video_faster?: boolean; video_fast_encode?: boolean }) => {
      props.onSettingsChanged(next);
      void api.saveSettings(next).catch(() => undefined);
    },
    [props],
  );

  if (!open) return null;

  const view = progressView(state, eta);
  // Two different questions, and conflating them was a real bug: `hasResult` is "there is
  // something to show" (the bar keeps saying 「已停止」 after a stop), `running` is "an encode
  // is moving" (inputs locked). Keying the lock off the first left every field disabled
  // forever after one stop, so the user had to close and reopen the panel to change a name.
  const hasResult = state.job !== null;
  const running = isRunning(state);
  const disabled = primaryDisabled(state, !filename.trim());

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => !running && onClose()}>
      <div
        className="modal modal-narrow"
        role="dialog"
        aria-modal="true"
        aria-label="视频导出"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>视频导出</h2>
          <button
            type="button"
            className="ghost"
            onClick={onClose}
            disabled={running}
            aria-label="关闭"
          >
            ✕
          </button>
        </header>

        <div className="modal-body modal-single">
          {error ? <p className="error-text">{error}</p> : null}

          <label className="field">
            <span>文件名</span>
            <input
              autoFocus
              value={filename}
              placeholder="成片-20261120"
              onChange={(event) => setFilename(event.target.value)}
              disabled={running}
            />
          </label>

          <label className="field">
            <span>
              导出到
              <em className="hint">留空表示系统的「下载」文件夹</em>
            </span>
            <div className="field-row">
              <input
                value={folder}
                placeholder="D:/导出"
                onChange={(event) => setFolder(event.target.value)}
                disabled={running}
              />
              <button type="button" className="ghost" onClick={() => void pick()} disabled={running || picking}>
                {picking ? "选择中…" : "选择…"}
              </button>
            </div>
          </label>

          <p className="section-note">MP4 1080p，字幕烧进画面。后缀由格式决定，不用手写。</p>

          <label className="field checkbox">
            <input
              type="checkbox"
              checked={faster}
              onChange={(event) => {
                setFaster(event.target.checked);
                remember({ video_faster: event.target.checked });
              }}
              disabled={running}
            />
            <span>
              并行编码（需至少 8.3 G 内存）
              <em className="hint">不够时自动退回串行，进度行会写明原因</em>
            </span>
          </label>

          <label className="field checkbox">
            <input
              type="checkbox"
              checked={fastEncode}
              onChange={(event) => {
                setFastEncode(event.target.checked);
                remember({ video_fast_encode: event.target.checked });
              }}
              disabled={running}
            />
            <span>
              快速编码（文件更大）
              <em className="hint">约大 8 %，画面不再逐位相同</em>
            </span>
          </label>

          {hasResult ? (
            <div className="export-progress" data-tone={view.tone} aria-live="polite">
              <div className="export-progress-track">
                <div className="export-progress-fill" style={{ width: `${view.percent}%` }} />
              </div>
              <p className="export-progress-text">{view.text}</p>
            </div>
          ) : null}
        </div>

        {/* One button, because there is one decision: 导出 starts it, 停止 ends it. A footer
            取消 next to 停止 would be two ways to say "no" — one closes the panel and lets the
            encode run, the other kills it — and they read identically. Closing is the ✕. */}
        <footer className="modal-foot">
          <button
            type="button"
            className={running ? "danger" : undefined}
            disabled={running ? state.stopping : disabled}
            onClick={() => void (running ? stop() : run())}
          >
            {primaryLabel(state)}
          </button>
        </footer>
      </div>
    </div>
  );
}

/**
 * Copy the finished film to the folder and name the user chose, then say where it went.
 *
 * The copy happens **server-side**: this app's server runs on the user's machine, and a
 * 900 MB film should not have to pass through browser memory to reach a folder they already
 * named. A delivery that fails is reported but does not pretend the encode failed — the film
 * is still there.
 */
async function deliver(
  renderId: string,
  filename: string,
  folder: string,
  onNotice: (message: string) => void,
): Promise<void> {
  try {
    const saved = await api.saveRenderTo(renderId, folder, filename);
    onNotice(deliveryNotice(saved.path, saved.bytes));
  } catch (caught) {
    onNotice(
      `成片已编码，但保存到文件夹失败：${caught instanceof Error ? caught.message : String(caught)}`,
    );
  }
}
