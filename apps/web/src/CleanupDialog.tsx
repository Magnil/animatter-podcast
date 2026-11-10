/**
 * The batch-processing dialog: show the list, then act.
 *
 * The workflow this encodes is the whole point of the feature being acceptable
 * at all. Rescript fires on one click and shows you what it took afterwards; we
 * show the plan first, with per-category counts and a preview of the actual cuts
 * on the waveform, and only then write. A bulk delete the user cannot inspect
 * beforehand is a bulk delete they stop trusting the first time it guesses wrong.
 *
 * **One action per opening.** The menu asks which action (delete the fillers,
 * hide the fillers, delete the silence, …) and this dialog is that action,
 * already scoped. An earlier plan had a delete/hide mode switch inside the
 * dialog; that was worse for the same reason a mode switch always is — the same
 * button does two different things depending on a toggle the user forgot they
 * set. The action is the thing they chose in the menu, and it stays visible in
 * the title.
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

/** Which batch action the menu asked for. */
export type BatchAction = "delete-filler" | "hide-filler" | "delete-silence" | "delete-disfluency" | "delete-all";

interface Props {
  open: boolean;
  action: BatchAction;
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

/** What each action means: a title, the kinds it covers, and whether it cuts audio. */
const ACTIONS: Record<
  BatchAction,
  { title: string; kinds: Kind[]; hide: boolean; note: string }
> = {
  "delete-filler": {
    title: "删除口癖词",
    kinds: ["filler"],
    hide: false,
    note: "把口癖词的音频剪掉。想保留音频、只从字幕里去掉，用「隐去口癖词」。",
  },
  "hide-filler": {
    title: "隐去口癖词",
    kinds: ["filler"],
    hide: true,
    note:
      "只在字幕和文稿里去掉，音频一秒都不剪。想连音频一起剪掉，用「删除口癖词」。" +
      "两者互斥：一个词要么隐去，要么删除。",
  },
  "delete-silence": {
    title: "删除静音",
    kinds: ["silence"],
    hide: false,
    note: "词与词之间的死气，超过阈值才切。可以先在下面调阈值再看清单。",
  },
  "delete-disfluency": {
    title: "删除未转写语流",
    kinds: ["disfluency"],
    hide: false,
    note: "识别漏掉的口癖：音频里是语音，稿子里没有对应的字。没有字可隐，所以只能删。",
  },
  "delete-all": {
    title: "删除全部三类",
    kinds: ["filler", "disfluency", "silence"],
    hide: false,
    note: "一次性清掉口癖词、未转写语流和静音。已经隐去的口癖词不会出现在清单里——它们没被删除。",
  },
};

export function CleanupDialog({
  open,
  action,
  versionId,
  onClose,
  onApplied,
  onNotice,
}: Props): React.JSX.Element | null {
  const spec = ACTIONS[action];
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [plan, setPlan] = useState<CleanupPlan | null>(null);
  const [selected, setSelected] = useState<Set<Kind>>(new Set(spec.kinds));
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
        // "本次会删 N 处" number can never disagree with the tick boxes. Only for
        // the actions that let the user tick anything, though: a scoped action
        // keeps its own selection so an empty plan reads as "nothing to do"
        // rather than as "the category you asked for is not here".
        if (spec.kinds.length > 1) {
          setSelected((current) => {
            const kept = new Set<Kind>();
            for (const kind of current) {
              if ((next.summary[kind]?.items ?? 0) > 0) kept.add(kind);
            }
            return kept;
          });
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setPlanning(false);
      }
    },
    [versionId, spec.kinds.length],
  );

  // A different action means a different scope; re-seed the ticks. Without this,
  // opening 删除静音 right after 删除全部三类 would inherit the three ticks.
  useEffect(() => {
    if (!open) return;
    setSelected(new Set(spec.kinds));
    setShowAll(false);
  }, [open, spec.kinds]);

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
  // round trip. Skipped entirely when the action does not cut audio — the
  // thresholds cannot change a hiding result.
  useEffect(() => {
    if (!open || !settings || spec.hide) return;
    const unchanged =
      minSilenceMs === settings.cleanup_min_silence_ms &&
      silencePadMs === settings.cleanup_silence_pad_ms &&
      minDisfluencyMs === settings.cleanup_min_disfluency_ms;
    if (unchanged) return;
    const timer = window.setTimeout(() => {
      void refresh({ minSilenceMs, silencePadMs, minDisfluencyMs });
    }, 350);
    return () => window.clearTimeout(timer);
  }, [open, settings, spec.hide, minSilenceMs, silencePadMs, minDisfluencyMs, refresh]);

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
      if (spec.hide) {
        const result = await api.hideFillers(versionId);
        onNotice(
          `已隐去 ${result.hidden} 处口癖词，涉及 ${formatDuration(result.span_ms)} 语音。` +
            "音频没有剪，可以在「处理 → 还原已隐去口癖词」一次拿回来。",
        );
        onApplied({ deletions: 0, removedMs: 0, counts: result.counts });
      } else {
        const result = await api.applyCleanup(versionId, {
          kinds: [...selected],
          minSilenceMs,
          silencePadMs,
          minDisfluencyMs,
        });
        onNotice(
          `已删除 ${result.deletions} 处，减少 ${formatDuration(result.removed_ms)}。` +
            "可以在操作记录里一次撤销。",
        );
        onApplied({
          deletions: result.deletions,
          removedMs: result.removed_ms,
          counts: result.counts,
        });
      }
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const verb = spec.hide ? "隐去" : "删除";
  const preview = chosen.slice(0, 8);

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => !busy && onClose()}>
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-label={spec.title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>{spec.title}</h2>
          <button type="button" className="ghost" onClick={onClose} disabled={busy} aria-label="关闭">
            ✕
          </button>
        </header>

        <div className="modal-body modal-single cleanup-body">
          {error ? <p className="error-text">{error}</p> : null}

          <p className="section-note">
            先看清楚要{verb}什么，再决定。这里只列出清单，点「执行{verb}」才会真的动。
          </p>
          <p className="section-note">{spec.note}</p>

          <div className="cleanup-kinds">
            {ORDER.map((kind) => {
              const summary = plan?.summary[kind] ?? { items: 0, duration_ms: 0 };
              const inScope = spec.kinds.includes(kind);
              // Silence and disfluency have no words to hide: the audio is speech
              // the transcript never captured, so there is nothing to strike out
              // of the text. Shown greyed with the reason instead of hidden, so
              // the row does not seem to have gone missing.
              const reason = !inScope
                ? spec.hide && kind !== "filler"
                  ? "没有对应的字，无法隐去"
                  : `不在「${spec.title}」范围内`
                : null;
              const disabled = reason !== null || summary.items === 0 || planning;
              return (
                <label className={`cleanup-kind${disabled ? " disabled" : ""}`} key={kind}>
                  <input
                    type="checkbox"
                    checked={inScope && selected.has(kind)}
                    disabled={disabled || busy}
                    onChange={() => toggle(kind)}
                  />
                  <span className="cleanup-kind-name">{KIND_LABELS[kind]}</span>
                  <span className="cleanup-kind-count">
                    {summary.items} 处 · {formatDuration(summary.duration_ms)}
                  </span>
                  <em className="hint">{reason ?? KIND_HINTS[kind]}</em>
                </label>
              );
            })}
          </div>

          {spec.hide ? (
            <p className="muted small">
              隐去不剪音频，所以静音判定、切点保护、未转写语流长度这些阈值不生效。
            </p>
          ) : (
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
          )}

          <div className="cleanup-list">
            <div className="cleanup-list-head">
              <strong>
                {/* Before the first plan arrives, the count is unknown — and
                    "0 处" does not read as "unknown", it reads as "nothing to do",
                    so the user closes the dialog. The first open pays for the
                    silence detection, which on a 3-hour file takes seconds. */}
                {plan === null
                  ? "正在计算…"
                  : spec.hide
                    ? `本次会隐去 ${chosen.length} 处 · 涉及 ${formatDuration(chosenMs)} 语音`
                    : `本次会删 ${chosen.length} 处 · ${formatDuration(chosenMs)}`}
              </strong>
              {chosen.length > 8 ? (
                <button type="button" className="ghost small" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? "收起" : `展开全部 ${chosen.length} 条`}
                </button>
              ) : null}
            </div>
            {planning ? <p className="muted small">重新计算中…</p> : null}
            {chosen.length === 0 && !planning ? (
              <p className="muted small">
                {spec.hide
                  ? "没有可隐去的口癖词。它们可能已经被隐去或删除了，或者词表里还没有能匹配到的词。"
                  : "没有可删除的内容。换个阈值，或者在设置里补充口癖词。"}
              </p>
            ) : (
              <ul className="cleanup-items">
                {(showAll ? chosen : preview).map((item) => (
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
            {busy
              ? `${verb}中…`
              : `执行${verb}（${chosen.length} 处）`}
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
