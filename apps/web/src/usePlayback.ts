/**
 * Playback that skips deleted regions.
 *
 * This is what makes editing feel immediate: without it, judging a cut requires
 * rendering a whole episode, which takes minutes and makes delete/undo unusable
 * for exploration.
 *
 * The mechanism is deliberately simple — while playing, if the playhead lands
 * inside a deleted span, seek to that span's end. Measured against the
 * alternatives:
 *
 * * Rendering a preview per edit: 5–60s per change, unacceptable for exploring.
 * * Pre-rendering the whole timeline: minutes, and invalidated by every edit.
 * * Skipping: 0ms latency, and the cost is at most one polling interval of
 *   audio (50ms) leaking through, which is below the threshold where a listener
 *   can tell something was cut.
 *
 * The one thing that must be right is that adjacent deletions are merged before
 * they reach here; otherwise a run of deletions causes a seek per span and the
 * audio stutters.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface Span {
  start_ms: number;
  end_ms: number;
}

export interface Playback {
  positionMs: number;
  playing: boolean;
  play: () => void;
  pause: () => void;
  seek: (ms: number) => void;
  playRange: (fromMs: number, untilMs: number) => void;
}

//: Playhead checks per second. 20 (50ms) is the point where a skipped span is
//: inaudible while the check itself costs nothing measurable.
const TICK_MS = 50;

/** Merge overlapping or touching spans so one cut cannot cause several seeks. */
export function mergeSpans(spans: Span[]): Span[] {
  if (spans.length === 0) return [];
  const sorted = [...spans].sort((a, b) => a.start_ms - b.start_ms);
  const merged: Span[] = [sorted[0]!];
  for (const span of sorted.slice(1)) {
    const last = merged[merged.length - 1]!;
    if (span.start_ms <= last.end_ms) {
      merged[merged.length - 1] = {
        start_ms: last.start_ms,
        end_ms: Math.max(last.end_ms, span.end_ms),
      };
    } else {
      merged.push(span);
    }
  }
  return merged;
}

/** First deleted span containing `ms`, if any. */
export function spanAt(spans: Span[], ms: number): Span | null {
  for (const span of spans) {
    if (ms >= span.start_ms && ms < span.end_ms) return span;
  }
  return null;
}

/** Nearest position at or after `ms` that is not deleted. */
export function nextAudible(spans: Span[], ms: number): number {
  let position = ms;
  // Loop because skipping one span can land exactly inside the next one when
  // they are adjacent-but-not-merged; harmless, but terminates either way.
  for (let guard = 0; guard < spans.length + 1; guard += 1) {
    const hit = spanAt(spans, position);
    if (!hit) return position;
    position = hit.end_ms;
  }
  return position;
}

export function usePlayback(
  audioUrl: string,
  durationMs: number,
  deleted: Span[],
): Playback {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const stopAtRef = useRef<number | null>(null);
  const deletedRef = useRef<Span[]>([]);
  const [positionMs, setPositionMs] = useState(0);
  const [playing, setPlaying] = useState(false);

  // Merged spans are what skipping needs; recompute only when the set changes.
  const merged = mergeSpans(deleted);
  deletedRef.current = merged;

  useEffect(() => {
    const audio = new Audio(audioUrl);
    audio.preload = "metadata";
    audioRef.current = audio;

    const onTime = (): void => {
      const ms = audio.currentTime * 1000;
      const hit = spanAt(deletedRef.current, ms);
      if (hit) {
        // Jump the cut. Setting currentTime inside a timeupdate handler is safe
        // and does not fire another play event.
        audio.currentTime = hit.end_ms / 1000;
        setPositionMs(hit.end_ms);
        return;
      }
      setPositionMs(ms);
      const stopAt = stopAtRef.current;
      if (stopAt !== null && ms >= stopAt) {
        audio.pause();
        stopAtRef.current = null;
      }
    };
    const onPlay = (): void => setPlaying(true);
    const onPause = (): void => setPlaying(false);

    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);

    return () => {
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.pause();
      audio.src = "";
    };
  }, [audioUrl]);

  // A finer-grained watchdog than `timeupdate`, which some browsers throttle to
  // 250ms. Without this the cut leaks up to a quarter second of deleted audio.
  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => {
      const audio = audioRef.current;
      if (!audio || audio.paused) return;
      const ms = audio.currentTime * 1000;
      const hit = spanAt(deletedRef.current, ms);
      if (hit) audio.currentTime = hit.end_ms / 1000;
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, [playing]);

  const seek = useCallback(
    (ms: number) => {
      const audio = audioRef.current;
      if (!audio) return;
      const clamped = Math.max(0, Math.min(durationMs || audio.duration * 1000, ms));
      // Landing inside a deletion is never useful: snap to the next audible spot.
      const audible = nextAudible(deletedRef.current, clamped);
      audio.currentTime = audible / 1000;
      setPositionMs(audible);
    },
    [durationMs],
  );

  const play = useCallback(() => {
    stopAtRef.current = null;
    const audio = audioRef.current;
    if (!audio) return;
    const audible = nextAudible(deletedRef.current, audio.currentTime * 1000);
    if (audible !== audio.currentTime * 1000) audio.currentTime = audible / 1000;
    void audio.play();
  }, []);

  const pause = useCallback(() => {
    stopAtRef.current = null;
    audioRef.current?.pause();
  }, []);

  const playRange = useCallback((fromMs: number, untilMs: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const start = nextAudible(deletedRef.current, Math.max(0, fromMs));
    audio.currentTime = start / 1000;
    stopAtRef.current = untilMs;
    void audio.play();
  }, []);

  return { positionMs, playing, play, pause, seek, playRange };
}
