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
import {
  api,
  type Sentence,
  type SpeakerInfo,
  type VideoAvatar,
  type VideoItem,
  type VideoTemplate,
  type VideoTimeline,
} from "./api";
import { AssetLibrary } from "./AssetLibrary";
import { CollapsiblePanel } from "./CollapsiblePanel";
import { DeckPanel } from "./DeckPanel";
import { formatDuration, formatTime } from "./format";
import { VideoTrack } from "./VideoTrack";
import {
  galleryCaption,
  lengthCaption,
  dropRange,
  orphanedBlocks,
  summaryLine,
  viewAround,
  type View,
} from "./videoTimeline";

interface Props {
  projectId: string;
  versionId: string;
  /** Playhead on the **original** timeline; the preview is asked for output time. */
  positionMs: number;
  hasDeletions: boolean;
  /** Sentences of this version — the deck's proofreading pane and its page starts. */
  sentences: Sentence[];
  onSeek: (ms: number) => void;
  onNotice: (message: string) => void;
}

const FITS: { id: string; label: string; hint: string }[] = [
  { id: "blur_fill", label: "模糊填充", hint: "竖图放进 16:9 框：模糊放大作底 + 完整居中" },
  { id: "contain", label: "完整显示", hint: "留边，不裁切" },
  { id: "cover", label: "填满裁切", hint: "铺满，裁掉溢出" },
  { id: "stretch", label: "拉伸", hint: "会变形" },
];

/** How many images a gallery block holds, tolerating a half-edited string. */
function galleryCount(item: VideoItem): number {
  try {
    const parsed = JSON.parse(item.gallery_json || "[]") as unknown;
    return Array.isArray(parsed) ? parsed.length : 0;
  } catch {
    return 0;
  }
}

