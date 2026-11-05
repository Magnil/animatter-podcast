/**
 * Waveform rendering with draggable boundary handles, horizontal scrolling and
 * wheel zoom.
 *
 * Drag targets are the **token edges that coincide with a sentence boundary** —
 * the first token's start and the last token's end. That is exactly the case
 * where ASR timing is most often wrong (it cuts a sentence too early or too
 * late), and it is the only case where adjusting one edge does not require
 * touching neighbouring words.
 *
 * Rendering stays canvas-based: a 3h episode is ~1.9GB of float PCM once
 * decoded, so the backend pre-reduces it to min/max buckets (AGENT.md §7.1).
 *
 * Zoom is a *window*, not a wider canvas. A 3h episode at 64 buckets/s is
 * 691k buckets; at 8× zoom it would be 5.5M, and the browser would spool the
 * JSON before drawing a single pixel. So the canvas keeps its container width
 * and only the visible ``[from_ms, to_ms)`` is drawn and fetched — which is
 * exactly what ``GET /waveform?from_ms=&to_ms=&buckets_per_second=`` was built
 * for. The scrollbar is therefore a custom thumb over a track rather than a
 * native overflow scrollbar: there is no wider element to scroll.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Waveform as WaveformData } from "./api";
import {
  MIN_VISIBLE_MS,
  ZOOM_STEP,
  bucketsForWindow,
  clampView,
  isZoomed,
  msToX as msToXIn,
  panTo as panToIn,
  scrollFraction,
  xToMs as xToMsIn,
  zoomAt as zoomAtIn,
  type View,
} from "./waveView";

export interface Marker {
  start_ms: number;
  end_ms: number;
  active: boolean;
}

/** A draggable edge: which token, which side, and where it sits. */
export interface EdgeHandle {
  tokenIndex: number;
  edge: "start" | "end";
  ms: number;
  sentenceOrd: number;
}

export interface DeletionSpan {
  start_ms: number;
  end_ms: number;
}

export interface Caption {
  start_ms: number;
  end_ms: number;
  text: string;
  ord: number;
}

interface Props {
  projectId: string;
  mediaId: string;
  durationMs: number;
  positionMs: number;
  markers: Marker[];
  handles: EdgeHandle[];
  /** Deleted spans, drawn as red overlays. */
  deletions?: DeletionSpan[];
  /** Subtitles drawn on the waveform so text and audio line up visually. */
  captions?: Caption[];
  activeOrd?: number;
  onCaptionClick?: (ord: number) => void;
  /** Called once on drop, with the final time. */
  onEdgeCommit: (handle: EdgeHandle, newMs: number) => void;
  onSeek: (ms: number) => void;
}

const CANVAS_HEIGHT = 168;
/** Top band reserved for subtitle text. */
const CAPTION_BAND = 22;
const HANDLE_HIT_PX = 6;

