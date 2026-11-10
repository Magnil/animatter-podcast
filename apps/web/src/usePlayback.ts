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
 *
 * ## Decode failures
 *
 * A click that produces no sound at all is worse than a click that plays the
 * wrong thing, because there is nothing to react to. On a real 61-minute MP3 the
 * user hit a sentence whose start the browser refused to decode; ``play()``
 * rejected and ``void`` swallowed it, so the transport flipped to 「暂停」 and
 * stayed silent forever.
 *
 * The measured shape of that failure (see ``playbackRecovery.ts``):
 *
 * * only a ~30ms window fails; 20ms either side is fine;
 * * an element that has errored stays broken — seeking elsewhere does nothing,
 *   and ``load()`` back to the same position fails again;
 * * ``load()`` plus a *different* position does work.
 *
 * So every play attempt goes through one path that knows the retry ladder, and
 * says so in the notice bar when it has to use it.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  MEDIA_ERROR,
  describeMediaError,
  recoveryTarget,
} from "./playbackRecovery";

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

/** What the hook reports back to the UI when playback had to improvise. */
export interface PlaybackIssue {
  kind: "recovered" | "failed";
  message: string;
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

/**
 * Resolve once the element has metadata, or after `timeoutMs`.
 *
 * After `load()` the element is back at ``HAVE_NOTHING`` and assigning
 * ``currentTime`` immediately is unreliable — the assignment lands before there
 * is anything to seek in.
 */
function whenReady(audio: HTMLAudioElement, timeoutMs = 8000): Promise<void> {
  if (audio.readyState >= 1) return Promise.resolve();
  return new Promise((resolve) => {
    const finish = (): void => {
      window.clearTimeout(timer);
      audio.removeEventListener("loadedmetadata", finish);
      audio.removeEventListener("error", finish);
      resolve();
    };
    const timer = window.setTimeout(finish, timeoutMs);
    audio.addEventListener("loadedmetadata", finish, { once: true });
    audio.addEventListener("error", finish, { once: true });
  });
}

/**
 * Start playback, and say what happened.
 *
 * The `play()` promise is **not** enough here. When the decoder fails,
 * Chrome leaves the promise pending forever — measured: after a
 * `MEDIA_ERR_DECODE` the promise neither resolved nor rejected in 4.5s. So this
 * races the promise against the element's own `error` event and a short timer,
 * then asks the element what actually happened.
 *
 * Returns:
 * - `ok` — audio is playing;
 * - `decode` — the position could not be decoded, worth retrying elsewhere;
 * - `blocked` — the browser refused the request (no user gesture, or another
 *   play superseded this one). Retrying cannot help.
 */
async function attemptPlay(audio: HTMLAudioElement): Promise<"ok" | "decode" | "blocked"> {
  if (audio.error?.code === MEDIA_ERROR.DECODE) return "decode";

  const playPromise = audio.play();
  const outcome = await Promise.race([
    playPromise.then(
      () => "ok" as const,
      // Any rejection lands here: AbortError (superseded), NotAllowedError (no
      // gesture), NotSupportedError. None of them is fixed by moving the
      // playhead, so they all classify as `blocked`; what distinguishes the
      // retryable case is `audio.error`, not the exception name.
      () => "error" as const,
    ),
    new Promise<"error">((resolve) => {
      audio.addEventListener("error", () => resolve("error"), { once: true });
    }),
    new Promise<"timeout">((resolve) => {
      window.setTimeout(() => resolve("timeout"), PLAY_PROBE_MS);
    }),
  ]);
  // The promise may still be pending; nothing below depends on it settling.
  void playPromise.catch(() => undefined);

  if (outcome === "ok") return "ok";
  // A pending promise plus a decode error is the silent-death case.
  if (audio.error?.code === MEDIA_ERROR.DECODE) return "decode";
  if (outcome === "timeout") {
    // Still pending and no error: it is genuinely playing, just slow to start.
    return audio.paused ? "blocked" : "ok";
  }
  return "blocked";
}

/** How long to wait for `play()` to prove itself before inspecting `error`. */
const PLAY_PROBE_MS = 600;

export function usePlayback(
  audioUrl: string,
  durationMs: number,
  deleted: Span[],
  onIssue?: (issue: PlaybackIssue) => void,
): Playback {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const stopAtRef = useRef<number | null>(null);
  const deletedRef = useRef<Span[]>([]);
  const issueRef = useRef<((issue: PlaybackIssue) => void) | undefined>(onIssue);
  const [positionMs, setPositionMs] = useState(0);
  const [playing, setPlaying] = useState(false);

  // Merged spans are what skipping needs; recompute only when the set changes.
  const merged = mergeSpans(deleted);
  deletedRef.current = merged;
  issueRef.current = onIssue;

  /**
   * Play from `targetMs`, retrying past undecodable positions.
   *
   * Every entry point (play / seek / playRange) funnels here so there is exactly
   * one place that knows an element can be poisoned and exactly one place that
   * can un-poison it.
   */
  const startAt = useCallback(async (targetMs: number, untilMs: number | null): Promise<void> => {
    const audio = audioRef.current;
    if (!audio) return;

    for (let attempt = 0; ; attempt += 1) {
      if (attempt > 0) {
        const retryAt = recoveryTarget(targetMs, attempt - 1);
        if (retryAt === null) {
          issueRef.current?.({
            kind: "failed",
            message: `${describeMediaError(MEDIA_ERROR.DECODE)}，附近也都不行。换一句试试，或导出成片后试听。`,
          });
          return;
        }
        // Ordered deliberately: load() first, *then* aim. Aiming a poisoned
        // element at a new position does nothing at all, which is what made this
        // look like a dead button.
        audio.load();
        await whenReady(audio);
        audio.currentTime = retryAt / 1000;
        if ((await attemptPlay(audio)) === "ok") {
          const skipped = Math.round(retryAt - targetMs);
          issueRef.current?.({
            kind: "recovered",
            message:
              skipped <= 50
                ? "这个位置开头的几十毫秒无法解码，已跳过。"
                : `这个位置有约 ${skipped} 毫秒无法解码，已跳过。`,
          });
          return;
        }
        continue;
      }

      audio.currentTime = nextAudible(deletedRef.current, targetMs) / 1000;
      stopAtRef.current = untilMs;
      const outcome = await attemptPlay(audio);
      if (outcome === "ok") return;
      if (outcome === "blocked") return;
      // outcome === "decode": fall through to the retry ladder.
    }
  }, []);

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
    void startAt(audio.currentTime * 1000, null);
  }, [startAt]);

  const pause = useCallback(() => {
    stopAtRef.current = null;
    audioRef.current?.pause();
  }, []);

  const playRange = useCallback(
    (fromMs: number, untilMs: number) => {
      void startAt(Math.max(0, fromMs), untilMs);
    },
    [startAt],
  );

  return { positionMs, playing, play, pause, seek, playRange };
}
