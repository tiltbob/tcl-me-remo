// What is installed, what happened, and what the log says. Works without the service: that
// is when it is needed most.

import { DialogButton, Field, Focusable, showModal } from "@decky/ui";
import type { FC } from "react";
import { useEffect, useRef, useState } from "react";

import { KEYS, installService, restartService } from "../actions";
import * as backend from "../backend";
import { rpc } from "../backend";
import { ButtonRow, Note, OutcomeView, Page, RecordView, Section, Value, rowButton } from "../components/common";
import { RemoveServiceModal } from "../components/modals";
import type { Choice } from "../components/settings";
import { SettingChoice, SettingSlider, rangeChoices, settingInfo } from "../components/settings";
import { store, useTv } from "../store";
import { UPDATE_ADDRESS, colors, duration } from "../text";
import type { DoctorCheck, DoctorReport } from "../types";

const LOG_LINES = 100;
const LOG_CHUNK = 20;

const pre = {
  margin: 0,
  fontFamily: "monospace",
  fontSize: "12px",
  lineHeight: "1.45",
  whiteSpace: "pre-wrap",
  overflowWrap: "anywhere",
  color: colors.text,
} as const;

/** Lines of plain text the D-pad can stop on, so that they can be scrolled into view. */
const TextBlock: FC<{ lines: string[] }> = ({ lines }) => (
  <Focusable onActivate={() => undefined} style={{ padding: "6px 10px", margin: "2px 0", background: "rgba(0, 0, 0, 0.25)" }}>
    <pre style={pre}>{lines.join("\n")}</pre>
  </Focusable>
);

/**
 * The log. While the service answers it is the service's own; when it does not, its lines
 * in the system journal, which the plugin backend can read: a service that fails at every
 * start says why there and nowhere else.
 */
