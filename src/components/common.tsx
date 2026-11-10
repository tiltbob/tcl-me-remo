// Small building blocks shared by the settings pages.

import {
  DialogBody,
  DialogButton,
  DialogControlsSection,
  DialogControlsSectionHeader,
  Field,
  Focusable,
  Spinner,
} from "@decky/ui";
import type { CSSProperties, FC, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { FaCheckCircle, FaExclamationTriangle, FaRegCircle } from "react-icons/fa";

import { store } from "../store";
import type { Outcome, TvState } from "../store";
import { actionDisabled, recordKind } from "../store";
import { actionName, clockTime, colors, kindColor, kindWord, seconds, timeAgo, triggerName } from "../text";
import type { ActionRecord } from "../types";
import { ServiceProblem } from "./ServiceProblem";

const noop = () => undefined;

export const Section: FC<{ title: string; children?: ReactNode }> = ({ title, children }) => (
  <DialogControlsSection>
    <DialogControlsSectionHeader>{title}</DialogControlsSectionHeader>
    {children}
  </DialogControlsSection>
);

/**
 * A row of buttons. Steam's gamepad navigation only moves sideways between siblings of a
 * Focusable that says so, hence the wrapper.
 */
export const ButtonRow: FC<{ children?: ReactNode; style?: CSSProperties }> = ({ children, style }) => (
  <Focusable flow-children="right" style={{ display: "flex", flexWrap: "wrap", gap: "8px", ...style }}>
    {children}
  </Focusable>
);

export const rowButton: CSSProperties = { width: "auto", minWidth: "7em", flex: "0 0 auto", padding: "10px 16px" };

/** Read-only text the D-pad can stop on, so it can be scrolled into view. */
export const Note: FC<{ label?: ReactNode; children?: ReactNode; icon?: ReactNode }> = ({ label, children, icon }) => (
  <Field focusable={true} label={label} description={children} icon={icon} bottomSeparator="none" />
);

/** One line of a checklist: a tick when `done`, an empty circle when not, nothing when unknowable. */
export const CheckIcon: FC<{ done: boolean | null }> = ({ done }) =>
  done === null ? (
    <FaRegCircle style={{ color: colors.dim, opacity: 0.4 }} />
  ) : done ? (
    <FaCheckCircle style={{ color: colors.ok }} />
  ) : (
    <FaRegCircle style={{ color: colors.warn }} />
  );

export const Value: FC<{ children?: ReactNode; color?: string }> = ({ children, color }) => (
  <span style={{ color: color ?? colors.text, textAlign: "right", overflowWrap: "anywhere" }}>{children}</span>
);

export const Steps: FC<{ steps: string[] }> = ({ steps }) =>
  steps.length ? (
    <ol style={{ margin: "6px 0 0 0", paddingLeft: "1.4em", color: colors.text }}>
      {steps.map((step, index) => (
        <li key={index} style={{ margin: "2px 0", overflowWrap: "anywhere" }}>
          {step}
        </li>
      ))}
    </ol>
  ) : (
    <div style={{ marginTop: "6px", color: colors.dim }}>No steps were recorded.</div>
  );

/** An action record: what, why, how it ended; the step trace opens with one press. */
export const RecordView: FC<{ record: ActionRecord; title?: string; startOpen?: boolean }> = ({
  record,
  title,
  startOpen,
}) => {
  const kind = recordKind(record);
  const [open, setOpen] = useState(startOpen ?? kind === "failed");
  const heading = `${title ?? actionName(record)}: ${kindWord[kind]}`;
  return (
    <Field
      label={
        <span style={{ color: kindColor[kind] }}>
          {kind === "failed" && <FaExclamationTriangle style={{ marginRight: "8px", verticalAlign: "-2px" }} />}
          {heading}
        </span>
      }
      description={
        <div>
          <div style={{ color: colors.text, overflowWrap: "anywhere" }}>{record.detail || "No detail was given."}</div>
          <div style={{ color: colors.dim }}>
            {triggerName(record)} · {clockTime(record.time)} · took {seconds(record.seconds)}
          </div>
          {open && <Steps steps={record.steps} />}
        </div>
      }
      bottomSeparator="none"
    >
      <DialogButton style={rowButton} onClick={() => setOpen(!open)}>
        {open ? "Hide steps" : `Show steps (${record.steps.length})`}
      </DialogButton>
    </Field>
  );
};

/**
 * How the action behind `actionKey` went, if it has been run.
 *
 * A result describes one moment, so it does not stay for ever: it goes when the user leaves
 * the page after having seen it, and when the live state has since come to say otherwise
 * (`contradicted`: "Forget TV: done" next to a TV that has been paired again). What stays
 * says how old it is.
 */
export const OutcomeView: FC<{ state: TvState; actionKey: string; contradicted?: boolean }> = ({
  state,
  actionKey,
  contradicted,
}) => {
  const title = state.pending[actionKey];
  const outcome: Outcome | undefined = state.outcomes[actionKey];
  const seen = useRef(false);
  if (outcome) seen.current = true;
  // Only a status that was asked for after the action ended can contradict its result.
  const outdated = !!outcome && contradicted === true && state.statusAsOf > outcome.at;
  useEffect(() => {
    if (outdated) store.clearOutcome(actionKey);
  }, [outdated, actionKey]);
  useEffect(
    () => () => {
      if (seen.current) store.clearOutcome(actionKey);
    },
    [actionKey],
  );

  if (title !== undefined) {
    return (
      <Field
        focusable={true}
        bottomSeparator="none"
        label={
          <span>
            <Spinner style={{ width: "16px", height: "16px", marginRight: "8px", verticalAlign: "-3px" }} />
            {title}: working
          </span>
        }
        description="This can take a while when the TV has to be reached first."
      />
    );
  }
  if (!outcome || outdated) return null;
  if (outcome.record) return <RecordView record={outcome.record} title={outcome.title} />;
  return (
    <Field
      focusable={true}
      bottomSeparator="none"
      onActivate={outcome.kind === "failed" ? noop : undefined}
      label={
        <span style={{ color: kindColor[outcome.kind] }}>
          {outcome.kind === "failed" && <FaExclamationTriangle style={{ marginRight: "8px", verticalAlign: "-2px" }} />}
          {outcome.title}: {kindWord[outcome.kind]}
        </span>
      }
      description={
        <div>
          <div style={{ color: colors.text, overflowWrap: "anywhere" }}>{outcome.text}</div>
          <div style={{ color: colors.dim }}>{timeAgo(outcome.at / 1000)}</div>
        </div>
      }
    />
  );
};

/** A button that is disabled while its own request is in flight and while the service is busy. */
export const ActionButton: FC<{
  state: TvState;
  actionKey: string;
  onClick: () => void;
  disabled?: boolean;
  children?: ReactNode;
  style?: CSSProperties;
}> = ({ state, actionKey, onClick, disabled, children, style }) => (
  <DialogButton
    style={{ ...rowButton, ...style }}
    disabled={disabled || actionDisabled(state, actionKey)}
    onClick={() => onClick()}
  >
    {actionKey in state.pending ? "Working…" : children}
  </DialogButton>
);

const Banner: FC<{ title: string; text: string; onDismiss?: () => void }> = ({ title, text, onDismiss }) => (
  <div style={{ background: colors.badBackground, borderLeft: `4px solid ${colors.bad}`, marginBottom: "12px" }}>
    <Field
      focusable={!onDismiss}
      bottomSeparator="none"
      icon={<FaExclamationTriangle style={{ color: colors.bad }} />}
      label={<span style={{ color: colors.bad }}>{title}</span>}
      description={<span style={{ color: colors.text, overflowWrap: "anywhere" }}>{text}</span>}
    >
      {onDismiss && (
        <DialogButton style={rowButton} onClick={() => onDismiss()}>
          Dismiss
        </DialogButton>
      )}
    </Field>
  </div>
);

/**
 * The frame of every settings page: problems first (service unreachable, a rejected
 * setting, settings that did not load), then the page.
 */
export const Page: FC<{ state: TvState; children?: ReactNode; worksOffline?: boolean }> = ({
  state,
  children,
  worksOffline = false,
}) => {
  const offline = state.started && !state.status;
  // Without the service there is nothing true to show: no stale status, no settings that
  // cannot be changed. Pages that are about the service itself say so with worksOffline.
  const ready = worksOffline ? state.started : !!state.status && !!state.config;
  return (
    <DialogBody>
      {offline && <ServiceProblem state={state} />}
      {state.configError && (
        <Banner
          title="That change was not accepted"
          text={state.configError}
          onDismiss={() => store.clearConfigError()}
        />
      )}
      {state.status && !state.config && state.configLoadError && (
        <Banner title="The settings could not be loaded" text={state.configLoadError} />
      )}
      {!state.started && <Note label="Checking the background service">One moment.</Note>}
      {state.status && !state.config && !state.configLoadError && <Note label="Loading the settings">One moment.</Note>}
      {ready ? children : null}
    </DialogBody>
  );
};
