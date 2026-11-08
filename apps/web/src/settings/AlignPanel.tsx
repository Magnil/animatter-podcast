/**
 * 时间轴对齐：the one settings panel that acts on a *version*, not on preferences.
 *
 * Every other panel edits `AppSettings` and is written when the dialog's 保存 is
 * pressed. This one writes immediately, because it changes transcripts that
 * already exist — and it says so in the UI rather than pretending to be a
 * preference that "takes effect next time". Mixing the two commit models behind
 * one 保存 button is how a user ends up cancelling and still having a shifted
 * timeline.
 *
 * The number the panel offers first is −80 ms, because that is what was measured
 * on two real episodes (median −65 ms late, 27 % more than 100 ms late). It is a
 * starting point, not a law: different ASR models have different VAD behaviour,
 * so the value stays editable and the panel shows what is currently applied.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

interface Props {
  /** Absent when no project is loaded; the panel is then read-only. */
  versionId?: string;
  /** Reload the transcript after a shift, so the editor shows the new times. */
  onApplied: () => Promise<void>;
  onNotice: (message: string) => void;
}

/** Measured on two real episodes; see services/align.py for the numbers. */
const DEFAULT_SHIFT_MS = -80;

interface State {
  time_shift_ms: number;
  earliest_ms: number;
  latest_ms: number;
  duration_ms: number;
}

export function AlignPanel({ versionId, onApplied, onNotice }: Props): React.JSX.Element {
  const [state, setState] = useState<State | null>(null);
  const [draft, setDraft] = useState<number>(DEFAULT_SHIFT_MS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [clamped, setClamped] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!versionId) return;
    try {
      const loaded = await api.timeShiftState(versionId);
      setState(loaded);
      // An unaligned version fills the box with the measured default rather than
      // with 0, so the common case is one click instead of "change the number,
      // then press the button". A version that already has an offset shows it —
      // that is the value the user needs in order to nudge it.
      setDraft(loaded.time_shift_ms === 0 ? DEFAULT_SHIFT_MS : loaded.time_shift_ms);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [versionId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const apply = async (shiftMs: number): Promise<void> => {
    if (!versionId) return;
    setBusy(true);
    setError(null);
    setClamped(null);
    try {
      const result = await api.timeShift(versionId, shiftMs);
      if (result.clamped) {
        setClamped(`请求 ${shiftMs} ms，实际只移到 ${result.shift_ms} ms：${result.reason}`);
      }
      await onApplied();
      // Refetch rather than patch the local copy: the range shown next to the
      // offset moves with it, and `refresh` runs *after* the reload so it reads
      // the new times. Patching only `time_shift_ms` left the panel claiming
      // "−80 ms" beside the unshifted range.
      await refresh();
      onNotice(
        result.delta_ms === 0
          ? "时间轴没有变化"
          : `时间轴已移到 ${result.shift_ms} ms（${result.tokens} 字 / ${result.deletions} 处删除）`,
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  if (!versionId) {
    return (
      <div className="settings-panel">
        <p className="section-note">没有打开的转写版本，无法调整时间轴。</p>
      </div>
    );
  }

  const current = state?.time_shift_ms ?? 0;

  return (
    <div className="settings-panel">
      <p className="section-note">
        百炼返回的时间戳整体偏晚，点播放时句首会被吞掉一点。整条时间轴一起移动，可随时撤回。
      </p>

      {state ? (
        <dl className="align-state">
          <div>
            <dt>当前偏移</dt>
            <dd>
              {current === 0 ? "未对齐（模型原始时间）" : `${current > 0 ? "+" : ""}${current} ms`}
            </dd>
          </div>
          <div>
            <dt>时间轴范围</dt>
            <dd>
              {(state.earliest_ms / 1000).toFixed(2)}s – {(state.latest_ms / 1000).toFixed(2)}s
            </dd>
          </div>
        </dl>
      ) : (
        <p className="muted">载入中…</p>
      )}

      <label className="field">
        <span>
          整体偏移 (ms)
          <em className="hint">负数 = 整条时间轴提前。实测中位数约 −65，默认给 −80</em>
        </span>
        <input
          type="number"
          min={-1000}
          max={1000}
          step={10}
          value={draft}
          onChange={(event) => {
            const next = Number(event.target.value);
            setDraft(Number.isFinite(next) ? next : 0);
          }}
        />
      </label>

      <div className="align-actions">
        <button type="button" disabled={busy} onClick={() => void apply(draft)}>
          {busy ? "对齐中…" : draft === current ? `已对齐到 ${draft} ms` : `对齐到 ${draft} ms`}
        </button>
        <button
          type="button"
          className="ghost"
          disabled={busy || current === 0}
          onClick={() => void apply(0)}
        >
          撤销对齐
        </button>
        <button
          type="button"
          className="ghost"
          disabled={busy || draft === DEFAULT_SHIFT_MS}
          onClick={() => setDraft(DEFAULT_SHIFT_MS)}
        >
          回到默认 −80
        </button>
      </div>

      {clamped ? <p className="warn-text">{clamped}</p> : null}
      {error ? <p className="error-text">{error}</p> : null}

      <div className="settings-subsection">
        <h4>自动对齐</h4>
        <p className="section-note">
          用波形能量重新定位每句的起声与落静，逐句修正而不只是一个整体偏移。整条时间轴偏晚，
          但每句偏多少并不一样（实测 p25 −125 ms、p75 −35 ms），所以自动对齐能修得比整体偏移更准。
        </p>
        <p className="section-note">
          尚未实现。它要处理人工拖过的边界、删除区间映射和渲染切点，不是一个小改动——
          具体计划记在仓库根目录的 <code>todolist.md</code>。
        </p>
        <button type="button" className="ghost" disabled title="待开发，见 todolist.md">
          自动对齐（待开发）
        </button>
      </div>
    </div>
  );
}
