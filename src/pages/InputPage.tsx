// Getting the TV onto this machine's input: HDMI port, method, macro presets, the macro
// editor. Input switching cannot be turned on without a dialog that shows what will be sent.

import { DialogButton, Field, Focusable, TextField, showModal } from "@decky/ui";
import type { FC } from "react";
import { useEffect, useRef, useState } from "react";

import { KEYS, applyConfig, runMacro, switchInput } from "../actions";
import { rpc } from "../backend";
import { ActionButton, ButtonRow, Note, OutcomeView, Page, Section, rowButton } from "../components/common";
import { ConfirmSendModal } from "../components/modals";
import { SettingChoice, SettingSlider, SettingToggle, rangeChoices, settingInfo } from "../components/settings";
import { useTv } from "../store";
import type { TvState } from "../store";
import { colors, seconds } from "../text";
import type { Config, MacroCheck, Preset } from "../types";

const code = { fontFamily: "monospace", color: colors.text, overflowWrap: "anywhere" as const };
const paragraph = { margin: "0 0 10px 0" };

// Keys offered in the palette, in this order, when the service knows them. Power keys are
// left out on purpose: a macro is for menus.
const PALETTE_KEYS = ["F3", "TV", "HOME", "BACK", "MENU", "UP", "DOWN", "LEFT", "RIGHT", "OK"];
const PALETTE_WAITS = ["500ms", "1s", "2s"];
const WAIT_RE = /^\d+(\.\d+)?(ms|s)$/i;

function tokens(text: string): string[] {
  return text.trim().split(/[\s,;]+/).filter(Boolean);
}

/** Add a step; pressing the same key again turns LEFT into LEFT*2 instead of repeating it. */
function appendToken(text: string, token: string): string {
  const parts = tokens(text);
  const last = parts[parts.length - 1];
  if (last && !WAIT_RE.test(token)) {
    const match = /^(.+?)(?:\*(\d+))?$/.exec(last);
    if (match && match[1].toUpperCase() === token.toUpperCase()) {
      parts[parts.length - 1] = `${match[1]}*${Number(match[2] ?? 1) + 1}`;
      return parts.join(" ");
    }
  }
  parts.push(token);
  return parts.join(" ");
}

/** Make the last key repeat once per HDMI number: RIGHT becomes RIGHT*{port}. */
function repeatPerPort(text: string): string {
  const parts = tokens(text);
  const last = parts[parts.length - 1];
  if (!last || WAIT_RE.test(last)) return text;
  parts[parts.length - 1] = `${last.replace(/\*[^*]*$/, "")}*{port}`;
  return parts.join(" ");
}

function dropLast(text: string): string {
  return tokens(text).slice(0, -1).join(" ");
}

interface PresetChecks {
  /** The HDMI port the answers are for. */
  port: number;
  byName: Record<string, MacroCheck>;
}

/** What the service makes of each preset for the chosen HDMI port. */
function usePresetChecks(presets: Preset[], state: TvState): PresetChecks {
  const port = state.config?.input.hdmi_port ?? 1;
  const [checks, setChecks] = useState<PresetChecks>({ port, byName: {} });
  const asked = useRef("");
  const up = !!state.status;
  // The answers depend on the port (sent along) and on the key timing the service has, so
  // after a settings change they are asked for again once the change has arrived there.
  const settling = state.settling;
  const wanted = [
    presets.map((preset) => preset.name).join(","),
    port,
    state.config?.bluetooth.key_press_ms,
    state.config?.bluetooth.key_gap_ms,
  ].join("|");
  useEffect(() => {
    if (!up) {
      asked.current = "";
      return undefined;
    }
    if (settling || asked.current === wanted) return undefined;
    asked.current = wanted;
    let live = true;
    let open = presets.length;
    for (const preset of presets) {
      void rpc<MacroCheck>("check_macro", { macro: preset.template, port }).then((reply) => {
        open -= 1;
        if (!live || !reply.ok) return;
        setChecks((known) => ({
          port,
          byName: { ...(known.port === port ? known.byName : {}), [preset.name]: reply.result },
        }));
      });
    }
    return () => {
      live = false;
      // Interrupted before every answer was in: ask again next time.
      if (open > 0 && asked.current === wanted) asked.current = "";
    };
  }, [wanted, up, settling]);
  return checks.port === port ? checks : { port, byName: {} };
}