export function Waveform({
  projectId,
  mediaId,
  durationMs,
  positionMs,
  markers,
  handles,
  deletions = [],
  captions = [],
  activeOrd = -1,
  onCaptionClick,
  onEdgeCommit,
  onSeek,
}: Props): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ handle: EdgeHandle; previewMs: number } | null>(null);
  const [hovered, setHovered] = useState<EdgeHandle | null>(null);
  // Bumped on every drag move so the canvas redraws the preview position.
  const [, setTick] = useState(0);

  const [peaks, setPeaks] = useState<WaveformData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [width, setWidth] = useState(800);
  const [view, setView] = useState<View>({ from: 0, to: Math.max(1, durationMs) });

  const total = Math.max(1, peaks?.duration_ms || durationMs || 1);
  const span = Math.max(1, view.to - view.from);
  const zoomed = isZoomed(view, total);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(Math.max(200, entry.contentRect.width));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // A new media file invalidates the window; keep the old one only while the
  // same audio is loaded, otherwise the view could point past the new end.
  useEffect(() => {
    setView({ from: 0, to: Math.max(1, durationMs) });
  }, [projectId, mediaId, durationMs]);

  const zoomAt = useCallback(
    (anchorMs: number, factor: number) => {
      setView((current) => zoomAtIn(current, total, anchorMs, factor));
    },
    [total],
  );

  const panTo = useCallback(
    (from: number) => {
      setView((current) => panToIn(current, total, from));
    },
    [total],
  );

  // Fetch only the visible window. Debounced because a wheel gesture fires
  // dozens of events and each one would otherwise be a request.
  useEffect(() => {
    let cancelled = false;
    const bps = bucketsForWindow(width, Math.max(1, view.to - view.from));
    const timer = window.setTimeout(() => {
      void api
        .waveform(projectId, mediaId, bps, Math.round(view.from), Math.round(view.to))
        .then((payload) => {
          if (!cancelled) {
            setPeaks(payload);
            setLoadError(null);
          }
        })
        .catch((error: unknown) => {
          if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error));
        });
    }, 90);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [projectId, mediaId, view.from, view.to, width]);

  const msToX = useCallback((ms: number) => msToXIn(ms, view, width), [view, width]);
  const xToMs = useCallback((x: number) => xToMsIn(x, view, width), [view, width]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(CANVAS_HEIGHT * dpr);

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, CANVAS_HEIGHT);

    // The waveform occupies the area below the caption band.
    const mid = CAPTION_BAND + (CANVAS_HEIGHT - CAPTION_BAND) / 2;
    const waveHeight = CANVAS_HEIGHT - CAPTION_BAND;

    const band = (startMs: number, endMs: number, fill: string): void => {
      const x0 = Math.max(-2, msToX(startMs));
      const x1 = Math.min(width + 2, msToX(endMs));
      if (x1 <= 0 || x0 >= width) return;
      ctx.fillStyle = fill;
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), CANVAS_HEIGHT);
    };

    for (const marker of markers) {
      band(marker.start_ms, marker.end_ms, marker.active ? "rgba(59,130,246,0.18)" : "rgba(148,163,184,0.10)");
    }

    // Deleted regions sit above the sentence bands so a cut is unmistakable.
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(220,38,38,0.7)";
    for (const deleted of deletions) {
      band(deleted.start_ms, deleted.end_ms, "rgba(220,38,38,0.22)");
      const x0 = msToX(deleted.start_ms);
      const x1 = msToX(deleted.end_ms);
      ctx.beginPath();
      if (x0 >= -1 && x0 <= width + 1) {
        ctx.moveTo(x0, 0);
        ctx.lineTo(x0, CANVAS_HEIGHT);
      }
      if (x1 >= -1 && x1 <= width + 1) {
        ctx.moveTo(x1, 0);
        ctx.lineTo(x1, CANVAS_HEIGHT);
      }
      ctx.stroke();
    }

    if (!peaks || peaks.min.length === 0 || !peaks.rate) {
      ctx.fillStyle = "#cbd5e1";
      ctx.fillRect(0, mid - 1, width, 2);
    } else {
      // Bucket i sits at peaks.from_ms + i / rate. Mapping through time (not
      // through array index) keeps the shape correct even when the server
      // snapped the window to its own bucket grid.
      const rate = peaks.rate;
      const firstBucketMs = peaks.from_ms;
      const bucketWidthPx = (1000 / rate / span) * width;
      ctx.fillStyle = "#475569";
      const count = peaks.min.length;
      for (let x = 0; x < width; x += 1) {
        const ms = view.from + (x / width) * span;
        const index = Math.floor(((ms - firstBucketMs) / 1000) * rate);
        if (index < 0 || index >= count) continue;
        const reach = Math.max(1, Math.ceil(bucketWidthPx));
        let low = 0;
        let high = 0;
        for (let i = index; i < Math.min(count, index + reach); i += 1) {
          low = Math.min(low, peaks.min[i] ?? 0);
          high = Math.max(high, peaks.max[i] ?? 0);
        }
        ctx.fillRect(x, mid - high * (waveHeight / 2), 1, Math.max(1, (high - low) * (waveHeight / 2)));
      }
    }

    // Boundary handles.
    const drag = dragRef.current;
    for (const handle of handles) {
      const isDragging = drag?.handle.tokenIndex === handle.tokenIndex && drag.handle.edge === handle.edge;
      const ms = isDragging && drag ? drag.previewMs : handle.ms;
      const x = msToX(ms);
      if (x < -8 || x > width + 8) continue;
      const isHovered =
        hovered?.tokenIndex === handle.tokenIndex && hovered?.edge === handle.edge;

      ctx.fillStyle = isDragging ? "#f59e0b" : isHovered ? "#2563eb" : "#94a3b8";
      ctx.fillRect(Math.max(0, Math.min(width - 2, x - 1)), 0, 2, CANVAS_HEIGHT);

      // Grip affordance at mid-height.
      ctx.beginPath();
      ctx.arc(x, mid, isHovered || isDragging ? 6 : 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.stroke();

      if (isDragging) {
        ctx.fillStyle = "#111827";
        ctx.font = "11px system-ui, sans-serif";
        ctx.fillText(formatMs(ms), Math.min(width - 60, x + 8), 14);
      }
    }

    // Captions sit in the top band, above the waveform, so text never obscures
    // the shape of the audio it describes.
    for (const caption of captions) {
      const x0 = msToX(caption.start_ms);
      const x1 = msToX(caption.end_ms);
      const captionWidth = x1 - x0;
      if (captionWidth < 12) continue; // too narrow for legible text
      if (x1 < 0 || x0 > width) continue;

      const isActive = caption.ord === activeOrd;
      ctx.font = `${isActive ? "600 " : ""}11px system-ui, sans-serif`;
      ctx.fillStyle = isActive ? "#1d4ed8" : "#64748b";
      ctx.textBaseline = "top";

      // Clip the text to the sentence's own slice of time, so a long sentence
      // does not bleed into its neighbour.
      ctx.save();
      ctx.beginPath();
      ctx.rect(x0, 0, Math.max(0, captionWidth - 2), CAPTION_BAND);
      ctx.clip();
      ctx.fillText(caption.text, x0 + 2, 3);
      ctx.restore();
    }

    const playX = msToX(positionMs);
    if (playX >= -1 && playX <= width + 1) {
      ctx.fillStyle = "#dc2626";
      ctx.fillRect(Math.min(width - 2, Math.max(0, playX)), 0, 2, CANVAS_HEIGHT);
    }
  }, [peaks, markers, handles, deletions, captions, activeOrd, positionMs, width, msToX, hovered, span, view.from]);

  useEffect(() => {
    draw();
  }, [draw]);

  // Keep the playhead in view while zoomed. Without this, playing a long
  // episode while zoomed in leaves a red line that walked off the screen.
  useEffect(() => {
    if (!zoomed) return;
    setView((current) => {
      if (positionMs >= current.from && positionMs < current.to) return current;
      const currentSpan = current.to - current.from;
      return clampView(
        { from: positionMs - currentSpan * 0.3, to: positionMs + currentSpan * 0.7 },
        total,
      );
    });
  }, [positionMs, zoomed, total]);

  // Wheel zoom needs a non-passive listener: React's synthetic wheel handler
  // cannot preventDefault, so the page would scroll underneath the zoom.
  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    const onWheel = (event: WheelEvent): void => {
      if (event.shiftKey) {
        // Escape hatch: Shift+wheel pans, like a horizontal scrollbar.
        event.preventDefault();
        panTo(view.from + (event.deltaY + event.deltaX) * (span / Math.max(1, width)));
        return;
      }
      const zoomingOut = event.deltaY > 0;
      // At full zoom-out there is nothing left to reveal; let the page scroll.
      if (zoomingOut && span >= total - 1) return;
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      const anchor = xToMs(Math.min(rect.width, Math.max(0, event.clientX - rect.left)));
      zoomAt(anchor, event.deltaY > 0 ? 1 / ZOOM_STEP : ZOOM_STEP);
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, [panTo, span, total, width, xToMs, zoomAt]);

  const msFromClientX = useCallback(
    (clientX: number): number => {
      const canvas = canvasRef.current;
      if (!canvas) return view.from;
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      return Math.round(xToMs(ratio * width));
    },
    [width, xToMs],
  );

  const handleAt = useCallback(
    (clientX: number): EdgeHandle | null => {
      const canvas = canvasRef.current;
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      const px = clientX - rect.left;
      let best: { handle: EdgeHandle; distance: number } | null = null;
      for (const handle of handles) {
        const distance = Math.abs(msToX(handle.ms) - px);
        if (distance <= HANDLE_HIT_PX && (best === null || distance < best.distance)) {
          best = { handle, distance };
        }
      }
      return best?.handle ?? null;
    },
    [handles, msToX],
  );

  const captionAt = useCallback(
    (clientX: number, clientY: number): Caption | null => {
      const canvas = canvasRef.current;
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      if (clientY - rect.top > CAPTION_BAND) return null;
      const ms = msFromClientX(clientX);
      return captions.find((c) => ms >= c.start_ms && ms < c.end_ms) ?? null;
    },
    [captions, msFromClientX],
  );

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>): void => {
    const caption = captionAt(event.clientX, event.clientY);
    if (caption && onCaptionClick) {
      onCaptionClick(caption.ord);
      return;
    }
    const handle = handleAt(event.clientX);
    if (handle) {
      dragRef.current = { handle, previewMs: handle.ms };
      event.currentTarget.setPointerCapture(event.pointerId);
      setTick((value) => value + 1);
      return;
    }
    onSeek(msFromClientX(event.clientX));
  };

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>): void => {
    const drag = dragRef.current;
    if (drag) {
      drag.previewMs = msFromClientX(event.clientX);
      setTick((value) => value + 1);
      return;
    }
    const handle = handleAt(event.clientX);
    setHovered(handle);
    event.currentTarget.style.cursor = handle ? "ew-resize" : "pointer";
  };

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>): void => {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (drag.previewMs !== drag.handle.ms) {
      onEdgeCommit(drag.handle, drag.previewMs);
    }
    setTick((value) => value + 1);
  };

  const windowErrors = loadError;

  return (
    <div className="waveform" ref={containerRef}>
      <canvas
        ref={canvasRef}
        style={{ width: "100%", height: CANVAS_HEIGHT, display: "block", touchAction: "none" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={() => setHovered(null)}
        role="slider"
        tabIndex={0}
        aria-label="音频波形：点击跳转，拖动圆点调整句边界，滚轮缩放"
        aria-valuemin={0}
        aria-valuemax={durationMs}
        aria-valuenow={Math.round(positionMs)}
        onKeyDown={(event) => {
          const anchor = Math.min(total, Math.max(0, positionMs));
          if (event.key === "ArrowLeft") onSeek(Math.max(0, positionMs - span / 20));
          if (event.key === "ArrowRight") onSeek(Math.min(durationMs, positionMs + span / 20));
          if (event.key === "+" || event.key === "=") {
            event.preventDefault();
            zoomAt(anchor, ZOOM_STEP);
          }
          if (event.key === "-" || event.key === "_") {
            event.preventDefault();
            zoomAt(anchor, 1 / ZOOM_STEP);
          }
          if (event.key === "0") {
            event.preventDefault();
            setView({ from: 0, to: total });
          }
          if (event.key === "Home") {
            event.preventDefault();
            panTo(0);
          }
          if (event.key === "End") {
            event.preventDefault();
            panTo(total - span);
          }
        }}
      />
      <Scrollbar view={view} total={total} onPan={panTo} onZoomTo={setView} />
      <div className="waveform-bar">
        <p className="waveform-hint">
          滚轮缩放 · Shift+滚轮平移 · 拖动圆点调整句边界（只有被拖的词会变）· 红色区域是已删除
        </p>
        <div className="waveform-zoom">
          <span className="muted small">{formatSpan(span)}</span>
          <button
            type="button"
            className="ghost small"
            aria-label="缩小波形"
            disabled={span >= total - 1}
            onClick={() => zoomAt(view.from + span / 2, 1 / ZOOM_STEP)}
          >
            −
          </button>
          <button
            type="button"
            className="ghost small"
            aria-label="放大波形"
            disabled={span <= MIN_VISIBLE_MS}
            onClick={() => zoomAt(view.from + span / 2, ZOOM_STEP)}
          >
            +
          </button>
          <button
            type="button"
            className="ghost small"
            aria-label="显示完整波形"
            disabled={!zoomed}
            onClick={() => setView({ from: 0, to: total })}
          >
            全部
          </button>
        </div>
      </div>
      {windowErrors ? <p className="error-text">波形加载失败：{windowErrors}</p> : null}
    </div>
  );
}

/** A scrollbar over the visible window: thumb drag pans, track click pages. */
function Scrollbar({
  view,
  total,
  onPan,
  onZoomTo,
}: {
  view: View;
  total: number;
  onPan: (from: number) => void;
  onZoomTo: (view: View) => void;
}): React.JSX.Element {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ startX: number; startFrom: number } | null>(null);
  const span = Math.max(1, view.to - view.from);
  const { start, size } = scrollFraction(view, total);
  const hidden = size >= 0.999;

  const jumpTo = (clientX: number): void => {
    const track = trackRef.current;
    if (!track) return;
    const rect = track.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    onPan(ratio * total - span / 2);
  };

  return (
    <div
      className={hidden ? "waveform-scroll idle" : "waveform-scroll"}
      ref={trackRef}
      role="scrollbar"
      aria-controls="waveform-canvas"
      aria-orientation="horizontal"
      aria-valuemin={0}
      aria-valuemax={Math.round(total)}
      aria-valuenow={Math.round(view.from)}
      tabIndex={hidden ? -1 : 0}
      onPointerDown={(event) => {
        if (hidden) return;
        const track = trackRef.current;
        if (!track) return;
        const rect = track.getBoundingClientRect();
        const thumbLeft = start * rect.width;
        const thumbWidth = size * rect.width;
        const local = event.clientX - rect.left;
        const onThumb = local >= thumbLeft && local <= thumbLeft + thumbWidth;
        if (onThumb) {
          dragRef.current = { startX: event.clientX, startFrom: view.from };
          event.currentTarget.setPointerCapture(event.pointerId);
        } else {
          jumpTo(event.clientX);
        }
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current;
        const track = trackRef.current;
        if (!drag || !track) return;
        const rect = track.getBoundingClientRect();
        const delta = ((event.clientX - drag.startX) / rect.width) * total;
        onPan(drag.startFrom + delta);
      }}
      onPointerUp={(event) => {
        dragRef.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
      onDoubleClick={() => onZoomTo({ from: 0, to: total })}
    >
      <div
        className="waveform-thumb"
        style={{
          width: `${Math.max(2, size * 100)}%`,
          left: `${start * 100}%`,
        }}
      />
    </div>
  );
}

function formatMs(ms: number): string {
  const total = Math.max(0, Math.round(ms));
  const seconds = Math.floor(total / 1000);
  const millis = total % 1000;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function formatSpan(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} 分钟`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} 秒`;
  return `${Math.round(ms)} 毫秒`;
}
