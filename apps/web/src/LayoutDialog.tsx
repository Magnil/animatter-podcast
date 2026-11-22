/**
 * 「编辑布局…」——把画布编辑器装进一个模态框（第 140 轮）。
 *
 * **为什么不是页签。** 第 139 轮把它做成了第五个页签，理由是「拖动需要一块够大的画布 + 一份
 * 槽位清单同时在场」。那个理由是对的，结论错了：决定一件事该不该占一个常驻入口的是**使用频率**，
 * 不是它需要多大地方。布局改一次就不动，而视频面板的页签是每一轮剪辑都要点的东西——给一个
 * 一次性动作留一个永久的页签，等于每次打开面板都多问一遍「你要不要看布局」。
 *
 * **为什么不是行内展开。** 展开在 `video-stage` 里就得挤走预览；而用户判断「这样摆好不好看」
 * 靠的正是那块预览。模态框盖住它，代价是「拖的时候看不见成片」——但编辑器画布画的**就是**
 * 成片的样子（海报 + 底板 + 描边 + 四个头像，与预览、导出同一条图层栈），所以并没有丢信息；
 * 关掉之后预览会立刻按新布局重取（`onChanged` → plan digest）。
 *
 * **关闭不等于丢弃。** 每一次松手都已经落库了，关框只是收起画布。所以脚注只有一个「关闭」，
 * 没有「取消」——有一个点下去会回滚一切的按钮，才是真正危险的东西。反过来「全部恢复默认」
 * 在编辑器内部，是一次显式动作。
 */

import { useEffect } from "react";

import { LayoutEditor } from "./LayoutEditor";
import type { VideoTemplate } from "./api";

interface Props {
  open: boolean;
  projectId: string;
  template: VideoTemplate;
  onClose: () => void;
  onNotice: (message: string) => void;
  /** A save landed: the panel refetches the plan so the preview agrees with the canvas. */
  onChanged: () => void;
}

export function LayoutDialog({
  open,
  projectId,
  template,
  onClose,
  onNotice,
  onChanged,
}: Props): React.JSX.Element | null {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal layout-modal"
        role="dialog"
        aria-modal="true"
        aria-label="编辑布局"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>编辑布局</h2>
          <button type="button" className="ghost" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </header>

        <div className="modal-body modal-single">
          {/* Mounted only while open: opening fetches the layout fresh, and closing drops the
              draft. Nothing is lost — every drop already committed. */}
          <LayoutEditor
            projectId={projectId}
            template={template}
            onNotice={onNotice}
            onChanged={onChanged}
          />
          <p className="section-note">
            画布上摆的就是成片的样子：海报、底板、描边、四个头像。框里那两行给作者看的说明文字
            （role=content）不会画进成片，所以这里也没有它们。布局只存在这个项目里——换模板、
            重新编译 PSD 都不会丢，导出模板包也不会带走。
          </p>
        </div>

        <footer className="modal-foot">
          <button type="button" onClick={onClose}>
            关闭
          </button>
        </footer>
      </div>
    </div>
  );
}
