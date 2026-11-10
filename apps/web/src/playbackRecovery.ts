/**
 * What to do when the browser refuses to decode a position.
 *
 * The problem this exists for is narrow but real. On a 61-minute episode the
 * user clicked a sentence and nothing happened — no error, no sound. The cause
 * was three things lining up:
 *
 * 1. Chrome's MP3 decoder returns ``MEDIA_ERR_DECODE`` for a ~30ms window
 *    (measured: 215.14s and 215.20s decode fine, 215.16–215.19s do not). The file
 *    is conformant — ``ffmpeg`` decodes all 153,998 frames with zero errors, and
 *    remuxing does not move the bad window. It is the browser being picky about
 *    one granule.
 * 2. Once an ``<audio>`` element is in the error state, **nothing** recovers it
 *    in place: seeking elsewhere silently does nothing, and ``load()`` followed
 *    by the same position fails again. Only ``load()`` plus a *different*
 *    position works. That is measured, not assumed.
 * 3. ``void audio.play()`` throws away the rejection, so the failure was
 *    invisible.
 *
 * So the recovery has to be "reload, then aim a little past the bad spot". The
 * offsets below are tried in order; 30ms is already enough for this file, the
 * rest are there because the size of the bad window is a property of the file
 * and the codec, not something we get to know in advance.
 *
 * 400ms is the ceiling on purpose: nudging further starts to eat the word the
 * user asked to hear, and a "play this sentence" that silently drops its first
 * syllable is worse than one that says it cannot play.
 */

/** How far past the requested position to retry. Milliseconds, tried in order. */
export const RECOVERY_OFFSETS_MS: readonly number[] = [30, 60, 120, 240, 400];

/**
 * Position to retry at, or ``null`` when the ladder is exhausted.
 *
 * ``attempt`` is 0 for the first retry. Pure so the ladder can be tested without
 * a DOM — the failure mode being guarded against ("we nudge but forget to stop
 * nudging") is invisible to the type checker.
 */
export function recoveryTarget(targetMs: number, attempt: number): number | null {
  if (attempt < 0 || attempt >= RECOVERY_OFFSETS_MS.length) return null;
  const offset = RECOVERY_OFFSETS_MS[attempt];
  if (offset === undefined) return null;
  return targetMs + offset;
}

/** ``HTMLMediaElement.error.code`` values, named. */
export const MEDIA_ERROR = {
  ABORTED: 1,
  NETWORK: 2,
  DECODE: 3,
  SRC_NOT_SUPPORTED: 4,
} as const;

/**
 * Chinese description of a media error, for the notice bar.
 *
 * Decode failures are the only ones we try to recover from, and the caller says
 * "已跳过 N 毫秒" instead — this is for the ones we give up on, so the user is
 * told *why* instead of watching a dead button.
 */
export function describeMediaError(code: number): string {
  switch (code) {
    case MEDIA_ERROR.ABORTED:
      return "音频加载被中断";
    case MEDIA_ERROR.NETWORK:
      return "音频读取失败，检查后端是否还在运行";
    case MEDIA_ERROR.DECODE:
      return "这个位置的音频无法解码";
    case MEDIA_ERROR.SRC_NOT_SUPPORTED:
      return "浏览器不支持这个音频格式";
    default:
      return "音频播放出错";
  }
}
