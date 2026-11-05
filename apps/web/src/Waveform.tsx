/**
 * Waveform rendering with draggable boundary handles.
 *
 * Drag targets are the **token edges that coincide with a sentence boundary** —
 * the first token's start and the last token's end. That is exactly the case
 * where ASR timing is most often wrong (it cuts a sentence too early or too
 * late), and it is the only case where adjusting one edge does not require
 * touching neighbouring words.
 *
 * Rendering stays canvas-based: a 3h episode is ~1.9GB of float PCM once
 * decoded, so the backend pre-reduces it to min/max buckets (AGENT.md §7.1).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Waveform as WaveformData } from "./api";

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

interface Props {
  projectId: string;
  mediaId: string;
  durationMs: number;
  positionMs: number;
  markers: Marker[];
  handles: EdgeHandle[];
  /** Called once on drop, with the final time. */
  onEdgeCommit: (handle: EdgeHandle, newMs: number) => void;
  onSeek: (ms: number) => void;
}

const CANVAS_HEIGHT = 140;
const BUCKETS_PER_SECOND = 64;
const HANDLE_HIT_PX = 6;

export function Waveform({
  projectId,
  mediaId,
  durationMs,
  positionMs,
  markers,
  handles,
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

  useEffect(() => {
    let cancelled = false;
    setPeaks(null);
    setLoadError(null);
    void api
      .waveform(projectId, mediaId, BUCKETS_PER_SECOND, 0, durationMs)
      .then((payload) => {
        if (!cancelled) setPeaks(payload);
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, mediaId, durationMs]);

  const total = peaks?.duration_ms || durationMs || 1;
  const msToX = useCallback((ms: number) => (ms / total) * width, [total, width]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = CANVAS_HEIGHT * dpr;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, CANVAS_HEIGHT);

    const mid = CANVAS_HEIGHT / 2;

    for (const marker of markers) {
      const x0 = msToX(marker.start_ms);
      const x1 = msToX(marker.end_ms);
      ctx.fillStyle = marker.active ? "rgba(59,130,246,0.18)" : "rgba(148,163,184,0.10)";
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), CANVAS_HEIGHT);
    }

    if (!peaks || peaks.min.length === 0) {
      ctx.fillStyle = "#cbd5e1";
      ctx.fillRect(0, mid - 1, width, 2);
    } else {
      const count = peaks.min.length;
      const perPixel = count / width;
      ctx.fillStyle = "#475569";
      for (let x = 0; x < width; x += 1) {
        const from = Math.floor(x * perPixel);
        const to = Math.min(count, Math.max(from + 1, Math.floor((x + 1) * perPixel)));
        let low = 0;
        let high = 0;
        for (let i = from; i < to; i += 1) {
          low = Math.min(low, peaks.min[i] ?? 0);
          high = Math.max(high, peaks.max[i] ?? 0);
        }
        ctx.fillRect(x, mid - high * mid, 1, Math.max(1, (high - low) * mid));
      }
    }

    // Boundary handles.
    const drag = dragRef.current;
    for (const handle of handles) {
      const isDragging = drag?.handle.tokenIndex === handle.tokenIndex && drag.handle.edge === handle.edge;
      const ms = isDragging && drag ? drag.previewMs : handle.ms;
      const x = msToX(ms);
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

    const playX = msToX(positionMs);
    ctx.fillStyle = "#dc2626";
    ctx.fillRect(Math.min(width - 2, playX), 0, 2, CANVAS_HEIGHT);
  }, [peaks, markers, handles, positionMs, width, msToX, hovered]);

  useEffect(() => {
    draw();
  }, [draw]);

  const msFromClientX = useCallback(
    (clientX: number): number => {
      const canvas = canvasRef.current;
      if (!canvas) return 0;
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      return Math.round(ratio * total);
    },
    [total],
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

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>): void => {
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
        aria-label="音频波形：点击跳转，拖动圆点调整句边界"
        aria-valuemin={0}
        aria-valuemax={durationMs}
        aria-valuenow={Math.round(positionMs)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") onSeek(Math.max(0, positionMs - 5000));
          if (event.key === "ArrowRight") onSeek(Math.min(durationMs, positionMs + 5000));
        }}
      />
      {loadError ? <p className="error-text">波形加载失败：{loadError}</p> : null}
      <p className="waveform-hint">
        拖动圆点可调整句边界；蓝线为首尾词，只有被拖的那个词的时间会变。
      </p>
    </div>
  );
}

function formatMs(ms: number): string {
  const total = Math.max(0, Math.round(ms));
  const seconds = Math.floor(total / 1000);
  const millis = total % 1000;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}
