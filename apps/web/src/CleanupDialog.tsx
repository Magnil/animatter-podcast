/**
 * One-click cleanup dialog: show the list, then act.
 *
 * The workflow this encodes is the whole point of the feature being acceptable
 * at all. Rescript fires on one click and shows you what it took afterwards; we
 * show the plan first, with per-category counts and a preview of the actual cuts
 * on the waveform, and only then write. A bulk delete the user cannot inspect
 * beforehand is a bulk delete they stop trusting the first time it guesses wrong.
 *
 * Three things the dialog deliberately does *not* do:
 *
 * - It does not save the thresholds. Dragging a slider re-plans; clicking apply
 *   uses that plan. Persisting only happens in 设置, because a slider that
 *   silently becomes a global default is how "why did it eat my audio" starts.
 * - It does not re-plan after applying. The plan is a snapshot of an intention;
 *   recomputing it behind the modal would make the counts jump under the user.
 * - It does not offer "undo" here. The history panel owns undo, and having two
 *   undo affordances that can disagree is worse than one place to look.
 */

import { useCallback, useEffect, useState } from "react";
import { api, type AppSettings, type CleanupItem, type CleanupPlan } from "./api";

interface Props {
  open: boolean;
  versionId: string;
  onClose: () => void;
  /** Called after a successful apply so the workspace refetches the EDL. */
  onApplied: (result: { deletions: number; removedMs: number; counts: Record<string, number> }) => void;
  onNotice: (message: string) => void;
}

type Kind = "filler" | "disfluency" | "silence";

const KIND_LABELS: Record<Kind, string> = {
  filler: "口癖词",
  disfluency: "未转写语流",
  silence: "静音",
};

const KIND_HINTS: Record<Kind, string> = {
  filler: "嗯、呃、唔这类没有词义的音，按词表匹配",
  disfluency: "识别漏掉的口癖：音频里是语音，稿子里没有字",
  silence: "词与词之间的死气，超过阈值才切",
};

const ORDER: Kind[] = ["filler", "disfluency", "silence"];

