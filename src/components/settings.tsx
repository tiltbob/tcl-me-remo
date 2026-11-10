// Controls bound to one setting each. Limits, choices and help texts come from the schema
// the service sends with get_config; nothing about a setting's range is repeated here.

import { ConfirmModal, DialogButton, DropdownItem, Field, SliderField, TextField, ToggleField, showModal } from "@decky/ui";
import type { FC, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";

import { applyConfig } from "../actions";
import { store } from "../store";
import type { TvState } from "../store";
import { colors } from "../text";
import type { Config, ConfigBundle, ConfigPatch, SettingInfo } from "../types";

type Scalar = string | number | boolean;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The schema entry of a setting, by dotted path ("input.hdmi_port"). */
export function settingInfo(bundle: ConfigBundle | null, path: string): SettingInfo | null {
  let node: unknown = bundle?.schema;
  for (const part of path.split(".")) {
    if (!isObject(node)) return null;
    node = node[part];
  }
  return isObject(node) && typeof node.type === "string" ? (node as unknown as SettingInfo) : null;
}

export function settingValue(config: Config | null, path: string): unknown {
  let node: unknown = config;
  for (const part of path.split(".")) {
    if (!isObject(node)) return undefined;
    node = node[part];
  }
  return node;
}

export function patchFor(path: string, value: Scalar): ConfigPatch {
  const parts = path.split(".");
  let patch: unknown = value;
  for (let index = parts.length - 1; index >= 0; index--) patch = { [parts[index]]: patch };
  return patch as ConfigPatch;
}

function helpOf(info: SettingInfo | null, description: ReactNode | undefined): ReactNode {
  return description !== undefined ? description : (info?.help ?? "");
}

interface SettingProps {
  state: TvState;
  path: string;
  label: ReactNode;
  /** Replaces the service's help text. */
  description?: ReactNode;
  disabled?: boolean;
}

// ---------------------------------------------------------------------- on / off

export const SettingToggle: FC<SettingProps> = ({ state, path, label, description, disabled }) => {
  const info = settingInfo(state.bundle, path);
  const value = settingValue(state.config, path);
  return (
    <ToggleField
      label={label}
      description={helpOf(info, description)}
      checked={value === true}
      disabled={disabled || typeof value !== "boolean"}
      onChange={(checked) => void applyConfig(patchFor(path, checked))}
    />
  );
};

// ---------------------------------------------------------------------- numbers

function decimalsOf(step: number): number {
  const text = String(step);
  const dot = text.indexOf(".");
  return dot < 0 ? 0 : text.length - dot - 1;
}

/**
 * A number with limits from the schema. Dragging sends one change when the slider comes to
 * rest, not one per step.
 */
export const SettingSlider: FC<SettingProps & { step: number; suffix?: string }> = ({
  state,
  path,
  label,
  description,
  disabled,
  step,
  suffix,
}) => {
  const info = settingInfo(state.bundle, path);
  const saved = settingValue(state.config, path);
  const [draft, setDraft] = useState<number | null>(null);
  const waiting = useRef<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mounted = useRef(true);

  const send = () => {
    timer.current = null;
    const value = waiting.current;
    waiting.current = null;
    if (value === null) return;
    void applyConfig(patchFor(path, value)).then(() => {
      if (mounted.current && waiting.current === null) setDraft(null);
    });
  };

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (timer.current !== null) {
        // Leaving the page must neither lose the change nor leave a timer behind.
        clearTimeout(timer.current);
        send();
      }
    };
  }, []);

  if (!info || typeof saved !== "number" || info.min === undefined || info.max === undefined) {
    return <Field label={label} description={helpOf(info, description)} focusable={true} />;
  }
  const whole = info.type === "int";
  const usedStep = whole ? Math.max(1, Math.round(step)) : step;
  const decimals = decimalsOf(usedStep);
  const clean = (raw: number) => {
    const snapped = Math.round(raw / usedStep) * usedStep;
    const bounded = Math.min(info.max as number, Math.max(info.min as number, snapped));
    return Number(bounded.toFixed(decimals));
  };
  return (
    <SliderField
      label={label}
      description={helpOf(info, description)}
      value={draft ?? saved}
      min={info.min}
      max={info.max}
      step={usedStep}
      showValue={true}
      valueSuffix={suffix}
      disabled={disabled}
      onChange={(raw) => {
        const value = clean(raw);
        setDraft(value);
        waiting.current = value;
        if (timer.current !== null) clearTimeout(timer.current);
        timer.current = setTimeout(send, 400);
      }}
    />
  );
};

// ---------------------------------------------------------------------- choices

export interface Choice {
  value: Scalar;
  label: string;
  /** One line about what this choice means; shown while it is selected. */
  hint?: string;
}

function allowed(info: SettingInfo | null, value: Scalar): boolean {
  if (!info) return true;
  if (info.choices && typeof value === "string") return info.choices.includes(value);
  if (typeof value === "number") {
    if (info.min !== undefined && value < info.min) return false;
    if (info.max !== undefined && value > info.max) return false;
  }
  return true;
}

