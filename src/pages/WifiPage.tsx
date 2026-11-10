// The optional Wi-Fi assist: find or name the TV, pair with the code it shows, choose how
// the input is asked for, test.

import { DialogButton, Field, showModal } from "@decky/ui";
import type { FC } from "react";
import { useState } from "react";

import { KEYS, applyConfig, switchInputOverWifi, wifiDiscover, wifiPairCancel, wifiTest, wifiUnpair } from "../actions";
import { ActionButton, ButtonRow, CheckIcon, Note, OutcomeView, Page, Section, rowButton } from "../components/common";
import { ConfirmSendModal, WifiPairModal } from "../components/modals";
import { SettingChoice, SettingSlider, SettingText, SettingToggle } from "../components/settings";
import { actionDisabled, useTv } from "../store";
import type { TvState } from "../store";
import { colors } from "../text";
import type { LinkPreset, WifiDevice } from "../types";

const code = { fontFamily: "monospace", color: colors.text, overflowWrap: "anywhere" as const };

function pair(host: string, name?: string): void {
  showModal(<WifiPairModal host={host} name={name} />);
}

/** A chosen link set is only in use while the input is switched over Wi-Fi. */
const Selected: FC<{ active: boolean }> = ({ active }) => (
  <span style={{ color: active ? colors.ok : colors.dim, marginLeft: "10px" }}>{active ? "In use" : "Selected"}</span>
);

const LinkRow: FC<{ state: TvState; preset: LinkPreset }> = ({ state, preset }) => {
  const config = state.config;
  const port = config?.input.hdmi_port ?? 1;
  const inUse = config?.wifi.link_preset === preset.name;
  const active = config?.input.method === "wifi";
  const link = preset.links[String(port)];
  return (
    <Field
      label={
        <span>
          {preset.title}
          {inUse && <Selected active={active} />}
        </span>
      }
      description={
        <div>
          <div>{preset.description}</div>
          <div>
            {link ? (
              <span>
                For HDMI {port} it opens <span style={code}>{link}</span>
              </span>
            ) : (
              <span style={{ color: colors.warn }}>It has no link for HDMI {port}.</span>
            )}
          </div>
        </div>
      }
    >
      <DialogButton
        style={rowButton}
        disabled={inUse}
        onClick={() => void applyConfig({ wifi: { link_preset: preset.name } })}
      >
        Use
      </DialogButton>
    </Field>
  );
};

