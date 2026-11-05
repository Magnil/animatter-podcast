/**
 * New-project dialog: pick a local audio file and a model, then transcribe.
 *
 * The audio path is typed rather than chosen with a file picker on purpose: the
 * server reads the file, so a browser-provided File object would have to be
 * uploaded first — gigabytes over HTTP just to reach a machine that already has
 * the file. The backend runs locally, so a path is both faster and honest about
 * where the data lives.
 */

import { useEffect, useState } from "react";
import { api } from "./api";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Called after a project is created; the parent reloads the workspace. */
  onCreated: (projectId: string) => void;
  onNotice: (message: string) => void;
}

interface AsrModel {
  id: string;
  instant_vocabulary: boolean;
  diarization: boolean;
  max_hotwords: number;
}

export function NewProjectDialog({ open, onClose, onCreated, onNotice }: Props): React.JSX.Element | null {
  const [name, setName] = useState("");
  const [audioPath, setAudioPath] = useState("");
  const [model, setModel] = useState("");
  const [useHotwords, setUseHotwords] = useState(false);
  const [models, setModels] = useState<AsrModel[]>([]);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setStage("");
    void api
      .modelOptions()
      .then((options) => {
        setModels(options.asr);
        setModel((current) => current || (options.asr[0]?.id ?? ""));
      })
      .catch(() => undefined);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);

  if (!open) return null;

  const selected = models.find((item) => item.id === model);

  const create = async (): Promise<void> => {
    if (!name.trim() || !audioPath.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setStage("创建项目…");
      const project = await api.createProject(name.trim());

      setStage("检查音频文件…");
      const media = await api.registerMedia(project.id, audioPath.trim());

      setStage(`转写中（约 ${Math.max(1, Math.round(media.duration_ms / 60000))} 分钟音频）…`);
      await api.transcribe(project.id, media.id, { model: model || undefined, hotwords: useHotwords });

      onNotice(`项目「${project.name}」已创建`);
      onCreated(project.id);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
      setStage("");
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => !busy && onClose()}>
      <div
        className="modal modal-narrow"
        role="dialog"
        aria-modal="true"
        aria-label="新建项目"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>新建项目</h2>
          <button type="button" className="ghost" onClick={onClose} disabled={busy} aria-label="关闭">
            ✕
          </button>
        </header>

        <div className="modal-body modal-single">
          {error ? <p className="error-text">{error}</p> : null}

          <label className="field">
            <span>项目名</span>
            <input
              autoFocus
              value={name}
              placeholder="例如：S02E07 对话某某"
              onChange={(event) => setName(event.target.value)}
              disabled={busy}
            />
          </label>

          <label className="field">
            <span>
              本地音频路径
              <em className="hint">完整路径，服务端直接读取</em>
            </span>
            <input
              value={audioPath}
              placeholder="D:/podcast/ep07.wav"
              onChange={(event) => setAudioPath(event.target.value)}
              disabled={busy}
            />
          </label>

          <label className="field">
            <span>语音识别模型</span>
            <select value={model} onChange={(event) => setModel(event.target.value)} disabled={busy}>
              {models.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.id}
                </option>
              ))}
            </select>
          </label>

          {selected ? (
            <p className="section-note">
              {selected.instant_vocabulary
                ? `支持热词（上限 ${selected.max_hotwords} 个）`
                : "不支持热词，选它会忽略热词设置"}
              {" · "}
              {selected.diarization ? "支持区分说话人" : "不支持区分说话人"}
            </p>
          ) : null}

          <label className="field checkbox">
            <input
              type="checkbox"
              checked={useHotwords}
              onChange={(event) => setUseHotwords(event.target.checked)}
              disabled={busy || (selected ? !selected.instant_vocabulary : false)}
            />
            <span>转写时启用热词</span>
          </label>

          <p className="section-note">
            转写会调用云端接口，按音频长度计费。完成后工作区会自动切换到新项目。
          </p>
        </div>

        <footer className="modal-foot">
          {stage ? <span className="muted small">{stage}</span> : null}
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            disabled={busy || !name.trim() || !audioPath.trim()}
            onClick={() => void create()}
          >
            {busy ? "处理中…" : "创建并转写"}
          </button>
        </footer>
      </div>
    </div>
  );
}
