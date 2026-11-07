/**
 * Small field primitives shared by the settings panels.
 *
 * Extracted because the alternative is what the old single-page dialog did:
 * one function per control type, then `NUMERIC.slice(1, 2)` to pick "the one
 * field that belongs to this section". Slice indices are positional, so moving
 * a field between panels silently moved a *different* field with it. Each panel
 * now names its fields explicitly.
 */

import type { AppSettings } from "../api";

export function SectionNote({
  children,
  strong,
}: {
  children: React.ReactNode;
  strong?: boolean;
}): React.JSX.Element {
  return (
    <p className="section-note">
      {children}
      {strong ? <strong>（只影响下一次）</strong> : null}
    </p>
  );
}

export function TextField({
  label,
  hint,
  value,
  placeholder,
  rows,
  mono,
  onChange,
}: {
  label: string;
  hint?: string;
  value: string;
  placeholder?: string;
  rows?: number;
  mono?: boolean;
  onChange: (value: string) => void;
}): React.JSX.Element {
  return (
    <label className="field">
      <span>
        {label}
        {hint ? <em className="hint">{hint}</em> : null}
      </span>
      {rows ? (
        <textarea
          rows={rows}
          className={mono ? "mono-input" : undefined}
          value={value}
          placeholder={placeholder}
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </label>
  );
}

export function NumberField({
  label,
  hint,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
}): React.JSX.Element {
  return (
    <label className="field">
      <span>
        {label}
        {hint ? <em className="hint">{hint}</em> : null}
      </span>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        // An empty box reports 0, which then fails server validation for fields
        // whose minimum is above zero. Falling back to the minimum keeps the
        // field usable while the user is retyping it.
        onChange={(event) => {
          const next = Number(event.target.value);
          onChange(Number.isFinite(next) && event.target.value !== "" ? next : min);
        }}
      />
    </label>
  );
}

export function CheckboxField({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}): React.JSX.Element {
  return (
    <label className="field checkbox">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        {label}
        {hint ? <em className="hint">{hint}</em> : null}
      </span>
    </label>
  );
}

export function SelectField<T extends string>({
  label,
  hint,
  value,
  options,
  onChange,
}: {
  label: string;
  hint?: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}): React.JSX.Element {
  return (
    <label className="field">
      <span>
        {label}
        {hint ? <em className="hint">{hint}</em> : null}
      </span>
      <select value={value} onChange={(event) => onChange(event.target.value as T)}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Every panel receives the draft and a typed setter; none of them save. */
export interface PanelProps {
  settings: AppSettings;
  patch: <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => void;
}
