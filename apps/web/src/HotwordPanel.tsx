/**
 * Hotword quick-editing panel.
 *
 * Hotwords are split into global (the whole podcast's shared vocabulary) and
 * per-episode (this recording's names and jargon). Both are shown here because
 * mixing them up is the common mistake: putting an episode's guest name in the
 * global list pollutes every future episode's transcription.
 */

import { useCallback, useState } from "react";
import { api, type HotwordItem } from "./api";

interface Props {
  projectId: string;
  mediaId: string;
  episodeHotwords: HotwordItem[];
  globalHotwords: HotwordItem[];
  onChanged: () => Promise<void>;
  onNotice: (message: string) => void;
}

type Tab = "episode" | "global";

export function HotwordPanel({
  projectId,
  mediaId,
  episodeHotwords,
  globalHotwords,
  onChanged,
  onNotice,
}: Props): React.JSX.Element {
  const [tab, setTab] = useState<Tab>("episode");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [outline, setOutline] = useState("");
  const [showOutline, setShowOutline] = useState(false);

  const items = tab === "episode" ? episodeHotwords : globalHotwords;

  const add = useCallback(async () => {
    const text = draft.trim();
    if (!text) return;
    setBusy(true);
    try {
      if (tab === "episode") await api.addEpisodeHotword(projectId, mediaId, text);
      else await api.addGlobalHotword(text);
      setDraft("");
      await onChanged();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }, [draft, onChanged, onNotice, projectId, tab]);

  const remove = async (item: HotwordItem): Promise<void> => {
    setBusy(true);
    try {
      if (tab === "episode") await api.removeEpisodeHotword(projectId, mediaId, item.text);
      else await api.removeGlobalHotword(item.text);
      await onChanged();
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const generateFromOutline = async (): Promise<void> => {
    if (!outline.trim()) return;
    setBusy(true);
    try {
      await api.saveOutline(projectId, mediaId, outline);
      const result = await api.suggestHotwords(projectId, mediaId);
      await onChanged();
      onNotice(`从大纲提取了 ${result.terms.length} 个热词`);
      setShowOutline(false);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel hotword-panel">
      <div className="panel-head">
        <h3>热词</h3>
        <div className="tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "episode"}
            className={tab === "episode" ? "tab active" : "tab"}
            onClick={() => setTab("episode")}
          >
            本期 {episodeHotwords.length}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "global"}
            className={tab === "global" ? "tab active" : "tab"}
            onClick={() => setTab("global")}
          >
            全局 {globalHotwords.length}
          </button>
        </div>
      </div>

      <p className="panel-note">
        {tab === "episode"
          ? "只对这一期生效：人名、专有名词、本期术语。"
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
          disabled={busy}
        />
        <button type="button" onClick={() => void add()} disabled={busy || !draft.trim()}>
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
                disabled={busy}
              >
                ×
              </button>
            </span>
          ))
        )}
      </div>

      {tab === "episode" ? (
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
                disabled={busy || !outline.trim()}
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
