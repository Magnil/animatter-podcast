/**
 * The template's editable text and avatar photos (§19.12).
 *
 * What this panel is for: the PSD supplies the **layout** — where each piece of text sits, how
 * big it is, what colour it is — and this lets the user change the **content** without
 * Photoshop. Nothing here can move a slot; that is deliberate, because the whole promise of
 * 「导入模板」 is that the designer's layout is kept.
 *
 * Three decisions worth knowing before changing this file:
 *
 * 1. **Edits are committed per field, on blur.** Every keystroke writing to the server would
 *    make each letter of a title a recompile-triggering request. Blur (and Enter) is the
 *    gesture the user already expects from a settings field.
 * 2. **The server's response replaces local state.** The response is the *resolved* slot list,
 *    so clearing a field visibly returns to the PSD's text — no "did it save?" guessing, and
 *    no second source of truth to drift.
 * 3. **A content slot is shown but not editable.** 「开场及嘉宾介绍」and the guest block sit
 *    inside the media box; the footage covers them. Listing them with an explanation is
 *    honest; hiding them would make the PSD's own text look like it was dropped by a bug.
 */

import { useEffect, useState } from "react";
import { api, type VideoTemplate, type VideoTextSlot } from "./api";

interface Props {
  projectId: string;
  template: VideoTemplate;
  onTemplate: (next: VideoTemplate) => void;
  onNotice: (message: string) => void;
}

/** Roles the user can edit, in the order the poster reads them. */
const EDITABLE: VideoTextSlot["role"][] = ["episode", "title", "avatar_name", "note"];

const ROLE_LABEL: Record<VideoTextSlot["role"], string> = {
  episode: "期号",
  title: "当期标题",
  avatar_name: "头像名",
  note: "其他文字",
  content: "框内文字",
};

export function TemplateTexts({ projectId, template, onTemplate, onNotice }: Props): React.JSX.Element {
  const [families, setFamilies] = useState<string[]>([]);
  const [preferred, setPreferred] = useState<string[]>([]);
  const [editing, setEditing] = useState<string | null>(null);

  useEffect(() => {
    void api
      .fonts()
      .then((list) => {
        setFamilies(list.families);
        setPreferred(list.preferred);
      })
      .catch(() => setFamilies([]));
  }, []);

  const slots = template.slots.texts ?? [];

  const commit = async (
    slot: VideoTextSlot,
    edit: { text?: string; font?: string; font_size?: number },
  ): Promise<void> => {
    setEditing(slot.key);
    try {
      onTemplate(await api.editVideoText(projectId, slot.key, edit));
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setEditing(null);
    }
  };

  /**
   * The dropdown offers the fonts this machine actually has, with the design's two first.
   *
   * A hard-coded list would offer 数黑 on a machine without 字由 and then fail at render time
   * with a 503 the user cannot act on. ``preferred`` is computed server-side by resolving the
   * design fonts, so it is empty rather than wrong when they are missing.
   */
  const fontOptions = (current: string): string[] => {
    const head = [current, ...preferred.filter((name) => name !== current)].filter(Boolean);
    const rest = families.filter((name) => !head.includes(name));
    return [...head, ...rest];
  };

  const rows = EDITABLE.flatMap((role) => slots.filter((slot) => slot.role === role));
  const content = slots.filter((slot) => slot.role === "content");

  return (
    <div className="template-texts">
      {rows.map((slot) => {
        const isName = slot.role === "avatar_name";
        const busy = editing === slot.key;
        return (
          <div
            key={slot.key}
            className={isName ? "template-text-row is-name" : "template-text-row"}
            data-slot={slot.key}
          >
            <span className="template-text-label" title={`${ROLE_LABEL[slot.role]} · ${slot.name}`}>
              {slot.role === "note" ? slot.label : ROLE_LABEL[slot.role]}
              {isName ? <small>{slot.label.replace("头像名 · ", "")}</small> : null}
            </span>
            <input
              // Remounted when the server's text changes, so clearing a field visibly goes
              // back to the PSD's own string instead of keeping the cleared one on screen.
              key={`text:${slot.text}`}
              type="text"
              defaultValue={slot.text}
              disabled={busy}
              aria-label={`${ROLE_LABEL[slot.role]}内容`}
              onBlur={(event) => {
                const next = event.target.value;
                if (next !== slot.text) void commit(slot, { text: next });
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
            />
            <select
              value={slot.font}
              disabled={busy || families.length === 0}
              aria-label={`${ROLE_LABEL[slot.role]}字体`}
              onChange={(event) => void commit(slot, { font: event.target.value })}
            >
              {fontOptions(slot.font).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <input
              type="number"
              className="template-size"
              defaultValue={slot.font_size}
              min={6}
              max={400}
              step={1}
              disabled={busy}
              aria-label={`${ROLE_LABEL[slot.role]}字号`}
              title={`字号 ${slot.font_size} px（PSD 里的实际大小）`}
              onBlur={(event) => {
                const next = Number(event.target.value);
                if (Number.isFinite(next) && next > 0 && next !== slot.font_size) {
                  void commit(slot, { font_size: Math.round(next) });
                }
              }}
            />
          </div>
        );
      })}

      {content.length > 0 ? (
        <p className="section-note">
          框内有 {content.length} 段模板文字（
          {content.map((slot) => slot.text.split("\n")[0]).join("、")}
          ），素材会把它们盖住，这轮不参与渲染。
        </p>
      ) : null}
    </div>
  );
}

/** The per-avatar photo button, kept separate so its busy state cannot block the text rows. */
export function AvatarPhotoButton({
  projectId,
  slot,
  label,
  onTemplate,
  onNotice,
}: {
  projectId: string;
  slot: { name: string; photo_replaced: boolean };
  label: string;
  onTemplate: (next: VideoTemplate) => void;
  onNotice: (message: string) => void;
}): React.JSX.Element {
  const [busy, setBusy] = useState(false);

  /**
   * Two gestures, both here: 「换图」 imports a file from disk *and* binds it in one step, and
   * 「还原」 drops the override.
   *
   * The file comes through the hidden `<input type=file>` rather than a path prompt. A browser
   * `File` has no path, so the upload endpoint is the only one that works from a click — and a
   * typed path is what people try last, not first.
   */
  const replace = async (file: File): Promise<void> => {
    setBusy(true);
    try {
      const asset = await api.uploadVideoAsset(projectId, file);
      onTemplate(await api.setVideoAvatarPhoto(projectId, slot.name, asset.id));
      onNotice(`${label} 换图完成`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const restore = async (): Promise<void> => {
    setBusy(true);
    try {
      onTemplate(await api.setVideoAvatarPhoto(projectId, slot.name, null));
      onNotice(`${label} 已还原成模板里的图`);
    } catch (caught) {
      onNotice(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="avatar-photo-actions">
      <label className={busy ? "ghost small disabled" : "ghost small"}>
        {slot.photo_replaced ? "换另一张" : "换图"}
        <input
          type="file"
          accept="image/*"
          hidden
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void replace(file);
          }}
        />
      </label>
      {slot.photo_replaced ? (
        <button type="button" className="ghost small" disabled={busy} onClick={() => void restore()}>
          还原
        </button>
      ) : null}
    </span>
  );
}
