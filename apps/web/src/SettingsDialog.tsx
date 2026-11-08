/**
 * Settings, one panel at a time.
 *
 * The old dialog stacked everything into one scrolling page: 22 fields plus
 * three prompt editors, with menu items scrolling to a section that might be
 * below the fold. That worked while there were three sections. By the time it
 * reached six, "设置" was a page you scrolled rather than a place you went, and
 * a menu item called 「热词与校对」 landed you in the middle of unrelated fields.
 *
 * The fix is structural, not cosmetic: **only the selected panel is mounted**.
 * That is what makes the dialog stop growing — a new section adds one entry to
 * the nav and one file, and the page a user sees stays the size of one topic.
 *
 * The nav is ordered by *when* the setting takes effect, which is the question
 * a user actually has: 模型 decides the next transcription, 热词 feeds it,
 * 清理 decides what gets cut, 剪辑 shapes the render, 导出 only applies when
 * writing a file, and 文本处理 is downstream of everything.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type AppSettings, type HotwordItem } from "./api";
import type { HotwordTab } from "./HotwordEditor";
import type { PromptRow } from "./PromptEditor";
import { AlignPanel } from "./settings/AlignPanel";
import { CleanupPanel } from "./settings/CleanupPanel";
import { EditingPanel } from "./settings/EditingPanel";
import { ExportPanel } from "./settings/ExportPanel";
import { HotwordsPanel } from "./settings/HotwordsPanel";
import { ModelsPanel } from "./settings/ModelsPanel";
import { TextPanel } from "./settings/TextPanel";

/** Which panel the menu item asked for. */
export type SettingsSection =
  | "models"
  | "hotwords"
  | "cleanup"
  | "editing"
  | "export"
  | "text"
  | "align";

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved: (settings: AppSettings) => void;
  /** Absent when no project is loaded; the episode hotword tab is then disabled. */
  projectId?: string;
  mediaId?: string;
  onHotwordsChanged?: () => void;
  section?: SettingsSection;
  /** Absent when no project is loaded; 时间轴对齐 then says so instead of acting. */
  versionId?: string;
  /** Reload the transcript after 时间轴对齐 rewrites token times. */
  onTranscriptChanged?: () => Promise<void>;
  onNotice?: (message: string) => void;
}

interface NavEntry {
  id: SettingsSection;
  label: string;
  /** One line saying what this panel governs, shown under the title. */
  summary: string;
}

const NAV: NavEntry[] = [
  // The summary answers "when does this take effect", not "what is in here " —
  // each panel already opens with its own note explaining that, and two
  // sentences saying the same thing read as a stutter.
  { id: "models", label: "模型", summary: "改动只在重新转写后生效" },
  { id: "hotwords", label: "热词与校对", summary: "热词进识别，校对读同一份词表" },
  { id: "cleanup", label: "一键清理", summary: "会真的剪掉音频，只影响下一次清理" },
  { id: "editing", label: "剪辑", summary: "影响下一次渲染与边界吸附" },
  { id: "export", label: "导出", summary: "只在写出文件时生效，不改工程" },
  { id: "text", label: "文本处理", summary: "只在生成 ShowNotes / 文稿时生效" },
  // Applies to a version rather than to preferences, so it is placed last and
  // says so — the other six panels are all "next time" settings.
  { id: "align", label: "时间轴对齐", summary: "直接改写当前版本的词级时间轴（可撤销）" },
];

