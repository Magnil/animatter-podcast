/** Formatting helpers shared by the editor. */

export function formatTime(ms: number): string {
  const total = Math.max(0, Math.round(ms));
  const hours = Math.floor(total / 3_600_000);
  const minutes = Math.floor((total % 3_600_000) / 60_000);
  const seconds = Math.floor((total % 60_000) / 1000);
  const millis = total % 1000;
  const base = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return hours > 0 ? `${hours}:${base}.${String(millis).padStart(3, "0")}` : `${base}.${String(millis).padStart(3, "0")}`;
}

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

/** Reading speed in characters per second — the metric that flags unreadable subtitles. */
export function charsPerSecond(text: string, startMs: number, endMs: number): number {
  const seconds = Math.max(0.001, (endMs - startMs) / 1000);
  return text.replace(/\s/g, "").length / seconds;
}
