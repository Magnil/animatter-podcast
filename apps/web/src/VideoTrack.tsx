/**
 * The video track: blocks you can drag, resize, and drop onto (P2).
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
import {
  MIN_BLOCK_MS,
  blockAt,
  layoutBlock,
  moveBlock,
  resizeBlock,
  type View,
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
}

type Drag =
  | { kind: "move"; item: VideoItem; grabMs: number; originStart: number; originEnd: number }
  | { kind: "edge"; item: VideoItem; edge: "start" | "end" }
  | null;

const HEIGHT = 56;

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
    <div className="video-track-wrap">
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
            title={`${formatTime(item.start_ms)} · ${item.kind}`}
          >
            <span className="video-block-kind">
              {item.kind === "gallery" ? "画廊" : item.kind === "slide" ? "演示稿" : "素材"}
            </span>
            <span className="video-block-kept" style={{ width: `${keptPercent(item)}%` }} />
            <span className="video-block-time">{formatTime(item.start_ms)}</span>
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
    </div>
  );
}

/** How much of the block survives the audio cuts, as a percentage. */
function keptPercent(item: VideoItem): number {
  const total = Math.max(1, item.end_ms - item.start_ms);
  return Math.max(0, Math.min(100, Math.round((item.output_ms / total) * 100)));
}
