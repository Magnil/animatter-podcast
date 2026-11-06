/**
 * Progress and time-remaining estimates for a long export.
 *
 * A 105-minute episode takes about twelve minutes to encode. The old endpoint ran
 * the encode inside the request, so the only thing the UI could show was a
 * spinner — and a spinner that never moves reads as "the button does nothing",
 * which is exactly how the user reported it.
 *
 * The estimate is measured, not guessed. ffmpeg reports how much of the *output*
 * it has written, and dividing that by the wall time already spent gives the
 * encode's actual throughput. An earlier version extrapolated from the output
 * duration instead, which on this machine over-reported by 8× — encoding runs at
 * roughly eight times real time, so "10 minutes of audio left" was really 70
 * seconds. A wrong countdown is worse than none.
 */

/**
 * Human-readable remaining time.
 *
 * @param elapsedMs  wall time since the encode started
 * @param progress   0..1 of the output timeline written
 */
export function renderEta(elapsedMs: number, progress: number): string {
  // Below ~2 % the throughput estimate is dominated by process startup, and a
  // countdown that lurches from "9 分钟" to "30 秒" is worse than waiting a beat.
  if (progress <= 0.02 || elapsedMs <= 0) return "正在估算…";

  const remainingMs = (elapsedMs * (1 - progress)) / progress;
  const seconds = Math.round(remainingMs / 1000);
  if (seconds < 60) return "不到 1 分钟";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `约 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `约 ${hours} 小时` : `约 ${hours} 小时 ${rest} 分钟`;
}

/** 0..100, clamped. ffmpeg can report slightly past 100 % on a rounded duration. */
export function percentOf(progress: number): number {
  if (!Number.isFinite(progress)) return 0;
  return Math.max(0, Math.min(100, Math.floor(progress * 100)));
}

/**
 * Poll interval: quick at first, then back off.
 *
 * The encode starts within a second of the POST, and that first "正在导出 1%" is
 * what tells the user the click worked. After that a twelve-minute wait does not
 * need a request every 400 ms.
 */
export function pollDelayMs(elapsedMs: number): number {
  return elapsedMs < 5_000 ? 400 : 1_500;
}

/** Is this a terminal render state? */
export function isTerminal(status: string): boolean {
  return status === "done" || status === "failed";
}
