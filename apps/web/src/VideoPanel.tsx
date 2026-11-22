/**
 * Video area: template confirm, timeline blocks, still preview (P1).
 *
 * **Export is not here.** A 导出视频 button plus a 下载成片 link plus a
 * 「成片 1 小时 1 分 / 原始 1 小时 1 分」 line used to sit at the bottom of this panel; all
 * three were removed in round 102. Delivering the film belongs with the other deliveries
 * (字幕 / ShowNotes / 文稿 / 成片音频) in the 导出 menu, and the two durations are the
 * 音频剪辑 panel's own summary repeated one panel higher. What this panel does is arrange
 * the film.
 *
 * **Collapsed by default.** Most sessions are audio-only; reserving vertical space
 * above the waveform for a feature nobody has switched on is a tax on every edit.
 * Collapsed it is one row — title plus a summary that answers "is this project using
 * video?" without opening anything (see `summaryLine`).
 *
 * **The preview is a still.** v1 deliberately does not play the film: a `<video>`
 * would need its own clock, its own buffering states and its own sync with the
 * audio playhead, and the thing the user actually needs from a preview is "does the
 * layout look right".
 *
 * Since round 128 it **follows the playhead on its own** (300 ms after it stops) instead of
 * waiting for a button press, and it converts the playhead from the **original** timeline to
 * **output** time before asking (round 128, §19.31.5 P0-1). Both are in this file because both
 * are about *when* to ask and *which instant* to ask for; the arithmetic itself is in
 * `timeMap.ts`.
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

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  api,
  type Sentence,
  type SpeakerInfo,
  type VideoAvatar,
  type VideoItem,
  type VideoPlan,
  type VideoTemplate,
  type VideoTimeline,
} from "./api";
import { AssetLibrary } from "./AssetLibrary";
import { DomStage } from "./DomStage";
import { CollapsiblePanel } from "./CollapsiblePanel";
import { DeckPanel } from "./DeckPanel";
import { TemplateTexts } from "./TemplateTexts";
import { LayoutDialog } from "./LayoutDialog";
import { formatTime } from "./format";
import { playheadClock } from "./playheadClock";
import { timeMapFrom, toOutputClamped, type Keep } from "./timeMap";
import { VideoTrack } from "./VideoTrack";
import { saveBlob } from "./download";
import { REPLACE_WARNING, filenameFromDisposition, packageSize } from "./templatePackage";
import type { View } from "./waveView";
import { FITS, blockName, dropRange, orphanedBlocks, summaryLine } from "./videoTimeline";

interface Props {
  projectId: string;
  /**
   * The project's name, for the template package's fallback filename.
   *
   * Only a fallback: the server names the download after the same project, and its
   * `Content-Disposition` wins. Passed anyway so a header-less response still produces
   * 「第 3 期.template」 instead of 「模板.template」.
   */
  projectName: string;
  versionId: string;
  /** Playhead on the **original** timeline; the preview is asked for output time. */
  positionMs: number;
  /**
   * The EDL's keep list — the original→output mapping.
   *
   * Passed down rather than fetched here because `App` already holds it for the transport
   * and the audio panel: a second fetch would be a second thing to keep in sync, and a
   * slightly different one would make the preview agree with neither.
   */
  keeps: Keep[];
  /**
   * Whether the audio is currently playing.
   *
   * The preview follows the playhead while it is moving, but not while it is playing: a still
   * costs ~0.65 s (§19.31.5), and asking for one per playhead update would queue up work behind
   * the audio and finish showing frames nobody is looking at.
   */
  playing: boolean;
  /** Sentences of this version — the deck's proofreading pane and its page starts. */
  sentences: Sentence[];
  /**
   * The burnt-in subtitle's two numbers, **controlled from `App`**.
   *
   * They used to be local state here, which was fine while the export button was also here.
   * The export moved to the 导出 menu (round 102) and the menu is rendered by `App`; a slider
   * whose value only the panel can see would be honoured by the still preview and silently
   * ignored by the film — the worst possible split, because the preview would agree with the
   * slider and the output would not.
   */
  fontSize: number;
  marginV: number;
  onFontSize: (value: number) => void;
  onMarginV: (value: number) => void;
  onSeek: (ms: number) => void;
  onNotice: (message: string) => void;
  /**
   * The **shared** zoom/scroll window — the same one the audio waveform uses.
   *
   * A separate window here was the whole 「不知道素材该放哪」 problem: the video track drew
   * its own 0–60 s window while the waveform underneath drew 0–61 min, so the block above
   * a given centimetre of screen was a different moment than the audio below it.
   */
  view: View;
  onZoom: (anchorMs: number, factor: number) => void;
  onPan: (from: number) => void;
}

