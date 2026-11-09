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
 * decoded, so the backend pre-reduces it to min/max buckets (AGENTS.md §7.1).
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
import { api } from "./api";
import { hasShape, parseEnvelope, peakBetween, type Envelope } from "./envelope";
import {
  MIN_VISIBLE_MS,
  ZOOM_STEP,
  clampView,
  initialView,
  isZoomed,
  msToX as msToXIn,
  panTo as panToIn,
  scrollFraction,
  shouldRememberView,
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

/**
 * A word hidden from the subtitles but still audible.
 *
 * Drawn as a grey hatched *region*, deliberately unlike a deletion: a deletion is
 * a solid red block because the audio is gone, while here the audio is still
 * there and only the text is missing.
 */
export interface HiddenSpan {
  start_ms: number;
  end_ms: number;
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
  /** Words hidden from the text but still audible, drawn as grey hatched regions. */
  hidden?: HiddenSpan[];
  /** Subtitles drawn on the waveform so text and audio line up visually. */
  captions?: Caption[];
  activeOrd?: number;
  onCaptionClick?: (ord: number) => void;
  /** Called once on drop, with the final time. */
  onEdgeCommit: (handle: EdgeHandle, newMs: number) => void;
  onSeek: (ms: number) => void;
}

/**
 * Zoom/scroll window per media file, kept outside React.
 *
 * The bug this exists for: dragging a sentence boundary re-fetched the
 * transcript, which unmounted the workbench, which rebuilt this component and
 * reset the window to "whole file". The user had to re-zoom after every drag.
 * The mount fix in `App.tsx` removes the cause; this removes the symptom even
 * if something else remounts the component later (project switch, HMR).
 *
 * A plain Map, not storage: a zoom level is not worth persisting across a page
 * reload, and a stale window pointing past the end of a re-imported file would
 * be worse than forgetting.
 */
const VIEW_MEMORY = new Map<string, View>();

function rememberedView(projectId: string, mediaId: string): View | null {
  const stored = VIEW_MEMORY.get(`${projectId}:${mediaId}`);
  if (!stored) return null;
  return { from: stored.from, to: stored.to };
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
  hidden = [],
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
  /**
   * Bumped on every drag move so the canvas redraws the preview position.
   *
   * The value is read by `draw`'s dependency list — that is the whole point of
   * the state. Leaving it out (as it was) meant `setTick` re-rendered the
   * component but `draw`'s identity never changed, so the effect that calls it
   * never re-ran: the handle moved in `dragRef` and nothing was painted. The
   * drag looked frozen until release, which read as 「按住边界卡一下」.
   */
  const [tick, setTick] = useState(0);

  const [peaks, setPeaks] = useState<Envelope | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [width, setWidth] = useState(800);
  // Restored from the per-media memory below, so the window survives even a
  // remount (project switch, hot reload) and not just a re-render.
  const [view, setView] = useState<View>(() =>
    initialView(rememberedView(projectId, mediaId), durationMs),
  );

  const total = Math.max(1, peaks?.durationMs || durationMs || 1);
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

  // Only a *different* media file resets the window. Editing a sentence boundary
  // changes tokens, not the file, so it must not touch the zoom — re-zooming after
  // every drag is exactly what the user complained about.
  //
  // `durationMs` is in the dependency list but deliberately does *not* reset:
  // when it merely changes (a re-transcribed version, a relinked file with a
  // slightly different length) the existing window is clamped into the new range
  // instead. It used to call `initialView(...)`, which reads the remembered entry
  // or falls back to "whole file" — so a length change threw away a zoom without
  // anybody asking for it. Losing the window is only ever acceptable when the
  // audio is genuinely a different file.
  const mediaKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const key = `${projectId}:${mediaId}`;
    if (mediaKeyRef.current === key) {
      setView((current) => clampView(current, total));
      return;
    }
    mediaKeyRef.current = key;
    setView(initialView(rememberedView(projectId, mediaId), durationMs));
  }, [projectId, mediaId, durationMs, total]);

  // Remember the window per media file. `App` now keeps the workbench mounted
  // across reloads, so this is the second line of defence: it also covers the
  // case where the component really is torn down and rebuilt.
  useEffect(() => {
    if (!shouldRememberView(view, total)) {
      // Full view is the default; remembering it would just be noise.
      VIEW_MEMORY.delete(`${projectId}:${mediaId}`);
      return;
    }
    VIEW_MEMORY.set(`${projectId}:${mediaId}`, view);
  }, [projectId, mediaId, view, total]);

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

  // Fetch the whole envelope once per media file.
  //
  // This replaces a per-window fetch that fired on every zoom notch. Measured
  // on a real 105-minute episode: 121–152 ms per notch, because each one waited
  // for a round trip. Now the download happens once and zooming never touches
  // the network again.
  //
  // No debounce and no cancellation token: there is exactly one request per
  // media file, so the machinery that used to coalesce a burst has nothing left
  // to coalesce.
  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    void api
      .waveformEnvelope(projectId, mediaId)
      .then((buffer) => {
        if (cancelled) return;
        setPeaks(parseEnvelope(buffer));
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setLoadError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, mediaId]);

  const msToX = useCallback((ms: number) => msToXIn(ms, view, width), [view, width]);
  const xToMs = useCallback((x: number) => xToMsIn(x, view, width), [view, width]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const dpr = window.devicePixelRatio || 1;
    const pixelWidth = Math.round(width * dpr);
    const pixelHeight = Math.round(CANVAS_HEIGHT * dpr);
    // Assigning `canvas.width` reallocates the whole backing store and wipes it
    // — on every draw. During a drag that was a fresh ~1.6 MB bitmap 60 times a
    // second for a resize that had not happened.
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }

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

    // Cull to the visible window before drawing. At full zoom-out all 1074
    // sentence bands are on screen and this saves nothing; zoomed in it is the
    // difference between touching 1074 rects and touching the dozen that show.
    const viewTo = view.from + span;
    for (const marker of markers) {
      if (marker.end_ms < view.from || marker.start_ms > viewTo) continue;
      band(marker.start_ms, marker.end_ms, marker.active ? "rgba(59,130,246,0.18)" : "rgba(148,163,184,0.10)");
    }

    // Deleted regions sit above the sentence bands so a cut is unmistakable.
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(220,38,38,0.7)";
    for (const deleted of deletions) {
      if (deleted.end_ms < view.from || deleted.start_ms > viewTo) continue;
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

    if (!hasShape(peaks)) {
      ctx.fillStyle = "#cbd5e1";
      ctx.fillRect(0, mid - 1, width, 2);
    } else {
      // One column = one `peakBetween` over that column's own span. Reading
      // through the envelope's mip levels (rather than an array index computed
      // from a server-snapped window) is what lets the shape stay correct at
      // every zoom without asking the server to re-slice anything.
      //
      // Columns go into one Path2D and one `fill()`. Setting `fillStyle` inside
      // the loop is what used to make this ~1.5 ms per frame instead of ~0.4:
      // every assignment is a state change the canvas has to honour.
      const msPerPixel = span / width;
      const bars = new Path2D();
      for (let x = 0; x < width; x += 1) {
        const from = view.from + x * msPerPixel;
        const amp = peakBetween(peaks, from, from + msPerPixel);
        if (amp <= 0) continue;
        const h = Math.max(1, amp * waveHeight);
        bars.rect(x, mid - h / 2, 1, h);
      }
      ctx.fillStyle = "#475569";
      ctx.fill(bars);
    }

    // Words hidden from the text, still audible.
    //
    // A *region*, not a mark. The first version drew a single line at the start of
    // each run, which answered "something happens here" but not "until where" — the
    // user had to guess where it ended. Grey wash + 45° hatch + both edges.
    //
    // Still deliberately unlike a deletion: that is a solid red block with no
    // pattern, because the audio is gone. Here the fill is a light wash and the
    // hatch reads as "striped out of the text", which is exactly what happened —
    // the audio underneath is untouched and still visible through it.
    //
    // Drawn *after* the wave so the fill tints the bars instead of being covered by
    // them; captions are painted later still, so subtitle text stays legible on
    // top. Goes *before* the boundary handles so their grips stay on top.
    ctx.save();
    for (const span of hidden) {
      if (span.end_ms < view.from || span.start_ms > viewTo) continue;
      const x0 = msToX(span.start_ms);
      const x1 = msToX(span.end_ms);
      if (x1 < 0 || x0 > width) continue;
      // A 200 ms word is sub-pixel at full zoom-out. Widening it to 2 px keeps the
      // region visible without pretending it covers more time than it does.
      const left = Math.max(0, x0);
      const right = Math.min(width, Math.max(x1, x0 + 2));
      const regionWidth = right - left;

      ctx.fillStyle = "rgba(148,163,184,0.22)";
      ctx.fillRect(left, 0, regionWidth, CANVAS_HEIGHT);

      // Hatch: 45° lines, clipped to the region. Clipping rather than computing
      // each line's span keeps the diagonals continuous across the whole height
      // and stops them bleeding into the neighbouring audio.
      //
      // Only ~6 px apart on purpose: a hidden run is often a couple of words, which
      // at a normal zoom is 10–30 px wide. Wider spacing and some regions would
      // catch no diagonal at all and read as a plain grey block — i.e. as a
      // deletion.
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, 0, regionWidth, CANVAS_HEIGHT);
      ctx.clip();
      ctx.strokeStyle = "rgba(71,85,105,0.32)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      const spacing = 6;
      for (let x = left - CANVAS_HEIGHT; x < right + CANVAS_HEIGHT; x += spacing) {
        ctx.moveTo(x + 0.5, CANVAS_HEIGHT);
        ctx.lineTo(x + 0.5 + CANVAS_HEIGHT, 0);
      }
      ctx.stroke();
      ctx.restore();

      // Both edges, so the extent is unambiguous even when the hatch is dense.
      // 1 device pixel, not 1 CSS pixel: at dpr 1.5 a CSS-pixel edge is 1.5 device
      // pixels wide and picks up antialiasing on both sides, which at this zoom
      // read as a fuzzy grey band rather than an edge.
      ctx.fillStyle = "rgba(100,116,139,0.55)";
      const edgeWidth = 1 / (window.devicePixelRatio || 1);
      if (x0 >= 0 && x0 <= width) ctx.fillRect(Math.round(x0), 0, edgeWidth, CANVAS_HEIGHT);
      if (x1 >= 0 && x1 <= width) {
        ctx.fillRect(Math.round(x1) - edgeWidth, 0, edgeWidth, CANVAS_HEIGHT);
      }
    }
    ctx.restore();

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
      if (caption.end_ms < view.from || caption.start_ms > viewTo) continue;
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
  }, [peaks, markers, handles, deletions, hidden, captions, activeOrd, positionMs, width, msToX, hovered, span, view.from, tick]);

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
        aria-label="音频波形：点击跳转，拖动圆点调整句边界，滚轮缩放（放大缩小只能用滚轮和下方按钮）"
        aria-valuemin={0}
        aria-valuemax={durationMs}
        aria-valuenow={Math.round(positionMs)}
        onKeyDown={(event) => {
          // Only seeking and panning. Zoom and the reset to "whole file" are
          // deliberately *not* on the keyboard: the window is something the user
          // sets up once and expects to stay put, and every extra binding is one
          // more way to lose it by accident (a stray key while the canvas has
          // focus). Wheel over the canvas and the − / + / 全部 buttons are the only
          // two entries, and both are explicit targets.
          if (event.key === "ArrowLeft") onSeek(Math.max(0, positionMs - span / 20));
          if (event.key === "ArrowRight") onSeek(Math.min(durationMs, positionMs + span / 20));
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
      <Scrollbar view={view} total={total} onPan={panTo} />
      <div className="waveform-bar">
        <p className="waveform-hint">
          滚轮缩放 · Shift+滚轮平移 · 拖动圆点调整句边界（只有被拖的词会变）· 红色区域是已删除 ·
          灰色斜线区域是已隐去（音频还在）
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

/**
 * A scrollbar over the visible window: thumb drag pans, track click pages.
 *
 * Panning only. It used to reset to "whole file" on double-click, and that was a
 * trap: clicking the progress bar twice in a row (or missing the thumb and
 * hitting the track again) silently threw away a zoom the user had spent a while
 * dialling in. Reset is the 全部 button's job — a deliberate target you cannot hit
 * by accident. Nothing on this control changes the *span*, only its position.
 */
function Scrollbar({
  view,
  total,
  onPan,
}: {
  view: View;
  total: number;
  onPan: (from: number) => void;
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
