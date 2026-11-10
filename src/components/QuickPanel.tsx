// The Quick Access Menu panel: state at a glance, the three buttons, the few switches people
// change often, and the way into the rest. Kept short on purpose.

import { useQuickAccessVisible } from "@decky/api";
import { ButtonItem, Focusable, PanelSection, PanelSectionRow, Spinner, ToggleField, showModal } from "@decky/ui";
import type { FC, ReactNode } from "react";
import { FaExclamationTriangle } from "react-icons/fa";

import {
  KEYS,
  applyConfig,
  installService,
  switchInput,
  toastOutcome,
  tvOff,
  tvOn,
} from "../actions";
import { actionDisabled, busyReason, recordKind, useTv } from "../store";
import type { Outcome, TvState } from "../store";
import { actionName, colors, kindColor, kindWord, timeAgo, triggerName } from "../text";
import { PairModal, RecordModal } from "./modals";
import { serviceVerdict } from "./ServiceProblem";
import { openSettings } from "./TitleView";

const Dot: FC<{ color: string }> = ({ color }) => (
  <span
    style={{
      display: "inline-block",
      width: "8px",
      height: "8px",
      borderRadius: "50%",
      background: color,
      marginRight: "8px",
      flex: "0 0 auto",
    }}
  />
);

const Line: FC<{ color: string; children?: ReactNode }> = ({ color, children }) => (
  <div style={{ display: "flex", alignItems: "center", margin: "3px 0", color: colors.text }}>
    <Dot color={color} />
    <span style={{ overflowWrap: "anywhere" }}>{children}</span>
  </div>
);

/** A failure nobody can overlook: red, with the reason in the service's words. */
const Alarm: FC<{ title: string; children?: ReactNode }> = ({ title, children }) => (
  <div
    style={{
      background: colors.badBackground,
      borderLeft: `4px solid ${colors.bad}`,
      padding: "8px 10px",
      margin: "4px 0",
      color: colors.text,
    }}
  >
    <div style={{ color: colors.bad, fontWeight: "bold", display: "flex", alignItems: "center" }}>
      <FaExclamationTriangle style={{ marginRight: "8px", flex: "0 0 auto" }} />
      {title}
    </div>
    <div style={{ overflowWrap: "anywhere", marginTop: "2px" }}>{children}</div>
  </div>
);

function run(action: () => Promise<Outcome>): void {
  void action().then(toastOutcome, (reason) => console.warn("tcl-me-remo: action failed", reason));
}

function tvLine(state: TvState): { color: string; text: string } {
  const bluetooth = state.status?.bluetooth;
  if (!bluetooth) return { color: colors.dim, text: "TV state unknown" };
  if (!bluetooth.available) return { color: colors.bad, text: bluetooth.error || "Bluetooth is not available" };
  if (!bluetooth.powered) return { color: colors.bad, text: "Bluetooth is turned off in SteamOS" };
  if (bluetooth.pairing) return { color: colors.warn, text: "Pairing mode is open" };
  if (!bluetooth.paired) return { color: colors.warn, text: "No TV paired" };
  const name = bluetooth.tv_name || "TV";
  if (bluetooth.ready) return { color: colors.ok, text: `${name}: connected` };
  if (bluetooth.connected) return { color: colors.warn, text: `${name}: connecting` };
  return { color: colors.dim, text: `${name}: paired, not connected` };
}

// A refusal is news for this long; after that it is history (and it was announced when it happened).
const REFUSAL_SHOWN_MS = 60_000;

/**
 * A request that was refused outright (busy, not paired, ...) leaves no action record. It is
 * shown until something newer has happened: an action has finished since, the service is no
 * longer busy with what it was refused for, or a minute has passed.
 */
