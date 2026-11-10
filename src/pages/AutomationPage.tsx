// What happens by itself, and when it is allowed to happen.

import { Field } from "@decky/ui";
import type { FC } from "react";

import { CheckIcon, Page, Section } from "../components/common";
import type { Choice } from "../components/settings";
import { SettingChoice, SettingSlider, SettingToggle } from "../components/settings";
import { useTv } from "../store";

export const AutomationPage: FC = () => {
  const state = useTv();
  const status = state.status;
  const config = state.config;
  const off = config?.enabled === false;
  // With "Never" the service does not look at the displays at all, so the two settings
  // that refine the check do nothing.
  const noCheck = config?.display.require_external === "no";
  const unused = "Not used while Require an external display is set to Never.";
  const noInput = config?.input.method === "none";
  const inputNote = noInput ? "Input switching is turned off; see Input." : "Uses what is set on the Input page.";

  // Displays to choose from: what is connected right now, by the name the display reports.
  const displays: Choice[] = [{ value: "", label: "Any display" }];
  for (const connector of status?.display.connectors ?? []) {
    const name = `${connector.maker} ${connector.model}`.trim();
    if (connector.connected && !connector.internal && name && !displays.some((choice) => choice.value === name)) {
      displays.push({ value: name, label: connector.label });
    }
  }

  return (
    <Page state={state}>
      <Section title="Automatic actions">
        <SettingToggle
          state={state}
          path="enabled"
          label="Automatic actions"
          description="Master switch for everything below. The buttons in the panel keep working when this is off."
        />
      </Section>

      <Section title="When SteamOS starts or wakes">
        <SettingToggle state={state} path="events.boot_tv_on" label="Turn the TV on at start" description="" disabled={off} />
        <SettingToggle
          state={state}
          path="events.boot_switch_input"
          label="Switch the TV input after start"
          description={inputNote}
          disabled={off || noInput}
        />
        <SettingToggle state={state} path="events.resume_tv_on" label="Turn the TV on at wake" description="" disabled={off} />
        <SettingToggle
          state={state}
          path="events.resume_switch_input"
          label="Switch the TV input after wake"
          description={inputNote}
          disabled={off || noInput}
        />
      </Section>

      <Section title="When SteamOS sleeps or shuts down">
        <SettingToggle
          state={state}
          path="events.suspend_tv_off"
          label="Turn the TV off on suspend"
          description=""
          disabled={off}
        />
        <SettingToggle
          state={state}
          path="events.shutdown_tv_off"
          label="Turn the TV off on shutdown"
          description=""
          disabled={off}
        />
        <SettingToggle
          state={state}
          path="events.reboot_tv_off"
          label="Turn the TV off on restart"
          description="Usually off: the machine is back in a moment."
          disabled={off}
        />
      </Section>

      <Section title="Only while the TV is the display">
        <Field
          focusable={true}
          icon={<CheckIcon done={status ? status.display.gate_ok && !off : null} />}
          label={
            off
              ? "Automatic actions are turned off"
              : status?.display.gate_ok
                ? "Automatic actions may run right now"
                : "Automatic actions are skipped right now"
          }
          description={
            <div>
              {off && <div>Nothing happens by itself until Automatic actions, at the top of this page, is turned on.</div>}
              {status && <div>Display check: {status.display.gate_reason}.</div>}
            </div>
          }
        />
        <SettingChoice
          state={state}
          path="display.require_external"
          label="Require an external display"
          description="Stops a handheld that wakes on the sofa from turning the TV on."
          showAllHints={true}
          choices={[
            {
              value: "auto",
              label: "Automatic",
              hint: "a machine with a built-in screen acts only while an external display is connected; one without is assumed to be on the TV.",
            },
            { value: "yes", label: "Always", hint: "act only while an external display is connected." },
            { value: "no", label: "Never", hint: "do not check; always act." },
          ]}
        />
        <SettingChoice
          state={state}
          path="display.edid_match"
          label="Only for this display"
          description={
            noCheck
              ? unused
              : "Act only while this display is connected. The list shows what is connected now; connect the TV to see it here."
          }
          choices={displays}
          disabled={noCheck}
        />
        <SettingSlider
          state={state}
          path="display.wait_seconds"
          label="Wait for the display"
          description={noCheck ? unused : undefined}
          step={1}
          suffix=" s"
          disabled={noCheck}
        />
      </Section>
    </Page>
  );
};
