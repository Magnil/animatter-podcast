/**
 * 音频导出面板（第 135 轮）。
 *
 * 用户要的三件事：起个名、选个文件夹、选格式。前两件和视频面板一样；格式做成**下拉**，
 * 跟文件名并排，因为它是同一行里的第三个决定，单独占一行会让面板看起来比实际复杂。
 *
 * **没有进度条，也没有取消。** 这是有意的：一小时的 M4A 导出是十几秒级的事（`-c copy`
 * 加上按 EDL 重切 PCM），进度条还没走明白就结束了，而「取消」在这个时长上没有意义。
 * 视频那边要进度条是因为它是十八分钟。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { defaultExportName, deliveryNotice } from "./exportPanels";

interface Props {
  open: boolean;
  episode: string;
  versionId: string;
  format: "m4a" | "wav";
  onFormatChanged: (format: "m4a" | "wav") => void;
  onClose: () => void;
  onNotice: (message: string) => void;
  /** Called once the file is on disk, so the workspace can offer 试听成片. */
  onExported: (url: string, durationMs: number) => void;
}

export function AudioExportDialog(props: Props): React.JSX.Element | null {
  const { open, episode, versionId, onClose, onNotice } = props;
  const [filename, setFilename] = useState("");
  const [folder, setFolder] = useState("");
  const [format, setFormat] = useState<"m4a" | "wav">(props.format);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setFilename(defaultExportName(episode, new Date()));
    setFolder("");
    setFormat(props.format);
    setError(null);
    setBusy(false);
  }, [open, episode, props.format]);

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

  const exportAudio = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      let job = await api.render(versionId, { lossless: format === "wav" });
      // Same poll loop as the film, minus the progress bar: the wait is short, and the only
      // thing the user needs is the moment it becomes a file.
      for (;;) {
        if (job.status === "done") break;
        if (job.status === "failed") throw new Error(job.error ?? "音频导出失败");
        await new Promise((resolve) => setTimeout(resolve, 400));
        job = await api.renderStatus(job.id);
      }
      const saved = await api.saveRenderTo(job.id, folder, filename);
      onNotice(deliveryNotice(saved.path, saved.bytes));
      props.onExported(job.url, job.duration_ms);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }, [versionId, format, folder, filename, onNotice, onClose, props]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => !busy && onClose()}>
      <div
        className="modal modal-narrow"
        role="dialog"
        aria-modal="true"
        aria-label="音频导出"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>音频导出</h2>
          <button type="button" className="ghost" onClick={onClose} disabled={busy} aria-label="关闭">
            ✕
          </button>
        </header>

        <div className="modal-body modal-single">
          {error ? <p className="error-text">{error}</p> : null}

          {/* 文件名和格式并排：两个都是「这个文件叫什么」，一起看才不会写出
              「成片.wav」却选了 M4A 这种情况。 */}
          <div className="field-row">
            <label className="field grow">
              <span>文件名</span>
              <input
                autoFocus
                value={filename}
                placeholder="成片-20261120"
                onChange={(event) => setFilename(event.target.value)}
                disabled={busy}
              />
            </label>
            <label className="field">
              <span>格式</span>
              <select
                value={format}
                onChange={(event) => {
                  const next = event.target.value === "wav" ? "wav" : "m4a";
                  setFormat(next);
                  props.onFormatChanged(next);
                  void api.saveSettings({ export_format: next }).catch(() => undefined);
                }}
                disabled={busy}
              >
                <option value="m4a">M4A（有损 192k）</option>
                <option value="wav">WAV（无损）</option>
              </select>
            </label>
          </div>

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
                disabled={busy}
              />
              <button type="button" className="ghost" onClick={() => void pick()} disabled={busy || picking}>
                {picking ? "选择中…" : "选择…"}
              </button>
            </div>
          </label>
        </div>

        {/* No 取消 here on purpose (round 135): the ✕ in the header closes the panel, and a
            footer 取消 next to 导出 reads like "cancel the export" — an action this panel does
            not have (audio export takes seconds, so there is nothing to interrupt). */}
        <footer className="modal-foot">
          <button
            type="button"
            disabled={busy || !filename.trim()}
            onClick={() => void exportAudio()}
          >
            {busy ? "导出中…" : "导出"}
          </button>
        </footer>
      </div>
    </div>
  );
}
