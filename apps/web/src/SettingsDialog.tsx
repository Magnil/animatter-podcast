/**
 * Settings dialog opened from the menu bar.
 *
 * Grouped by *when* a setting takes effect, because that is what the user needs
 * to know: model settings need re-transcription, hotwords need a re-run before
 * they can influence recognition, editing settings apply to the next render,
 * export settings apply at export time. Mixing them in one flat list invites the
 * assumption that changing the ASR model fixes an existing transcript.
 *
 * 热词与校对 exists as a section because the glossary stopped being a
 * write-only list nobody could find: the terms an episode needs are decided
 * right before you transcribe it, and the LLM pass that fixes what ASR still got
 * wrong re-reads the same list. Both belong together, and both belonged in this
 * menu rather than only in the sidebar.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AppSettings, type HotwordItem } from "./api";
import { HotwordEditor, type HotwordTab } from "./HotwordEditor";

/** Which section the menu item asked for; scrolled into view on open. */
export type SettingsSection = "models" | "hotwords" | "cleanup" | "editing" | "export";

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved: (settings: AppSettings) => void;
  /** Absent when no project is loaded; the episode tab is then disabled. */
  projectId?: string;
  mediaId?: string;
  onHotwordsChanged?: () => void;
  section?: SettingsSection;
}

type NumericKey =  | "hotword_weight"
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

