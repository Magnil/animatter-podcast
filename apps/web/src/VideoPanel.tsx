/**
 * Video area: template confirm, timeline blocks, still preview, export (P1).
 *
 * **Collapsed by default.** Most sessions are audio-only; reserving vertical space
 * above the waveform for a feature nobody has switched on is a tax on every edit.
 * Collapsed it is one row — title plus a summary that answers "is this project using
 * video?" without opening anything (see `summaryLine`).
 *
 * **The preview is a still.** v1 deliberately does not play the film: a `<video>`
 * would need its own clock, its own buffering states and its own sync with the
 * audio playhead, and the thing the user actually needs from a preview is "does the
 * layout look right". A frame costs ~200 ms, so it can follow the playhead.
 *
 * **Two lengths on every block.** A block spans its own edges on the timeline, but
 * plays for `output_ms` in the film because audio inside it may have been cut. That
 * pair is the single most important number here: showing only one makes the user
 * believe their 10-second clip became 8 seconds for no reason.
 *
 * **Nothing on this screen rejects an audio edit.** A block whose audio was deleted in
 * full is marked 「不含音频」 and offered two actions; the edit itself already happened
 * and undoing the user's cut on their behalf would be worse than a dead block.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type VideoItem, type VideoTemplate, type VideoTimeline } from "./api";
import { formatDuration, formatTime } from "./format";
import {
  galleryCaption,
  lengthCaption,
  orphanedBlocks,
  summaryLine,
} from "./videoTimeline";

interface Props {
  projectId: string;
  versionId: string;
  /** Playhead on the **original** timeline; the preview is asked for output time. */
  positionMs: number;
  hasDeletions: boolean;
  onNotice: (message: string) => void;
}

const FITS: { id: string; label: string; hint: string }[] = [
  { id: "blur_fill", label: "模糊填充", hint: "竖图放进 16:9 框：模糊放大作底 + 完整居中" },
  { id: "contain", label: "完整显示", hint: "留边，不裁切" },
  { id: "cover", label: "填满裁切", hint: "铺满，裁掉溢出" },
  { id: "stretch", label: "拉伸", hint: "会变形" },
];

