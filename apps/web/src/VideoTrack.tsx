/**
 * The video track: blocks you can drag, resize, and drop onto (P2).
 *
 * **The block says what it is and when it is.** Two lines of text on the block itself — the
 * asset's own name (or the deck's title) on top, the range it covers underneath — because
 * the same facts used to be printed *outside* the track, in a panel row that described
 * whichever block was selected. That row said 「01:10.970–03:18.850（127.9s）」 once, for one
 * block; the number belongs under the block it describes, where all of them have it at once
 * and where it moves when the block moves (round 104).
 *
 * **The bubble replaces that row.** Selecting a block opens a small panel anchored to it,
 * holding the two things one actually does to a block — choose how its picture fills the
 * box, and delete it. It lives in the wrapper above the track, with its own reserved row, so
 * it neither pushes the waveform down (what the old row did on every selection) nor floats
 * over the settings column (what an absolutely positioned bubble did when the selected block
 * sat in the right half).
 *
 * Renderer stays a plain `<div>` + CSS transform stack rather than a canvas. A real
 * episode has a few dozen blocks at most, not the tens of thousands that push the
 * transcript into a virtual list, and DOM gives drag/click/hover for free — the canvas
 * version would have to reimplement all three to answer "which block did I grab".
 *
 * The block is drawn at its **original** width even when its output is shorter. Drawing
 * the output width would make two adjacent blocks leave a gap the moment one of them lost
 * audio to a cut, and a gap reads as "there is no video here" when the block plays
 * straight through. The kept share is shown as text instead.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { VideoItem } from "./api";
import { formatTime } from "./format";
import { ZOOM_STEP, xToMs as xToMsIn } from "./waveView";
import type { View } from "./waveView";
import {
  FITS,
  MIN_BLOCK_MS,
  PLACEMENTS,
  abutNeighbour,
  blockAt,
  blockName,
  layoutBlock,
  moveBlock,
  resizeBlock,
  stretchToNeighbour,
} from "./videoTimeline";

export interface VideoTrackProps {
  items: VideoItem[];
  targets: number[];
  durationMs: number;
  view: View;
  positionMs: number;
  /** The asset that would be dropped, when a drag from the library is in flight. */
  draggingAssetId: string | null;
  onSeek: (ms: number) => void;
  onSelect: (item: VideoItem | null) => void;
  onMove: (item: VideoItem, startMs: number, endMs: number) => void;
  onDropAsset: (assetId: string, atMs: number) => void;
  /**
   * The selected block's id.
   *
   * Passed down rather than kept here: `VideoPanel` also needs to know what is selected (it
   * feeds 「加进画廊」 in the library tab), and two copies of that state is how a bubble ends
   * up open around a block the panel thinks is not selected.
   */
  selectedId: string | null;
  onFit: (item: VideoItem, fit: string) => void;
  /** Toggle the block's own sound on or off (§19.20). */
  onMute: (item: VideoItem, muted: boolean) => void;
  /**
   * 「S」: replace the podcast with this block's sound over its stretch, or stop doing so. One
   * setter for both letters because they are one three-state control (§19.25).
   */
  onSolo: (item: VideoItem, solo: boolean) => void;
  /** 素材框 / 全屏 (§19.26). */
  onPlacement: (item: VideoItem, placement: string) => void;
  onDelete: (item: VideoItem) => void;
  /** Zoom/pan the **shared** window — the same one the audio waveform uses. */
  onZoom: (anchorMs: number, factor: number) => void;
  onPan: (from: number) => void;
}

type Drag =
  | { kind: "move"; item: VideoItem; grabMs: number; originStart: number; originEnd: number }
  | { kind: "edge"; item: VideoItem; edge: "start" | "end" }
  | null;

const HEIGHT = 56;
/** Kept in sync with `.video-bubble { width: … }` — the anchor maths needs the number. */
const BUBBLE_WIDTH = 300;
/** `.video-track-wrap { padding: 0 7px }`, the same 7px the waveform canvas is inset by. */
const TRACK_INSET = 7;

