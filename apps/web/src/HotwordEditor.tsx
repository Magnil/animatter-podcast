/**
 * Hotword editor: the global glossary and one episode's list, side by side.
 *
 * Extracted from the sidebar panel so the same editing surface can appear in
 * three places that must agree: the workbench sidebar, the new-project dialog
 * (before a transcription runs, when the terms still change the audio), and
 * 设置 → 热词与校对. Duplicating the widget would let the three drift.
 *
 * Global and episode are separate tabs because putting a guest's name in the
 * global list pollutes every future episode's transcription — the single most
 * common mistake this panel exists to prevent.
 */

import { useCallback, useState } from "react";
import { api, type HotwordItem } from "./api";

export type HotwordTab = "episode" | "global";

export interface HotwordEditorProps {
  /** Absent before a project exists; the episode tab is then disabled. */
  projectId?: string;
  /** Absent until the audio is registered; the episode tab is then disabled. */
  mediaId?: string;
  episodeHotwords: HotwordItem[];
  globalHotwords: HotwordItem[];
  tab: HotwordTab;
  onTabChange: (tab: HotwordTab) => void;
  onChanged: () => Promise<void>;
  onNotice: (message: string) => void;
  /**
   * Override how episode terms are written. The new-project dialog uses this
   * to keep a local draft before any media row exists: creating the project
   * early would leave an orphan if the user then cancels.
   */
  onAddEpisode?: (text: string) => Promise<void>;
  onRemoveEpisode?: (item: HotwordItem) => Promise<void>;
  /** Show the outline textarea that extracts candidates with the LLM. */
  allowOutline?: boolean;
  /** Hide the tab bar when the caller already chose the list. */
  hideTabs?: boolean;
  /** Compact stops at the input + chips; used inside dialogs. */
  compact?: boolean;
  disabled?: boolean;
}

export function HotwordEditor({
  projectId,
  mediaId,
  episodeHotwords,
  globalHotwords,
  tab,
  onTabChange,
  onChanged,
  onNotice,
  onAddEpisode,
  onRemoveEpisode,
  allowOutline = false,
  hideTabs = false,
  compact = false,
  disabled = false,
}: HotwordEditorProps): React.JSX.Element {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [outline, setOutline] = useState("");
  const [showOutline, setShowOutline] = useState(false);

  const episodeReady = Boolean(projectId && mediaId) || Boolean(onAddEpisode);
  const usable = tab === "global" || episodeReady;
  const items = tab === "episode" ? episodeHotwords : globalHotwords;

  const add = useCallback(async () => {
    const text = draft.trim();
    if (!text || !usable) return;
    setBusy(true);
    try {
      if (tab === "global") {
        await api.addGlobalHotword(text);
      } else if (onAddEpisode) {
        await onAddEpisode(text);
      } else if (projectId && mediaId) {
        await api.addEpisodeHotword(projectId, mediaId, text);
      }
      setDraft("");
      await onChanged();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }, [draft, mediaId, onAddEpisode, onChanged, onNotice, projectId, tab, usable]);

  const remove = async (item: HotwordItem): Promise<void> => {
    setBusy(true);
    try {
      if (tab === "global") {
        await api.removeGlobalHotword(item.text);
      } else if (onRemoveEpisode) {
        await onRemoveEpisode(item);
      } else if (projectId && mediaId) {
        await api.removeEpisodeHotword(projectId, mediaId, item.text);
      }
      await onChanged();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const generateFromOutline = async (): Promise<void> => {
    if (!outline.trim() || !projectId || !mediaId) return;
    setBusy(true);
    try {
      await api.saveOutline(projectId, mediaId, outline);
      // `/hotwords/suggest` only *proposes*; nothing is written until the accept
      // call below. The old flow stopped after the proposal and then refetched,
      // so the notice said "提取了 N 个热词" while the list stayed empty — the
      // extraction ran, the terms were thrown away.
      const result = await api.suggestFromOutline(projectId, mediaId, outline);
      if (result.terms.length === 0) {
        onNotice(
          result.warnings[0] ?? "大纲里没有提取到可用热词，可以手动添加",
        );
        return;
      }
      const accepted = await api.acceptSuggestions(
        projectId,
        mediaId,
        result.terms.map((term) => ({ text: term.text, weight: term.weight })),
      );
      await onChanged();
      const extra = result.rejected.length > 0 ? `，${result.rejected.length} 个未通过校验` : "";
      onNotice(`从大纲提取了 ${accepted.episode_count} 个热词${extra}`);
      setShowOutline(false);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const locked = disabled || busy;

  return (
    <div className={compact ? "hotword-editor compact" : "hotword-editor"}>
      {hideTabs ? null : (
        <div className="tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "episode"}
            className={tab === "episode" ? "tab active" : "tab"}
            onClick={() => onTabChange("episode")}
          >
            本期 {episodeHotwords.length}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "global"}
            className={tab === "global" ? "tab active" : "tab"}
            onClick={() => onTabChange("global")}
          >
            全局 {globalHotwords.length}
          </button>
        </div>
      )}

      <p className="panel-note">
        {tab === "episode"
          ? episodeReady
            ? "只对这一期生效：人名、专有名词、本期术语。"
            : "登记音频后才能添加本期热词；也可以先用全局热词。"
          : "所有期共用：播客名、固定栏目名。别放本期嘉宾的名字。"}
      </p>

      <div className="hotword-input">
        <input
          value={draft}
          placeholder={tab === "episode" ? "添加本期热词…" : "添加全局热词…"}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void add();
            }
          }}
          disabled={locked || !usable}
        />
        <button
          type="button"
          onClick={() => void add()}
          disabled={locked || !usable || !draft.trim()}
        >
          添加
        </button>
      </div>

      <div className="chips">
        {items.length === 0 ? (
          <span className="muted small">还没有热词</span>
        ) : (
          items.map((item) => (
            <span className="chip" key={item.text} title={`权重 ${item.weight}`}>
              {item.text}
              <button
                type="button"
                aria-label={`移除 ${item.text}`}
                onClick={() => void remove(item)}
                disabled={locked}
              >
                ×
              </button>
            </span>
          ))
        )}
      </div>

      {allowOutline && tab === "episode" && episodeReady ? (
        <div className="hotword-outline">
          <button
            type="button"
            className="ghost small"
            onClick={() => setShowOutline((value) => !value)}
          >
            {showOutline ? "收起大纲" : "从大纲自动提取…"}
          </button>
          {showOutline ? (
            <>
              <textarea
                value={outline}
                placeholder="粘贴本期大纲或 shownotes，自动提取人名和术语"
                rows={4}
                onChange={(event) => setOutline(event.target.value)}
              />
              <button
                type="button"
                className="small"
                disabled={locked || !outline.trim()}
                onClick={() => void generateFromOutline()}
              >
                提取热词
              </button>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** Counts for the "传入 N 个热词" summary both callers show. */
export function describeHotwords(
  globalHotwords: HotwordItem[],
  episodeHotwords: HotwordItem[],
): string {
  const total = globalHotwords.length + episodeHotwords.length;
  if (total === 0) return "热词表为空，转写不会带入任何词";
  return `本次会带入 ${total} 个热词（全局 ${globalHotwords.length} · 本期 ${episodeHotwords.length}）`;
}