/** Validates macro text with the service a moment after the user stops changing it. */
function useMacroCheck(text: string, port: number): MacroCheck | null {
  const [result, setResult] = useState<{ text: string; port: number; check: MacroCheck } | null>(null);
  useEffect(() => {
    if (!text.trim()) return undefined;
    let live = true;
    const timer = setTimeout(() => {
      void rpc<MacroCheck>("check_macro", { macro: text, port }).then((reply) => {
        if (!live) return;
        setResult({ text, port, check: reply.ok ? reply.result : { ok: false, error: reply.error.message } });
      });
    }, 350);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [text, port]);
  return result && result.text === text && result.port === port ? result.check : null;
}

/**
 * When what is configured gets sent, in words. "TV on" switches the input as well, and the
 * automatic part follows the switches on the Automation page.
 */
function whenSent(config: Config): string {
  const byHand = "This happens when you press TV on or Switch input";
  if (!config.enabled) return `${byHand}. It does not happen by itself: automatic actions are turned off.`;
  const atStart = config.events.boot_switch_input;
  const atWake = config.events.resume_switch_input;
  if (atStart && atWake) return `${byHand}, and by itself after SteamOS starts or wakes.`;
  if (atStart) return `${byHand}, and by itself after SteamOS starts. After a wake it is turned off (see Automation).`;
  if (atWake) return `${byHand}, and by itself after SteamOS wakes. After a start it is turned off (see Automation).`;
  return `${byHand}. It does not happen by itself: switching the input after start and after wake is turned off (see Automation).`;
}

/**
 * Shown before input switching is turned on: what will be sent, and when. It follows the
 * live state, and it cannot be agreed to while a settings change (another HDMI port, another
 * preset) has not yet arrived in what the service reports it will send.
 */
const MethodConfirm: FC<{ closeModal?: () => void; method: "macro" | "wifi" }> = ({ closeModal, method }) => {
  const state = useTv();
  const { status, config } = state;
  const apply = () => void applyConfig({ input: { method } });
  const title = method === "macro" ? "Switch input with a Bluetooth macro?" : "Switch input over Wi-Fi?";

  if (!status || !config) {
    return (
      <ConfirmSendModal
        closeModal={closeModal}
        title={title}
        okLabel="Turn on"
        blocked="The background service does not answer, so what would be sent is not known."
        onOK={apply}
      />
    );
  }
  const input = status.input;
  const port = config.input.hdmi_port;
  const waiting = state.settling || input.hdmi_port !== port;
  const checking = <p style={{ ...paragraph, color: colors.dim }}>Checking what will be sent.</p>;
  const macroProblem = input.macro_error
    ? `The macro is not valid: ${input.macro_error}`
    : input.macro.trim()
      ? ""
      : "No macro is set. Choose a preset or save your own macro first.";
  const macroLines = waiting ? (
    checking
  ) : (
    <>
      <p style={{ ...paragraph, ...code }}>{input.macro || "(nothing)"}</p>
      <p style={paragraph}>
        That takes about {seconds(input.macro_seconds)}. After turning the TV on, the machine first waits{" "}
        {seconds(config.input.delay_after_wake)} for it to start.
      </p>
    </>
  );

  if (method === "macro") {
    return (
      <ConfirmSendModal
        closeModal={closeModal}
        title={title}
        okLabel="Turn on"
        blocked={macroProblem}
        waiting={waiting}
        onOK={apply}
      >
        <p style={paragraph}>These keys are sent to the TV for HDMI {port}:</p>
        {macroLines}
        <p style={paragraph}>{whenSent(config)}</p>
        <p style={{ ...paragraph, color: colors.warn }}>
          The presets come from owners' reports and are not confirmed. Use Try on this page and watch the TV before
          turning this on.
        </p>
      </ConfirmSendModal>
    );
  }

  const wifi = status.wifi;
  const byLink = config.wifi.input_mode === "app_link";
  const fallback = config.input.wifi_fallback_to_macro && input.macro.trim() !== "" && !input.macro_error;
  const blocked = !wifi.available
    ? "The Wi-Fi assist is not available in this build."
    : !wifi.paired || !wifi.enabled || !wifi.host
      ? "The Wi-Fi assist is not set up yet. Pair it on the Wi-Fi assist page first."
      : byLink && !input.wifi_link
        ? `No link is set for HDMI ${port}. Choose a link set on the Wi-Fi assist page.`
        : "";
  return (
    <ConfirmSendModal closeModal={closeModal} title={title} okLabel="Turn on" blocked={blocked} waiting={waiting} onOK={apply}>
      <p style={paragraph}>
        {wifi.name || wifi.host || "The TV"} is asked over Wi-Fi to{" "}
        {byLink ? `open this link for HDMI ${port}:` : `press its HDMI ${port} key.`}
      </p>
      {byLink ? (
        waiting ? (
          checking
        ) : (
          <p style={{ ...paragraph, ...code }}>{input.wifi_link || "(no link set)"}</p>
        )
      ) : (
        <p style={{ ...paragraph, color: colors.warn }}>
          TCL TVs are reported to ignore that key; opening a link is reported to work.
        </p>
      )}
      <p style={paragraph}>{whenSent(config)}</p>
      {fallback ? (
        <>
          <p style={paragraph}>If the TV does not answer over Wi-Fi, these keys are sent over Bluetooth instead:</p>
          {macroLines}
        </>
      ) : (
        <p style={paragraph}>If the TV does not answer over Wi-Fi, nothing else is tried.</p>
      )}
      <p style={{ ...paragraph, color: colors.warn }}>The link sets come from owners' reports and are not confirmed.</p>
    </ConfirmSendModal>
  );
};

/** What the chosen preset or macro is used for right now: a selection is not yet a use. */
function useOf(config: Config | null): "active" | "fallback" | "idle" {
  if (config?.input.method === "macro") return "active";
  if (config?.input.method === "wifi" && config.input.wifi_fallback_to_macro) return "fallback";
  return "idle";
}

const SelectedBadge: FC<{ use: "active" | "fallback" | "idle" }> = ({ use }) => (
  <span style={{ color: use === "idle" ? colors.dim : colors.ok, marginLeft: "10px" }}>
    {use === "active" ? "In use" : use === "fallback" ? "In use as the fallback" : "Selected"}
  </span>
);

const PresetRow: FC<{ state: TvState; preset: Preset; port: number; check: MacroCheck | undefined }> = ({
  state,
  preset,
  port,
  check,
}) => {
  const inUse = state.config?.input.preset === preset.name;
  const key = `tv.preset.${preset.name}`;
  return (
    <>
      <Field
        label={
          <span>
            {preset.title}
            {inUse && <SelectedBadge use={useOf(state.config)} />}
          </span>
        }
        description={
          <div>
            <div>{preset.description}</div>
            {check?.ok ? (
              <div>
                On HDMI {port} it sends <span style={code}>{check.rendered}</span> (about {seconds(check.seconds ?? 0)}).
              </div>
            ) : (
              <div>
                Pattern: <span style={code}>{preset.template}</span>
              </div>
            )}
          </div>
        }
      >
        <ButtonRow style={{ flexWrap: "nowrap" }}>
          <ActionButton state={state} actionKey={key} onClick={() => void runMacro(key, preset.template)}>
            Try
          </ActionButton>
          <DialogButton
            style={rowButton}
            disabled={inUse}
            onClick={() => void applyConfig({ input: { preset: preset.name } })}
          >
            Use
          </DialogButton>
        </ButtonRow>
      </Field>
      <OutcomeView state={state} actionKey={key} />
    </>
  );
};

const MacroEditor: FC<{ state: TvState }> = ({ state }) => {
  const saved = state.config?.input.macro ?? "";
  const custom = state.config?.input.preset === "custom";
  const [draft, setDraft] = useState<string | null>(null);
  const text = draft ?? saved;
  const port = state.config?.input.hdmi_port ?? 1;
  const check = useMacroCheck(text, port);
  const empty = text.trim() === "";
  const valid = !empty && check?.ok === true;
  const known = new Set((state.bundle?.keys ?? []).map((key) => key.name));
  const keys = PALETTE_KEYS.filter((name) => known.has(name));
  const limit = settingInfo(state.bundle, "input.macro")?.maxlen;
  const add = (token: string) => setDraft(appendToken(text, token));

  return (
    <>
      <TextField
        label="Macro"
        description="Steps separated by spaces. HOME presses a key, DOWN*3 presses it three times, HOME~1.5 holds it, 2s waits, {port} is the HDMI number."
        value={text}
        onChange={(event) => setDraft(event.target.value)}
      />
      <Field
        focusable={true}
        bottomSeparator="none"
        label={
          empty ? (
            "Nothing entered yet"
          ) : !check ? (
            "Checking"
          ) : check.ok ? (
            <span style={{ color: colors.ok }}>This macro is valid</span>
          ) : (
            <span style={{ color: colors.bad }}>This macro is not valid</span>
          )
        }
        description={
          empty ? (
            "Type a macro, or build one with the buttons below."
          ) : !check ? (
            ""
          ) : check.ok ? (
            <span>
              On HDMI {port} it sends <span style={code}>{check.rendered}</span> and takes about{" "}
              {seconds(check.seconds ?? 0)}.
            </span>
          ) : (
            <span style={{ color: colors.text, overflowWrap: "anywhere" }}>{check.error}</span>
          )
        }
      />
      <Field
        label="Add a step"
        description={`Each button adds to the end of the macro.${limit ? ` At most ${limit} characters.` : ""}`}
        childrenLayout="below"
      >
        <Focusable style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <ButtonRow>
            {keys.map((name) => (
              <DialogButton key={name} style={{ ...rowButton, minWidth: "4.5em" }} onClick={() => add(name)}>
                {name}
              </DialogButton>
            ))}
          </ButtonRow>
          <ButtonRow>
            {PALETTE_WAITS.map((wait) => (
              <DialogButton key={wait} style={{ ...rowButton, minWidth: "4.5em" }} onClick={() => add(wait)}>
                Wait {wait}
              </DialogButton>
            ))}
            <DialogButton style={rowButton} disabled={empty} onClick={() => setDraft(repeatPerPort(text))}>
              Repeat once per HDMI number
            </DialogButton>
            <DialogButton style={rowButton} disabled={empty} onClick={() => setDraft(dropLast(text))}>
              Remove last
            </DialogButton>
            <DialogButton style={rowButton} disabled={empty} onClick={() => setDraft("")}>
              Clear
            </DialogButton>
          </ButtonRow>
        </Focusable>
      </Field>
      <Field
        label={
          !custom
            ? "Use your own macro"
            : useOf(state.config) === "active"
              ? "Your macro is in use"
              : useOf(state.config) === "fallback"
                ? "Your macro is in use as the fallback"
                : "Your macro is selected"
        }
        description="Try it and watch the TV. Save stores it and makes it the macro that is used."
        childrenLayout="below"
      >
        <ButtonRow>
          <ActionButton state={state} actionKey={KEYS.macro} disabled={!valid} onClick={() => void runMacro(KEYS.macro, text)}>
            Try it now
          </ActionButton>
          <DialogButton
            style={rowButton}
            disabled={!valid || (custom && text.trim() === saved)}
            onClick={() =>
              void applyConfig({ input: { macro: check?.normalized ?? text.trim(), preset: "custom" } }).then(
                (result) => {
                  if (result.ok) setDraft(null);
                },
              )
            }
          >
            Save
          </DialogButton>
          <DialogButton style={rowButton} disabled={draft === null || draft === saved} onClick={() => setDraft(null)}>
            Discard changes
          </DialogButton>
        </ButtonRow>
      </Field>
      <OutcomeView state={state} actionKey={KEYS.macro} />
    </>
  );
};

export const InputPage: FC = () => {
  const state = useTv();
  const { status, config, bundle } = state;
  const presets = bundle?.presets ?? [];
  const checks = usePresetChecks(presets, state);

  const ports = rangeChoices(settingInfo(bundle, "input.hdmi_port"), (port) => `HDMI ${port}`);
  const method = config?.input.method;
  const input = status?.input;

  return (
    <Page state={state}>
      <Section title="Getting to the right input">
        <Note label="Recommended: let the TV do it">
          On the TV, set Settings &gt; System &gt; Power &amp; Energy &gt; Power On Behavior to Last Input Used. The TV
          then comes back on the input it showed last. This needs no key presses, and it makes everything below a
          fallback rather than a necessity.
        </Note>
        <SettingChoice
          state={state}
          path="input.hdmi_port"
          label="This machine is on"
          description="The HDMI port of the TV that this machine is plugged into."
          choices={ports}
        />
        <SettingChoice
          state={state}
          path="input.method"
          label="Switch the input"
          description=""
          showAllHints={true}
          choices={[
            { value: "none", label: "Off", hint: "nothing is sent; the TV decides." },
            {
              value: "macro",
              label: "Bluetooth macro",
              hint: "presses the keys you would: open the source list, move to the entry, OK.",
            },
            {
              value: "wifi",
              label: "Wi-Fi",
              hint: "asks the TV for the input directly; needs the Wi-Fi assist and the TV's network.",
            },
          ]}
          onPick={(value) => {
            if (value !== "macro" && value !== "wifi") return false;
            showModal(<MethodConfirm method={value} />);
            return true;
          }}
        />
        {input && (
          <Field
            focusable={true}
            label="What is sent"
            description={
              method === "none" ? (
                "Nothing. Input switching is off."
              ) : method === "wifi" ? (
                <div>
                  <div>
                    Over Wi-Fi: <span style={code}>{input.wifi_link || `the HDMI ${input.hdmi_port} key`}</span>
                  </div>
                  {config?.input.wifi_fallback_to_macro && input.macro && (
                    <div>
                      If that fails, over Bluetooth: <span style={code}>{input.macro}</span>
                    </div>
                  )}
                </div>
              ) : input.macro_error ? (
                <span style={{ color: colors.bad }}>The macro is not valid: {input.macro_error}</span>
              ) : (
                <span>
                  <span style={code}>{input.macro || "(no macro set)"}</span> (about {seconds(input.macro_seconds)})
                </span>
              )
            }
          />
        )}
        {method === "wifi" && (
          <SettingToggle
            state={state}
            path="input.wifi_fallback_to_macro"
            label="Fall back to the Bluetooth macro"
          />
        )}
        <SettingSlider
          state={state}
          path="input.delay_after_wake"
          label="Wait after turning the TV on"
          description="A TV that has just woken ignores keys for a few seconds."
          step={0.5}
          suffix=" s"
        />
        <Field
          label="Switch input now"
          description={method === "none" ? "Turn input switching on first." : "Sends what is shown above, right now."}
        >
          <ActionButton
            state={state}
            actionKey={KEYS.switchInput}
            disabled={method === "none"}
            onClick={() => void switchInput()}
          >
            Switch input now
          </ActionButton>
        </Field>
        <OutcomeView state={state} actionKey={KEYS.switchInput} />
      </Section>

      <Section title="Bluetooth macro presets">
        <Note label="Reported, not confirmed">
          Android TVs have no key for a given HDMI input, so a macro walks through the TV's source list. These
          presets are starting points from owners' reports. Switch the TV to another input, press Try and watch
          what happens. F3 is reported to open the source list on one TCL model; if nothing opens, try TV in your
          own macro.
        </Note>
        {presets.map((preset) => (
          <PresetRow
            key={preset.name}
            state={state}
            preset={preset}
            port={checks.port}
            check={checks.byName[preset.name]}
          />
        ))}
      </Section>

      <Section title="Your own macro">
        <MacroEditor state={state} />
        <Note label="Finding what your TV needs">
          Look at the source list: is it a row or a column, what comes before HDMI 1, are there entries you never
          use? The list does not wrap around, so go to one end with more presses than it has entries, then count
          back. Hide unused inputs in the TV's settings and set the others to On rather than Auto, so the list
          keeps its length.
        </Note>
      </Section>
    </Page>
  );
};
