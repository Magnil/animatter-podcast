/**
 * The template's editable properties (§19.12, §19.15).
 *
 * What this panel is for: the PSD supplies the **layout** — where each piece of text sits, how
 * big it is, what colour it is — and this lets the user change the **content** without
 * Photoshop. Nothing here can move a slot; that is deliberate, because the whole promise of
 * 「导入模板」 is that the designer's layout is kept.
 *
 * **One list, not two** (round 99). The avatar rows used to live in a second list below this
 * one, so 「主播1」 appeared twice — once as a name-plate input here, once as a binding row
 * there — and the two could disagree. There is one row per property now:
 *
 * | 行 | 能改什么 |
 * |---|---|
 * | 期号 / 当期标题 / 页脚 | 文字、字体、字号 |
 * | 主播1、主播2 / 嘉宾1、嘉宾2 | 绑定哪个说话人，以及换掉这张头像 |
 *
 * and they share one label style, because the two lists used to colour their labels
 * differently for no reason a reader could see.
 *
 * **The avatar name is no longer an input.** It is the bound speaker's name (server-side,
 * `video_avatars.name_plates`), so the plate cannot say 「彼方」 over 呈AA's face, and renaming
 * a speaker renames the plate. That is why `onBind` also refreshes the template.
 *
 * Two decisions worth knowing before changing this file:
 *
 * 1. **Edits are committed per field, on blur.** Every keystroke writing to the server would
 *    make each letter of a title a recompile-triggering request. Blur (and Enter) is the
 *    gesture the user already expects from a settings field.
 * 2. **The server's response replaces local state.** The response is the *resolved* slot list,
 *    so clearing a field visibly returns to the PSD's text — no "did it save?" guessing, and
 *    no second source of truth to drift.
 */

import { useEffect, useState } from "react";
import {
  api,
  type SpeakerInfo,
  type VideoAvatar,
  type VideoTemplate,
  type VideoTextSlot,
} from "./api";
import { formatDuration } from "./format";

interface Props {
  projectId: string;
  template: VideoTemplate;
  /** The avatar slots with their current speaker and talk time (P3). */
  avatars: VideoAvatar[];
  speakers: SpeakerInfo[];
  onTemplate: (next: VideoTemplate) => void;
  onBind: (slot: string, speakerId: number | null) => void;
  onNotice: (message: string) => void;
}

/** Roles that are a text field, in the order the poster reads them. */
const EDITABLE: VideoTextSlot["role"][] = ["episode", "title", "note"];

const ROLE_LABEL: Record<VideoTextSlot["role"], string> = {
  episode: "期号",
  title: "当期标题",
  avatar_name: "头像名",
  note: "其他文字",
  content: "框内文字",
};

export function TemplateTexts({
  projectId,
  template,
  avatars,
  speakers,
  onTemplate,
  onBind,
  onNotice,
}: Props): React.JSX.Element {
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

  const head = EDITABLE.slice(0, 2).flatMap((role) => slots.filter((slot) => slot.role === role));
  const notes = slots.filter((slot) => slot.role === "note");

  const textRow = (slot: VideoTextSlot): React.JSX.Element => {
    const busy = editing === slot.key;
    /*
      A multi-line value needs a `<textarea>`, not an `<input type="text">`.

      Measured in the walkthrough: the title slot's value is 31 characters holding a `\n`, and
      the `<input>`'s `value` came back with **no newline at all** — HTML's value sanitisation
      strips CR/LF from a text input. So the field showed 「…独家专访**六年磨一剑…」 as one run-on
      line, and *any* edit (even changing one character elsewhere in it) committed that
      stripped string back, silently collapsing the design's two-line title into one. The
      poster is the thing this panel exists to control, so the control has to be able to
      express what the design actually contains.

      The 期号 is one line by definition (`EP.23`), so it keeps the single-line input.
    */
    const multiline = slot.role !== "episode";
    const lines = Math.min(3, Math.max(1, slot.text.split("\n").length));
    const shared = {
      key: `text:${slot.text}`,
      defaultValue: slot.text,
      disabled: busy,
      "aria-label": `${ROLE_LABEL[slot.role]}内容`,
      onBlur: (event: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement>) => {
        const next = event.target.value;
        if (next !== slot.text) void commit(slot, { text: next });
      },
    };
    return (
      <div
        key={slot.key}
        className={`template-row is-text${multiline ? " is-multiline" : ""}`}
        data-slot={slot.key}
      >
        {/*
          The server's label, not the layer name: 「页脚」 is a role name derived at compile
          time. A [data-]label taken from the layer name was the value-labelled-with-itself bug
          of round 98 — Photoshop names an un-renamed text layer after its own text.
        */}
        <span className="template-row-label" title={`${ROLE_LABEL[slot.role]} · ${slot.name}`}>
          {slot.label}
        </span>
        {multiline ? (
          <textarea
            {...shared}
            rows={lines}
            onKeyDown={(event) => {
              // Enter inserts a line break here — that is the point of the field. Escape is the
              // way out without committing, which is what a multi-line editor should do.
              if (event.key === "Escape") event.currentTarget.blur();
            }}
          />
        ) : (
          <input
            {...shared}
            type="text"
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
            }}
          />
        )}
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
  };

  /**
   * One avatar row: which speaker it shows, how much they talk, and the photo button.
   *
   * The talk time is not decoration — a slot bound to a speaker who never speaks reads
   * 「没说话」, and that number is the only way to tell a wrong binding from a broken render.
   * It is one short line, not a paragraph: the panel's job is the form, not the explanation.
   */
  const avatarRow = (slot: NonNullable<VideoTemplate["slots"]["avatars"]>[number]) => {
    const bound = avatars.find((item) => item.name === slot.name);
    const label = slot.label_name || slot.name;
    return (
      <div key={slot.name} className="template-row is-avatar" data-slot={slot.name}>
        <span className="template-row-label" title={`头像位 ${slot.name}`}>
          {label}
        </span>
        <select
          value={bound?.speaker_id ?? ""}
          aria-label={`${label}绑定说话人`}
          onChange={(event) =>
            onBind(slot.name, event.target.value === "" ? null : Number(event.target.value))
          }
        >
          <option value="">不绑定（不出现）</option>
          {speakers.map((speaker) => (
            <option key={speaker.local_id} value={speaker.local_id}>
              {speaker.name || `说话人 ${speaker.local_id}`}
            </option>
          ))}
        </select>
        <small className="template-row-talk">
          {bound === undefined || bound.speaker_id === null
            ? "—"
            : bound.turns === 0
              ? "没说话"
              : `${bound.turns} 段 · ${formatDuration(bound.lit_ms)}`}
        </small>
        <AvatarPhotoButton
          projectId={projectId}
          slot={slot}
          label={label}
          onTemplate={onTemplate}
          onNotice={onNotice}
        />
      </div>
    );
  };

  return (
    <div className="template-form">
      {head.map(textRow)}
      {(template.slots.avatars ?? []).map(avatarRow)}
      {notes.map(textRow)}

      {slots.some((slot) => slot.text.includes("**")) ? (
        <p className="section-note template-hint">
          用 <b>**</b> 标出要变色的字，例如 <code>EP.**23**</code>。
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