/** One choice per whole number the schema allows, for settings with a handful of values. */
export function rangeChoices(info: SettingInfo | null, label: (value: number) => string): Choice[] {
  const choices: Choice[] = [];
  if (info && info.min !== undefined && info.max !== undefined && info.max - info.min <= 20) {
    for (let value = Math.ceil(info.min); value <= info.max; value++) choices.push({ value, label: label(value) });
  }
  return choices;
}

/**
 * One of a few values. Without `choices` the list is the schema's; with them, only those the
 * schema allows are offered. `onPick` may take over (return true) instead of saving at once.
 */
export const SettingChoice: FC<
  SettingProps & { choices?: Choice[]; showAllHints?: boolean; onPick?: (value: Scalar) => boolean }
> = ({ state, path, label, description, disabled, choices, showAllHints, onPick }) => {
  const info = settingInfo(state.bundle, path);
  const value = settingValue(state.config, path) as Scalar | undefined;
  const base: Choice[] = choices ?? (info?.choices ?? []).map((name) => ({ value: name, label: name }));
  const offered = base.filter((choice) => allowed(info, choice.value));
  if (value !== undefined && !offered.some((choice) => choice.value === value)) {
    // Set from the command line to something this list does not have: keep it selectable.
    offered.unshift({ value, label: value === "" ? "Not set" : String(value) });
  }
  const selected = offered.find((choice) => choice.value === value);
  const help = helpOf(info, description);
  return (
    <DropdownItem
      label={label}
      description={
        <div>
          {help && <div>{help}</div>}
          {showAllHints
            ? offered
                .filter((choice) => choice.hint)
                .map((choice) => (
                  <div key={String(choice.value)} style={choice.value === value ? { color: colors.text } : undefined}>
                    {choice.label}: {choice.hint}
                  </div>
                ))
            : selected?.hint && <div style={{ color: colors.text }}>{selected.hint}</div>}
        </div>
      }
      rgOptions={offered.map((choice) => ({ data: choice.value, label: choice.label }))}
      selectedOption={value}
      // Steam's Dropdown keeps a selection of its own. Whether it lets go of a pick that was
      // not accepted (a cancelled confirmation, a setting the service rejected) could not be
      // checked without a Steam client, so the row's text is always the setting itself.
      renderButtonValue={() => selected?.label ?? ""}
      disabled={disabled || value === undefined}
      onChange={(option) => {
        const picked = option.data as Scalar;
        if (picked === value) return;
        if (onPick && onPick(picked)) return;
        void applyConfig(patchFor(path, picked));
      }}
    />
  );
};

// ---------------------------------------------------------------------- text

/**
 * A dialog with one text field. Steam's on-screen keyboard opens for its TextField; the
 * value is only sent when the user chooses Save.
 */
export const TextEditModal: FC<{
  closeModal?: () => void;
  title: string;
  label: string;
  description?: ReactNode;
  initial: string;
  saveLabel?: string;
  onSave: (value: string) => void;
}> = ({ closeModal, title, label, description, initial, saveLabel, onSave }) => {
  const [value, setValue] = useState(initial);
  return (
    <ConfirmModal
      strTitle={title}
      strOKButtonText={saveLabel ?? "Save"}
      strCancelButtonText="Cancel"
      onOK={() => onSave(value)}
      closeModal={closeModal}
    >
      <TextField
        label={label}
        description={description}
        value={value}
        focusOnMount={true}
        bShowClearAction={true}
        onChange={(event) => setValue(event.target.value)}
      />
    </ConfirmModal>
  );
};

/** A text setting: shows the value; "Edit" opens a dialog with a text field. */
export const SettingText: FC<SettingProps & { title: string; emptyLabel?: string; trim?: boolean }> = ({
  state,
  path,
  label,
  description,
  disabled,
  title,
  emptyLabel,
  trim = true,
}) => {
  const info = settingInfo(state.bundle, path);
  const value = settingValue(state.config, path);
  const text = typeof value === "string" ? value : "";
  const limit = info?.maxlen ? `At most ${info.maxlen} characters.` : "";
  return (
    <Field
      label={label}
      description={
        <div>
          <div style={{ color: text ? colors.text : colors.dim, overflowWrap: "anywhere" }}>
            {text || emptyLabel || "Not set"}
          </div>
          <div>{helpOf(info, description)}</div>
        </div>
      }
    >
      <DialogButton
        style={{ width: "auto", minWidth: "7em", padding: "10px 16px" }}
        disabled={disabled || typeof value !== "string"}
        onClick={() =>
          showModal(
            <TextEditModal
              title={title}
              label={typeof label === "string" ? label : title}
              description={[info?.help, limit].filter(Boolean).join(" ")}
              initial={text}
              onSave={(entered) => {
                const next = trim ? entered.trim() : entered;
                // Read the value again: it may have changed while the dialog was open.
                if (next !== settingValue(store.getState().config, path)) void applyConfig(patchFor(path, next));
              }}
            />,
          )
        }
      >
        Edit
      </DialogButton>
    </Field>
  );
};
