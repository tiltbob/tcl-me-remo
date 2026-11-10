// The Bluetooth link: its live state, the pairing, and the settings that shape it. The
// rarely needed ones sit under "Advanced".

import { DialogButton, Field, showModal } from "@decky/ui";
import type { FC, ReactNode } from "react";

import { KEYS, connectTv, disconnectTv } from "../actions";
import { ActionButton, ButtonRow, Note, OutcomeView, Page, Section, Value, rowButton } from "../components/common";
import { ForgetModal, PairModal } from "../components/modals";
import type { Choice } from "../components/settings";
import { SettingChoice, SettingSlider, SettingText, SettingToggle } from "../components/settings";
import { actionDisabled, useTv } from "../store";
import { colors } from "../text";

const Row: FC<{ label: string; children?: ReactNode; hint?: string }> = ({ label, children, hint }) => (
  <Field focusable={true} label={label} description={hint} padding="compact">
    {children}
  </Field>
);

const yes = (on: boolean, yesText = "Yes", noText = "No") => (
  <Value color={on ? colors.ok : colors.dim}>{on ? yesText : noText}</Value>
);

const IDLE_CHOICES: Choice[] = [
  { value: 0, label: "Never" },
  { value: 30, label: "After 30 seconds" },
  { value: 60, label: "After 1 minute" },
  { value: 300, label: "After 5 minutes" },
  { value: 900, label: "After 15 minutes" },
  { value: 3600, label: "After 1 hour" },
];