/** The four things that used to be stacked; they are tabs now. */
const VIDEO_TABS: { id: TabId; label: string; hint: string }[] = [
  { id: "template", label: "模板", hint: "素材框尺寸、头像绑定、框内默认填充" },
  { id: "subtitle", label: "字幕", hint: "烧进画面的字幕字号与底部留白" },
  { id: "library", label: "素材库", hint: "导入图片/视频，拖到视频轴上" },
  { id: "deck", label: "演示稿", hint: "从字幕分页，逐页改标题正文" },
];

type TabId = "template" | "subtitle" | "library" | "deck";

/**
 * The real-time stage, subscribed to the 50 ms playhead clock.
 *
 * Separated from `VideoPanel` deliberately: only this component re-renders 20 times a second, and
 * everything it needs (the plan, the time map, the playing flag) is stable across those renders.
 */
function LiveDomStage({
  plan,
  map,
  playing,
  onNotice,
}: {
  plan: VideoPlan;
  map: ReturnType<typeof timeMapFrom>;
  playing: boolean;
  onNotice: (message: string) => void;
}): React.JSX.Element {
  const fineMs = useSyncExternalStore(playheadClock.subscribe, playheadClock.read);
  return (
    <DomStage
      plan={plan}
      atMs={toOutputClamped(map, fineMs)}
      playing={playing}
      onNotice={onNotice}
    />
  );
}

