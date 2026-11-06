/**
 * Import hotwords from a show outline, before the project exists.
 *
 * This is the one place where an outline genuinely changes the audio: the terms
 * become `parameters.vocabulary`, so a guest's name is either recognised or it
 * is not. Offering it only in the workbench — where the transcription has
 * already run — was offering it too late.
 *
 * Extraction is a *proposal*. The LLM reliably returns a few plausible but
 * wrong terms, and every wrong term biases the recogniser, so the candidates
 * are reviewed before they reach the episode list: nothing is added until the
 * operator presses 加入本期热词, and any single word can be dropped first
 * (AGENT.md §6.1).
 *
 * The new-project dialog has no project or media row yet, which is why this
 * calls the stateless endpoint rather than the per-episode one. Creating the
 * project up front just to hold the outline would leave an orphan behind when
 * the user cancels.
 */

import { useState } from "react";
import { api } from "./api";

interface Props {
  /** Merge the reviewed candidates into the caller's own list. */
  onAdd: (terms: { text: string; weight: number }[]) => Promise<void>;
  /** Remember the raw outline once extraction succeeds (store or keep as draft). */
  onOutlineText?: (text: string) => Promise<void>;
  /** Terms already present; shown as 已有 and left unselected. */
  existing?: string[];
  onNotice: (message: string) => void;
  disabled?: boolean;
}

interface Candidate {
  text: string;
  weight: number;
  checked: boolean;
  /** Already in the caller's list: adding it again would do nothing. */
  duplicate: boolean;
}

export function OutlineImport({
  onAdd,
  onOutlineText,
  existing = [],
  onNotice,
  disabled = false,
}: Props): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [outline, setOutline] = useState("");
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const locked = disabled || busy;

  const extract = async (): Promise<void> => {
    if (!outline.trim()) return;
    setBusy(true);
    setNote("");
    try {
      const result = await api.suggestOutlineTerms(outline);
      const present = new Set(existing);
      setCandidates(
        result.terms.map((term) => ({
          text: term.text,
          weight: term.weight,
          // Pre-ticked: most candidates are right, so the operator's job is to
          // drop the obvious misses rather than tick ninety boxes.
          checked: !present.has(term.text),
          duplicate: present.has(term.text),
        })),
      );
      await onOutlineText?.(outline);

      const bits = [`提取到 ${result.terms.length} 个候选`];
      if (result.rejected.length > 0) bits.push(`${result.rejected.length} 个未通过校验`);
      if (result.truncated) bits.push("大纲过长，只用了前半段");
      if (result.terms.length === 0 && result.warnings.length > 0) bits.push(result.warnings[0] ?? "");
      setNote(bits.join("；"));
      if (result.terms.length === 0) onNotice("大纲里没有提取到可用热词，可以手动添加");
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const accept = async (): Promise<void> => {
    const picked = candidates.filter((item) => item.checked && !item.duplicate);
    if (picked.length === 0) return;
    setBusy(true);
    try {
      await onAdd(picked.map((item) => ({ text: item.text, weight: item.weight })));
      onNotice(`已加入 ${picked.length} 个本期热词`);
      setCandidates([]);
      setNote("");
      setOpen(false);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (text: string): void =>
    setCandidates((current) =>
      current.map((item) => (item.text === text ? { ...item, checked: !item.checked } : item)),
    );

  const setAll = (checked: boolean): void =>
    setCandidates((current) =>
      current.map((item) => (item.duplicate ? item : { ...item, checked })),
    );

  const pickedCount = candidates.filter((item) => item.checked && !item.duplicate).length;

  return (
    <div className="outline-import">
      <button
        type="button"
        className="ghost small"
        onClick={() => setOpen((value) => !value)}
        disabled={disabled}
      >
        {open ? "收起大纲导入" : "从大纲导入热词…"}
      </button>

      {open ? (
        <>
          <textarea
            value={outline}
            rows={4}
            placeholder="粘贴本期大纲或 show notes，自动提取人名和术语"
            onChange={(event) => setOutline(event.target.value)}
            disabled={locked}
          />
          <div className="outline-import-actions">
            <button
              type="button"
              className="small"
              onClick={() => void extract()}
              disabled={locked || !outline.trim()}
            >
              {busy ? "提取中…" : "提取候选"}
            </button>
            {note ? <span className="muted small">{note}</span> : null}
          </div>

          {candidates.length > 0 ? (
            <div className="outline-candidates">
              <div className="outline-candidates-head">
                <span className="muted small">去掉明显误判的，再加入本期热词</span>
                <span className="outline-candidates-tools">
                  <button type="button" className="ghost small" onClick={() => setAll(true)} disabled={locked}>
                    全选
                  </button>
                  <button type="button" className="ghost small" onClick={() => setAll(false)} disabled={locked}>
                    全不选
                  </button>
                </span>
              </div>
              <div className="chips">
                {candidates.map((item) => (
                  <label
                    key={item.text}
                    className={`chip chip-toggle${item.checked ? " on" : ""}${item.duplicate ? " dup" : ""}`}
                    title={item.duplicate ? "已经在列表里" : `权重 ${item.weight}`}
                  >
                    <input
                      type="checkbox"
                      checked={item.checked}
                      onChange={() => toggle(item.text)}
                      disabled={locked || item.duplicate}
                    />
                    {item.text}
                    {item.duplicate ? <em className="hint">已有</em> : null}
                  </label>
                ))}
              </div>
              <button
                type="button"
                className="small"
                onClick={() => void accept()}
                disabled={locked || pickedCount === 0}
              >
                加入本期热词（{pickedCount}）
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