export function VideoPanel({
  projectId,
  versionId,
  positionMs,
  hasDeletions,
  onNotice,
}: Props): React.JSX.Element {
  const [template, setTemplate] = useState<VideoTemplate | null>(null);
  const [timeline, setTimeline] = useState<VideoTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [importPath, setImportPath] = useState("");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewAt, setPreviewAt] = useState(0);
  const [exporting, setExporting] = useState<{ id: string; percent: number; url: string } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [nextTemplate, nextTimeline] = await Promise.all([
        api.videoTemplate(projectId),
        api.videoTimeline(versionId),
      ]);
      setTemplate(nextTemplate);
      setTimeline(nextTimeline);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [projectId, versionId]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Poll while a compile is running.
   *
   * A compile is ~6 s on a real PSD and there is no progress to report, so the only
   * honest thing the UI can say is 「编译中…」 — but it has to say it until it ends,
   * or the screen sits on 「未启用」 after a successful import.
   */
  useEffect(() => {
    if (template === null) return;
    if (template.status !== "pending" && template.status !== "compiling") return;
    const timer = window.setTimeout(() => void load(), 1200);
    return () => window.clearTimeout(timer);
  }, [template, load]);

  const orphans = useMemo(() => (timeline ? orphanedBlocks(timeline.items) : []), [timeline]);

  const doImport = async (): Promise<void> => {
    if (!importPath.trim()) return;
    setBusy(true);
    try {
      const next = await api.importVideoTemplate(projectId, importPath.trim());
      setTemplate(next);
      setImportPath("");
      onNotice(
        next.status === "ready"
          ? "模板已编译完成"
          : next.status === "failed"
            ? `模板编译失败：${next.error}`
            : "模板已导入，正在编译…",
      );
      await load();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doRecompile = async (): Promise<void> => {
    setBusy(true);
    try {
      setTemplate(await api.recompileVideoTemplate(projectId));
      onNotice("正在重新编译模板…");
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doFit = async (fit: string): Promise<void> => {
    try {
      setTemplate(await api.updateVideoTemplate(projectId, fit));
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const doPreview = async (): Promise<void> => {
    if (!timeline) return;
    setBusy(true);
    try {
      const result = await api.videoPreview(versionId, positionMs);
      setPreviewUrl(api.previewUrl(projectId, result.url.split("/").pop() ?? ""));
      setPreviewAt(positionMs);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doExport = async (): Promise<void> => {
    setBusy(true);
    try {
      const started = await api.exportVideo(versionId, { burn_subtitles: true });
      setExporting({ id: started.id, percent: 0, url: "" });
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  // Poll the export while it runs. A one-hour film is ~14 minutes of x264, so a
  // spinner with no number is indistinguishable from a dead button.
  useEffect(() => {
    if (!exporting || exporting.percent >= 100) return;
    const timer = window.setTimeout(async () => {
      try {
        const state = await api.videoExport(exporting.id);
        if (state.status === "failed") {
          onNotice(`导出失败：${state.error ?? "未知原因"}`);
          setExporting(null);
          return;
        }
        if (state.status === "done") {
          setExporting({ id: state.id, percent: 100, url: state.url });
          onNotice("视频导出完成");
          return;
        }
        setExporting({ ...exporting, percent: Math.round(state.progress * 100) });
      } catch {
        setExporting(null);
      }
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [exporting, onNotice]);

  const doDeleteItem = async (item: VideoItem): Promise<void> => {
    try {
      await api.deleteVideoItem(item.id);
      await load();
      onNotice("素材块已删除");
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * Move an orphaned block onto the next slot that still has audio.
   *
   * The action has to be **deterministic and explainable**, not clever: pick the next
   * two drag targets after the block's start and put the block there. Anything
   * smarter (searching for a gap of the same length, say) would move the block to a
   * place the user did not ask for and could not predict, and this button exists only
   * to get them unstuck — dragging is the real tool.
   */
  const doNudge = async (item: VideoItem): Promise<void> => {
    if (!timeline) return;
    const targets = timeline.grid.targets;
    const later = targets.filter((point) => point > item.start_ms);
    if (later.length < 2) {
      onNotice("后面的素材块位置不够了，把播放头挪到别处再试");
      return;
    }
    const start = later[0] ?? item.start_ms;
    const end = later[1] ?? item.end_ms;
    try {
      await api.patchVideoItem(item.id, { start_ms: start, end_ms: end });
      await load();
      onNotice(`已把素材块挪到 ${formatTime(start)}`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  return (
    <div className="panel video-panel">
      <div className="panel-head">
        <h3>视频</h3>
        <span className="muted small">
          {summaryLine(template?.status ?? null, timeline?.items.length ?? 0, orphans.length)}
        </span>
      </div>

      {error ? <p className="error-text">{error}</p> : null}

      {template === null || template.status === "failed" ? (
        <div className="video-import">
          <label className="field">
            <span>PSD 模板路径</span>
            <input
              type="text"
              value={importPath}
              placeholder="D:/templates/播客模板.psd"
              onChange={(event) => setImportPath(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void doImport();
              }}
            />
          </label>
          <p className="section-note">
            16:9 的 PSD。导入时会把模板压平成背景，PSD 里的文字不会跟着改——
            文字由本程序另画。
          </p>
          {template?.status === "failed" ? (
            <p className="error-text">{template.error}</p>
          ) : null}
          <div className="edl-actions">
            <button type="button" disabled={busy || !importPath.trim()} onClick={() => void doImport()}>
              导入并编译
            </button>
            {template ? (
              <button type="button" className="ghost" disabled={busy} onClick={() => void doRecompile()}>
                重新编译
              </button>
            ) : null}
          </div>
        </div>
      ) : template.status === "pending" || template.status === "compiling" ? (
        <p className="section-note">正在编译模板（约 6 秒）…</p>
      ) : (
        <div className="video-ready">
          <div className="video-preview-box">
            {previewUrl ? (
              <img src={previewUrl} alt={`预览 ${formatTime(previewAt)}`} />
            ) : (
              <p className="muted small">还没有预览。把播放头停在想检查的位置，点「生成静帧」。</p>
            )}
          </div>
          <div className="edl-actions">
            <button type="button" disabled={busy} onClick={() => void doPreview()}>
              生成静帧
            </button>
            <span className="muted small">预览为近似：成片 1080p，预览更大字号一致</span>
          </div>

          <details className="video-box-info">
            <summary>
              版式：素材框 {template.box_rect.w}×{template.box_rect.h} ·{" "}
              {template.slots.avatars?.length ?? 0} 个头像位
            </summary>
            <ul>
              {(template.slots.notes ?? []).map((note, index) => (
                <li key={index}>{note}</li>
              ))}
              {(template.slots.avatars ?? []).map((slot) => (
                <li key={slot.name}>
                  {slot.name}：{slot.label || "（无名牌）"} @ {slot.rect.x},{slot.rect.y}
                </li>
              ))}
            </ul>
            <label className="field">
              <span>框内默认填充</span>
              <select
                value={template.box_fit_default}
                onChange={(event) => void doFit(event.target.value)}
              >
                {FITS.map((fit) => (
                  <option key={fit.id} value={fit.id}>
                    {fit.label} — {fit.hint}
                  </option>
                ))}
              </select>
            </label>
          </details>

          {orphans.length > 0 ? (
            <div className="video-orphans">
              <p className="error-text">
                {orphans.length} 个素材块对应的音频已删除，成片里不会有它们。
              </p>
              <ul>
                {orphans.map((item) => (
                  <li key={item.id}>
                    {formatTime(item.start_ms)} ·{" "}
                    <button type="button" className="ghost small" onClick={() => void doNudge(item)}>
                      往后挪一秒
                    </button>
                    <button type="button" className="ghost small" onClick={() => void doDeleteItem(item)}>
                      删掉这个块
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="video-items">
            {timeline && timeline.items.length === 0 ? (
              <p className="muted small">还没有素材块。素材库和拖拽在 P2。</p>
            ) : (
              <ul>
                {(timeline?.items ?? []).map((item) => (
                  <li key={item.id} className={item.orphaned ? "video-item is-orphan" : "video-item"}>
                    <span className="video-item-when">{formatTime(item.start_ms)}</span>
                    <span className="video-item-len">{lengthCaption(item)}</span>
                    <span className="muted small">
                      {item.kind === "gallery"
                        ? galleryCaption(item, JSON.parse(item.gallery_json || "[]").length)
                        : item.fit}
                    </span>
                    <button type="button" className="ghost small" onClick={() => void doDeleteItem(item)}>
                      删
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="edl-actions">
            <button
              type="button"
              disabled={busy || !hasDeletions || exporting !== null}
              title={hasDeletions ? "" : "先删除要剪掉的内容，视频用成片音频"}
              onClick={() => void doExport()}
            >
              {exporting ? `导出中… ${exporting.percent}%` : "导出视频"}
            </button>
            {exporting && exporting.url ? (
              <a className="button-link" href={exporting.url} download>
                下载成片
              </a>
            ) : null}
            {timeline ? (
              <span className="muted small">
                成片 {formatDuration(timeline.output_duration_ms)} / 原始{" "}
                {formatDuration(timeline.original_duration_ms)}
              </span>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
