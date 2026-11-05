/**
 * Settings dialog opened from the menu bar.
 *
 * Grouped by *when* a setting takes effect, because that is what the user needs
 * to know: model settings need re-transcription, editing settings apply to the
 * next render, export settings apply at export time. Mixing them in one flat
 * list invites the assumption that changing the ASR model fixes an existing
 * transcript.
 */

import { useEffect, useState } from "react";
import { api, type AppSettings } from "./api";

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved: (settings: AppSettings) => void;
}

type NumericKey =
  | "hotword_weight"
  | "speaker_count"
  | "proofread_chunk_size"
  | "guard_ms"
  | "min_keep_ms"
  | "subtitle_cps"
  | "subtitle_max_line"
  | "subtitle_max_lines";

const NUMERIC: { key: NumericKey; label: string; min: number; max: number; hint: string }[] = [
  { key: "hotword_weight", label: "热词权重", min: 1, max: 50, hint: "实测 5 会矫枉过正，建议 3" },
  { key: "speaker_count", label: "说话人数", min: 1, max: 8, hint: "已知人数比自动检测准" },
  { key: "proofread_chunk_size", label: "校对每批句数", min: 5, max: 50, hint: "批太大模型会漏改" },
  { key: "guard_ms", label: "切点保护 (ms)", min: 0, max: 200, hint: "防止切掉首尾音素" },
  { key: "min_keep_ms", label: "最短保留 (ms)", min: 0, max: 1000, hint: "更短的片段会爆音" },
  { key: "subtitle_cps", label: "字幕语速上限", min: 4, max: 30, hint: "字/秒，超出会标黄" },
  { key: "subtitle_max_line", label: "每行字数", min: 8, max: 60, hint: "中文一般 18–20" },
  { key: "subtitle_max_lines", label: "最多行数", min: 1, max: 4, hint: "两行是常见上限" },
];

export function SettingsDialog({ open, onClose, onSaved }: Props): React.JSX.Element | null {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [models, setModels] = useState<{ id: string; instant_vocabulary: boolean; diarization: boolean }[]>([]);
  const [llmModels, setLlmModels] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void Promise.all([api.settings(), api.modelOptions()])
      .then(([loaded, options]) => {
        if (cancelled) return;
        setSettings(loaded);
        setModels(options.asr);
        setLlmModels(options.llm);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const patch = <K extends keyof AppSettings>(key: K, value: AppSettings[K]): void => {
    setSettings((current) => (current ? { ...current, [key]: value } : current));
  };

  const save = async (): Promise<void> => {
    if (!settings) return;
    setSaving(true);
    try {
      const saved = await api.saveSettings(settings);
      onSaved(saved);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="设置"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>设置</h2>
          <button type="button" className="ghost" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </header>

        {error ? <p className="error-text">{error}</p> : null}
        {!settings ? (
          <p className="muted">载入中…</p>
        ) : (
          <div className="modal-body">
            <section>
              <h3>模型</h3>
              <p className="section-note">改动后需要重新转写才会生效。</p>
              <label className="field">
                <span>语音识别模型</span>
                <select
                  value={settings.asr_model}
                  onChange={(event) => patch("asr_model", event.target.value)}
                >
                  {models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.id}
                      {model.instant_vocabulary ? " · 支持热词" : " · 不支持热词"}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>语言模型</span>
                <select
                  value={settings.llm_model}
                  onChange={(event) => patch("llm_model", event.target.value)}
                >
                  {llmModels.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field checkbox">
                <input
                  type="checkbox"
                  checked={settings.use_hotwords}
                  onChange={(event) => patch("use_hotwords", event.target.checked)}
                />
                <span>转写时启用热词</span>
              </label>
              <label className="field checkbox">
                <input
                  type="checkbox"
                  checked={settings.diarization}
                  onChange={(event) => patch("diarization", event.target.checked)}
                />
                <span>区分说话人</span>
              </label>
            </section>

            <section>
              <h3>剪辑参数</h3>
              <p className="section-note">影响下一次渲染与边界吸附。</p>
              <label className="field checkbox">
                <input
                  type="checkbox"
                  checked={settings.snap_to_silence}
                  onChange={(event) => patch("snap_to_silence", event.target.checked)}
                />
                <span>切点吸附到静音</span>
              </label>
              <label className="field checkbox">
                <input
                  type="checkbox"
                  checked={settings.realtime_skip}
                  onChange={(event) => patch("realtime_skip", event.target.checked)}
                />
                <span>试听时跳过已删除片段</span>
              </label>
              {NUMERIC.slice(0, 5).map((item) => (
                <label className="field" key={item.key}>
                  <span>
                    {item.label}
                    <em className="hint">{item.hint}</em>
                  </span>
                  <input
                    type="number"
                    min={item.min}
                    max={item.max}
                    value={settings[item.key]}
                    onChange={(event) => patch(item.key, Number(event.target.value))}
                  />
                </label>
              ))}
            </section>

            <section>
              <h3>导出</h3>
              <p className="section-note">仅在导出时生效，不改动工程。</p>
              <label className="field">
                <span>音频格式</span>
                <select
                  value={settings.export_format}
                  onChange={(event) =>
                    patch("export_format", event.target.value as AppSettings["export_format"])
                  }
                >
                  <option value="m4a">M4A（体积小，有损）</option>
                  <option value="wav">WAV（无损）</option>
                </select>
              </label>
              <label className="field">
                <span>码率</span>
                <select
                  value={settings.export_bitrate}
                  onChange={(event) =>
                    patch("export_bitrate", event.target.value as AppSettings["export_bitrate"])
                  }
                >
                  {["128k", "192k", "256k", "320k"].map((rate) => (
                    <option key={rate} value={rate}>
                      {rate}
                    </option>
                  ))}
                </select>
              </label>
              {NUMERIC.slice(5).map((item) => (
                <label className="field" key={item.key}>
                  <span>
                    {item.label}
                    <em className="hint">{item.hint}</em>
                  </span>
                  <input
                    type="number"
                    min={item.min}
                    max={item.max}
                    value={settings[item.key]}
                    onChange={(event) => patch(item.key, Number(event.target.value))}
                  />
                </label>
              ))}
            </section>
          </div>
        )}

        <footer className="modal-foot">
          <button type="button" className="ghost" onClick={onClose}>
            取消
          </button>
          <button type="button" disabled={!settings || saving} onClick={() => void save()}>
            {saving ? "保存中…" : "保存"}
          </button>
        </footer>
      </div>
    </div>
  );
}