export function SettingsDialog({
  open,
  onClose,
  onSaved,
  projectId,
  mediaId,
  onHotwordsChanged,
  section = "models",
}: Props): React.JSX.Element | null {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [models, setModels] = useState<{ id: string; instant_vocabulary: boolean; diarization: boolean }[]>([]);
  const [llmModels, setLlmModels] = useState<string[]>([]);
  const [globalHotwords, setGlobalHotwords] = useState<HotwordItem[]>([]);
  const [episodeHotwords, setEpisodeHotwords] = useState<HotwordItem[]>([]);
  const [hotwordTab, setHotwordTab] = useState<HotwordTab>("global");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const sectionRefs = useRef<Record<string, HTMLElement | null>>({});

  // Two menu items open this dialog ("模型与参数…" and "热词与校对…"); land on
  // the section that was asked for instead of making the user scroll.
  useEffect(() => {
    if (!open || !settings) return;
    const element = sectionRefs.current[section];
    if (element) element.scrollIntoView({ block: "start" });
  }, [open, section, settings]);

  const refreshHotwords = useCallback(async () => {
    const global = await api.globalHotwords();
    setGlobalHotwords(global);
    if (projectId && mediaId) {
      const episode = await api.episodeHotwords(projectId, mediaId);
      setEpisodeHotwords(episode);
    } else {
      setEpisodeHotwords([]);
    }
    onHotwordsChanged?.();
  }, [mediaId, onHotwordsChanged, projectId]);

  // Load once per open, and **only** on open.
  //
  // `refreshHotwords` is a useCallback, but its dependency list reaches a prop
  // the parent builds inline, so its identity changes on every parent render.
  // Depending on it here made the effect re-run constantly and refetch
  // /api/settings each time — which silently overwrote whatever the user was
  // typing, a few hundred milliseconds after they typed it. Keeping the latest
  // callback in a ref lets the effect call it without being re-triggered by it.
  const refreshHotwordsRef = useRef(refreshHotwords);
  refreshHotwordsRef.current = refreshHotwords;

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
    void refreshHotwordsRef.current().catch((caught: unknown) => {
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

  const numeric = (item: (typeof NUMERIC)[number]): React.JSX.Element => (
    <label className="field" key={item.key}>
      <span>
        {item.label}
        <em className="hint">{item.hint}</em>
      </span>
      <input
        type="number"
        min={item.min}
        max={item.max}
        value={settings ? settings[item.key] : item.min}
        onChange={(event) => patch(item.key, Number(event.target.value))}
      />
    </label>
  );

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal modal-wide"
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
            <section ref={(node) => { sectionRefs.current.models = node; }}>
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
                  checked={settings.diarization}
                  onChange={(event) => patch("diarization", event.target.checked)}
                />
                <span>区分说话人</span>
              </label>
              {NUMERIC.slice(1, 2).map(numeric)}
            </section>

            <section
              className="settings-hotwords"
              ref={(node) => { sectionRefs.current.hotwords = node; }}
            >
              <h3>热词与校对</h3>
              <p className="section-note">
                热词是识别的输入：改完要重新转写才会生效。校对用同一份热词让 LLM 再核一遍。
              </p>

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
                  checked={settings.use_proofread}
                  onChange={(event) => patch("use_proofread", event.target.checked)}
                />
                <span>启用 LLM 校对（以热词为依据）</span>
              </label>

              {NUMERIC.slice(0, 1).map(numeric)}
              {NUMERIC.slice(2, 3).map(numeric)}

              <HotwordEditor
                projectId={projectId}
                mediaId={mediaId}
                episodeHotwords={episodeHotwords}
                globalHotwords={globalHotwords}
                tab={hotwordTab}
                onTabChange={setHotwordTab}
                onChanged={refreshHotwords}
                onNotice={(message) => setError(message)}
              />

              <p className="section-note">
                {projectId && mediaId
                  ? "本期热词只对这一期生效；全局热词每期都会带上。"
                  : "还没有打开项目，只能编辑全局热词。"}
              </p>
            </section>

            <section
              className="settings-cleanup"
              ref={(node) => { sectionRefs.current.cleanup = node; }}
            >
              <h3>一键清理</h3>
              <p className="section-note">
                清理是把音频真的剪掉，不是只改文字。改这里只影响<strong>下一次</strong>清理，
                已经剪过的内容不会被追溯改动。
              </p>

              <label className="field">
                <span>
                  口癖词表
                  <em className="hint">每行一个，也可用逗号分隔；大小写与首尾标点不敏感</em>
                </span>
                <textarea
                  rows={10}
                  value={settings.cleanup_fillers}
                  onChange={(event) => patch("cleanup_fillers", event.target.value)}
                  spellCheck={false}
                />
              </label>
              <p className="section-note">
                只放「没有词义的音」（嗯、呃、唔、um…）。「就是说」「然后呢」这类
                大多数时候是正常用词，默认不收——需要时自己加，误删比漏删贵得多。
              </p>

              <label className="field">
                <span>
                  静音判定
                  <em className="hint">词之间超过这个长度才算死气，进清单</em>
                </span>
                <input
                  type="number"
                  min={100}
                  max={3000}
                  step={50}
                  value={settings.cleanup_min_silence_ms}
                  onChange={(event) => patch("cleanup_min_silence_ms", Number(event.target.value))}
                />
              </label>
              <label className="field">
                <span>
                  切点保护
                  <em className="hint">贴着词的两侧各留一点，防止削掉音素</em>
                </span>
                <input
                  type="number"
                  min={0}
                  max={300}
                  step={10}
                  value={settings.cleanup_silence_pad_ms}
                  onChange={(event) => patch("cleanup_silence_pad_ms", Number(event.target.value))}
                />
              </label>
              <label className="field">
                <span>
                  未转写语流最短长度
                  <em className="hint">音频里是语音、稿子里没有字；太短的可能是时间戳抖动</em>
                </span>
                <input
                  type="number"
                  min={100}
                  max={3000}
                  step={50}
                  value={settings.cleanup_min_disfluency_ms}
                  onChange={(event) =>
                    patch("cleanup_min_disfluency_ms", Number(event.target.value))
                  }
                />
              </label>

              <h4 className="settings-subhead">静音检测器</h4>
              <p className="section-note">
                这两个值同时决定「哪里算静音」和「切点吸附到哪个静音谷」。改完要重新检测，
                旧缓存按参数分开存放，不会串。
              </p>
              <label className="field">
                <span>
                  噪声阈值 (dBFS)
                  <em className="hint">比这个更安静才算静音；房间吵就调高（-35 → -30）</em>
                </span>
                <input
                  type="number"
                  min={-80}
                  max={-10}
                  step={1}
                  value={settings.silence_noise_db}
                  onChange={(event) => patch("silence_noise_db", Number(event.target.value))}
                />
              </label>
              <label className="field">
                <span>
                  最短静音时长
                  <em className="hint">检测器本身的门限，通常和上面的「静音判定」一致</em>
                </span>
                <input
                  type="number"
                  min={100}
                  max={3000}
                  step={50}
                  value={settings.silence_min_duration_ms}
                  onChange={(event) =>
                    patch("silence_min_duration_ms", Number(event.target.value))
                  }
                />
              </label>
            </section>

            <section ref={(node) => { sectionRefs.current.editing = node; }}>
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
              {NUMERIC.slice(3, 5).map(numeric)}
            </section>

            <section ref={(node) => { sectionRefs.current.export = node; }}>
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
              {NUMERIC.slice(5).map(numeric)}
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