export function VideoTrack({
  items,
  targets,
  durationMs,
  view,
  positionMs,
  draggingAssetId,
  onSeek,
  onSelect,
  onMove,
  onDropAsset,
  selectedId,
  onFit,
  onMute,
  onSolo,
  onPlacement,
  onDelete,
  onZoom,
  onPan,
}: VideoTrackProps): React.JSX.Element {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(800);
  const [drag, setDrag] = useState<Drag>(null);
  const [hover, setHover] = useState<{ item: VideoItem; edge: "start" | "end" } | null>(null);
  const [ghost, setGhost] = useState<{ start_ms: number; end_ms: number } | null>(null);

  useEffect(() => {
    const node = trackRef.current;
    if (!node) return;
    const measure = (): void => setWidth(node.clientWidth || 800);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  /**
   * Wheel over the track zooms, exactly like the waveform under it.
   *
   * A non-passive listener, for the same reason the waveform uses one: React's synthetic
   * wheel handler cannot `preventDefault`, so the page would scroll underneath the zoom.
   * Both tracks zoom through the same function on the same window, so a notch anywhere
   * moves both of them and the block stays above the audio it covers.
   */
  useEffect(() => {
    const node = trackRef.current;
    if (!node) return;
    const onWheel = (event: WheelEvent): void => {
      const span = Math.max(1, view.to - view.from);
      if (event.shiftKey) {
        event.preventDefault();
        onPan(view.from + (event.deltaY + event.deltaX) * (span / Math.max(1, width)));
        return;
      }
      const zoomingOut = event.deltaY > 0;
      if (zoomingOut && span >= durationMs - 1) return;
      event.preventDefault();
      const rect = node.getBoundingClientRect();
      const x = Math.min(rect.width, Math.max(0, event.clientX - rect.left));
      onZoom(xToMsIn(x, view, width), event.deltaY > 0 ? 1 / ZOOM_STEP : ZOOM_STEP);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [durationMs, onPan, onZoom, view, width]);

  /**
   * Ctrl+← / Ctrl+→ move the whole block flush against its neighbour (length kept);
   * Shift+← / Shift+→ stretch one edge until it reaches it (length changes).
   *
   * Both keys are handled here because they share the neighbour search and because splitting
   * them across two listeners would let a future edit to the input field guard fix one and
   * miss the other — that guard is the only part that can go wrong silently.
   *
   * Dragging can close a gap, but not *exactly* — the snap grid is ~500 ms apart, so a hand
   * drag lands "about there" and two blocks that should share an edge end up a few hundred
   * milliseconds apart, which reads as 「这一段没有视频」. The shortcut is the only way to
   * say "exactly adjacent".
   *
   * A window listener rather than an `onKeyDown` on the track: the track is not focusable
   * (it is a `role="presentation"` div that owns pointer gestures), and making it focusable
   * would put a tab stop in the middle of the page and a focus ring around a 56-pixel strip.
   * The cost is having to guard against typing, which is four lines and cannot go wrong
   * silently — anything in an input, a textarea or a contenteditable keeps its own
   * Ctrl+←/→ (word-by-word caret movement) untouched.
   *
   * Both helpers return `null` when there is nothing to do, and nothing happens: a shortcut
   * that does nothing when it is already satisfied is correct, and a notice for it would fire
   * on every second press.
   */
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const move = event.ctrlKey || event.metaKey;
      const stretch = event.shiftKey;
      // Both at once means "do two things", which nobody asked for; Alt is reserved by the
      // window manager on Windows and never reaches a page.
      if (move === stretch || event.altKey) return;
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const target = event.target as HTMLElement | null;
      if (target) {
        if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;
        if (target.isContentEditable) return;
      }
      const item = items.find((candidate) => candidate.id === selectedId);
      if (!item) return;
      const direction = event.key === "ArrowLeft" ? "prev" : "next";
      const moved = stretch
        ? stretchToNeighbour(item, direction, items, durationMs)
        : abutNeighbour(item, direction, items, durationMs);
      if (moved === null) return;
      event.preventDefault();
      onMove(item, moved.start_ms, moved.end_ms);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [durationMs, items, onMove, selectedId]);

  const toMs = useCallback(
    (clientX: number): number => {
      const node = trackRef.current;
      if (!node) return 0;
      const rect = node.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
      return Math.round(view.from + ratio * (view.to - view.from));
    },
    [view],
  );

  /** The block the bubble describes, or `null`. Read from props so the panel owns it. */
  const bubbleItem = selectedId === null ? null : (items.find((item) => item.id === selectedId) ?? null);

  /**
   * Which edge, if any, the pointer is close enough to grab.
   *
   * 8 px, not a percentage: the tolerance has to be a distance on screen, because the
   * same block is 400 px wide when zoomed in and 4 px when zoomed out. A percentage-based
   * radius makes the handle unusable at one end of the zoom range and greedy at the other.
   */
  const edgeAt = useCallback(
    (clientX: number): { item: VideoItem; edge: "start" | "end" } | null => {
      const node = trackRef.current;
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      const x = clientX - rect.left;
      let best: { item: VideoItem; edge: "start" | "end" } | null = null;
      let bestDistance = 9;
      for (const item of items) {
        const layout = layoutBlock(
          { ...item, duration_ms: item.duration_ms, output_ms: item.output_ms },
          view,
          width,
        );
        for (const edge of ["start", "end"] as const) {
          const edgeX = edge === "start" ? layout.left : layout.left + layout.width;
          const distance = Math.abs(edgeX - x);
          if (distance < bestDistance) {
            best = { item, edge };
            bestDistance = distance;
          }
        }
      }
      return best;
    },
    [items, view, width],
  );

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    const handled = edgeAt(event.clientX);
    if (handled) {
      (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
      setDrag({ kind: "edge", item: handled.item, edge: handled.edge });
      onSelect(handled.item);
      return;
    }
    const hit = blockAt(toMs(event.clientX), items);
    if (hit) {
      onSelect(hit);
      setDrag({
        kind: "move",
        item: hit,
        grabMs: toMs(event.clientX),
        originStart: hit.start_ms,
        originEnd: hit.end_ms,
      });
      return;
    }
    onSelect(null);
    onSeek(toMs(event.clientX));
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (drag === null) {
      setHover(edgeAt(event.clientX));
      return;
    }
    const ms = toMs(event.clientX);
    if (drag.kind === "edge") {
      const next = resizeBlock(drag.item, drag.edge, ms, targets);
      setGhost(next);
      return;
    }
    const delta = ms - drag.grabMs;
    setGhost(
      moveBlock(
        { start_ms: drag.originStart, end_ms: drag.originEnd },
        delta,
        targets,
        durationMs,
      ),
    );
  };

  const finish = (): void => {
    if (drag !== null && ghost !== null) {
      const length = ghost.end_ms - ghost.start_ms;
      if (length >= MIN_BLOCK_MS) onMove(drag.item, ghost.start_ms, ghost.end_ms);
    }
    setGhost(null);
    setDrag(null);
  };

  /**
   * Where the bubble sits: above the selected block, clamped inside the track.
   *
   * Clamped on both edges because a block can be 4 px wide when zoomed out; anchoring the
   * bubble to its centre would hang most of the panel outside the track, and `.video-track`
   * clips (`overflow: hidden`) so it would simply be cut off. Above rather than below: below
   * is where the audio waveform is, and a bubble overlapping it reads as part of that track.
   */
  const bubbleStyle = (item: VideoItem): React.CSSProperties => {
    const layout = layoutBlock(
      { ...item, duration_ms: item.end_ms - item.start_ms, output_ms: item.output_ms },
      view,
      width,
    );
    const centre = layout.left + layout.width / 2;
    // Centred on the block, then clamped to the track: anchored on the centre alone, a
    // 4-pixel-wide block would put most of the panel outside the track. `TRACK_INSET` is the
    // wrapper's own horizontal padding — the track starts that far in, so the bubble's `left`
    // (measured against the wrapper) has to add it.
    const clamped = Math.max(
      0,
      Math.min(Math.max(0, width - BUBBLE_WIDTH), centre - BUBBLE_WIDTH / 2),
    );
    return { left: `${TRACK_INSET + clamped}px` };
  };

  const blockStyle = (item: VideoItem): React.CSSProperties => {
    const shown =
      ghost !== null && drag !== null && drag.item.id === item.id
        ? { start_ms: ghost.start_ms, end_ms: ghost.end_ms }
        : item;
    const layout = layoutBlock(
      { ...shown, duration_ms: shown.end_ms - shown.start_ms, output_ms: item.output_ms },
      view,
      width,
    );
    return {
      left: `${layout.left}px`,
      width: `${Math.max(2, layout.width)}px`,
    };
  };

  return (
    // `has-bubble` reserves the bubble's row. Anchoring it over the track's top edge instead
    // would float it across the settings column whenever the selected block sits in the right
    // half — in the demo it landed exactly on 「框内默认填充」 and hid the control it was
    // standing next to. The reserved row is ~34 px and only exists while something is
    // selected, which is less than the row this bubble replaced.
    <div className={`video-track-wrap${bubbleItem === null ? "" : " has-bubble"}`}>
      <div
        ref={trackRef}
        className={`video-track${drag === null ? "" : " is-dragging"}${
          draggingAssetId ? " wants-drop" : ""
        }`}
        style={{ height: `${HEIGHT}px` }}
        role="presentation"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={finish}
        onPointerCancel={finish}
        onPointerLeave={() => setHover(null)}
        onDragOver={(event) => {
          // Accept anything carrying an asset id, not just a drag this component saw
          // start. `draggingAssetId` is React state, and a drag session can begin before
          // that state has re-rendered — the classic result is a drop target that refuses
          // the very drag it was highlighting, which looks like the drop is broken.
          if (event.dataTransfer.types.includes("text/plain")) {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
          }
        }}
        onDrop={(event) => {
          event.preventDefault();
          const assetId = event.dataTransfer.getData("text/plain") || draggingAssetId;
          if (assetId) onDropAsset(assetId, toMs(event.clientX));
        }}
      >
        {items.map((item) => (
          <div
            key={item.id}
            className={`video-block${item.orphaned ? " is-orphan" : ""}${
              drag !== null && drag.item.id === item.id ? " is-dragged" : ""
            }`}
            style={blockStyle(item)}
            title={`${blockName(item)} · ${formatTime(item.start_ms)}–${formatTime(item.end_ms)}`}
          >
            <span className="video-block-text">
              <span className="video-block-name">{blockName(item)}</span>
              <span className="video-block-time">
                {formatTime(item.start_ms)}–{formatTime(item.end_ms)}
              </span>
            </span>
            <span className="video-block-kept" style={{ width: `${keptPercent(item)}%` }} />
            {hover !== null && hover.item.id === item.id ? (
              <span className={`video-block-handle ${hover.edge}`} />
            ) : null}
          </div>
        ))}

        {ghost !== null ? (
          <div
            className="video-ghost"
            style={{
              left: `${layoutBlock(
                { ...ghost, duration_ms: ghost.end_ms - ghost.start_ms, output_ms: ghost.end_ms - ghost.start_ms },
                view,
                width,
              ).left}px`,
              width: `${layoutBlock(
                { ...ghost, duration_ms: ghost.end_ms - ghost.start_ms, output_ms: ghost.end_ms - ghost.start_ms },
                view,
                width,
              ).width}px`,
            }}
          />
        ) : null}

        <div
          className="video-playhead"
          style={{ left: `${((positionMs - view.from) / Math.max(1, view.to - view.from)) * width}px` }}
        />
      </div>

      {/*
        Outside `.video-track` on purpose: the track clips (`overflow: hidden`) so a block
        that scrolls off its right edge cannot paint over the settings column. A bubble
        placed inside would be clipped the same way, and the clipping would look like the
        bubble failing to open.
      */}
      {bubbleItem !== null ? (
        <div
          className="video-bubble"
          style={bubbleStyle(bubbleItem)}
          // A pointerdown inside the bubble must not reach the track: the track reads it as
          // "grab the block behind" and would drag the block out from under the menu.
          onPointerDown={(event) => event.stopPropagation()}
        >
          <span className="video-bubble-name">{blockName(bubbleItem)}</span>
          {/*
            Neither select gets a label: 素材框 / 全屏 and 模糊填充 / 模糊裁切 say what they are, and
            with the labels the bubble wrapped onto a second line. The explanation lives in
            `title`, which costs nothing until someone opens it.
          */}
          <label className="video-bubble-fit">
            <select
              value={bubbleItem.placement || "box"}
              title="素材框：画在模板的素材框里；全屏：铺满整个画面"
              onChange={(event) => onPlacement(bubbleItem, event.target.value)}
            >
              {PLACEMENTS.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="video-bubble-fit">
            <select
              value={bubbleItem.fit}
              title={FITS.find((fit) => fit.id === bubbleItem.fit)?.hint ?? ""}
              onChange={(event) => onFit(bubbleItem, event.target.value)}
            >
              {FITS.map((fit) => (
                <option key={fit.id} value={fit.id}>
                  {fit.label}
                </option>
              ))}
            </select>
          </label>
          {/*
            Two letters, and only where they can do something. A still has no sound, so both
            buttons are *absent* rather than disabled: a permanently greyed-out M asks "what is
            wrong with this block", and the answer is "nothing".

            They are one three-state control (§19.25):
              neither pressed → the block is silent, which is the default;
              M pressed       → its sound is mixed **under** the podcast;
              S pressed       → its sound **replaces** the podcast over this stretch.
            `aria-pressed` therefore reads the flag directly on each, and the server keeps them
            exclusive, so the two can never both claim to be pressed.
          */}
          {bubbleItem.has_audio ? (
            <>
              <button
                type="button"
                className={`ghost small icon${bubbleItem.muted ? "" : " is-on"}`}
                // Pressed **is** muted: this button silences the block, so a pressed M is a
                // silent block. The inverse read as "pressed means audible" and told the user
                // the opposite of the truth on every silent block.
                aria-pressed={bubbleItem.muted}
                title={
                  bubbleItem.muted
                    ? `让 ${blockName(bubbleItem)} 自己的声音也进成片（混在播客下面）`
                    : `只留播客的声音，静音 ${blockName(bubbleItem)}`
                }
                onClick={() => onMute(bubbleItem, !bubbleItem.muted)}
              >
                M
              </button>
              <button
                type="button"
                className={`ghost small icon${bubbleItem.solo ? " is-on" : ""}`}
                aria-pressed={bubbleItem.solo}
                title={
                  bubbleItem.solo
                    ? `恢复播客的声音：${blockName(bubbleItem)} 不再顶替这一段的音频`
                    : `用 ${blockName(bubbleItem)} 自己的声音顶替这一段的播客音频`
                }
                onClick={() => onSolo(bubbleItem, !bubbleItem.solo)}
              >
                S
              </button>
            </>
          ) : null}
          <button
            type="button"
            className="ghost small"
            title={`删掉 ${blockName(bubbleItem)}`}
            onClick={() => onDelete(bubbleItem)}
          >
            删除素材
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** How much of the block survives the audio cuts, as a percentage. */
function keptPercent(item: VideoItem): number {
  const total = Math.max(1, item.end_ms - item.start_ms);
  return Math.max(0, Math.min(100, Math.round((item.output_ms / total) * 100)));
}
