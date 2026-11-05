/**
 * Waveform rendering — presentation only.
 *
 * Deliberately does NOT use wavesurfer's decodeAudioData path: a 3h episode is
 * ~1.9GB of float PCM once decoded and the tab dies (AGENT.md §7.1). The backend
 * pre-reduces audio to min/max buckets; this component draws the visible window
 * onto a canvas. Playback lives in the parent so keyboard shortcuts can drive it.
 */

import { useEffect, useRef, useState } from "react";
import { api, type Waveform as WaveformData } from "./api";

export interface Marker {
  start_ms: number;
  end_ms: number;
  active: boolean;
}

interface Props {
  projectId: string;
  mediaId: string;
  durationMs: number;
  positionMs: number;
  markers: Marker[];
  onSeek: (ms: number) => void;
}

const CANVAS_HEIGHT = 120;
const BUCKETS_PER_SECOND = 64;

export function Waveform({
  projectId,
  mediaId,
  durationMs,
  positionMs,
  markers,
  onSeek,
}: Props): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

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

  useEffect(() => {
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
    const total = peaks?.duration_ms || durationMs || 1;

    for (const marker of markers) {
      const x0 = (marker.start_ms / total) * width;
      const x1 = (marker.end_ms / total) * width;
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
        const y0 = mid - high * mid;
        const y1 = mid - low * mid;
        ctx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
      }
    }

    const playX = (positionMs / total) * width;
    ctx.fillStyle = "#dc2626";
    ctx.fillRect(Math.min(width - 2, playX), 0, 2, CANVAS_HEIGHT);
  }, [peaks, markers, positionMs, width, durationMs]);

  const seekFromEvent = (clientX: number): void => {
    const canvas = canvasRef.current;
    if (!canvas || !durationMs) return;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    onSeek(Math.round(ratio * durationMs));
  };

  return (
    <div className="waveform" ref={containerRef}>
      <canvas
        ref={canvasRef}
        style={{ width: "100%", height: CANVAS_HEIGHT, cursor: "pointer", display: "block" }}
        onClick={(event) => seekFromEvent(event.clientX)}
        role="slider"
        tabIndex={0}
        aria-label="音频波形，点击跳转"
        aria-valuemin={0}
        aria-valuemax={durationMs}
        aria-valuenow={Math.round(positionMs)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") onSeek(Math.max(0, positionMs - 5000));
          if (event.key === "ArrowRight") onSeek(Math.min(durationMs, positionMs + 5000));
        }}
      />
      {loadError ? <p className="error-text">波形加载失败：{loadError}</p> : null}
    </div>
  );
}