export function VideoPanel({
  projectId,
  versionId,
  positionMs,
  hasDeletions,
  sentences,
  onSeek,
  onNotice,
}: Props): React.JSX.Element {
  const [template, setTemplate] = useState<VideoTemplate | null>(null);
  const [timeline, setTimeline] = useState<VideoTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [importPath, setImportPath] = useState("");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewAt, setPreviewAt] = useState(0);
  const [fontSize, setFontSize] = useState(52);
  const [marginV, setMarginV] = useState(56);
  const [exporting, setExporting] = useState<{ id: string; percent: number; url: string } | null>(null);
  // P2: the library, the dragged asset, and the selected block.
  const [assetRevision, setAssetRevision] = useState(0);
  const [draggingAsset, setDraggingAsset] = useState<string | null>(null);
  const [selected, setSelected] = useState<VideoItem | null>(null);
  const [view, setView] = useState<View>({ from: 0, to: 60_000 });
  // P3: avatar slots, the speakers they can be bound to, and their talk time.
  const [avatars, setAvatars] = useState<VideoAvatar[]>([]);
  const [speakers, setSpeakers] = useState<SpeakerInfo[]>([]);

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
   * Avatar bindings load **separately** from the template.
   *
   * They come from two more endpoints (the slot list with talk time, and the project's
   * speakers), and a project without a template would 404 both — so a failure here must
   * not overwrite the template error the user actually needs to see.
   */
  const loadAvatars = useCallback(async () => {
    try {
      const [nextSlots, nextSpeakers] = await Promise.all([
        api.videoAvatars(versionId),
        api.speakers(projectId),
      ]);
      setAvatars(nextSlots);
      setSpeakers(nextSpeakers);
    } catch {
      setAvatars([]);
    }
  }, [projectId, versionId]);

  useEffect(() => {
    if (template?.status !== "ready") {
      setAvatars([]);
      return;
    }
    void loadAvatars();
  }, [template?.status, loadAvatars]);

  // Open on the renderer's real defaults instead of hard-coded numbers that would
  // drift the moment the ASS style changes.
  useEffect(() => {
    void api
      .videoSubtitleStyle()
      .then((style) => {
        setFontSize(style.font_size);
        setMarginV(style.margin_v);
      })
      .catch(() => undefined);
  }, []);

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

  const doDefaultFit = async (fit: string): Promise<void> => {
    try {
      setTemplate(await api.updateVideoTemplate(projectId, fit));
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * Bind one avatar slot, or unbind it.
   *
   * The response carries the new talk time, so the row updates from the server instead of
   * guessing — a slot bound to a speaker who never speaks reports 0 段, and that number is
   * the only way the user can tell a wrong binding from a broken render.
   */
  const doBind = async (slot: string, speakerId: number | null): Promise<void> => {
    try {
      const updated = await api.bindVideoAvatar(projectId, slot, versionId, speakerId);
      setAvatars((current) => current.map((item) => (item.name === slot ? updated : item)));
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const doPreview = async (): Promise<void> => {
    if (!timeline) return;
    setBusy(true);
    try {
      const result = await api.videoPreview(versionId, positionMs, {
        font_size: fontSize,
        margin_v: marginV,
      });
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
      const started = await api.exportVideo(versionId, {
        burn_subtitles: true,
        font_size: fontSize,
      });
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
      if (selected?.id === item.id) setSelected(null);
      await load();
      onNotice("素材块已删除");
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * A block was dragged, resized, or dropped onto.
   *
   * Patching one field at a time would leave the block in a state the server never sees
   * (start moved, end not yet), and the anchor rules would then re-seat it twice. So the
   * two edges always travel together.
   */
  const doMove = async (item: VideoItem, startMs: number, endMs: number): Promise<void> => {
    try {
      await api.patchVideoItem(item.id, { start_ms: startMs, end_ms: endMs });
      await load();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
      await load();
    }
  };

  /**
   * Drop an asset onto the track: create a block covering the sentences it landed on.
   *
   * The range comes from `dropRange`, which snaps to the grid and refuses to overlap an
   * existing block — an overlapping block would be mostly trimmed away by the chain and
   * read as "the drop did nothing".
   */
  const doDropAsset = async (assetId: string, atMs: number): Promise<void> => {
    if (!timeline) return;
    const range = dropRange(atMs, timeline.grid.targets, timeline.grid.duration_ms, timeline.items);
    if (range === null) {
      onNotice("这里放不下新的素材块，换一个位置再拖");
      return;
    }
    try {
      const created = await api.addVideoItem(versionId, {
        kind: "media",
        start_ms: range.start_ms,
        end_ms: range.end_ms,
        asset_id: assetId,
        fit: template?.box_fit_default ?? "blur_fill",
      });
      setSelected(created);
      await load();
      onNotice(`已加入素材块 ${formatTime(range.start_ms)}–${formatTime(range.end_ms)}`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * Create a slide block covering one page's stretch (P4).
   *
   * The edges are **not** snapped: the page's own ``start_ms`` is already a sentence start
   * (it came from one), and snapping again would move the page switch off the sentence it
   * was generated from. The end is the next page's start, which is also a sentence start.
   */
  const doInsertSlideBlock = async (
    deckId: string,
    startMs: number,
    endMs: number,
  ): Promise<void> => {
    if (!timeline) return;
    if (endMs <= startMs) {
      onNotice("这一页的时长是 0，先给下一页一个更晚的起点");
      return;
    }
    try {
      const created = await api.addVideoItem(versionId, {
        kind: "slide",
        start_ms: startMs,
        end_ms: endMs,
        deck_id: deckId,
        fit: "contain",
        snap: false,
      });
      setSelected(created);
      await load();
      onNotice(`已放进素材框 ${formatTime(startMs)}–${formatTime(endMs)}`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /** Turn the selected block into a gallery, or add the current asset to it. */  const doMakeGallery = async (item: VideoItem, assetId: string): Promise<void> => {
    let members: { asset_id: string; weight?: number }[] = [];
    try {
      members = item.kind === "gallery" ? (JSON.parse(item.gallery_json) as typeof members) : [];
    } catch {
      members = [];
    }
    if (item.kind === "media" && item.asset_id) {
      members = [{ asset_id: item.asset_id }];
    }
    if (members.some((member) => member.asset_id === assetId)) {
      onNotice("这个素材已经在这个画廊里了");
      return;
    }
    members.push({ asset_id: assetId });
    try {
      await api.patchVideoItem(item.id, {
        kind: "gallery",
        gallery_json: JSON.stringify(members),
        asset_id: "",
      });
      await load();
      onNotice(`画廊现在有 ${members.length} 张`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const doFit = async (item: VideoItem, fit: string): Promise<void> => {
    try {
      await api.patchVideoItem(item.id, { fit });
      await load();
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
    <CollapsiblePanel
      title="视频剪辑"
      summary={summaryLine(template?.status ?? null, timeline?.items.length ?? 0, orphans.length)}
      className="video-panel"
      defaultCollapsed
    >
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
            <span className="muted small">预览为近似：成片 1080p，字号与位置一致</span>
          </div>

          {/*
            The style controls live next to the still, not in 设置: the whole point is
            to nudge a number and look at the frame. Nothing is saved until 导出 — a
            slider that wrote to the database on every drag would be both slow and a
            surprise when reopening the project.
          */}
          <div className="video-style">
            <label className="field">
              <span>
                字幕字号 <b>{fontSize}</b>
              </span>
              <input
                type="range"
                min={28}
                max={96}
                step={2}
                value={fontSize}
                onChange={(event) => setFontSize(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>
                底部留白 <b>{marginV}</b> px
              </span>
              <input
                type="range"
                min={0}
                max={160}
                step={4}
                value={marginV}
                onChange={(event) => setMarginV(Number(event.target.value))}
              />
            </label>
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
              {(template.slots.avatars ?? []).map((slot) => {
                const bound = avatars.find((item) => item.name === slot.name);
                return (
                  <li key={slot.name} className="video-avatar-row">
                    <span className="video-avatar-name">
                      {slot.name}
                      <small>{slot.label || "（无名牌）"}</small>
                    </span>
                    <select
                      value={bound?.speaker_id ?? ""}
                      onChange={(event) =>
                        void doBind(
                          slot.name,
                          event.target.value === "" ? null : Number(event.target.value),
                        )
                      }
                    >
                      <option value="">不绑定（一直不变）</option>
                      {speakers.map((speaker) => (
                        <option key={speaker.local_id} value={speaker.local_id}>
                          {speaker.name || `说话人 ${speaker.local_id}`}
                        </option>
                      ))}
                    </select>
                    <small className="video-avatar-talk">
                      {bound === undefined || bound.speaker_id === null
                        ? "—"
                        : bound.turns === 0
                          ? "0 段（这个人没说话）"
                          : `${bound.turns} 段 · ${formatDuration(bound.lit_ms)}`}
                    </small>
                  </li>
                );
              })}
            </ul>
            <label className="field">
              <span>框内默认填充</span>
              <select
                value={template.box_fit_default}
                onChange={(event) => void doDefaultFit(event.target.value)}
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
                      挪到下一处
                    </button>
                    <button type="button" className="ghost small" onClick={() => void doDeleteItem(item)}>
                      删掉这个块
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {timeline ? (
            <VideoTrack
              items={timeline.items}
              targets={timeline.grid.targets}
              durationMs={timeline.grid.duration_ms}
              view={view}
              positionMs={positionMs}
              draggingAssetId={draggingAsset}
              onSeek={(ms) => setView(viewAround(ms, view, timeline.grid.duration_ms))}
              onSelect={setSelected}
              onMove={(item, start, end) => void doMove(item, start, end)}
              onDropAsset={(assetId, atMs) => void doDropAsset(assetId, atMs)}
            />
          ) : null}

          <div className="video-items">
            {timeline && timeline.items.length === 0 ? (
              <p className="muted small">
                还没有素材块。从下面的素材库里把一个文件拖到时间轴上，或者拖给某个句子。
              </p>
            ) : (
              <ul>
                {(timeline?.items ?? []).map((item) => (
                  <li
                    key={item.id}
                    className={`video-item${item.orphaned ? " is-orphan" : ""}${
                      selected?.id === item.id ? " is-selected" : ""
                    }`}
                    onClick={() => setSelected(item)}
                  >
                    <span className="video-item-when">{formatTime(item.start_ms)}</span>
                    <span className="video-item-len">{lengthCaption(item)}</span>
                    <span className="muted small">
                      {item.kind === "gallery"
                        ? galleryCaption(item, galleryCount(item))
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

          {selected ? (
            <div className="video-selected">
              <p className="muted small">
                选中 {formatTime(selected.start_ms)}–{formatTime(selected.end_ms)}（
                {lengthCaption(selected)}）
              </p>
              <div className="edl-actions">
                <label className="field inline">
                  <span>填充</span>
                  <select
                    value={selected.fit}
                    onChange={(event) => void doFit(selected, event.target.value)}
                  >
                    {FITS.map((fit) => (
                      <option key={fit.id} value={fit.id}>
                        {fit.label}
                      </option>
                    ))}
                  </select>
                </label>
                {draggingAsset ? null : (
                  <span className="muted small">
                    拖一个素材到这里可以加进画廊
                    {selected.kind === "gallery" ? `（现在 ${galleryCount(selected)} 张）` : ""}
                  </span>
                )}
              </div>
            </div>
          ) : null}

          <AssetLibrary
            projectId={projectId}
            revision={assetRevision}
            onDragStateChange={setDraggingAsset}
            onAddToBlock={
              selected === null ? undefined : (assetId) => void doMakeGallery(selected, assetId)
            }
            onNotice={(message) => {
              // Importing or deleting an asset can change what a block can show, so the
              // library and the timeline are refreshed together — but only on a *count*
              // change, which the notice text is the cheapest signal for.
              if (message.includes("素材")) setAssetRevision((value) => value + 1);
              onNotice(message);
            }}
          />

          <details className="video-deck-section" open={false}>
            <summary>演示稿（从字幕分页，可逐页改）</summary>
            <DeckPanel
              projectId={projectId}
              versionId={versionId}
              sentences={sentences}
              positionMs={positionMs}
              onNotice={onNotice}
              onSeek={onSeek}
              onInsertBlock={(deckId, startMs, endMs) =>
                void doInsertSlideBlock(deckId, startMs, endMs)
              }
            />
          </details>

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
    </CollapsiblePanel>
  );
}
