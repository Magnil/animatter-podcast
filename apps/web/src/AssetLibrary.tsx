/**
 * Asset library panel: import images and clips, then drag them onto the video track (P2).
 *
 * **Files are copied, not referenced.** The user's material lives wherever it lives — a
 * Downloads folder, a shared drive, a phone sync directory. A timeline pointing at those
 * paths works exactly once and then breaks silently when the folder is reorganised, and
 * the failure surfaces fourteen minutes into an export as 「文件不见了」. Copying costs a few
 * megabytes and makes the project self-contained.
 *
 * **Two import paths, because there are two real gestures.** Typing a path is what the PSD
 * import already does and is the fastest way to pull something in while editing; dragging
 * a file from Explorer is what people try first. A browser `File` has no path, so the
 * second one uploads.
 *
 * **Drag onto the track, not 「add to selected block」.** A button that fills whichever
 * block happens to be selected is a mode the user has to remember; dragging is the thing
 * they were already doing with their hand.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type VideoAsset } from "./api";
import { formatDuration } from "./format";

interface Props {
  projectId: string;
  /** Called when a drag starts/ends, so the track can show a drop target. */
  onDragStateChange: (assetId: string | null) => void;
  onNotice: (message: string) => void;
  /** Bumped by the parent to force a reload (e.g. after a block's asset was removed). */
  revision?: number;
  /**
   * When set, every asset gets a 「加进画廊」 button.
   *
   * Dragging onto a block would be the nicer gesture, but the track's drag target is the
   * *timeline position* — a drop lands where the pointer is, which for a gallery means
   * "create a new block", not "add to this one". Naming the block explicitly is the only
   * unambiguous way to say the second thing.
   */
  onAddToBlock?: (assetId: string) => void;
}

export function AssetLibrary({
  projectId,
  onDragStateChange,
  onNotice,
  revision = 0,
  onAddToBlock,
}: Props): React.JSX.Element {
  const [assets, setAssets] = useState<VideoAsset[]>([]);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      setAssets(await api.videoAssets(projectId));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load, revision]);

  const adopt = (asset: VideoAsset): void => {
    // Prepend rather than reload: an upload of a 40 MB clip should not cost a second
    // round-trip just to learn what the server already told us.
    setAssets((current) => [asset, ...current.filter((item) => item.id !== asset.id)]);
  };

  const doImport = async (): Promise<void> => {
    if (!path.trim()) return;
    setBusy(true);
    try {
      adopt(await api.importVideoAsset(projectId, path.trim()));
      setPath("");
      onNotice("素材已导入");
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doUpload = async (files: FileList | null): Promise<void> => {
    if (!files || files.length === 0) return;
    setBusy(true);
    try {
      for (const file of Array.from(files)) {
        adopt(await api.uploadVideoAsset(projectId, file));
      }
      onNotice(files.length === 1 ? "素材已导入" : `已导入 ${files.length} 个素材`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const doDelete = async (asset: VideoAsset): Promise<void> => {
    try {
      await api.deleteVideoAsset(asset.id);
      setAssets((current) => current.filter((item) => item.id !== asset.id));
      onNotice("素材已删除");
    } catch (caught) {
      // The server refuses while a block still uses it, and the message names which one.
      onNotice(caught instanceof Error ? caught.message : String(caught));
    }
  };

  return (
    <div className="asset-library">
      <div className="asset-import">
        <label className="field">
          <span>素材路径</span>
          <input
            type="text"
            value={path}
            placeholder="D:/素材/封面.png"
            onChange={(event) => setPath(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void doImport();
            }}
          />
        </label>
        <div className="edl-actions">
          <button type="button" disabled={busy || !path.trim()} onClick={() => void doImport()}>
            导入
          </button>
          <button
            type="button"
            className="ghost"
            disabled={busy}
            onClick={() => fileInput.current?.click()}
          >
            选文件…
          </button>
          <input
            ref={fileInput}
            type="file"
            accept="image/*,video/*"
            multiple
            hidden
            onChange={(event) => void doUpload(event.target.files)}
          />
        </div>
        <p className="section-note">
          图片和视频会复制进项目目录，之后移动或删除原文件都不影响。也可以直接把文件拖进来。
        </p>
      </div>

      {error ? <p className="error-text">{error}</p> : null}

      {assets.length === 0 ? (
        <p className="muted small">素材库是空的。导入一张图或一段视频，然后拖到上面的时间轴。</p>
      ) : (
        <ul className="asset-list">
          {assets.map((asset) => (
            <li
              key={asset.id}
              data-asset-id={asset.id}
              className={`asset-item${asset.exists ? "" : " is-broken"}${
                dragging === asset.id ? " is-dragging" : ""
              }`}
              draggable={asset.exists}
              onDragStart={(event) => {
                event.dataTransfer.setData("text/plain", asset.id);
                event.dataTransfer.effectAllowed = "copy";
                setDragging(asset.id);
                onDragStateChange(asset.id);
              }}
              onDragEnd={() => {
                setDragging(null);
                onDragStateChange(null);
              }}
            >
              <div
                className="asset-thumb"
                style={asset.kind === "image" ? { backgroundImage: `url(${asset.url})` } : undefined}
              >
                {asset.kind === "video" ? <span className="asset-play">▶</span> : null}
              </div>
              <div className="asset-meta">
                <span className="asset-name" title={asset.name}>
                  {asset.name}
                </span>
                <span className="muted small">
                  {asset.exists
                    ? asset.kind === "video"
                      ? `${asset.width}×${asset.height} · ${formatDuration(asset.duration_ms)}`
                      : `${asset.width}×${asset.height}`
                    : "文件不见了"}
                </span>
              </div>
              {/* Stacked, not side by side: two 22px buttons in a row cost 44px of a card
                  that is only 220px wide, and the name is what has to survive. */}
              <div className="asset-actions">
                {onAddToBlock ? (
                  <button
                    type="button"
                    className="ghost small"
                    title="加进选中的素材块"
                    onClick={() => onAddToBlock(asset.id)}
                  >
                    ＋
                  </button>
                ) : null}
                <button
                  type="button"
                  className="ghost small"
                  title="删除素材"
                  onClick={() => void doDelete(asset)}
                >
                  ✕
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
