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
  /**
   * Is an encode running right now? A boolean, not a percentage (round 135): the percentage
   * lives in the export dialog's progress bar, and repeating it in a menu item that closes on
   * click was a place progress could be seen only by accident.
   */
  exporting: boolean;
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
  if (input.exporting) {
    return {
      label: "正在导出成片视频…",
      // The dialog might have been closed while the encode runs (it is 18 minutes; nobody
      // watches a bar for 18 minutes). Saying where to look sends them back to it.
      hint: "在「视频导出」面板里看进度，可以停止",
      disabled: true,
    };
  }
  if (!input.hasEdl) {
    return { label: "导出成片视频…", hint: "正在计算剪辑方案…", disabled: true };
  }
  // Ready — whether or not anything was cut. The film's audio is rendered on demand at
  // export time, so there is nothing to wait for and nothing to do first. The fixed facts
  // (1080p, burned-in subtitles) moved into the dialog, where they can be read before the
  // click instead of as a hint clipped at the menu's edge.
  return {
    label: "导出成片视频…",
    hint: "文件名、文件夹、编码选项",
    disabled: false,
  };
}
