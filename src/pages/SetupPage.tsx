// First-run checklist: docs/tv-setup.md in short, with live ticks where the state is knowable.

import { DialogButton, Field, showModal } from "@decky/ui";
import type { FC } from "react";

import { KEYS, connectTv, testEvent } from "../actions";
import { ActionButton, ButtonRow, CheckIcon, Note, OutcomeView, Page, Section, rowButton } from "../components/common";
import { ForgetModal, PairModal } from "../components/modals";
import { actionDisabled, useTv } from "../store";
import { colors } from "../text";

export const SetupPage: FC = () => {
  const state = useTv();
  const status = state.status;
  const bluetooth = status?.bluetooth;
  const paired = bluetooth?.paired === true;
  const ready = bluetooth?.ready === true;
  const remoteName = state.config?.bluetooth.device_name || "SteamOS Remote";
  // Without the service nothing is known about the TV; that is not the same as "not paired".
  const unknown = "Not known while the service is not answering";
  const connectOutcome = state.outcomes[KEYS.connect];

  return (
    <Page state={state} worksOffline={true}>
      <Section title="1. The background service">
        <Field
          focusable={true}
          icon={<CheckIcon done={!!status} />}
          label={status ? "The service is running" : "The service is not answering"}
          description={
            status
              ? `Version ${status.version}. It keeps the Bluetooth link to the TV and reacts when SteamOS sleeps and wakes.`
              : "See the message at the top of this page."
          }
        />
      </Section>

      <Section title="2. Prepare the TV">
        <Note icon={<CheckIcon done={null} />} label="Quick start and Screenless service: On">
          On the TV: Settings &gt; System &gt; Power &amp; Energy. On 2025 models, set Standby Mode to the screenless
          option instead. This keeps the TV reachable while its screen is off; it uses a little more standby power.
        </Note>
        <Note icon={<CheckIcon done={null} />} label="Power On Behavior: Last Input Used">
          Same menu. The TV then comes back on the input it showed last, which is the most reliable way to land on
          this machine.
        </Note>
        <Note icon={<CheckIcon done={null} />} label="Never confirm Shut down">
          A long press of the TV remote's power button offers Shut down. A TV that is shut down can only be turned
          on with its own remote or the button on the set.
        </Note>
        <Note label="Not yet confirmed on real hardware">
          Which standby modes a TCL TV can be woken from is put together from manuals and owners' reports. The
          first thing to test after pairing is whether your TV wakes.
        </Note>
      </Section>

      <Section title="3. Pair">
        <Field
          icon={<CheckIcon done={status ? paired : null} />}
          label={!bluetooth ? `Pairing: ${unknown.toLowerCase()}` : paired ? `Paired with ${bluetooth.tv_name || "the TV"}` : "No TV is paired"}
          description={
            !bluetooth ? (
              "See the message at the top of this page."
            ) : paired ? (
              "Pair once; the pairing survives restarts of both sides."
            ) : (
              <div>
                <div>
                  Choose Pair TV. Then on the TV open Settings &gt; Remotes &amp; Accessories &gt; Pair accessory
                  and pick {remoteName}. Pairing mode stays open for two minutes.
                </div>
                {bluetooth && !bluetooth.available && (
                  <div style={{ color: colors.bad }}>{bluetooth.error || "Bluetooth is not available."}</div>
                )}
              </div>
            )
          }
        >
          {paired ? (
            <DialogButton
              style={rowButton}
              disabled={actionDisabled(state, KEYS.forget)}
              onClick={() =>
                showModal(<ForgetModal tvName={bluetooth?.tv_name ?? ""} remoteName={remoteName} />)
              }
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
        <Field
          icon={<CheckIcon done={status && paired ? ready : null} />}
          label={
            !bluetooth
              ? `Connection: ${unknown.toLowerCase()}`
              : ready
                ? "The TV is connected"
                : "The TV is not connected right now"
          }
          description={
            !bluetooth
              ? ""
              : ready
                ? "Keys can be sent right away."
                : "A paired TV connects by itself while it is on or in screenless standby."
          }
        >
          <ActionButton
            state={state}
            actionKey={KEYS.connect}
            disabled={!paired || ready}
            onClick={() => void connectTv()}
          >
            Connect now
          </ActionButton>
        </Field>
        <OutcomeView
          state={state}
          actionKey={KEYS.connect}
          contradicted={!!bluetooth && !!connectOutcome && (connectOutcome.kind === "failed") === ready}
        />
      </Section>

      <Section title="4. Test">
        <Field
          label="Try what happens on wake and on sleep"
          description={
            <div>
              <div>
                Test wake runs exactly what runs when SteamOS starts or wakes; Test sleep what runs when it
                suspends. SteamOS itself stays awake.
              </div>
              {status && !status.display.gate_ok && (
                <div style={{ color: colors.warn }}>
                  Right now both would be skipped: {status.display.gate_reason}. See Automation.
                </div>
              )}
              {status && !status.enabled && (
                <div style={{ color: colors.warn }}>Automatic actions are off, so both would be skipped.</div>
              )}
              {!status && <div style={{ color: colors.warn }}>The tests need the background service.</div>}
              {status && !paired && <div style={{ color: colors.warn }}>Pair a TV first.</div>}
            </div>
          }
          childrenLayout="below"
        >
          <ButtonRow>
            <ActionButton state={state} actionKey={KEYS.testSleep} disabled={!paired} onClick={() => void testEvent("suspend")}>
              Test sleep
            </ActionButton>
            <ActionButton state={state} actionKey={KEYS.testWake} disabled={!paired} onClick={() => void testEvent("resume")}>
              Test wake
            </ActionButton>
          </ButtonRow>
        </Field>
        <OutcomeView state={state} actionKey={KEYS.testSleep} />
        <OutcomeView state={state} actionKey={KEYS.testWake} />
        <Note label="Then try again after an hour">
          Some sets change standby state a while after they are switched off. Let the TV sit in standby for an
          hour and run Test wake once more.
        </Note>
        <Note label="If the TV does not wake">
          Re-check Screenless service or Standby Mode; that is the cause in most cases. Models with a MediaTek
          chipset may need the experimental wake beacon (Bluetooth page). A TV whose network stays up in standby
          may also answer a wake request from the Wi-Fi assist.
        </Note>
        <Note label="If the TV wakes when it should not">
          Turn off Keep calling the TV on the Bluetooth page.
        </Note>
      </Section>
    </Page>
  );
};
