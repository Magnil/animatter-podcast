/**
 * 文本处理：the model parameters and prompts behind ShowNotes / 文稿.
 *
 * Split out because it is the only panel with free-text *prose* in it — three
 * prompts and a signature — and mixing prose editors into a page of numeric
 * fields is what made the old dialog feel long: 22 fields, three of them taller
 * than the viewport.
 */

import type { PromptRow } from "../PromptEditor";
import { PromptEditor } from "../PromptEditor";
import { NumberField, SectionNote, SelectField, TextField, type PanelProps } from "./fields";

interface Props extends PanelProps {
  prompts: PromptRow[];
}

export function TextPanel({ settings, patch, prompts }: Props): React.JSX.Element {
  return (
    <div className="settings-panel">
      <SectionNote>
        「导出 › ShowNotes / 文稿」用这里的模型与提示词。改提示词只影响下一次生成，已经生成的内容不会被追溯改动。
      </SectionNote>

      <SelectField
        label="文稿默认格式"
        hint="对话式保留说话人；文章式改写成书面文章"
        value={settings.script_format}
        options={[
          { value: "dialogue", label: "对话式（轻度整理，保留说话人）" },
          { value: "article", label: "文章式（段落化、书面化，不分说话人）" },
        ]}
        onChange={(value) => patch("script_format", value)}
      />

      <NumberField
        label="温度"
        hint="0.2–0.4 保守贴近原话，0.6 平衡，0.9 以上容易自由发挥"
        min={0}
        max={1.5}
        step={0.1}
        value={settings.text_temperature}
        onChange={(value) => patch("text_temperature", value)}
      />
      <NumberField
        label="最大输出长度"
        hint="单次调用的 token 上限；长节目分段生成，每段都会用这个上限"
        min={1000}
        max={32000}
        step={500}
        value={settings.text_max_tokens}
        onChange={(value) => patch("text_max_tokens", value)}
      />

      <TextField
        label="ShowNotes 签名"
        hint="原文追加在 ShowNotes 末尾；可多行，留空则不写"
        rows={4}
        value={settings.shownotes_signature}
        placeholder={"主播：马小褂\n收听：小宇宙 / 苹果播客"}
        onChange={(value) => patch("shownotes_signature", value)}
      />

      {prompts.length > 0 ? (
        <PromptEditor
          prompts={prompts}
          values={{
            shownotes: settings.script_prompt_shownotes,
            dialogue: settings.script_prompt_dialogue,
            article: settings.script_prompt_article,
          }}
          onChange={(format, value) => {
            if (format === "shownotes") patch("script_prompt_shownotes", value);
            else if (format === "dialogue") patch("script_prompt_dialogue", value);
            else patch("script_prompt_article", value);
          }}
        />
      ) : (
        <p className="muted">提示词载入中…</p>
      )}
    </div>
  );
}