export function SettingsDialog({
  open,
  onClose,
  onSaved,
  projectId,
  mediaId,
  onHotwordsChanged,
  section = "models",
  versionId,
  onTranscriptChanged,
  onNotice,
}: Props): React.JSX.Element | null {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [models, setModels] = useState<
    { id: string; instant_vocabulary: boolean; diarization: boolean }[]
  >([]);
  const [llmModels, setLlmModels] = useState<string[]>([]);
  const [globalHotwords, setGlobalHotwords] = useState<HotwordItem[]>([]);
  const [episodeHotwords, setEpisodeHotwords] = useState<HotwordItem[]>([]);
  const [prompts, setPrompts] = useState<PromptRow[]>([]);
  const [hotwordTab, setHotwordTab] = useState<HotwordTab>("global");
  const [active, setActive] = useState<SettingsSection>(section);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  /**
   * Which keys the user actually changed.
   *
   * State, not a ref: the footer has to show "N 项未保存" as the user edits, and
   * a ref would leave that count stale. Save writes only these keys — see `save`.
   */
  const [dirty, setDirty] = useState<Set<keyof AppSettings>>(new Set());

  // The menu item decides the initial panel; switching panels afterwards must
  // not be overridden by a re-render of the parent.
  useEffect(() => {
    if (open) setActive(section);
  }, [open, section]);

  const refreshHotwords = useCallback(async () => {
    const global = await api.globalHotwords();
    setGlobalHotwords(global);
    if (projectId && mediaId) {
      setEpisodeHotwords(await api.episodeHotwords(projectId, mediaId));
    } else {
      setEpisodeHotwords([]);
    }
    onHotwordsChanged?.();
  }, [mediaId, onHotwordsChanged, projectId]);

  // Load once per open, and **only** on open.
  //
  // `refreshHotwords` is a useCallback whose dependency list reaches a prop the
  // parent builds inline, so its identity changes on every parent render.
  // Depending on it here made the effect refetch /api/settings constantly, which
  // silently overwrote whatever the user was typing. The ref keeps the latest
  // callback without re-triggering the effect.
  const refreshHotwordsRef = useRef(refreshHotwords);
  refreshHotwordsRef.current = refreshHotwords;

  /**
   * Stable identity for the hotword editor's `onChanged`.
   *
   * The editor lists `onChanged` in an effect's dependency list. Passing the
   * `useCallback` directly is not enough: its own deps include a prop the parent
   * builds inline, so it changes on every parent render, and the editor would
   * re-run that effect mid-edit. A ref-backed wrapper never changes.
   */
  const refreshHotwordsStable = useCallback(
    () => refreshHotwordsRef.current(),
    [],
  );

  useEffect(() => {
    if (!open) return;
    setDirty(new Set());
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
    // Prompts load separately: failing there must not blank the form, and the
    // built-in prompts are still usable without the defaults listed.
    void api
      .scriptPrompts()
      .then((rows) => {
        if (!cancelled) setPrompts(rows);
      })
      .catch(() => {
        if (!cancelled) setPrompts([]);
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

  const patch = useCallback(
    <K extends keyof AppSettings>(key: K, value: AppSettings[K]): void => {
      setDirty((current) => {
        if (current.has(key)) return current;
        const next = new Set(current);
        next.add(key);
        return next;
      });
      setSettings((current) => (current ? { ...current, [key]: value } : current));
    },
    [],
  );

  const save = async (): Promise<void> => {
    if (!settings) return;
    setSaving(true);
    try {
      // Only changed keys are sent. A full-payload PATCH would also write every
      // field whose panel was never opened, and a stale read on one of those
      // would silently revert a change made from the menu in the meantime.
      const values: Record<string, unknown> = {};
      for (const key of dirty) values[key] = settings[key];
      const saved = dirty.size === 0 ? settings : await api.saveSettings(values);
      onSaved(saved);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  };

  const current = useMemo(() => NAV.find((entry) => entry.id === active) ?? NAV[0]!, [active]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal modal-settings"
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
          <div className="settings-layout">
            <nav className="settings-nav" aria-label="设置分类">
              {NAV.map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  className={`settings-nav-item${entry.id === active ? " active" : ""}`}
                  aria-current={entry.id === active ? "page" : undefined}
                  onClick={() => setActive(entry.id)}
                >
                  {entry.label}
                </button>
              ))}
            </nav>

            <div className="settings-body">
              <h3>{current.label}</h3>
              <p className="section-note">{current.summary}</p>

              {active === "models" ? (
                <ModelsPanel
                  settings={settings}
                  patch={patch}
                  models={models}
                  llmModels={llmModels}
                />
              ) : null}

              {active === "hotwords" ? (
                <HotwordsPanel
                  settings={settings}
                  patch={patch}
                  projectId={projectId}
                  mediaId={mediaId}
                  episodeHotwords={episodeHotwords}
                  globalHotwords={globalHotwords}
                  tab={hotwordTab}
                  onTabChange={setHotwordTab}
                  onChanged={() => void refreshHotwordsStable()}
                  onError={setError}
                />
              ) : null}

              {active === "cleanup" ? (
                <CleanupPanel settings={settings} patch={patch} />
              ) : null}

              {active === "editing" ? (
                <EditingPanel settings={settings} patch={patch} />
              ) : null}

              {active === "export" ? (
                <ExportPanel settings={settings} patch={patch} />
              ) : null}

              {active === "text" ? (
                <TextPanel settings={settings} patch={patch} prompts={prompts} />
              ) : null}

              {active === "align" ? (
                <AlignPanel
                  versionId={versionId}
                  onApplied={onTranscriptChanged ?? (async () => undefined)}
                  onNotice={onNotice ?? (() => undefined)}
                />
              ) : null}
            </div>
          </div>
        )}

        <footer className="modal-foot">
          <span className="settings-dirty">
            {active === "align"
              ? // 时间轴对齐 writes immediately, so 保存 would be a lie. Saying it
                // here is cheaper than a user pressing 取消 and expecting a rewind.
                "对齐立即生效，与本面板的「保存」无关"
              : dirty.size > 0
                ? `${dirty.size} 项未保存`
                : ""}
          </span>
          <button type="button" className="ghost" onClick={onClose}>
            {active === "align" ? "关闭" : "取消"}
          </button>
          {active === "align" ? null : (
            <button type="button" disabled={!settings || saving} onClick={() => void save()}>
              {saving ? "保存中…" : "保存"}
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}