export function VideoPanel({
  projectId,
  projectName,
  versionId,
  positionMs,
  keeps,
  playing,
  sentences,
  fontSize,
  marginV,
  onFontSize,
  onMarginV,
  onSeek,
  onNotice,
  view,
  onZoom,
  onPan,
}: Props): React.JSX.Element {
  const [template, setTemplate] = useState<VideoTemplate | null>(null);
  const [timeline, setTimeline] = useState<VideoTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [importPath, setImportPath] = useState("");
  /**
   * The hidden `<input type="file">` for a `.template` package.
   *
   * A real file input rather than a path field (which is what the PSD import still uses)
   * because a package is something the user just downloaded and has no path for: the browser
   * hands over bytes, and a path box would ask them to find it in 下载 themselves.
   */
  const packageInput = useRef<HTMLInputElement | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewAt, setPreviewAt] = useState(0);
  /**
   * The film as one description, for the real-time stage (P1, §19.31.2).
   *
   * `null` until it arrives, and `null` again after a failure — in both cases the panel falls
   * back to the still preview, which is slower but always works. A real-time preview that
   * silently showed nothing when the plan failed would look like a broken panel rather than a
   * degraded one.
   */
  /**
   * Bumped when the layout changes. The plan's digest covers the layout, so every baked band's
   * URL changes with it — a preview that kept the old plan would show the film where the boxes
   * used to be, and the canvas would be the only place the drag was visible.
   */
  const [layoutRevision, setLayoutRevision] = useState(0);
  /**
   * The canvas editor is a dialog, not a tab (round 140). Layout is decided once per project
   * and then left alone; a permanently visible tab asked that question on every edit.
   */
  const [layoutOpen, setLayoutOpen] = useState(false);
  const [plan, setPlan] = useState<VideoPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  // P2: the library, the dragged asset, and the selected block.
  const [assetRevision, setAssetRevision] = useState(0);
  const [draggingAsset, setDraggingAsset] = useState<string | null>(null);
  const [selected, setSelected] = useState<VideoItem | null>(null);
  // Which settings tab is showing. Not persisted: reopening on 模板 is the order the
  // work happens in (bind the template, then subtitle, then assets, then the deck).
  const [tab, setTab] = useState<TabId>("template");
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

  // The style's defaults are fetched by `App` now (see the `fontSize` prop): the same two
  // numbers feed the film export, and an export must not depend on this panel having been
  // opened at least once.

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

  /**
   * Swap in the bundled default template.
   *
   * No confirm dialog: the empty state is the only place the button appears bare, and there
   * is nothing to lose there. Replacing an existing template goes through
   * `doReplaceWithDefault`, which warns first.
   */
  const doUseDefault = async (): Promise<void> => {
    setBusy(true);
    try {
      setTemplate(await api.installDefaultTemplate(projectId));
      onNotice("已换上默认模板");
      await load();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  /** Ask before replacing a template that already exists; see `REPLACE_WARNING`. */
  const doReplaceWithDefault = async (): Promise<void> => {
    if (!window.confirm(REPLACE_WARNING)) return;
    await doUseDefault();
  };

  const doDownloadPackage = async (): Promise<void> => {
    setBusy(true);
    try {
      const { blob, disposition, bytes } = await api.templatePackage(projectId);
      const cleaned = projectName.replace(/[\\/:*?"<>|]/g, "_").trim();
      const filename = filenameFromDisposition(disposition, `${cleaned || "模板"}.template`);
      saveBlob(blob, filename);
      onNotice(`已导出 ${filename}（${packageSize(bytes)}）`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doUploadPackage = async (files: FileList | null): Promise<void> => {
    const file = files?.[0];
    if (!file) return;
    if (template !== null && !window.confirm(REPLACE_WARNING)) {
      if (packageInput.current) packageInput.current.value = "";
      return;
    }
    setBusy(true);
    try {
      setTemplate(await api.uploadTemplatePackage(projectId, file));
      onNotice(`已导入 ${file.name}`);
      await load();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
      if (packageInput.current) packageInput.current.value = "";
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
   *
   * The **template** is reloaded too: the name plate is the bound speaker's name now (round
   * 99), so binding changes the poster's text, not just the avatar track. Refreshing one and
   * not the other is how the panel showed 「主播1 · 马小褂」 while the film still printed the
   * PSD's placeholder.
   */
  const doBind = async (slot: string, speakerId: number | null): Promise<void> => {
    try {
      const updated = await api.bindVideoAvatar(projectId, slot, versionId, speakerId);
      setAvatars((current) => current.map((item) => (item.name === slot ? updated : item)));
      await load();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * Ask for the frame at the playhead — **converted to output time**.
   *
   * `positionMs` is an original-timeline position and `at_ms` is output time. Passing one as
   * the other used to show a frame from `removed_ms` later (1.0–1.2 s on the three real
   * projects, §19.31.5 P0-1); on a project with no deletions it is invisible, which is why it
   * survived this long. The mapping comes from the EDL's keeps, so a version whose EDL has
   * not arrived yet maps identically — and with no deletions that is correct.
   *
   * `generation` guards against out-of-order answers: dragging the playhead fires several
   * requests, each ~0.65 s, so without it the *last* response to arrive wins and the panel
   * can settle on a frame from a position the user has already left.
   */
  const generation = useRef(0);
  const lastRequested = useRef<string | null>(null);
  const map = useMemo(() => timeMapFrom(keeps), [keeps]);

  const doPreview = useCallback(async (): Promise<void> => {
    if (!timeline) return;
    const outMs = toOutputClamped(map, positionMs);
    // The style is part of the key: dragging the 字号 slider must redraw the frame, and the
    // digest in the server-side file name carries the same two numbers, so the two agree
    // about what "the same picture" means.
    const key = `${outMs}:${fontSize}:${marginV}`;
    // Nothing to redraw when the mapped instant and the style are already on screen: the
    // server would answer with an identical file and charge 0.65 s for it.
    if (lastRequested.current === key && previewUrl !== null) return;
    const ticket = ++generation.current;
    lastRequested.current = key;
    setBusy(true);
    try {
      const result = await api.videoPreview(versionId, outMs, {
        font_size: fontSize,
        margin_v: marginV,
      });
      // A newer request has been issued while this one was in flight; its answer is the one
      // the user is waiting for.
      if (ticket !== generation.current) return;
      setPreviewUrl(api.previewUrl(projectId, result.url.split("/").pop() ?? ""));
      setPreviewAt(outMs);
    } catch (caught) {
      if (ticket !== generation.current) return;
      // A failed request leaves `lastRequested` pointing at an instant that never rendered,
      // so the next tick must be allowed to retry it rather than short-circuit.
      lastRequested.current = null;
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (ticket === generation.current) setBusy(false);
    }
  }, [timeline, map, positionMs, previewUrl, versionId, fontSize, marginV, projectId, onNotice]);

  /**
   * Load the plan that makes the preview real-time.
   *
   * Refetched when the timeline changes (a block moved) or the style changes, because both change
   * the plan's digest — and that digest is in every baked band's URL. Loading it is one request
   * for the whole film: 167 KB, measured ~210 ms, most of it the slide renders and the text PNG,
   * both cached on disk by their own digest.
   *
   * A failure is **not** fatal. The still preview still works, and a real-time preview that
   * silently showed nothing would read as a broken panel rather than a degraded one.
   */
  useEffect(() => {
    if (!timeline || template?.status !== "ready") {
      setPlan(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        // Subtitles are always burned in: `App`'s export call hard-codes
        // `burn_subtitles: true`, so a preview without them would show a film nobody can make.
        const next = await api.videoPlan(versionId, {
          font_size: fontSize,
          margin_v: marginV,
        });
        if (cancelled) return;
        setPlan(next);
        setPlanError(null);
      } catch (caught) {
        if (cancelled) return;
        // Keep the previous plan: one failed refetch must not blank a working stage.
        setPlanError(caught instanceof Error ? caught.message : String(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [versionId, timeline, template?.status, fontSize, marginV, layoutRevision]);

  /**
   * Follow the playhead: 300 ms after it stops moving, redraw.
   *
   * This is the **only** caller of `doPreview`, and it is what keeps the still alive as the
   * fallback for when the plan cannot be loaded (§19.32). There used to be a 「生成静帧」 button on
   * top of it; round 134 removed the button — with a plan the DOM stage draws every instant for
   * free, so a manual 0.65 s request could only produce a slightly *worse* picture, and without a
   * plan this effect already redraws on every playhead stop.
   */
  useEffect(() => {
    if (playing || plan) return;
    if (!timeline || template?.status !== "ready") return;
    const timer = window.setTimeout(() => void doPreview(), 300);
    return () => window.clearTimeout(timer);
  }, [positionMs, playing, timeline, template?.status, doPreview, plan]);

  /**
   * Nothing here exports the film.
   *
   * There used to be a 导出视频 button and a 下载成片 link in this panel, next to a
   * 「成片 1 小时 1 分 / 原始 1 小时 1 分」 line. All three went away in round 102: the two
   * durations are the 音频剪辑 panel's summary repeated one panel higher, and every other
   * delivery already lives in the 导出 menu. Arranging the film is this panel's job;
   * delivering it is the menu's. The state, the poll and the download moved to `App.tsx`
   * alongside 导出成片音频, because two exports with two progress readouts and two
   * different sets of disabled-reasons was the shape of the duplication.
   */

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
   * 「M」: the block is silent and the podcast plays alone. This is the default state.
   *
   * M is a **destination**, not a toggle (§19.24.1). Both letters off is not a state the
   * bubble can draw, so the "unmute into the mix" branch that M used to have no longer has
   * anywhere to land: pressing M while M is lit is a no-op, not a request the server would
   * have to ignore.
   *
   * Only flags are written; the mix happens at export. Saying so in the notice matters —
   * otherwise pressing M appears to do nothing, because the preview is a still and there is
   * no film to hear yet.
   */
  const doMute = async (item: VideoItem): Promise<void> => {
    if (item.muted && !item.solo) return;
    try {
      await api.patchVideoItem(item.id, { muted: true, solo: false });
      await load();
      onNotice(`已静音 ${blockName(item)}，成片里只留播客的声音`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /**
   * 「S」: replace the podcast with this block's sound, or go back to the default (§19.24).
   *
   * Un-soloing does not mean "neither": it means back to M, which is the only other state a
   * block with sound can be in. So the off-branch sends `muted: true` in the same request
   * instead of sending `solo: false` alone and leaving both letters dark.
   */
  const doSolo = async (item: VideoItem, solo: boolean): Promise<void> => {
    if (item.solo === solo) return;
    try {
      await api.patchVideoItem(item.id, solo ? { solo: true } : { solo: false, muted: true });
      await load();
      onNotice(
        solo
          ? `这一段的播客音频会被 ${blockName(item)} 自己的声音顶替（导出时生效）`
          : `已恢复这一段的播客音频，${blockName(item)} 回到静音`,
      );
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  /** 素材框 / 全屏 (§19.25). Both are visible immediately — the still proves it. */
  const doPlacement = async (item: VideoItem, placement: string): Promise<void> => {
    try {
      await api.patchVideoItem(item.id, { placement });
      await load();
      onNotice(
        placement === "full"
          ? `${blockName(item)} 改为全屏展现`
          : `${blockName(item)} 改回画在素材框里`,
      );
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
          {/*
            Two ways out of the empty state that are not "find a PSD and type its path".
            The default is the one most projects want; a `.template` is what people exchange
            with each other. Both sit below the PSD row because the PSD is the advanced path,
            not the first thing to try.
          */}
          <p className="section-note">
            没有 PSD 也可以：用程序自带的默认模板，或者导入别人给的 <code>.template</code> 文件。
          </p>
          <div className="edl-actions">
            <button type="button" className="ghost" disabled={busy} onClick={() => void doUseDefault()}>
              用默认模板
            </button>
            <button
              type="button"
              className="ghost"
              disabled={busy}
              onClick={() => packageInput.current?.click()}
            >
              导入模板包…
            </button>
            <input
              ref={packageInput}
              type="file"
              accept=".template,application/zip"
              hidden
              onChange={(event) => void doUploadPackage(event.target.files)}
            />
          </div>
        </div>
      ) : template.status === "pending" || template.status === "compiling" ? (
        <p className="section-note">正在编译模板（约 6 秒）…</p>
      ) : (
        <div className="video-ready">
          {/*
            Preview on the left, one settings column on the right, tabs inside it.
            Stacking 模板 / 字幕 / 素材库 / 演示稿 vertically pushed the timeline off the
            bottom of the panel: four sections of settings above the track meant you scrolled
            past the thing you were aiming at to reach the place you were aiming from.
          */}
          {/*
            The deck tab is a two-pane editor (page list + preview + fields); at 320px its
            list is squeezed to a column of truncated titles. Widen the settings column for
            that tab only, rather than making every tab pay for the widest one.
          */}
          <div
            className={`video-stage${tab === "deck" ? " wants-wide" : ""}${tab === "template" ? " wants-mid" : ""}`}
          >
            <div className="video-stage-main">
              {/*
                The real-time stage when there is a plan, the still otherwise. One box, two
                renderers: putting them side by side would be two previews of the same film that
                could disagree, which is the whole problem this round removes.
              */}
              {plan ? (
                <div className="video-preview-box is-live" aria-busy={false}>
                  {/*
                    The stage reads the **fine** playhead clock, not `positionMs`.

                    `positionMs` is React state driven by `timeupdate`, which Chrome throttles to
                    ~217 ms: the video inside the stage runs at 48 fps while the layer stack (which
                    cue band is up, which segment is drawn) could only change four times a second.
                    Measured before: an instant held for up to 232 ms, i.e. a subtitle appearing
                    five frames late.

                    It has to be this component that subscribes, not a prop from `App`: raising
                    `positionMs` to 20 Hz re-rendered the whole tree (transcript, waveform, video
                    track) and cost half the frame budget — measured rAF 49 → 27 fps.
                  */}
                  <LiveDomStage
                    plan={plan}
                    map={map}
                    playing={playing}
                    onNotice={onNotice}
                  />
                </div>
              ) : (
                <div className="video-preview-box" aria-busy={busy}>
                  {previewUrl ? (
                    <img
                      src={previewUrl}
                      alt={`预览 ${formatTime(previewAt)}`}
                      /*
                       * Dimmed, not cleared, while a new frame is on its way: a 0.65 s request
                       * per playhead stop would otherwise flash an empty box every time, and
                       * "the preview went blank" reads as broken rather than as busy.
                       */
                      className={busy ? "is-stale" : undefined}
                    />
                  ) : (
                    <p className="muted small">
                      {planError
                        ? `实时预览没有起来：${planError}。下面用静帧代替。`
                        : "还没有预览。播放头停下就会自动出图。"}
                    </p>
                  )}
                </div>
              )}
            </div>

            <div className="video-settings">
              <div className="tabs" role="tablist">
                {VIDEO_TABS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={tab === item.id}
                    className={tab === item.id ? "tab active" : "tab"}
                    title={item.hint}
                    onClick={() => setTab(item.id)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>

              <div className="video-settings-body">
                {tab === "template" ? (
                  <>
                    <TemplateTexts
                      projectId={projectId}
                      template={template}
                      avatars={avatars}
                      speakers={speakers}
                      onTemplate={setTemplate}
                      onBind={(slot, speakerId) => void doBind(slot, speakerId)}
                      onNotice={onNotice}
                    />
                    {/*
                      Only **problems** reach this list now — a missing 期号/标题 layer, a media
                      box with no plate. The compile used to also record facts nobody could act
                      on (hidden avatar groups, an evened-out box size) and they sat above the
                      form pushing the fields off the column. Nothing is printed when there is
                      nothing wrong, which on the sample PSD is the normal case.
                    */}
                    {(template.slots.notes ?? []).map((note, index) => (
                      <p key={index} className="section-note">
                        {note}
                      </p>
                    ))}
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
                    {/*
                      Position and size are a once-per-project decision, so they live behind this
                      button instead of on a tab of their own. The canvas inside draws the same
                      stack the preview and the export draw, so nothing is hidden by the modal.
                    */}
                    <div className="edl-actions">
                      <button
                        type="button"
                        className="ghost"
                        disabled={busy}
                        onClick={() => setLayoutOpen(true)}
                      >
                        编辑布局…
                      </button>
                    </div>
                    {/*
                      Sharing the template itself. A `.template` carries the poster, the slot
                      geometry and the two fonts — everything except this project's text and
                      avatar bindings — so another project (or another person) gets the same
                      layout without needing the PSD's fonts installed.
                    */}
                    <div className="edl-actions">
                      <button
                        type="button"
                        className="ghost"
                        disabled={busy}
                        onClick={() => void doDownloadPackage()}
                      >
                        导出模板包…
                      </button>
                      <button
                        type="button"
                        className="ghost"
                        disabled={busy}
                        onClick={() => void doReplaceWithDefault()}
                      >
                        换回默认模板
                      </button>
                      <button
                        type="button"
                        className="ghost"
                        disabled={busy}
                        onClick={() => packageInput.current?.click()}
                      >
                        导入模板包…
                      </button>
                      <input
                        ref={packageInput}
                        type="file"
                        accept=".template,application/zip"
                        hidden
                        onChange={(event) => void doUploadPackage(event.target.files)}
                      />
                    </div>
                  </>
                ) : null}

                {tab === "subtitle" ? (
                  /*
                    The style controls sit next to the still, not in 设置: the whole point
                    is to nudge a number and look at the frame. Nothing is saved until
                    导出 — a slider that wrote to the database on every drag would be both
                    slow and a surprise when reopening the project.
                  */
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
                        onChange={(event) => onFontSize(Number(event.target.value))}
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
                        onChange={(event) => onMarginV(Number(event.target.value))}
                      />
                    </label>
                    <p className="section-note">
                      改完拖一下播放头就能看到效果。字幕由成片渲染时烧进画面，这里只是预览参数。
                    </p>
                  </div>
                ) : null}

                {tab === "library" ? (
                  <AssetLibrary
                    projectId={projectId}
                    revision={assetRevision}
                    onDragStateChange={setDraggingAsset}
                    onAddToBlock={
                      selected === null ? undefined : (assetId) => void doMakeGallery(selected, assetId)
                    }
                    onNotice={(message) => {
                      // Importing or deleting an asset can change what a block can show, so
                      // the library and the timeline are refreshed together — but only on a
                      // *count* change, which the notice text is the cheapest signal for.
                      if (message.includes("素材")) setAssetRevision((value) => value + 1);
                      onNotice(message);
                    }}
                  />
                ) : null}

                {tab === "deck" ? (
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
                ) : null}
              </div>
            </div>
          </div>

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

          {/*
            No selected-block row any more (round 104). It said 选中 01:10.970–03:18.850
            （127.9s） outside the timeline, and offered 填充 / 删掉这个块 / a drag hint; all
            of that described *one* block, and all of it went stale the moment another block
            was clicked. The range now sits on the block itself (where every block has one)
            and the two actions are a bubble anchored to the selected block — see
            `VideoTrack`. What remains here is the only thing the panel still has to say on
            its own: that there are no blocks at all.
          */}
          {timeline && timeline.items.length === 0 ? (
            <p className="muted small">
              还没有素材块。从「素材库」标签页把一个文件拖到下面的视频轴上，或者拖给某个句子。
            </p>
          ) : null}

          {/*
            No export row here any more (round 102). It was a 导出视频 button, a 下载成片
            link, and a 「成片 1 小时 1 分 / 原始 1 小时 1 分」 line. All three were
            duplicates: the two durations are the 音频剪辑 panel's own summary one panel
            below, and every other delivery already lives in the 导出 menu. What remains
            here is arranging the film; delivering it is the menu's job.
          */}

          {/*
            The track is the **last** row in this panel, so it sits directly above the audio
            waveform. That adjacency is the feature: the two tracks are one timeline drawn
            twice from one shared window, and "which second is this block on" is answered by
            looking straight down rather than by matching numbers between two distant rows.
          */}
          <VideoTrack
            items={timeline?.items ?? []}
            targets={timeline?.grid.targets ?? []}
            durationMs={timeline?.grid.duration_ms ?? 0}
            view={view}
            positionMs={positionMs}
            draggingAssetId={draggingAsset}
            onSeek={onSeek}
            onSelect={setSelected}
            onMove={(item, start, end) => void doMove(item, start, end)}
            onDropAsset={(assetId, atMs) => void doDropAsset(assetId, atMs)}
            selectedId={selected?.id ?? null}
            onFit={(item, fit) => void doFit(item, fit)}
            onMute={(item) => void doMute(item)}
            onSolo={(item, solo) => void doSolo(item, solo)}
            onPlacement={(item, placement) => void doPlacement(item, placement)}
            onDelete={(item) => void doDeleteItem(item)}
            onZoom={onZoom}
            onPan={onPan}
          />

          {/*
            Only the keys, and only once there is a block to use them on.
            
            The line used to explain dragging and zooming too. Both are discoverable by
            trying them, and the zoom gestures are *identical to the waveform's* directly
            underneath — so that half was explaining the audio editor to someone who had
            already learned it there. What cannot be discovered is a modifier: nothing on
            screen hints that Ctrl or Shift exists.
          */}
          {timeline && timeline.items.length > 0 ? (
            <p className="muted small video-track-hint">
              选中一个块：Ctrl+← / Ctrl+→ 整块贴到前一个 / 后一个素材上（长度不变），
              Shift+← / Shift+→ 只把这一边拉过去（长度改变）。
            </p>
          ) : null}

        </div>
      )}

      {template !== null && template.status === "ready" ? (
        <LayoutDialog
          open={layoutOpen}
          projectId={projectId}
          template={template}
          onClose={() => setLayoutOpen(false)}
          onNotice={onNotice}
          onChanged={() => setLayoutRevision((value) => value + 1)}
        />
      ) : null}
    </CollapsiblePanel>
  );
}
