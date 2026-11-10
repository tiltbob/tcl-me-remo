// What to show when the service cannot be reached: its state from service_info(), in plain
// words, with the one button that fixes it. Never a blank or endlessly spinning panel.

import { DialogButton, Field, Spinner } from "@decky/ui";
import type { FC } from "react";
import { FaExclamationTriangle } from "react-icons/fa";

import { KEYS, installService } from "../actions";
import type { TvState } from "../store";
import { colors, kindColor, kindWord } from "../text";

export interface ServiceVerdict {
  title: string;
  lines: string[];
  /** Label of the button that calls install_service ("" while we do not know enough). */
  fix: string;
  /** True when this is progress rather than a problem (an install is running). */
  calm?: boolean;
}

export function serviceVerdict(state: TvState): ServiceVerdict {
  const info = state.service;
  const reason = state.statusError?.message ?? "";
  if (!info) {
    if (state.serviceError) {
      return {
        title: "The background service does not answer",
        lines: [reason, `Its state could not be checked: ${state.serviceError}`].filter(Boolean),
        fix: "Repair service",
      };
    }
    return {
      title: "The background service does not answer",
      lines: [reason, "Checking what is installed."].filter(Boolean),
      fix: "",
    };
  }
  if (info.installing) {
    return {
      title: "Installing the background service",
      lines: ["The plugin installs or updates it when it loads. This takes a moment.", ...info.problems],
      fix: "",
      calm: true,
    };
  }
  if (!info.installed) {
    return {
      title: "The background service is not installed",
      lines: [
        "The service does the work: it keeps the Bluetooth link to the TV and reacts when SteamOS sleeps and wakes.",
        // The plugin backend installs it in the background when the plugin loads and does not
        // say so while that runs; without a reported problem this may be that moment.
        ...(info.problems.length
          ? info.problems
          : ["The plugin installs it when it loads, which takes a moment. If this message stays, choose Install service."]),
      ],
      fix: "Install service",
    };
  }
  if (!info.active) {
    return {
      title: "The background service is not running",
      lines: info.problems.length ? info.problems : ["It is installed but stopped."],
      fix: "Repair service",
    };
  }
  return {
    title: "The background service is running but does not answer",
    lines: [reason, ...info.problems].filter(Boolean),
    fix: "Repair service",
  };
}

/** The settings-page version; the Quick Access panel renders the same verdict its own way. */
export const ServiceProblem: FC<{ state: TvState }> = ({ state }) => {
  const verdict = serviceVerdict(state);
  const working = KEYS.install in state.pending;
  const outcome = state.outcomes[KEYS.install];
  return (
    <div
      style={{
        background: verdict.calm ? "rgba(255, 255, 255, 0.06)" : colors.badBackground,
        borderLeft: `4px solid ${verdict.calm ? colors.dim : colors.bad}`,
        marginBottom: "12px",
      }}
    >
      <Field
        focusable={!verdict.fix}
        bottomSeparator="none"
        icon={verdict.calm ? <Spinner style={{ width: "18px", height: "18px" }} /> : <FaExclamationTriangle style={{ color: colors.bad }} />}
        label={<span style={{ color: verdict.calm ? colors.text : colors.bad }}>{verdict.title}</span>}
        description={
          <div style={{ color: colors.text }}>
            {verdict.lines.map((line, index) => (
              <div key={index} style={{ overflowWrap: "anywhere" }}>
                {line}
              </div>
            ))}
            {outcome && !working && (
              <div style={{ color: kindColor[outcome.kind], marginTop: "4px", overflowWrap: "anywhere" }}>
                {outcome.title}: {kindWord[outcome.kind]}. {outcome.text}
              </div>
            )}
          </div>
        }
      >
        {verdict.fix && (
          <DialogButton
            style={{ width: "auto", minWidth: "10em", padding: "10px 16px" }}
            disabled={working}
            onClick={() => void installService()}
          >
            {working ? "Working…" : verdict.fix}
          </DialogButton>
        )}
      </Field>
    </div>
  );
};
