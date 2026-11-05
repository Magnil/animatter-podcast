/**
 * Operation history panel.
 *
 * Shows what was done, newest first, and lets the user pick which boundary
 * operation to revert. Only the latest boundary op is undoable — reverting an
 * older one after later edits would replay a stale snapshot over newer tokens.
 * That restriction is surfaced in the UI rather than hidden, because a disabled
 * row with no explanation reads as a bug.
 */

import { useCallback, useEffect, useState } from "react";
import { api, type HistoryEntry } from "./api";

interface Props {
  versionId: string;
  /** Bumped by the parent after any edit, to force a refetch. */
  revision: number;
  onUndone: () => Promise<void>;
  onNotice: (message: string) => void;
}

export function HistoryPanel({ versionId, revision, onUndone, onNotice }: Props): React.JSX.Element {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setEntries(await api.history(versionId));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [versionId]);

  useEffect(() => {
    void reload();
  }, [reload, revision]);

  const undo = async (entry: HistoryEntry): Promise<void> => {
    setBusy(true);
    try {
      await api.undoOp(versionId, entry.id);
      await onUndone();
      await reload();
      onNotice(`已撤销：${entry.label}${entry.detail ? `（${entry.detail}）` : ""}`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel history-panel">
      <div className="panel-head">
        <h3>操作记录</h3>
        <button type="button" className="ghost small" onClick={() => void reload()} disabled={busy}>
          刷新
        </button>
      </div>

      {error ? <p className="error-text">{error}</p> : null}

      {entries.length === 0 ? (
        <p className="muted small">还没有编辑操作。重划边界、改字、拖动边界都会记录在这里。</p>
      ) : (
        <ol className="history-list">
          {entries.map((entry) => (
            <li key={entry.id} className={entry.undone ? "undone" : ""}>
              <div className="history-main">
                <span className="history-label">{entry.label}</span>
                {entry.detail ? <span className="history-detail">{entry.detail}</span> : null}
              </div>
              <span className="history-time">{formatClock(entry.created_at)}</span>
              {entry.undone ? (
                <span className="history-state">已撤销</span>
              ) : entry.undoable ? (
                <button
                  type="button"
                  className="ghost small"
                  disabled={busy}
                  onClick={() => void undo(entry)}
                >
                  撤销
                </button>
              ) : (
                <span className="history-state" title="文字修改请用反向替换；删除请用「恢复删除」">
                  不可撤销
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** HH:MM from an ISO timestamp; the date is noise inside one session. */
function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
