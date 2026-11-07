/**
 * Editable copy of the built-in prompts.
 *
 * Three decisions, each from a failure that is easy to cause and hard to see:
 *
 * * **Empty means "use the default".** An empty textarea is indistinguishable
 *   from a deliberately blanked one, so the field holds `""` and the server
 *   falls back. Otherwise a user who clears a box ships a document generated
 *   from no instructions at all.
 * * **The default is always visible.** 「恢复默认」 needs the original text, and
 *   editing a prompt you cannot compare against is editing blind — so the
 *   server sends both and the diff is one click away.
 * * **The transcript is not part of the template.** The prompt is a *system*
 *   message; the transcript arrives as the user message. That makes it
 *   impossible to edit a placeholder wrong and hand the model an empty input.
 */

import { useState } from "react";

export interface PromptRow {
  format: "shownotes" | "dialogue" | "article";
  label: string;
  default: string;
  current: string;
}

interface Props {
  prompts: PromptRow[];
  values: Record<PromptRow["format"], string>;
  onChange: (format: PromptRow["format"], value: string) => void;
}

export function PromptEditor({ prompts, values, onChange }: Props): React.JSX.Element {
  const [open, setOpen] = useState<PromptRow["format"] | null>(null);

  return (
    <div className="prompt-editor">
      <h4 className="settings-subhead">提示词</h4>
      <p className="section-note">
        这些是内置提示词。留空表示用内置版本，改了会一直生效直到你点「恢复默认」。
        转写稿是单独传进去的，所以改提示词不会把数据弄丢。
      </p>
      {prompts.map((row) => {
        const value = values[row.format];
        const isOpen = open === row.format;
        const customised = value.trim() !== "" && value.trim() !== row.default.trim();
        return (
          <div className="prompt-block" key={row.format}>
            <div className="prompt-head">
              <button
                type="button"
                className="ghost"
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : row.format)}
              >
                {isOpen ? "▾" : "▸"} {row.label}
                {customised ? " · 已自定义" : ""}
              </button>
              {customised ? (
                <button
                  type="button"
                  className="ghost small"
                  onClick={() => {
                    // Clearing the field is what restores the default: the
                    // server treats "" as "unset", so there is only one way to
                    // express it and no way for the two to disagree.
                    onChange(row.format, "");
                  }}
                >
                  恢复默认
                </button>
              ) : null}
            </div>
            {isOpen ? (
              <>
                <textarea
                  rows={12}
                  className="prompt-text"
                  value={value.trim() === "" ? row.default : value}
                  spellCheck={false}
                  onChange={(event) => onChange(row.format, event.target.value)}
                />
                <p className="section-note">
                  {value.trim() === ""
                    ? "当前是内置提示词；直接编辑就会变成自定义。"
                    : `已自定义（${value.length} 字）。`}
                </p>
              </>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