const Logs: FC<{ up: boolean }> = ({ up }) => {
  const [lines, setLines] = useState<string[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const mounted = useRef(true);
  const busy = useRef(false);
  const wanted = useRef(up);
  wanted.current = up;

  const load = () => {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    const source = wanted.current;
    const request = source ? rpc<{ lines: string[] }>("logs", { lines: LOG_LINES }) : backend.serviceLog(LOG_LINES);
    void request.then((reply) => {
      busy.current = false;
      if (!mounted.current) return;
      setLoading(false);
      if (source !== wanted.current) {
        // The service came up or went away meanwhile: what arrived is the other log.
        load();
        return;
      }
      if (reply.ok) {
        setLines(Array.isArray(reply.result?.lines) ? reply.result.lines.map(String) : []);
        setError("");
      } else {
        setLines(null);
        setError(reply.error.message);
      }
    });
  };

  useEffect(() => {
    mounted.current = true;
    setLines(null);
    setError("");
    load();
    return () => {
      mounted.current = false;
    };
  }, [up]);

  // Newest first, in pieces the D-pad can step through.
  const newestFirst = (lines ?? []).slice().reverse();
  const chunks: string[][] = [];
  for (let index = 0; index < newestFirst.length; index += LOG_CHUNK) chunks.push(newestFirst.slice(index, index + LOG_CHUNK));

  return (
    <Section title="Log">
      <Field
        label={up ? "Recent log lines" : "The service's last lines in the system journal"}
        description={
          error ? (
            <span style={{ color: colors.bad }}>The log could not be read: {error}</span>
          ) : lines === null ? (
            loading ? "Reading." : "Not loaded."
          ) : up ? (
            `The last ${lines.length} lines, newest first. The service writes down every step of every action.`
          ) : (
            `The service does not answer, so this is what the system kept of it: the last ${lines.length} lines, newest first. If the service fails to start, the reason is here.`
          )
        }
      >
        <DialogButton style={rowButton} disabled={loading} onClick={() => load()}>
          {loading ? "Loading…" : "Refresh"}
        </DialogButton>
      </Field>
      {chunks.map((chunk, index) => (
        <TextBlock key={index} lines={chunk} />
      ))}
      {lines !== null && lines.length === 0 && !error && (
        <Note label={up ? "The log is empty" : "The system journal has no lines from the service"} />
      )}
    </Section>
  );
};

const CHECK_WORD: Record<DoctorCheck["status"], string> = {
  ok: "OK",
  warn: "Warning",
  fail: "Problem",
  unknown: "Not known",
};
const CHECK_COLOR: Record<DoctorCheck["status"], string> = {
  ok: colors.ok,
  warn: colors.warn,
  fail: colors.bad,
  unknown: colors.dim,
};

function checkSummary(report: DoctorReport): string {
  const count = (status: DoctorCheck["status"]) => report.checks.filter((check) => check.status === status).length;
  const parts = [
    count("fail") ? `${count("fail")} ${count("fail") === 1 ? "problem" : "problems"}` : "",
    count("warn") ? `${count("warn")} ${count("warn") === 1 ? "warning" : "warnings"}` : "",
    count("unknown") ? `${count("unknown")} not known` : "",
  ].filter(Boolean);
  return parts.length ? `${report.checks.length} checks: ${parts.join(", ")}.` : `All ${report.checks.length} checks passed.`;
}

/**
 * The doctor's report, which the command line prints for "tcl-me-remo doctor". Asked for
 * with a button and never by itself: it starts a dozen system tools.
 */
const SetupCheck: FC = () => {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = () => {
    if (running) return;
    setRunning(true);
    void backend.doctor().then((reply) => {
      if (!mounted.current) return;
      setRunning(false);
      if (reply.ok) {
        setReport(reply.result);
        setError("");
      } else {
        setReport(null);
        setError(reply.error.message);
      }
    });
  };

  return (
    <Section title="Check the set-up">
      <Field
        label="Check Bluetooth, the service, the pairing and the suspend hooks"
        description={
          error ? (
            <span style={{ color: colors.bad }}>The check could not be made: {error}</span>
          ) : report ? (
            <span style={{ color: CHECK_COLOR[report.worst] ?? colors.text }}>{checkSummary(report)}</span>
          ) : (
            "Says what is wrong in plain words. It only looks; nothing is changed. Works when the service does not. Include a screenshot of the result in a bug report."
          )
        }
      >
        <DialogButton style={rowButton} disabled={running} onClick={() => run()}>
          {running ? "Checking…" : report ? "Check again" : "Run check"}
        </DialogButton>
      </Field>
      {report && report.system.length > 0 && <TextBlock lines={report.system} />}
      {report?.checks.map((check, index) => (
        <Field
          key={index}
          focusable={true}
          padding="compact"
          label={
            <span>
              <span style={{ color: CHECK_COLOR[check.status] ?? colors.dim }}>{CHECK_WORD[check.status] ?? check.status}</span>
              {` · ${check.name}`}
            </span>
          }
          description={<span style={{ color: colors.text, overflowWrap: "anywhere" }}>{check.detail}</span>}
        />
      ))}
      {report && report.details.length > 0 && <TextBlock lines={report.details} />}
    </Section>
  );
};

export const DiagnosticsPage: FC = () => {
  const state = useTv();
  const { status, service, config } = state;

  // What is installed can change behind the panel's back; look again when the page opens.
  useEffect(() => {
    void store.refreshService();
  }, []);

  const serviceWorking = [KEYS.install, KEYS.restart, KEYS.remove].some((key) => key in state.pending);
  const history = (status?.history ?? []).slice().reverse();

  const times = (count: number) => (count === 1 ? "Once" : `${count} times`);
  const onKeys: Choice[] = [
    { value: "WAKE", label: "Wake", hint: "Does nothing if the TV is already on." },
    { value: "POWER", label: "Power toggle", hint: "Turns a TV that is already on off. Avoid." },
  ];
  const offKeys: Choice[] = [
    { value: "SLEEP", label: "Sleep", hint: "Does nothing if the TV is already off." },
    { value: "POWER", label: "Power toggle", hint: "Turns a TV that is already off on. Avoid." },
  ];

  return (
    <Page state={state} worksOffline={true}>
      <Section title="Background service">
        {service ? (
          <>
            <Field focusable={true} label="Installed" padding="compact">
              <Value color={service.installed ? colors.ok : colors.bad}>
                {service.installed
                  ? `Yes, version ${service.installed_version || "unknown"}${service.install_dir ? ` in ${service.install_dir}` : ""}`
                  : "No"}
              </Value>
            </Field>
            <Field focusable={true} label="Running" padding="compact">
              <Value color={service.active ? colors.ok : colors.bad}>
                {service.active
                  ? status
                    ? `Yes, version ${status.version}, for ${duration(status.uptime)}`
                    : "Yes, but it does not answer"
                  : "No"}
              </Value>
            </Field>
            <Field focusable={true} label="Starts with SteamOS" padding="compact">
              <Value color={service.enabled ? colors.ok : colors.bad}>{service.enabled ? "Yes" : "No"}</Value>
            </Field>
            <Field focusable={true} label="Version in this plugin" padding="compact">
              <Value color={service.installed && service.needs_update ? colors.warn : colors.text}>
                {service.bundled_version || "unknown"}
                {service.installed && service.needs_update ? " (differs from the installed one; Repair updates it)" : ""}
              </Value>
            </Field>
            {service.problems.map((problem, index) => (
              <Field
                key={index}
                focusable={true}
                padding="compact"
                label={<span style={{ color: colors.bad }}>Problem</span>}
                description={<span style={{ color: colors.text, overflowWrap: "anywhere" }}>{problem}</span>}
              />
            ))}
          </>
        ) : (
          <Note label={state.serviceError ? "The service's state could not be checked" : "Checking what is installed"}>
            {state.serviceError}
          </Note>
        )}
        <Field
          label="Repair, restart, remove"
          description="Repair installs, updates and starts the service; it changes nothing that is already right. Restart makes the TV connect and subscribe anew, so use it only when something is stuck."
          childrenLayout="below"
        >
          <ButtonRow>
            <DialogButton style={rowButton} disabled={serviceWorking} onClick={() => void installService()}>
              {KEYS.install in state.pending ? "Working…" : service && !service.installed ? "Install" : "Repair"}
            </DialogButton>
            <DialogButton
              style={rowButton}
              disabled={serviceWorking || !service?.installed || (status !== null && status.busy !== "")}
              onClick={() => void restartService()}
            >
              {KEYS.restart in state.pending ? "Working…" : "Restart"}
            </DialogButton>
            <DialogButton
              style={rowButton}
              disabled={serviceWorking || !service?.installed}
              onClick={() => showModal(<RemoveServiceModal />)}
            >
              {KEYS.remove in state.pending ? "Working…" : "Remove"}
            </DialogButton>
          </ButtonRow>
        </Field>
        <OutcomeView state={state} actionKey={KEYS.install} />
        <OutcomeView state={state} actionKey={KEYS.restart} />
        <OutcomeView state={state} actionKey={KEYS.remove} />
      </Section>

      <Section title="Updates">
        <Field
          focusable={true}
          label={`This plugin is version ${service?.bundled_version || "unknown"}`}
          description={
            <div>
              <div>
                It does not look for updates, and nothing is updated by itself. To update, install the plugin again
                from the address it came from: Decky settings &gt; Developer &gt; Install Plugin from URL. Type the
                address exactly; the same address always gives the newest version.
              </div>
              <div style={{ ...pre, fontSize: "13px", margin: "6px 0" }}>{UPDATE_ADDRESS}</div>
              <div>
                The address is not on screen while you type it: write it down or have it on a phone before you leave
                this page. Decky then asks "Install tcl-me-remo"; if it shows any other name, the end of the address
                is wrong, so cancel and correct it.
              </div>
              <div style={{ marginTop: "6px" }}>
                Settings and the pairing with the TV are kept. The background service is updated when the new plugin
                loads. An update that changes the service restarts it, which for the TV is like a restart of SteamOS:
                if keys do not arrive afterwards, the Bluetooth page says why.
              </div>
            </div>
          }
        />
      </Section>

      <SetupCheck />

      {status && status.config_problems.length > 0 && (
        <Section title="Settings problems">
          {status.config_problems.map((problem, index) => (
            <Field
              key={index}
              focusable={true}
              label={<span style={{ color: colors.warn }}>Problem in the settings file</span>}
              description={<span style={{ color: colors.text, overflowWrap: "anywhere" }}>{problem}</span>}
            />
          ))}
          {/* Two kinds of entry. Single values that could not be used are settled by the next
              save. A file of which nothing could be read was moved aside (the entry names
              it, "config.json.broken"): that entry stays until the service restarts, and
              saving a setting does not bring the old ones back. */}
          {status.config_problems.some((problem) => !problem.includes(".broken")) && (
            <Note>Changing any setting writes a clean settings file.</Note>
          )}
          {status.config_problems.some((problem) => problem.includes(".broken")) && (
            <Note label="The service is running on default settings">
              Changing a setting does not clear this, and does not bring the old settings back: they are in the
              file named above. The way on from here: set things up again on these pages, and pair the TV again if
              it shows as unpaired. This notice then ends when the service next restarts (Restart, at the top of
              this page). Getting the old settings back instead means mending that file by hand, which takes a
              terminal in Desktop Mode: see "A settings file that was moved aside" in the README.
            </Note>
          )}
        </Section>
      )}

      {status && (
        <Section title="What happened">
          {history.length === 0 && (
            <Note label="Nothing yet">Actions show up here with every step that was tried.</Note>
          )}
          {/* Keyed by the record, not its place in the list: an entry the user has opened
              stays open when a newer action is added above it. */}
          {history.map((record) => (
            <RecordView key={`${record.time}-${record.name}-${record.trigger}`} record={record} startOpen={false} />
          ))}
        </Section>
      )}

      <Logs up={!!status} />

      {status && config && (
        <Section title="Advanced">
          <SettingChoice
            state={state}
            path="power.on_key"
            label="Key that turns the TV on"
            description="Other keys can be set from the command line."
            choices={onKeys}
          />
          <SettingChoice
            state={state}
            path="power.on_repeat"
            label="Send the on key"
            description=""
            choices={rangeChoices(settingInfo(state.bundle, "power.on_repeat"), times)}
          />
          <SettingChoice
            state={state}
            path="power.off_key"
            label="Key that turns the TV off"
            description="Other keys can be set from the command line."
            choices={offKeys}
          />
          <SettingChoice
            state={state}
            path="power.off_repeat"
            label="Send the off key"
            description=""
            choices={rangeChoices(settingInfo(state.bundle, "power.off_repeat"), times)}
          />
          <SettingSlider
            state={state}
            path="power.repeat_gap"
            label="Pause between repeats"
            description="Only matters when a key is sent more than once."
            step={0.1}
            suffix=" s"
          />
          <SettingChoice
            state={state}
            path="log_level"
            label="Log detail"
            description=""
            choices={[
              { value: "DEBUG", label: "Everything", hint: "For bug reports." },
              { value: "INFO", label: "Normal" },
              { value: "WARNING", label: "Problems only" },
              { value: "ERROR", label: "Errors only" },
            ]}
          />
        </Section>
      )}
    </Page>
  );
};