export function CleanupDialog({
  open,
  versionId,
  onClose,
  onApplied,
  onNotice,
}: Props): React.JSX.Element | null {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [plan, setPlan] = useState<CleanupPlan | null>(null);
  const [selected, setSelected] = useState<Set<Kind>>(new Set(ORDER));
  const [busy, setBusy] = useState(false);
  const [planning, setPlanning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Thresholds live here, not in settings, until the user saves them in 设置.
  const [minSilenceMs, setMinSilenceMs] = useState(300);
  const [silencePadMs, setSilencePadMs] = useState(50);
  const [minDisfluencyMs, setMinDisfluencyMs] = useState(300);
  const [showAll, setShowAll] = useState(false);

  const refresh = useCallback(
    async (options: { minSilenceMs: number; silencePadMs: number; minDisfluencyMs: number }) => {
      setPlanning(true);
      try {
        const next = await api.cleanupPlan(versionId, options);
        setPlan(next);
        setError(null);
        // Untick a category that turned out to have nothing in it, so the
        // "本次会删 N 处" number can never disagree with the tick boxes.
        setSelected((current) => {
          const kept = new Set<Kind>();
          for (const kind of current) {
            if ((next.summary[kind]?.items ?? 0) > 0) kept.add(kind);
          }
          return kept;
        });
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setPlanning(false);
      }
    },
    [versionId],
  );

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void api
      .settings()
      .then((loaded) => {
        if (cancelled) return;
        setSettings(loaded);
        setMinSilenceMs(loaded.cleanup_min_silence_ms);
        setSilencePadMs(loaded.cleanup_silence_pad_ms);
        setMinDisfluencyMs(loaded.cleanup_min_disfluency_ms);
        void refresh({
          minSilenceMs: loaded.cleanup_min_silence_ms,
          silencePadMs: loaded.cleanup_silence_pad_ms,
          minDisfluencyMs: loaded.cleanup_min_disfluency_ms,
        });
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);

  // Re-plan when a threshold settles. 350ms debounce: dragging a range input
  // fires dozens of events, and each one is an ffmpeg-cached but still real
  // round trip.
  useEffect(() => {
    if (!open || !settings) return;
    const unchanged =
      minSilenceMs === settings.cleanup_min_silence_ms &&
      silencePadMs === settings.cleanup_silence_pad_ms &&
      minDisfluencyMs === settings.cleanup_min_disfluency_ms;
    if (unchanged) return;
    const timer = window.setTimeout(() => {
      void refresh({ minSilenceMs, silencePadMs, minDisfluencyMs });
    }, 350);
    return () => window.clearTimeout(timer);
  }, [open, settings, minSilenceMs, silencePadMs, minDisfluencyMs, refresh]);

  if (!open) return null;

  const toggle = (kind: Kind): void => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const chosen = plan ? plan.items.filter((item) => selected.has(item.kind as Kind)) : [];
  const chosenMs = chosen.reduce((total, item) => total + item.duration_ms, 0);

  const apply = async (): Promise<void> => {
    if (selected.size === 0) return;
    setBusy(true);
    try {
      const result = await api.applyCleanup(versionId, {
        kinds: [...selected],
        minSilenceMs,
        silencePadMs,
        minDisfluencyMs,
      });
      onNotice(
        `已清理 ${result.deletions} 处，减少 ${formatDuration(result.removed_ms)}。` +
          "可以在操作记录里一次撤销。",
      );
      onApplied({
        deletions: result.deletions,
        removedMs: result.removed_ms,
        counts: result.counts,
      });
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => !busy && onClose()}>
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-label="一键清理"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>一键清理</h2>
          <button type="button" className="ghost" onClick={onClose} disabled={busy} aria-label="关闭">
            ✕
          </button>
        </header>

        <div className="modal-body modal-single cleanup-body">
          {error ? <p className="error-text">{error}</p> : null}

          <p className="section-note">
            先看清楚要删什么，再决定删不删。这里只列出清单，点「执行清理」才会真的动音频。
          </p>

          <div className="cleanup-kinds">
            {ORDER.map((kind) => {
              const summary = plan?.summary[kind] ?? { items: 0, duration_ms: 0 };
              const disabled = summary.items === 0 || planning;
              return (
                <label className={`cleanup-kind${disabled ? " disabled" : ""}`} key={kind}>
                  <input
                    type="checkbox"
                    checked={selected.has(kind)}
                    disabled={disabled || busy}
                    onChange={() => toggle(kind)}
                  />
                  <span className="cleanup-kind-name">{KIND_LABELS[kind]}</span>
                  <span className="cleanup-kind-count">
                    {summary.items} 处 · {formatDuration(summary.duration_ms)}
                  </span>
                  <em className="hint">{KIND_HINTS[kind]}</em>
                </label>
              );
            })}
          </div>

          <details className="cleanup-thresholds">
            <summary>阈值（只影响这一次和下一次，不写入设置）</summary>
            <label className="field">
              <span>
                静音判定
                <em className="hint">词之间超过这个长度才算死气</em>
              </span>
              <input
                type="number"
                min={100}
                max={3000}
                step={50}
                value={minSilenceMs}
                disabled={busy}
                onChange={(event) => setMinSilenceMs(Number(event.target.value))}
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
                value={silencePadMs}
                disabled={busy}
                onChange={(event) => setSilencePadMs(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>
                未转写语流最短长度
                <em className="hint">太短的更可能是时间戳抖动，不是漏掉的口癖</em>
              </span>
              <input
                type="number"
                min={100}
                max={3000}
                step={50}
                value={minDisfluencyMs}
                disabled={busy}
                onChange={(event) => setMinDisfluencyMs(Number(event.target.value))}
              />
            </label>
          </details>

          <div className="cleanup-list">
            <div className="cleanup-list-head">
              <strong>
                本次会删 {chosen.length} 处 · {formatDuration(chosenMs)}
              </strong>
              {chosen.length > 8 ? (
                <button type="button" className="ghost small" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? "收起" : `展开全部 ${chosen.length} 条`}
                </button>
              ) : null}
            </div>
            {planning ? <p className="muted small">重新计算中…</p> : null}
            {chosen.length === 0 && !planning ? (
              <p className="muted small">没有可清理的内容。换个阈值，或者在设置里补充口癖词。</p>
            ) : (
              <ul className="cleanup-items">
                {(showAll ? chosen : chosen.slice(0, 8)).map((item) => (
                  <CleanupRow key={`${item.kind}-${item.start_ms}-${item.end_ms}`} item={item} />
                ))}
              </ul>
            )}
            {!showAll && chosen.length > 8 ? (
              <p className="muted small">还有 {chosen.length - 8} 条未显示</p>
            ) : null}
          </div>
        </div>

        <footer className="modal-foot">
          {plan ? (
            <span className="muted small">
              词表 {plan.fillers.length} 个词 · 音频 {formatDuration(plan.duration_ms)}
            </span>
          ) : null}
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            disabled={busy || planning || chosen.length === 0}
            onClick={() => void apply()}
          >
            {busy ? "清理中…" : `执行清理（${chosen.length} 处）`}
          </button>
        </footer>
      </div>
    </div>
  );
}

function CleanupRow({ item }: { item: CleanupItem }): React.JSX.Element {
  // The kind column already says "静音", so repeating it in the text column
  // would just be noise; only the filler and the placeholder need explaining.
  const label =
    item.kind === "filler"
      ? `「${item.text}」`
      : item.kind === "disfluency"
        ? "识别没写的语音"
        : "—";
  return (
    <li className={`cleanup-row kind-${item.kind}`}>
      <span className="cleanup-row-time">{formatClock(item.start_ms)}</span>
      <span className="cleanup-row-kind">{KIND_LABELS[item.kind as Kind]}</span>
      <span className="cleanup-row-text">{label}</span>
      <span className="cleanup-row-dur">{formatDuration(item.duration_ms)}</span>
      {item.sentence_ord >= 0 ? (
        <span className="cleanup-row-sentence">第 {item.sentence_ord + 1} 句</span>
      ) : null}
    </li>
  );
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} 秒`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} 分 ${seconds} 秒`;
}

function formatClock(ms: number): string {
  const total = Math.max(0, Math.round(ms));
  const seconds = Math.floor(total / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