function refusedOutcome(state: TvState, now: number = Date.now()): Outcome | null {
  let latest: Outcome | null = null;
  for (const key of [KEYS.tvOn, KEYS.tvOff, KEYS.switchInput, KEYS.install]) {
    const outcome = state.outcomes[key];
    if (outcome && outcome.kind === "failed" && !outcome.record && (!latest || outcome.at > latest.at)) latest = outcome;
  }
  if (!latest || now - latest.at > REFUSAL_SHOWN_MS) return null;
  const status = state.status;
  const last = status?.last_action;
  if (last && (last.time + last.seconds) * 1000 > latest.at) return null;
  if (latest.code === "busy" && status && !status.busy && state.statusAsOf > latest.at) return null;
  return latest;
}

const StatusBlock: FC<{ state: TvState }> = ({ state }) => {
  const status = state.status;
  if (!status) return null;
  if (state.checking) {
    // The panel was reopened (or the service is slow to answer): what is known is from
    // before, so say nothing about the TV until the service has answered.
    return (
      <PanelSectionRow>
        <Focusable style={{ padding: "2px 0" }} onActivate={() => undefined}>
          <Line color={colors.dim}>Checking the background service</Line>
        </Focusable>
      </PanelSectionRow>
    );
  }
  const busy = busyReason(state);
  const tv = tvLine(state);
  const last = status.last_action;
  const lastKind = last ? recordKind(last) : null;
  const refused = refusedOutcome(state);
  return (
    <>
      <PanelSectionRow>
        {/* Focusable so the D-pad can come back up to it; pressing it shows the last action. */}
        <Focusable
          style={{ padding: "2px 0" }}
          onActivate={() => {
            if (last) showModal(<RecordModal record={last} />);
          }}
        >
          <div style={{ width: "100%" }}>
            <Line color={status.phase === "running" ? colors.ok : colors.warn}>
              {status.phase === "running" ? "Service running" : "SteamOS is going to sleep or shutting down"}
            </Line>
            <Line color={tv.color}>{tv.text}</Line>
            {busy ? (
              <div style={{ display: "flex", alignItems: "center", margin: "3px 0", color: colors.text }}>
                <Spinner style={{ width: "14px", height: "14px", marginRight: "6px", marginLeft: "-3px" }} />
                <span style={{ overflowWrap: "anywhere" }}>Working: {busy}</span>
              </div>
            ) : (
              last &&
              lastKind !== "failed" && (
                <Line color={kindColor[lastKind ?? "ok"]}>
                  {actionName(last)}: {kindWord[lastKind ?? "ok"]} ({triggerName(last)}, {timeAgo(last.time)})
                  {lastKind === "skipped" ? `. ${last.detail}` : ""}
                </Line>
              )
            )}
            {!status.enabled && <Line color={colors.warn}>Automatic actions are off</Line>}
            {status.config_problems.length > 0 && <Line color={colors.warn}>Settings problem: see Diagnostics</Line>}
          </div>
        </Focusable>
      </PanelSectionRow>
      {refused && refused.code === "busy" && (
        <PanelSectionRow>
          <Line color={colors.warn}>
            {refused.title} was not started. {refused.text}
          </Line>
        </PanelSectionRow>
      )}
      {refused && refused.code !== "busy" && (
        <PanelSectionRow>
          <Alarm title={`${refused.title} did not start`}>{refused.text}</Alarm>
        </PanelSectionRow>
      )}
      {!busy && last && lastKind === "failed" && (
        <>
          <PanelSectionRow>
            <Alarm title={`${actionName(last)} failed`}>
              {last.detail}
              <div style={{ color: colors.dim }}>
                {triggerName(last)}, {timeAgo(last.time)}
              </div>
            </Alarm>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => showModal(<RecordModal record={last} />)}>
              Show what was tried
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </>
  );
};

const ServiceDown: FC<{ state: TvState }> = ({ state }) => {
  const verdict = serviceVerdict(state);
  const working = KEYS.install in state.pending;
  return (
    <PanelSection>
      <PanelSectionRow>
        {verdict.calm ? (
          <div style={{ color: colors.text, margin: "4px 0" }}>
            <div style={{ display: "flex", alignItems: "center", fontWeight: "bold" }}>
              <Spinner style={{ width: "14px", height: "14px", marginRight: "8px", flex: "0 0 auto" }} />
              {verdict.title}
            </div>
            {verdict.lines.map((line, index) => (
              <div key={index} style={{ overflowWrap: "anywhere", marginTop: "2px" }}>
                {line}
              </div>
            ))}
          </div>
        ) : (
          <Alarm title={verdict.title}>
            {verdict.lines.map((line, index) => (
              <div key={index}>{line}</div>
            ))}
          </Alarm>
        )}
      </PanelSectionRow>
      {verdict.fix && (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={working} onClick={() => run(installService)}>
            {working ? "Working…" : verdict.fix}
          </ButtonItem>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => openSettings("diagnostics")}>
          Diagnostics
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

const PowerButtons: FC<{ state: TvState }> = ({ state }) => {
  const status = state.status;
  if (!status) return null;
  const bluetooth = status.bluetooth;
  if (!bluetooth.paired) {
    return (
      <PanelSection title="Get started">
        <PanelSectionRow>
          <div style={{ color: colors.text, margin: "2px 0 6px 0" }}>
            Pair this machine with the TV once, as a Bluetooth remote. Setup lists the TV settings to change first.
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!bluetooth.available || state.checking}
            onClick={() => showModal(<PairModal />)}
          >
            Pair TV
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => openSettings("setup")}>
            Setup
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }
  const label = (key: string, text: string) => (key in state.pending ? "Working…" : text);
  return (
    <PanelSection title="TV">
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={actionDisabled(state, KEYS.tvOn)} onClick={() => run(tvOn)}>
          {label(KEYS.tvOn, "TV on")}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={actionDisabled(state, KEYS.tvOff)} onClick={() => run(tvOff)}>
          {label(KEYS.tvOff, "TV off")}
        </ButtonItem>
      </PanelSectionRow>
      {status.input.method !== "none" && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={actionDisabled(state, KEYS.switchInput)}
            onClick={() => run(switchInput)}
          >
            {label(KEYS.switchInput, `Switch to HDMI ${status.input.hdmi_port}`)}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};

const Toggles: FC<{ state: TvState }> = ({ state }) => {
  const config = state.config;
  if (!state.status || !config) return null;
  const events = config.events;
  const onBoth = events.boot_tv_on && events.resume_tv_on;
  const mixed = events.boot_tv_on !== events.resume_tv_on;
  return (
    <PanelSection title="Automatic">
      <PanelSectionRow>
        <ToggleField
          label="Automatic actions"
          checked={config.enabled}
          onChange={(enabled) => void applyConfig({ enabled })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="TV on at start and wake"
          description={mixed ? "Start and wake are set differently; see Settings." : undefined}
          checked={onBoth}
          disabled={!config.enabled}
          onChange={(on) => void applyConfig({ events: { boot_tv_on: on, resume_tv_on: on } })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="TV off on suspend"
          checked={events.suspend_tv_off}
          disabled={!config.enabled}
          onChange={(on) => void applyConfig({ events: { suspend_tv_off: on } })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="TV off on shutdown"
          checked={events.shutdown_tv_off}
          disabled={!config.enabled}
          onChange={(on) => void applyConfig({ events: { shutdown_tv_off: on } })}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};

export const QuickPanel: FC = () => {
  const visible = useQuickAccessVisible();
  // The panel keeps its layout while a stale status is being replaced (the Quick Access Menu
  // was reopened): the buttons stay where they are, disabled, and the status lines give way
  // to "Checking". Swapping the whole panel for a moment would throw the gamepad focus.
  const state = useTv(visible, false);

  if (!state.started) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Line color={colors.dim}>Checking the background service</Line>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => openSettings("diagnostics")}>
            Diagnostics
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }
  if (!state.status) return <ServiceDown state={state} />;

  return (
    <>
      <PanelSection>
        <StatusBlock state={state} />
      </PanelSection>
      <PowerButtons state={state} />
      <Toggles state={state} />
      <PanelSection>
        {state.status.bluetooth.paired && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => openSettings("remote")}>
              Remote
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => openSettings("setup")}>
            Settings
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
