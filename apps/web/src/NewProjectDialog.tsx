/**
 * New-project dialog: pick a local audio file, set the hotwords, then transcribe.
 *
 * The audio path is typed rather than chosen with a file picker on purpose: the
 * server reads the file, so a browser-provided File object would have to be
 * uploaded first — gigabytes over HTTP just to reach a machine that already has
 * the file. The backend runs locally, so a path is both faster and honest about
 * where the data lives.
 *
 * Hotwords are edited *here* rather than after the transcription finishes. They
 * are an input to recognition: offering them in the workbench meant the only
 * moment they could help had already passed, and the UI's "转写时启用热词"
 * checkbox pointed at a list the user had no way to fill yet.
 *
 * Episode terms are held as a local draft until the media row exists (that is
 * the only thing they can be keyed to). Adding them earlier would mean creating
 * the project up front, which leaves an orphan project behind when the user
 * cancels.
 */

import { useCallback, useEffect, useState } from "react";
import { api, type AppSettings, type HotwordItem } from "./api";
import { HotwordEditor, describeHotwords, type HotwordTab } from "./HotwordEditor";

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
  const [useHotwords, setUseHotwords] = useState(true);
  const [models, setModels] = useState<AsrModel[]>([]);
  const [globalHotwords, setGlobalHotwords] = useState<HotwordItem[]>([]);
  const [episodeDraft, setEpisodeDraft] = useState<HotwordItem[]>([]);
  const [tab, setTab] = useState<HotwordTab>("episode");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refreshGlobal = useCallback(async () => {
    const rows = await api.globalHotwords();
    setGlobalHotwords(rows);
  }, []);

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
    void api
      .settings()
      .then((loaded: AppSettings) => setUseHotwords(loaded.use_hotwords))
      .catch(() => undefined);
    void refreshGlobal().catch(() => undefined);
  }, [open, refreshGlobal]);

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
  const hotwordsSupported = selected ? selected.instant_vocabulary : true;
  const hotwordsOn = useHotwords && hotwordsSupported;

  const addEpisode = async (text: string): Promise<void> => {
    setEpisodeDraft((current) =>
      current.some((item) => item.text === text) ? current : [...current, { text, weight: 3, lang: "zh", enabled: true, hit_count: 0, miss_count: 0 }],
    );
  };

  const removeEpisode = async (item: HotwordItem): Promise<void> => {
    setEpisodeDraft((current) => current.filter((entry) => entry.text !== item.text));
  };

  const create = async (): Promise<void> => {
    if (!name.trim() || !audioPath.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setStage("创建项目…");
      const project = await api.createProject(name.trim());

      setStage("检查音频文件…");
      const media = await api.registerMedia(project.id, audioPath.trim());

      // Episode terms were drafted before the media row existed; this is the
      // first moment they can be stored, and it must happen *before* the
      // transcription call or they would not be in the vocabulary.
      if (hotwordsOn && episodeDraft.length > 0) {
        setStage("保存本期热词…");
        const applied = await api.setEpisodeHotwords(
          project.id,
          media.id,
          episodeDraft.map((item) => item.text),
        );
        if (applied.rejected.length > 0) {
          onNotice(
            `${applied.rejected.length} 个热词未通过校验：` +
              applied.rejected.map((item) => `${item.text}（${item.reason}）`).join("、"),
          );
        }
      }

      setStage(`转写中（约 ${Math.max(1, Math.round(media.duration_ms / 60000))} 分钟音频）…`);
      const version = await api.transcribe(project.id, media.id, {
        model: model || undefined,
        hotwords: hotwordsOn,
      });

      if (hotwordsOn) {
        onNotice(
          version.hotword_count > 0
            ? `项目「${project.name}」已创建，转写带入 ${version.hotword_count} 个热词`
            : `项目「${project.name}」已创建，但热词表是空的，这次转写没有带入热词`,
        );
      } else {
        onNotice(`项目「${project.name}」已创建（未使用热词）`);
      }
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
              <em className="hint">完整路径，服务端直接读取；中文路径也可以</em>
            </span>
            <input
              value={audioPath}
              placeholder="D:/播客/第 07 期.wav"
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
              checked={hotwordsOn}
              onChange={(event) => setUseHotwords(event.target.checked)}
              disabled={busy || !hotwordsSupported}
            />
            <span>转写时启用热词</span>
          </label>

          {hotwordsOn ? (
            <div className="new-project-hotwords">
              <p className="section-note">{describeHotwords(globalHotwords, episodeDraft)}</p>
              <HotwordEditor
                episodeHotwords={episodeDraft}
                globalHotwords={globalHotwords}
                tab={tab}
                onTabChange={setTab}
                onAddEpisode={addEpisode}
                onRemoveEpisode={removeEpisode}
                onChanged={refreshGlobal}
                onNotice={onNotice}
                compact
                disabled={busy}
              />
            </div>
          ) : null}

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
