/**
 * 剪辑：how the timeline behaves and how the render is cut.
 *
 * Distinct from 一键清理 on purpose. Cleanup decides *what* to remove and is
 * destructive; this decides how edges snap and how the encode is built, and it
 * is reversible. Grouping them made "切点吸附到静音" look like a cleanup option.
 */

import { CheckboxField, NumberField, SectionNote, type PanelProps } from "./fields";

export function EditingPanel({ settings, patch }: PanelProps): React.JSX.Element {
  return (
    <div className="settings-panel">
      <SectionNote>影响下一次渲染与边界吸附，不改动已完成的剪辑。</SectionNote>

      <CheckboxField
        label="切点吸附到静音"
        hint="拖动句边界时吸附到最近的静音谷，避免切在音素中间"
        checked={settings.snap_to_silence}
        onChange={(value) => patch("snap_to_silence", value)}
      />
      <CheckboxField
        label="试听时跳过已删除片段"
        hint="关掉可以听到原始音频，用来核对剪掉的内容"
        checked={settings.realtime_skip}
        onChange={(value) => patch("realtime_skip", value)}
      />

      <NumberField
        label="切点保护 (ms)"
        hint="防止切掉首尾音素"
        min={0}
        max={200}
        value={settings.guard_ms}
        onChange={(value) => patch("guard_ms", value)}
      />
      <NumberField
        label="最短保留 (ms)"
        hint="更短的片段会爆音"
        min={0}
        max={1000}
        value={settings.min_keep_ms}
        onChange={(value) => patch("min_keep_ms", value)}
      />
    </div>
  );
}
