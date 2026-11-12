/**
 * The 导出 → 导出成片视频… menu entry (round 102).
 *
 * The button lived in the video panel next to a 「成片 1 小时 1 分 / 原始 1 小时 1 分」 line.
 * Both are gone. Exporting a film is a **delivery** action, and every other delivery —
 * 字幕 / Audacity 标签 / ShowNotes / 文稿 / 成片音频 — is already in the 导出 menu. The two
 * durations were the 音频剪辑 panel's own summary repeated one panel higher, so the same
 * fact appeared three times on one screen. What is left in the panel is **arranging** the
 * film, which is what that panel is for.
 *
 * The reason a disabled entry is disabled now has nowhere else to go. A greyed-out menu item
 * looks identical whether the edit is empty, the EDL has not arrived, or an encode is already
 * running — and an unexplained dead control is round 42's defect (「导出成片 点击后无反应」)
 * wearing a new coat. So the entry is derived here, once, and tested, instead of living in a
 * `disabled={a || b || c}` expression nobody re-reads.
 */

export interface VideoExportInputs {
  /** Has the EDL arrived yet? Until it has, there is no duration and no timeline to film. */
  hasEdl: boolean;
  /**
   * There is deliberately **no deletion count** (round 110). An empty edit is a film of the
   * whole episode — the normal end of a proofreading-only session — and the audio the film
   * needs is rendered on demand. Leaving a `deletionCount` in the inputs would invite the
   * condition back; not having it makes that impossible to write by accident.
   */
  /** Live percentage while an encode runs; `null` when idle. */
  exportingPercent: number | null;
}

export interface VideoExportEntry {
  label: string;
  hint: string;
  disabled: boolean;
}

/**
 * Label, hint and enablement for the menu entry.
 *
 * Ordered by what the user can act on. An encode in flight is a *state*, and a missing EDL
 * is a *wait* — everything else is ready to go.
 */
export function videoExportEntry(input: VideoExportInputs): VideoExportEntry {
  if (input.exportingPercent !== null) {
    return {
      label: `正在导出成片视频… ${input.exportingPercent}%`,
      // A one-hour film is ~14 minutes of x264. Saying "you can go do something else" is
      // the only useful thing a disabled entry has to offer for that long.
      hint: "编码在后台跑，可以先做别的",
      disabled: true,
    };
  }
  if (!input.hasEdl) {
    return { label: "导出成片视频…", hint: "正在计算剪辑方案…", disabled: true };
  }
  // Ready — whether or not anything was cut. The film's audio is rendered on demand at
  // export time, so there is nothing to wait for and nothing to do first.
  return {
    label: "导出成片视频…",
    // The two facts the render is fixed at, in the entry itself: there is no dialog to hold
    // them, and a film that turns out to have burned-in subtitles you did not want is not a
    // surprise worth discovering after fourteen minutes of encoding.
    hint: "MP4 1080p，字幕烧进画面",
    disabled: false,
  };
}

/**
 * The name the saved film gets.
 *
 * Derived from the episode, not from the render id: a folder of `vren_1m76jprn….mp4` is
 * unusable a week later, and the render id is never shown anywhere else in the UI.
 *
 * Local date, not `toISOString()`: that would name a film made at 01:00 in Shanghai with
 * yesterday's date, and the user's calendar is the one the film belongs to.
 */
export function filmFilename(episode: string, now: Date): string {
  const cleaned = episode.replace(/[\\/:*?"<>|]/g, "_").trim() || "成片视频";
  const pad = (value: number): string => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
  return `${cleaned}-${stamp}.mp4`;
}
