/**
 * 模型：what recognition runs on.
 *
 * Its own panel because changing these is a *re-transcription* decision, not an
 * adjustment. The old single page put it at the top and everything else below,
 * which invited the assumption that picking a different ASR model fixes an
 * existing transcript. It does not, and the panel says so once.
 */

import { CheckboxField, NumberField, SectionNote, SelectField, type PanelProps } from "./fields";

interface Props extends PanelProps {
  models: { id: string; instant_vocabulary: boolean; diarization: boolean }[];
  llmModels: string[];
}

export function ModelsPanel({ settings, patch, models, llmModels }: Props): React.JSX.Element {
  return (
    <div className="settings-panel">
      <SectionNote>
        这些是<strong>下一次转写</strong>的输入。改动不会重跑已完成的转写，也不会修正已经识别错的字。
      </SectionNote>

      <SelectField
        label="语音识别模型"
        hint="决定是否支持热词与说话人分离"
        value={settings.asr_model}
        options={models.map((model) => ({
          value: model.id,
          label: `${model.id}${model.instant_vocabulary ? " · 支持热词" : " · 不支持热词"}`,
        }))}
        onChange={(value) => patch("asr_model", value)}
      />

      <SelectField
        label="语言模型"
        hint="热词抽取、校对、ShowNotes 与文稿都用它"
        value={settings.llm_model}
        options={llmModels.map((model) => ({ value: model, label: model }))}
        onChange={(value) => patch("llm_model", value)}
      />

      <CheckboxField
        label="区分说话人"
        hint="仅单声道有效；开启后长音频建议控制在两小时内"
        checked={settings.diarization}
        onChange={(value) => patch("diarization", value)}
      />

      <NumberField
        label="说话人数"
        hint="已知人数比自动检测准"
        min={1}
        max={8}
        value={settings.speaker_count}
        onChange={(value) => patch("speaker_count", value)}
      />
    </div>
  );
}
