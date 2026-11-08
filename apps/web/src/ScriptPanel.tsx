/**
 * ShowNotes / 文稿 generation panel.
 *
 * The shape of this screen follows from one fact: generating a document for a
 * 105-minute episode is several model calls that take minutes, and the browser
 * gets nothing back until the last one finishes. So the panel must answer three
 * questions the whole time it is waiting — *did my click land*, *is it still
 * working*, *how long has it been*. A spinner answers none of them, which is
 * exactly how the async audio render was reported as「点击后无反应」.
 *
 * The other deliberate choice: the result appears in an editable textarea, not
 * a read-only preview. Every model rewrite needs a human pass — a dropped name,
 * a chapter on the wrong line, a paragraph that reads like a summary — and a
 * preview that cannot be corrected means the fix happens in another editor after
 * an export. What is exported is what is in the box.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ScriptFormat } from "./api";
import { countTitleChars } from "./titleCount";

interface Props {
  versionId: string;
  hasDeletions: boolean;
  onNotice: (message: string) => void;
  /** Written back to settings when the user switches style, so it persists. */
  initialFormat?: ScriptFormat;
  onFormatChange?: (format: ScriptFormat) => void;
}

interface Result {
  format: ScriptFormat;
  title: string;
  body: string;
  filename: string;
  chapters: { heading: string; start_ms: number; clock: string }[];
  titles: string[];
  turns: number;
  blocks: number;
  warnings: string[];
}

const FORMATS: { id: ScriptFormat; label: string; hint: string }[] = [
  { id: "shownotes", label: "ShowNotes", hint: "备选标题 + 两段概述 + 时间轴 + 签名" },
  { id: "dialogue", label: "文稿 · 对话式", hint: "保留说话人，清口癖与口语" },
  { id: "article", label: "文稿 · 文章式", hint: "改写成书面文章，不分说话人" },
];