export const WifiPage: FC = () => {
  const state = useTv();
  const [found, setFound] = useState<WifiDevice[] | null>(null);
  const { status, config, bundle } = state;
  const wifi = status?.wifi;
  const port = config?.input.hdmi_port ?? 1;
  const method = config?.input.method;
  const customLink = config?.wifi.link_preset === "custom";
  const byLink = config?.wifi.input_mode === "app_link";
  // The link the service says it will open; it lags a settings change by a moment.
  const sentLink = state.settling ? "" : (status?.input.wifi_link ?? "");
  const testBlocked = !wifi?.paired
    ? "Pair the Wi-Fi assist first."
    : !wifi.enabled
      ? "Turn on Use the Wi-Fi assist first."
      : !wifi.host
        ? "Set the TV address first."
        : state.settling
          ? "Checking what will be sent."
          : byLink && !sentLink
            ? `No link is set for HDMI ${port}. Choose a link set above.`
            : "";

  if (wifi && !wifi.available) {
    return (
      <Page state={state}>
        <Section title="Wi-Fi assist">
          <Note label="Not available">The Wi-Fi assist is not available in this build of the service.</Note>
        </Section>
      </Page>
    );
  }

  return (
    <Page state={state}>
      <Section title="Wi-Fi assist">
        <Note label="Optional">
          The machine pairs with the TV over your network, the way the Google TV phone app does. It can then ask for
          an input directly, which is more reliable than a key macro, and turn the TV on or off when Bluetooth cannot
          reach it. It only works while the TV's network is up: when the TV is on, or in standby with Screenless
          service.
        </Note>
        <Field
          icon={<CheckIcon done={wifi ? wifi.paired : null} />}
          label={
            wifi?.paired
              ? `Paired with ${wifi.name || wifi.host || "the TV"}`
              : wifi?.pairing
                ? `Pairing with ${wifi.pairing_host}`
                : "Not paired"
          }
          description={
            wifi?.paired
              ? `Address: ${wifi.host || "not set"}. ${wifi.enabled ? "The assist is on." : "The assist is off."}${
                  wifi.pairing ? ` A new pairing with ${wifi.pairing_host} is open.` : ""
                }`
              : wifi?.pairing
                ? "The TV is showing a code. Choose Pair to enter it, or cancel."
                : "Search for the TV or enter its address below, then pair."
          }
        >
          {wifi?.pairing && !wifi.paired && (
            <DialogButton
              style={rowButton}
              disabled={KEYS.wifiPairCancel in state.pending}
              onClick={() => void wifiPairCancel()}
            >
              Cancel pairing
            </DialogButton>
          )}
          {wifi?.paired && (
            <ButtonRow style={{ flexWrap: "nowrap" }}>
              <ActionButton state={state} actionKey={KEYS.wifiTest} onClick={() => void wifiTest()}>
                Check connection
              </ActionButton>
              {wifi.pairing && (
                <DialogButton
                  style={rowButton}
                  disabled={KEYS.wifiPairCancel in state.pending}
                  onClick={() => void wifiPairCancel()}
                >
                  Cancel pairing
                </DialogButton>
              )}
              <DialogButton
                style={rowButton}
                disabled={actionDisabled(state, KEYS.wifiUnpair)}
                onClick={() =>
                  showModal(
                    <ConfirmSendModal
                      title="Remove the Wi-Fi pairing?"
                      okLabel="Unpair"
                      blocked=""
                      onOK={() => void wifiUnpair()}
                    >
                      <p style={{ margin: 0 }}>
                        The Wi-Fi assist is turned off and has to be paired again before it can be used. The
                        Bluetooth pairing is not touched.
                      </p>
                    </ConfirmSendModal>,
                  )
                }
              >
                Unpair
              </DialogButton>
            </ButtonRow>
          )}
        </Field>
        <OutcomeView state={state} actionKey={KEYS.wifiPairCancel} contradicted={wifi?.pairing === true} />
        <OutcomeView state={state} actionKey={KEYS.wifiTest} contradicted={wifi?.paired === false} />
        <OutcomeView state={state} actionKey={KEYS.wifiUnpair} contradicted={wifi?.paired === true} />
        <SettingToggle
          state={state}
          path="wifi.enabled"
          label="Use the Wi-Fi assist"
          description={wifi?.paired ? "Off keeps the pairing but does not use it." : "Pair first; pairing turns this on."}
          disabled={!wifi?.paired && config?.wifi.enabled !== true}
        />
      </Section>

      <Section title="Find the TV">
        <Field
          label="Search the network"
          description="Looks for Google TV devices for a few seconds. The TV must be on."
        >
          <ActionButton
            state={state}
            actionKey={KEYS.wifiDiscover}
            onClick={() => void wifiDiscover().then((result) => setFound(result.devices))}
          >
            Search
          </ActionButton>
        </Field>
        <OutcomeView state={state} actionKey={KEYS.wifiDiscover} />
        {(found ?? []).map((device) => (
          <Field
            key={`${device.host}:${device.port}`}
            label={device.name || device.host}
            description={`Address: ${device.host}`}
          >
            <DialogButton style={rowButton} onClick={() => pair(device.host, device.name)}>
              Pair
            </DialogButton>
          </Field>
        ))}
        {found !== null && found.length === 0 && !(KEYS.wifiDiscover in state.pending) && (
          <Note label="No TV answered">
            Check that the TV is on and on the same network, or enter its address below.
          </Note>
        )}
        <SettingText
          state={state}
          path="wifi.host"
          label="TV address"
          title="TV address"
          emptyLabel="Not set"
        />
        <Field label="Pair with this address" description="The TV shows a code; you enter it here.">
          <DialogButton
            style={rowButton}
            disabled={!config?.wifi.host}
            onClick={() => pair(config?.wifi.host ?? "", config?.wifi.name || undefined)}
          >
            {wifi?.paired ? "Pair again" : "Pair"}
          </DialogButton>
        </Field>
      </Section>

      <Section title="Switching the input">
        <SettingChoice
          state={state}
          path="wifi.input_mode"
          label="How the input is asked for"
          description=""
          choices={[
            {
              value: "app_link",
              label: "Open a link",
              hint: "Opens the TV's own link for the input. Reported to work on TCL TVs.",
            },
            { value: "keycode", label: "HDMI key", hint: "Sends the HDMI key code. TCL TVs are reported to ignore it." },
          ]}
        />
        {byLink && (
          <>
            <Note label="Link sets: reported, not confirmed">
              Each TV chipset names its inputs differently. These sets come from owners' reports. Pick the one for
              your model and test it.
            </Note>
            {(bundle?.link_presets ?? []).map((preset) => (
              <LinkRow key={preset.name} state={state} preset={preset} />
            ))}
            <Field
              label={
                <span>
                  Your own link
                  {customLink && <Selected active={method === "wifi"} />}
                </span>
              }
              description="Uses the link below; {port} stands for the HDMI number."
            >
              <DialogButton
                style={rowButton}
                disabled={customLink}
                onClick={() => void applyConfig({ wifi: { link_preset: "custom" } })}
              >
                Use
              </DialogButton>
            </Field>
            <SettingText
              state={state}
              path="wifi.app_link"
              label="Custom link"
              title="Custom link"
              description="Only used when Your own link is chosen."
            />
          </>
        )}
        <SettingSlider
          state={state}
          path="wifi.connect_timeout"
          label="Keep trying to switch for"
          description="A TV that has just woken can take a while to answer on the network. Only used when switching the input."
          step={1}
          suffix=" s"
        />
        <Field
          label={`Test: switch to HDMI ${port} now`}
          description={
            <div>
              {testBlocked ? (
                <div style={{ color: colors.warn }}>{testBlocked}</div>
              ) : (
                <div>
                  Sends: <span style={code}>{byLink ? sentLink : `the HDMI ${port} key`}</span>
                </div>
              )}
              <div>
                Asks over Wi-Fi only, whatever is chosen on the Input page, and tries nothing else if the TV does not
                answer. Switch the TV to another input first and watch what happens.
              </div>
            </div>
          }
        >
          <ActionButton
            state={state}
            actionKey={KEYS.wifiSwitch}
            disabled={testBlocked !== ""}
            onClick={() => void switchInputOverWifi()}
          >
            Switch input now
          </ActionButton>
        </Field>
        <OutcomeView state={state} actionKey={KEYS.wifiSwitch} />
      </Section>

      <Section title="Power">
        <SettingToggle
          state={state}
          path="wifi.power_fallback"
          label="Use Wi-Fi for power when Bluetooth fails"
          description="Turns the TV on or off over Wi-Fi when the Bluetooth link is not available."
        />
      </Section>
    </Page>
  );
};
