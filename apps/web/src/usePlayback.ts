/**
 * Audio playback driven by the same clock as the transcript.
 *
 * A plain HTMLAudioElement: seeking streams via HTTP Range, and no decoding of
 * the whole file ever happens. `playRange` powers the "listen to this sentence"
 * workflow that makes proof-reading bearable.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface Playback {
  positionMs: number;
  playing: boolean;
  play: () => void;
  pause: () => void;
  seek: (ms: number) => void;
  playRange: (fromMs: number, untilMs: number) => void;
  isWithinRange: boolean;
}

export function usePlayback(audioUrl: string, durationMs: number): Playback {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const stopAtRef = useRef<number | null>(null);
  const [positionMs, setPositionMs] = useState(0);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    const audio = new Audio(audioUrl);
    audio.preload = "metadata";
    audioRef.current = audio;

    const onTime = (): void => {
      const ms = audio.currentTime * 1000;
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

  const seek = useCallback((ms: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const clamped = Math.max(0, Math.min(durationMs || audio.duration * 1000, ms));
    audio.currentTime = clamped / 1000;
    setPositionMs(clamped);
  }, [durationMs]);

  const play = useCallback(() => {
    stopAtRef.current = null;
    void audioRef.current?.play();
  }, []);

  const pause = useCallback(() => {
    stopAtRef.current = null;
    audioRef.current?.pause();
  }, []);

  const playRange = useCallback(
    (fromMs: number, untilMs: number) => {
      const audio = audioRef.current;
      if (!audio) return;
      audio.currentTime = Math.max(0, fromMs) / 1000;
      stopAtRef.current = untilMs;
      void audio.play();
    },
    [],
  );

  return { positionMs, playing, play, pause, seek, playRange, isWithinRange: false };
}
