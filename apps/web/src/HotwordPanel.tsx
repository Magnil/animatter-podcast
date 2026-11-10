/**
 * Sidebar hotword panel: the workbench's quick access to the glossary.
 *
 * The editing surface itself lives in ``HotwordEditor`` because the same
 * widget appears in the new-project dialog and in 设置 → 热词与校对; this file
 * only adds the panel chrome and remembers which tab was open.
 */

import { useState } from "react";
import type { HotwordItem } from "./api";
import { CollapsiblePanel } from "./CollapsiblePanel";
import { HotwordEditor, describeHotwords, type HotwordTab } from "./HotwordEditor";

interface Props {
  projectId: string;
  mediaId: string;
  episodeHotwords: HotwordItem[];
  globalHotwords: HotwordItem[];
  onChanged: () => Promise<void>;
  onNotice: (message: string) => void;
}

export function HotwordPanel({
  projectId,
  mediaId,
  episodeHotwords,
  globalHotwords,
  onChanged,
  onNotice,
}: Props): React.JSX.Element {
  const [tab, setTab] = useState<HotwordTab>("episode");

  return (
    <CollapsiblePanel
      title="热词"
      summary={describeHotwords(globalHotwords, episodeHotwords)}
      className="hotword-panel"
    >
      <HotwordEditor
        projectId={projectId}
        mediaId={mediaId}
        episodeHotwords={episodeHotwords}
        globalHotwords={globalHotwords}
        tab={tab}
        onTabChange={setTab}
        onChanged={onChanged}
        onNotice={onNotice}
        allowOutline
      />
    </CollapsiblePanel>
  );
}
