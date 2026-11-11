/**
 * One zoom/scroll window shared by the audio waveform and the video track.
 *
 * The two tracks draw the **same** timeline: a video block is anchored to original-audio
 * milliseconds, so a block at 12:00 has to appear above the waveform at 12:00. Giving each
 * track its own window meant the marker under the cursor and the block under the cursor
 * were in different places, and "where do I drop this asset" became guesswork — which is
 * exactly how it was reported.
 *
 * So the window lives here, above both components, and both are rendered from it.
 *
 * The per-media memory lives here too, for the same reason it lived in `Waveform`: a drag
 * that re-fetches the transcript remounts the workbench, and a window that resets to "whole
 * file" after every boundary drag is unusable. It stays a plain Map and not storage — a
 * stale window pointing past the end of a re-imported file would be worse than forgetting.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  clampView,
  followView,
  initialView,
  panTo as panToIn,
  shouldRememberView,
  zoomAt as zoomAtIn,
  type View,
} from "./waveView";

const VIEW_MEMORY = new Map<string, View>();

function rememberedView(projectId: string, mediaId: string): View | null {
  const stored = VIEW_MEMORY.get(`${projectId}:${mediaId}`);
  if (stored === null || stored === undefined) return null;
  return { from: stored.from, to: stored.to };
}

export interface TimelineView {
  /** The visible window, in milliseconds on the original (un-cut) timeline. */
  view: View;
  /** Total length the window is clamped into. */
  total: number;
  setView: React.Dispatch<React.SetStateAction<View>>;
  /** Zoom by `factor` (>1 zooms in) keeping `anchorMs` under the cursor. */
  zoomAt: (anchorMs: number, factor: number) => void;
  /** Move the window so it starts at `from`, keeping its length. */
  panTo: (from: number) => void;
  /**
   * Scroll so the playhead is visible, but only while zoomed in.
   *
   * Called from the waveform's playhead effect. It lives here rather than in the
   * waveform because the *video* track has to move with it: a playhead that walks off
   * the shared window would keep following itself while the blocks above slid away.
   */
  followPlayhead: (ms: number) => void;
  /** Back to the whole file. */
  reset: () => void;
}

export function useTimelineView(
  projectId: string,
  mediaId: string,
  durationMs: number,
): TimelineView {
  const total = Math.max(1, durationMs || 1);
  const [view, setView] = useState<View>(() =>
    initialView(rememberedView(projectId, mediaId), durationMs),
  );

  // Only a *different* media file resets the window. Editing a sentence boundary changes
  // tokens, not the file, so it must not touch the zoom — re-zooming after every drag is
  // exactly what the user complained about. A mere length change (re-transcribed version,
  // relinked file) clamps the existing window into the new range instead of starting over.
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

  const followPlayhead = useCallback(
    (ms: number) => {
      setView((current) => followView(current, total, ms));
    },
    [total],
  );

  const reset = useCallback(() => {
    setView({ from: 0, to: total });
  }, [total]);

  return { view, total, setView, zoomAt, panTo, followPlayhead, reset };
}
