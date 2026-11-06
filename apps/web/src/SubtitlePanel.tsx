/**
 * Subtitle export panel.
 *
 * Shows the actual SRT before exporting, because the two things that go wrong
 * here are invisible until playback: timestamps that were not mapped onto the
 * render, and cues too brief or too fast to read. Both are surfaced as a count
 * and a preview rather than hidden behind a download button.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

interface Props {
  versionId: string;
  /** True when the project has deletions, so mapping matters. */
  hasDeletions: boolean;
  onNotice: (message: string) => void;
}

type Format = "srt" | "vtt" | "txt";

interface Issue {
  sentence_ord: number;
  kind: string;
  detail: string;
}

const KIND_LABELS: Record<string, string> = {
  split: "长句已拆分",
  too_fast: "语速过快",
  too_brief: "时长过短",
  zero_length: "时长为零",
  partially_deleted: "部分内容已删除",
};

export function SubtitlePanel({ versionId, hasDeletions, onNotice }: Props): React.JSX.Element {
  const [format, setFormat] = useState<Format>("srt");
  const [mapped, setMapped] = useState(true);
  const [preview, setPreview] = useState("");
  const [issues, setIssues] = useState<Issue[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      // Deliberately not `Promise.all`: the cue text is the thing the user came
      // for, and a failing issues request must not blank it out. They failed
      // together once (both hit the JSON-parsing bug) and the panel showed
      // 「没有可导出的字幕」 for 1205 perfectly good cues.
      const text = await api.subtitlePreview(versionId, { fmt: format, limit: 20, mapped });
      setPreview(text);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPreview("");
    }
    try {
      setIssues(await api.subtitleIssues(versionId));
    } catch {
      // Quality warnings are advisory; losing them is not worth an error banner
      // on top of a preview that worked.
      setIssues([]);
    }
  }, [format, mapped, versionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const doExport = async (): Promise<void> => {
    setBusy(true);
    try {
      const result = await api.exportSubtitles(versionId, format);
      // Trigger the browser download from the endpoint that streams the file.
      const link = document.createElement("a");
      link.href = api.exportFileUrl(result.url);
      link.download = result.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      onNotice(`已导出 ${result.cue_count} 条字幕（${result.filename}）`);
      setIssues(result.issues);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel subtitle-panel">
      <div className="panel-head">
        <h3>字幕导出</h3>
        <div className="tabs" role="tablist">
          {(["srt", "vtt", "txt"] as Format[]).map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              aria-selected={format === item}
              className={format === item ? "tab active" : "tab"}
              onClick={() => setFormat(item)}
            >
              {item.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {hasDeletions ? (
        <label className="field checkbox">
          <input type="checkbox" checked={mapped} onChange={(event) => setMapped(event.target.checked)} />
          <span>
            对齐成片时间轴
            <em className="hint">关掉则按原始音频计时，通常不是你想要的</em>
          </span>
        </label>
      ) : null}

      {error ? <p className="error-text">{error}</p> : null}

      {issues.length > 0 ? (
        <details className="issue-list">
          <summary>
            {issues.length} 处需要留意
            {summarise(issues)}
          </summary>
          <ul>
            {issues.slice(0, 12).map((issue, index) => (
              <li key={`${issue.kind}-${issue.sentence_ord}-${index}`}>
                <span className="issue-kind">{KIND_LABELS[issue.kind] ?? issue.kind}</span>
                {issue.sentence_ord >= 0 ? `（第 ${issue.sentence_ord + 1} 句）` : ""} {issue.detail}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <pre className="subtitle-preview">
        {preview ||
          (error
            ? `（预览加载失败：${error}）`
            : "（还没有转写内容，所以没有可导出的字幕）")}
      </pre>

      <div className="edl-actions">
        <button type="button" onClick={() => void doExport()} disabled={busy || !preview}>
          {busy ? "导出中…" : `导出 ${format.toUpperCase()}`}
        </button>
        <button type="button" className="ghost" onClick={() => void load()} disabled={busy}>
          刷新预览
        </button>
      </div>
    </div>
  );
}

/** "（语速过快 2、长句已拆分 1）" — grouped, because a raw list is noise. */
function summarise(issues: Issue[]): string {
  const counts = new Map<string, number>();
  for (const issue of issues) counts.set(issue.kind, (counts.get(issue.kind) ?? 0) + 1);
  const parts = [...counts.entries()].map(
    ([kind, count]) => `${KIND_LABELS[kind] ?? kind} ${count}`,
  );
  return parts.length > 0 ? `（${parts.join("、")}）` : "";
}