export function ScriptPanel({
  versionId,
  hasDeletions,
  onNotice,
  initialFormat = "shownotes",
  onFormatChange,
}: Props): React.JSX.Element {
  const [format, setFormat] = useState<ScriptFormat>(initialFormat);
  const [mapped, setMapped] = useState(true);
  const [body, setBody] = useState("");
  const [filename, setFilename] = useState("script.md");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const startedAt = useRef(0);

  // Reveal the requested format when the menu item asks for a different one.
  useEffect(() => {
    setFormat(initialFormat);
  }, [initialFormat]);

  // Elapsed time, ticking only while a call is in flight. This is the only
  // honest progress signal available: the request either answers or it does not.
  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => {
      setElapsed(Math.round((performance.now() - startedAt.current) / 1000));
    }, 500);
    return () => window.clearInterval(timer);
  }, [busy]);

  const generate = useCallback(async () => {
    setBusy(true);
    setError(null);
    startedAt.current = performance.now();
    setElapsed(0);
    try {
      const next = await api.generateScript(versionId, { format, mapped });
      setResult(next);
      setBody(next.body);
      setFilename(next.filename);
      const parts = [`已生成 ${next.filename}`];
      // Only 文稿 is actually generated in blocks; ShowNotes makes one planning
      // call, so quoting a block count there describes work that did not happen.
      if (next.format !== "shownotes" && next.blocks > 1) parts.push(`分 ${next.blocks} 段`);
      onNotice(parts.join("，"));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }, [format, mapped, onNotice, versionId]);

  const download = useCallback(async () => {
    try {
      const blob = await api.downloadScript(body, filename);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      onNotice(`已导出 ${filename}`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  }, [body, filename, onNotice]);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(body);
      onNotice("已复制到剪贴板");
    } catch {
      // Clipboard access is gated by permission and by secure-context rules;
      // failing to copy must not look like failing to generate.
      onNotice("复制失败，请在文本框里手动全选复制");
    }
  }, [body, onNotice]);

  const dirty = result !== null && body !== result.body;

  /**
   * Swap the H1 for one of the other candidate titles.
   *
   * Rewrites the first `# ` line of the *current* body rather than re-rendering
   * from the result: the user may already have edited the paragraphs, and
   * replacing the whole body to change one line would throw that away. If the
   * body has no H1 (the user deleted it), one is inserted at the top.
   */
  const applyTitle = useCallback(
    (title: string) => {
      setBody((current) => {
        const lines = current.split("\n");
        const index = lines.findIndex((line) => line.startsWith("# "));
        if (index >= 0) {
          lines[index] = `# ${title}`;
        } else {
          lines.unshift(`# ${title}`, "");
        }
        return lines.join("\n");
      });
      onNotice(`标题已换成「${title}」`);
    },
    [onNotice],
  );

  const summary = useMemo(() => {
    if (!result) return null;
    return `${result.turns} 个发言段 · ${result.chapters.length} 个章节 · ${body.length} 字${
      dirty ? " · 已手动修改" : ""
    }`;
  }, [body.length, dirty, result]);

  return (
    <div className="panel script-panel">
      <div className="tabs" role="tablist">
        {FORMATS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={format === item.id}
            className={format === item.id ? "tab active" : "tab"}
            title={item.hint}
            disabled={busy}
            onClick={() => {
              setFormat(item.id);
              onFormatChange?.(item.id);
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
      <p className="section-note">{FORMATS.find((item) => item.id === format)?.hint}</p>

      {hasDeletions ? (
        <label className="field checkbox">
          <input
            type="checkbox"
            checked={mapped}
            onChange={(event) => setMapped(event.target.checked)}
            disabled={busy}
          />
          <span>
            章节时间对齐成片
            <em className="hint">关掉则按原始音频计时；发布时通常应该打开</em>
          </span>
        </label>
      ) : null}

      <div className="edl-actions">
        <button type="button" onClick={() => void generate()} disabled={busy}>
          {busy ? `生成中… ${elapsed} 秒` : result ? "重新生成" : "生成"}
        </button>
        <button type="button" className="ghost" onClick={() => void copy()} disabled={!body || busy}>
          复制
        </button>
        <button type="button" className="ghost" onClick={() => void download()} disabled={!body || busy}>
          下载 .md
        </button>
      </div>

      {busy ? (
        <p className="section-note">
          正在调用模型，长节目需要几分钟；这一步没有中间进度，只能等它返回。
          {elapsed > 90 ? "已经超过一分半，仍在等待。" : ""}
        </p>
      ) : null}

      {error ? <p className="error-text">{error}</p> : null}

      {result && result.warnings.length > 0 ? (
        <details className="issue-list" open>
          <summary>{result.warnings.length} 处需要留意</summary>
          <ul>
            {result.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </details>
      ) : null}

      {result && result.chapters.length > 0 ? (
        <details className="issue-list">
          <summary>章节（{result.chapters.length}）</summary>
          <ul>
            {result.chapters.map((chapter) => (
              <li key={`${chapter.start_ms}-${chapter.heading}`}>
                <span className="issue-kind">{chapter.clock}</span> {chapter.heading}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {result && result.titles.length > 0 ? (
        <div className="title-options">
          <p className="section-note">
            备选标题（已按 22 字上限筛过，第一个是正文里的标题）。点一个替换 H1，不会动正文其他部分。
          </p>
          <ul>
            {result.titles.map((title, index) => {
              const inUse = body.split("\n").some((line) => line === `# ${title}`);
              return (
                <li key={title}>
                  <button
                    type="button"
                    className={inUse ? "title-option in-use" : "title-option"}
                    disabled={inUse}
                    onClick={() => applyTitle(title)}
                  >
                    <span className="title-text">{title}</span>
                    <span className="title-count">{countTitleChars(title)} 字</span>
                  </button>
                  {index === 0 && !inUse ? <span className="title-badge">原选</span> : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {body ? (
        <>
          <label className="field">
            <span>
              文件名
              <em className="hint">下载时使用；正文里的标题是另一回事</em>
            </span>
            <input value={filename} onChange={(event) => setFilename(event.target.value)} />
          </label>
          {summary ? <p className="section-note">{summary}</p> : null}
          <textarea
            className="script-body"
            value={body}
            spellCheck={false}
            rows={22}
            onChange={(event) => setBody(event.target.value)}
          />
        </>
      ) : (
        <p className="muted">
          {busy ? "正在生成…" : "还没有生成内容。选一种格式后点「生成」。"}
        </p>
      )}
    </div>
  );
}
