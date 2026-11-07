/**
 * 一键清理：how the one-click cleanup decides what to cut.
 *
 * Separate panel because it is the only place in this dialog where saving has a
 * destructive consequence. Everything else adjusts how future output *looks*;
 * this decides what gets removed from the audio. Giving it a page of its own is
 * the cheapest way to make that distinction felt.
 */

import { NumberField, SectionNote, TextField, type PanelProps } from "./fields";

export function CleanupPanel({ settings, patch }: PanelProps): React.JSX.Element {
  return (
    <div className="settings-panel">
      <SectionNote>
        清理是把音频<strong>真的剪掉</strong>，不是只改文字。改这里只影响下一次清理，已经剪过的内容不会被追溯改动。
      </SectionNote>

      <TextField
        label="口癖词表"
        hint="每行一个，也可用逗号分隔；大小写与首尾标点不敏感"
        rows={10}
        mono
        value={settings.cleanup_fillers}
        onChange={(value) => patch("cleanup_fillers", value)}
      />
      <SectionNote>
        只放「没有词义的音」（嗯、呃、唔、um…）。「就是说」「然后呢」这类大多数时候是正常用词，
        默认不收——需要时自己加，误删比漏删贵得多。
      </SectionNote>

      <NumberField
        label="静音判定 (ms)"
        hint="词之间超过这个长度才算死气，进清单"
        min={100}
        max={3000}
        step={50}
        value={settings.cleanup_min_silence_ms}
        onChange={(value) => patch("cleanup_min_silence_ms", value)}
      />
      <NumberField
        label="切点保护 (ms)"
        hint="贴着词的两侧各留一点，防止削掉音素"
        min={0}
        max={300}
        step={10}
        value={settings.cleanup_silence_pad_ms}
        onChange={(value) => patch("cleanup_silence_pad_ms", value)}
      />
      <NumberField
        label="未转写语流最短长度 (ms)"
        hint="音频里是语音、稿子里没有字；太短的可能是时间戳抖动"
        min={100}
        max={3000}
        step={50}
        value={settings.cleanup_min_disfluency_ms}
        onChange={(value) => patch("cleanup_min_disfluency_ms", value)}
      />

      <h4 className="settings-subhead">静音检测器</h4>
      <SectionNote>
        这两个值同时决定「哪里算静音」和「切点吸附到哪个静音谷」。改完要重新检测，旧缓存按参数分开存放，不会串。
      </SectionNote>
      <NumberField
        label="噪声阈值 (dBFS)"
        hint="比这个更安静才算静音；房间吵就调高（-35 → -30）"
        min={-80}
        max={-10}
        step={1}
        value={settings.silence_noise_db}
        onChange={(value) => patch("silence_noise_db", value)}
      />
      <NumberField
        label="最短静音时长 (ms)"
        hint="检测器本身的门限，通常和上面的「静音判定」一致"
        min={100}
        max={3000}
        step={50}
        value={settings.silence_min_duration_ms}
        onChange={(value) => patch("silence_min_duration_ms", value)}
      />
    </div>
  );
}