export const BluetoothPage: FC = () => {
  const state = useTv();
  const bluetooth = state.status?.bluetooth;
  const config = state.config;
  const paired = bluetooth?.paired === true;
  const remoteName = config?.bluetooth.device_name || "SteamOS Remote";
  const connectOutcome = state.outcomes[KEYS.connect];

  const adapters: Choice[] = [{ value: "auto", label: "Automatic" }];
  if (bluetooth?.adapter && bluetooth.adapter !== "auto") {
    adapters.push({ value: bluetooth.adapter, label: bluetooth.adapter });
  }

  return (
    <Page state={state}>
      {bluetooth && (
        <Section title="Right now">
          {bluetooth.error && (
            <Field
              focusable={true}
              label={<span style={{ color: colors.bad }}>Bluetooth problem</span>}
              description={<span style={{ color: colors.text, overflowWrap: "anywhere" }}>{bluetooth.error}</span>}
            />
          )}
          <Row label="Adapter">
            <Value color={bluetooth.available && bluetooth.powered ? colors.text : colors.bad}>
              {!bluetooth.available
                ? "Not available"
                : `${bluetooth.adapter || "?"}${bluetooth.adapter_address ? ` (${bluetooth.adapter_address})` : ""}${
                    bluetooth.powered ? "" : ", turned off"
                  }`}
            </Value>
          </Row>
          <Row label="TV">
            <Value color={paired ? colors.text : colors.warn}>
              {paired ? `${bluetooth.tv_name || "TV"} (${bluetooth.tv_address})` : "None paired"}
            </Value>
          </Row>
          <Row label="Calling the TV" hint="Advertising, so that the paired TV connects.">
            {bluetooth.pairing ? <Value color={colors.warn}>Pairing mode is open</Value> : yes(bluetooth.advertising)}
          </Row>
          <Row label="Connected">{yes(bluetooth.connected)}</Row>
          <Row label="Ready for keys" hint="Connected and settled.">
            {yes(bluetooth.ready)}
          </Row>
          <Row label="TV subscribed" hint="The TV has asked to be sent keys. Lost when SteamOS restarts.">
            {yes(bluetooth.subscribed)}
          </Row>
          <Row label="Delivery">
            <Value color={bluetooth.delivery ? colors.text : colors.dim}>
              {bluetooth.delivery === "bluez"
                ? "Normal"
                : bluetooth.delivery === "raw"
                  ? "Direct (see Advanced)"
                  : "Not connected"}
            </Value>
          </Row>
          <Field label="Link" description="Connect now calls the TV and waits for it. Disconnect drops the link.">
            <ButtonRow style={{ flexWrap: "nowrap" }}>
              <ActionButton
                state={state}
                actionKey={KEYS.connect}
                disabled={!paired || bluetooth.ready}
                onClick={() => void connectTv()}
              >
                Connect now
              </ActionButton>
              <ActionButton
                state={state}
                actionKey={KEYS.disconnect}
                disabled={!bluetooth.connected}
                onClick={() => void disconnectTv()}
              >
                Disconnect
              </ActionButton>
            </ButtonRow>
          </Field>
          <OutcomeView
            state={state}
            actionKey={KEYS.connect}
            contradicted={!!connectOutcome && (connectOutcome.kind === "failed") === bluetooth.ready}
          />
          <OutcomeView
            state={state}
            actionKey={KEYS.disconnect}
            contradicted={bluetooth.connected && state.outcomes[KEYS.disconnect]?.kind === "ok"}
          />
        </Section>
      )}

      <Section title="Pairing">
        <Field
          label={paired ? `Paired with ${bluetooth?.tv_name || "the TV"}` : "No TV is paired"}
          description={
            paired
              ? "To start over, forget the TV here and remove this remote on the TV as well."
              : "Pairing mode stays open for two minutes."
          }
        >
          {paired ? (
            <DialogButton
              style={rowButton}
              disabled={actionDisabled(state, KEYS.forget)}
              onClick={() => showModal(<ForgetModal tvName={bluetooth?.tv_name ?? ""} remoteName={remoteName} />)}
            >
              Forget TV
            </DialogButton>
          ) : (
            <DialogButton
              style={rowButton}
              disabled={!bluetooth || !bluetooth.available}
              onClick={() => showModal(<PairModal />)}
            >
              Pair TV
            </DialogButton>
          )}
        </Field>
        <OutcomeView state={state} actionKey={KEYS.forget} contradicted={paired} />
        <SettingText
          state={state}
          path="bluetooth.device_name"
          label="Name shown while pairing"
          title="Name shown while pairing"
          description="What the TV lists this machine as. A new name applies to the next pairing."
        />
      </Section>

      <Section title="Link">
        <SettingToggle state={state} path="bluetooth.reconnect_on_drop" label="Keep calling the TV" />
        <SettingChoice
          state={state}
          path="bluetooth.idle_disconnect_seconds"
          label="Disconnect when idle"
          description="Drop the link after this long without a key. Helps adapters that cannot reconnect Bluetooth LE controllers while the TV is connected."
          choices={IDLE_CHOICES}
        />
        <SettingSlider state={state} path="bluetooth.settle_ms" label="Settle delay" step={100} suffix=" ms" />
        <SettingSlider state={state} path="bluetooth.key_press_ms" label="Key press length" step={10} suffix=" ms" />
        <SettingSlider
          state={state}
          path="bluetooth.key_gap_ms"
          label="Pause between macro keys"
          description="Gives the TV's menus time to keep up."
          step={50}
          suffix=" ms"
        />
      </Section>

      <Section title="Advanced">
        <Note label="Rarely needed">Leave these alone unless something does not work.</Note>
        <SettingChoice
          state={state}
          path="bluetooth.raw_notify"
          label="Direct delivery"
          description="After SteamOS restarts, some TVs (Android 11 to 13) never ask for keys again although they are listening. Direct delivery sends keys to them anyway, past the Bluetooth service. On Linux kernels before 6.17 that upsets the kernel's count of Bluetooth packets; this service repairs the count afterwards, but if the repair fell short, controllers and headsets could stall. Designed from source code; not confirmed on a TV."
          showAllHints={true}
          choices={[
            {
              value: "auto",
              label: "Automatic",
              hint: "Used when the TV needs it, but only on Linux 6.17 or newer, where it has no side effects. On an older kernel it is not used, and a Bluetooth problem at the top of this page says so when a TV needs it.",
            },
            { value: "on", label: "On", hint: "Used when the TV needs it, on any kernel." },
            {
              value: "off",
              label: "Off",
              hint: "Never used. A TV that needs it has to be paired again after each restart of SteamOS.",
            },
            { value: "always", label: "Always", hint: "Used for every key, even when the TV has subscribed. For testing." },
          ]}
        />
        <SettingSlider
          state={state}
          path="bluetooth.resubscribe_wait"
          label="Wait for the TV to subscribe"
          step={0.5}
          suffix=" s"
        />
        <SettingChoice
          state={state}
          path="bluetooth.wake_beacon"
          label="Wake beacon (experimental)"
          description="For a TV that sleeps too deeply to connect: while turning it on, also broadcast the signal its own remote sends. TCL's pattern is not public; treat this as an experiment."
          choices={[
            { value: "off", label: "Off" },
            { value: "mediatek", label: "MediaTek default", hint: "MediaTek's default pattern, built from the TV's address." },
            { value: "custom", label: "Custom", hint: "The bytes below, captured from your own remote." },
          ]}
        />
        {config?.bluetooth.wake_beacon === "custom" && (
          <SettingText
            state={state}
            path="bluetooth.wake_beacon_data"
            label="Custom beacon data"
            title="Custom beacon data"
          />
        )}
        <SettingSlider
          state={state}
          path="bluetooth.connect_timeout"
          label="Wait for the TV when turning it on"
          step={1}
          suffix=" s"
        />
        <SettingSlider
          state={state}
          path="bluetooth.off_connect_timeout"
          label="Wait for the TV when turning it off"
          description="Keep this short: SteamOS only waits a few seconds before it suspends."
          step={0.5}
          suffix=" s"
        />
        <SettingToggle state={state} path="bluetooth.power_on_adapter" label="Turn Bluetooth on when needed" />
        <SettingChoice
          state={state}
          path="bluetooth.adapter"
          label="Bluetooth adapter"
          description="Leave on Automatic unless the machine has more than one adapter. A change takes effect when the service restarts (Diagnostics)."
          choices={adapters}
        />
        <SettingChoice
          state={state}
          path="bluetooth.gatt_security"
          label="Link security"
          description="A bring-up aid. A change takes effect when the service restarts (Diagnostics)."
          choices={[
            { value: "encrypt", label: "Encrypted", hint: "Normal." },
            { value: "plain", label: "Not encrypted", hint: "Drops the requirement that the link to the TV is encrypted. For testing only." },
          ]}
        />
      </Section>
    </Page>
  );
};
