/**
 * 导出：audio encoding and subtitle layout.
 *
 * These only apply at export time, which is why they are not in 剪辑 — nothing
 * here changes the project. Subtitle layout lives here rather than in 文本处理
 * because a subtitle is a *timed* artifact; 文本处理 is about text with no clock.
 */

import type { AppSettings } from "../api";
import { NumberField, SectionNote, SelectField, type PanelProps } from "./fields";

export function ExportPanel({ settings, patch }: PanelProps): React.JSX.Element {
  return (
    <div className="settings-panel">
      <SectionNote>仅在导出时生效，不改动工程。</SectionNote>

      <SelectField
        label="音频格式"
        value={settings.export_format}
        options={[
          { value: "m4a", label: "M4A（体积小，有损）" },
          { value: "wav", label: "WAV（无损）" },
        ]}
        onChange={(value) => patch("export_format", value)}
      />
      <SelectField
        label="码率"
        hint={settings.export_format === "wav" ? "WAV 无损，码率不生效" : undefined}
        value={settings.export_bitrate}
        options={(["128k", "192k", "256k", "320k"] as AppSettings["export_bitrate"][]).map(
          (rate) => ({ value: rate, label: rate }),
        )}
        onChange={(value) => patch("export_bitrate", value)}
      />

      <h4 className="settings-subhead">字幕</h4>
      <SectionNote>决定长句怎么拆、每行放几个字；改动只影响导出的字幕文件。</SectionNote>
      <NumberField
        label="字幕语速上限"
        hint="字/秒，超出会标黄"
        min={4}
        max={30}
        value={settings.subtitle_cps}
        onChange={(value) => patch("subtitle_cps", value)}
      />
      <NumberField
        label="每行字数"
        hint="中文一般 18–20"
        min={8}
        max={60}
        value={settings.subtitle_max_line}
        onChange={(value) => patch("subtitle_max_line", value)}
      />
      <NumberField
        label="最多行数"
        hint="两行是常见上限"
        min={1}
        max={4}
        value={settings.subtitle_max_lines}
        onChange={(value) => patch("subtitle_max_lines", value)}
      />
    </div>
  );
}
