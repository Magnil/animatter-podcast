/**
 * A panel that can fold down to its title row.
 *
 * The sidebar holds three stacked regions — 热词 / 字幕 / 操作记录 — and the one in
 * the middle is the one people actually work in. The other two are reference
 * material: useful while setting up, in the way while editing.
 *
 * So they collapse instead of being removed. Collapsed is *the title row plus a
 * 展开 button*, not a hidden panel: a panel that disappears leaves no trace of
 * what was closed or how to get it back, and the only way to discover it is to
 * guess. The summary stays visible for the same reason — "本次会带入 31 个热词"
 * answers "do I need to open this?" without opening it.
 *
 * Collapse state is deliberately **not** persisted. A reload putting the panel
 * back the way the layout was designed is the behaviour that needs no
 * documentation; remembering a session's folding would surprise anyone who
 * wondered why their sidebar looks wrong on a fresh start.
 */

import { useState, type ReactNode } from "react";

interface Props {
  title: string;
  /** Right-aligned text in the title row; also visible while collapsed. */
  summary?: ReactNode;
  /** Head controls (刷新 …) that only make sense while the body is showing. */
  actions?: ReactNode;
  className?: string;
  /**
   * Start folded.
   *
   * The video area uses this: it is a real panel with a real header now, so it stays
   * findable while costing one row to an audio-only session. The header — and therefore the
   * summary — still renders, which is what makes collapsing different from hiding.
   */
  defaultCollapsed?: boolean;
  children: ReactNode;
}

export function CollapsiblePanel({
  title,
  summary,
  actions,
  className = "",
  defaultCollapsed = false,
  children,
}: Props): React.JSX.Element {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);

  return (
    <div className={`panel collapsible-panel ${className}${collapsed ? " is-collapsed" : ""}`}>
      <div className="panel-head">
        <h3>{title}</h3>
        <div className="panel-head-tools">
          {summary ? <span className="muted small">{summary}</span> : null}
          {collapsed ? null : actions}
          <button
            type="button"
            className="ghost small"
            aria-expanded={!collapsed}
            onClick={() => setCollapsed((value) => !value)}
          >
            {collapsed ? "展开" : "收起"}
          </button>
        </div>
      </div>
      {collapsed ? null : children}
    </div>
  );
}
