/**
 * 热词与校对：the glossary, and the LLM pass that re-reads it.
 *
 * The two belong together — the terms decide what recognition hears, and the
 * same terms are the evidence the proof-reader argues from — but they do *not*
 * belong in the same panel as 模型 or 一键清理. In the old page this section was
 * wedged between two unrelated ones and its editor rendered inline, which made
 * the page jump every time a term was added.
 */

import { useCallback } from "react";
import { api, type HotwordItem } from "../api";
import { HotwordEditor, type HotwordTab } from "../HotwordEditor";
import { CheckboxField, NumberField, SectionNote, type PanelProps } from "./fields";

interface Props extends PanelProps {
  projectId?: string;
  mediaId?: string;
  episodeHotwords: HotwordItem[];
  globalHotwords: HotwordItem[];
  tab: HotwordTab;
  onTabChange: (tab: HotwordTab) => void;
  /** Ref-backed and therefore stable — the editor lists it as an effect dep. */
  onChanged: () => void;
  onError: (message: string) => void;
}

export function HotwordsPanel({
  settings,
  patch,
  projectId,
  mediaId,
  episodeHotwords,
  globalHotwords,
  tab,
  onTabChange,
  onChanged,
  onError,
}: Props): React.JSX.Element {
  // The editor awaits this, so wrap the void callback rather than changing its
  // contract — but keep the wrapper's identity stable across renders.
  const refresh = useCallback(async () => {
    onChanged();
  }, [onChanged]);

  return (
    <div className="settings-panel">
      <SectionNote>
        热词是<strong>识别</strong>的输入：改完要重新转写才会生效。校对用同一份热词让 LLM
        再核一遍文字。
      </SectionNote>

      <CheckboxField
        label="转写时启用热词"
        checked={settings.use_hotwords}
        onChange={(value) => patch("use_hotwords", value)}
      />
      <CheckboxField
        label="启用 LLM 校对（以热词为依据）"
        checked={settings.use_proofread}
        onChange={(value) => patch("use_proofread", value)}
      />

      <NumberField
        label="热词权重"
        hint="实测 5 会矫枉过正，建议 3"
        min={1}
        max={50}
        value={settings.hotword_weight}
        onChange={(value) => patch("hotword_weight", value)}
      />
      <NumberField
        label="校对每批句数"
        hint="批太大模型会漏改"
        min={5}
        max={50}
        value={settings.proofread_chunk_size}
        onChange={(value) => patch("proofread_chunk_size", value)}
      />

      <HotwordEditor
        projectId={projectId}
        mediaId={mediaId}
        episodeHotwords={episodeHotwords}
        globalHotwords={globalHotwords}
        tab={tab}
        onTabChange={onTabChange}
        onChanged={refresh}
        onNotice={onError}
      />

      <SectionNote>
        {projectId && mediaId
          ? "本期热词只对这一期生效；全局热词每期都会带上。"
          : "还没有打开项目，只能编辑全局热词。"}
      </SectionNote>
    </div>
  );
}

/** Re-export so the dialog can refresh without importing the editor. */
export { api };
