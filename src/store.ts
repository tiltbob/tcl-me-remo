// The shared data layer.
//
// One store serves the Quick Access panel, the settings page and every dialog:
//
//   * it polls the service's status about once a second while at least one of them is
//     mounted, and stops when none is (acquire() / the function it returns);
//   * a status that may be out of date is never passed off as current: when polling resumes
//     after a pause, or a request hangs, `checking` is set until the service has answered;
//   * it loads the settings once and after anything that can change them;
//   * setConfig() applies a change at once, sends it, adopts what the service answers and
//     takes the change back (with the service's message) when it is rejected;
//   * run() tracks an action from button press to outcome, so a button stays disabled while
//     its request is in flight and the result is still there when the user comes back.
//
// Nothing in here throws or rejects: a failed call is a normal state with a message.

import { useEffect, useState } from "react";

import * as backend from "./backend";
import { busyName, refusalText } from "./text";
import type {
  ActionRecord,
  Config,
  ConfigBundle,
  ConfigPatch,
  RpcError,
  RpcReply,
  ServiceInfo,
  Status,
} from "./types";

const POLL_MS = 1000;
// service_info() runs the installer's status check (a subprocess and a few systemctl calls),
// so while the service is unreachable it is refreshed at this slower pace.
const SERVICE_INFO_MS = 5000;
const CONFIG_RESYNC_MS = 3000;
// A status older than this is not shown as the current state.
const STALE_MS = 3000;
// What service_info() said is trusted for this long after the service stops answering.
const SERVICE_FRESH_MS = 2000;

export type OutcomeKind = "ok" | "skipped" | "failed";

/** Outcome code of a second press that arrived while the first was still in flight. */
export const ALREADY_RUNNING = "already_running";

/** How one action ended. */
export interface Outcome {
  key: string;
  title: string;
  kind: OutcomeKind;
  /** The service's own words: an action record's detail, or an error message. */
  text: string;
  /** Error code when the request itself was refused ("busy", "not_paired", ...). */
  code: string;
  /** The action record with its step trace, when the service returned one. */
  record: ActionRecord | null;
  at: number;
}

export interface TvState {
  /** False until the first status request has been answered, one way or the other. */
  started: boolean;
  /**
   * True while `status` is older than a few seconds and the request that replaces it has not
   * been answered: polling has just resumed (the panel was reopened), or a request hangs.
   * Nothing in `status` may be presented as current, and no action offered, while this is set.
   */
  checking: boolean;
  /** Live status; null whenever the service cannot be reached (never stale data). */
  status: Status | null;
  statusError: RpcError | null;
  /** When the request that produced `status` was sent (ms); 0 without a status. */
  statusAsOf: number;
  /**
   * True while a settings change is on its way, or `status` was asked for before the last
   * change took effect. `status.input` (what will be sent) is not to be trusted meanwhile.
   */
  settling: boolean;
  /** From service_info(): what is installed and whether it runs. */
  service: ServiceInfo | null;
  serviceError: string;
  bundle: ConfigBundle | null;
  /** Settings as shown: what the service confirmed plus changes still on their way. */
  config: Config | null;
  /** Why the last settings change was rejected (the service names the setting). */
  configError: string;
  configLoadError: string;
  /** Titles of the actions whose request is in flight, by key. */
  pending: Readonly<Record<string, string>>;
  outcomes: Readonly<Record<string, Outcome>>;
}

export type ConfigResult = { ok: true } | { ok: false; message: string };

type Listener = (state: TvState) => void;

const INITIAL: TvState = {
  started: false,
  checking: false,
  status: null,
  statusError: null,
  statusAsOf: 0,
  settling: false,
  service: null,
  serviceError: "",
  bundle: null,
  config: null,
  configError: "",
  configLoadError: "",
  pending: {},
  outcomes: {},
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function merge<T>(base: T, patch: unknown): T {
  if (!isPlainObject(base) || !isPlainObject(patch)) return patch as T;
  const out: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    out[key] = key in base ? merge(base[key], value) : value;
  }
  return out as T;
}

/**
 * Whether a status has every part the UI reads. An older service that is about to be updated
 * may answer with less; that must become a message, not a crash in the middle of rendering.
 */
function isStatus(value: unknown): value is Status {
  if (!isPlainObject(value)) return false;
  const sections = ["bluetooth", "wifi", "display", "input"].every((name) => isPlainObject(value[name]));
  return (
    sections &&
    typeof value.busy === "string" &&
    Array.isArray(value.history) &&
    Array.isArray(value.config_problems) &&
    Array.isArray((value.display as Record<string, unknown>).connectors)
  );
}

function isBundle(value: unknown): value is ConfigBundle {
  if (!isPlainObject(value) || !isPlainObject(value.config) || !isPlainObject(value.schema)) return false;
  const config = value.config;
  return ["events", "display", "power", "input", "bluetooth", "wifi"].every((name) => isPlainObject(config[name]));
}

const list = <T>(value: T[] | undefined): T[] => (Array.isArray(value) ? value : []);

export function isActionRecord(value: unknown): value is ActionRecord {
  return (
    isPlainObject(value) &&
    typeof value.ok === "boolean" &&
    typeof value.detail === "string" &&
    Array.isArray(value.steps)
  );
}

export function recordKind(record: ActionRecord): OutcomeKind {
  return !record.ok ? "failed" : record.skipped ? "skipped" : "ok";
}

/** Keys of actions that occupy the TV; the service runs one of them at a time. */
export function isTvAction(key: string): boolean {
  return key.startsWith("tv.");
}

/** What is keeping the TV busy right now ("" when nothing is). */
export function busyReason(state: TvState): string {
  if (state.status?.busy) return busyName(state.status.busy);
  for (const [key, title] of Object.entries(state.pending)) {
    if (isTvAction(key)) return title;
  }
  return "";
}

/** Whether the button for `key` must be disabled: its own request, or a busy service. */
export function actionDisabled(state: TvState, key: string): boolean {
  return state.checking || key in state.pending || busyReason(state) !== "";
}

export class TvStore {
  private state: TvState = INITIAL;
  private listeners = new Set<Listener>();
  private holders = 0;
  // Raised by shutdown(), so that holds handed out before it cannot be given back twice.
  private generation = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private polling = false;
  private pollAgain = false;
  private pollStartedAt = 0;
  // Counts changes to the settings the service has; a status knows which count it was asked at.
  private configSeq = 0;
  private statusSeq = 0;
  private serviceChecking = false;
  private serviceCheckedAt = 0;
  private configLoading = false;
  private configLoadedAt = 0;
  private resyncedFor = "";
  // Settings as last confirmed by the service, and the changes sent but not yet answered.
  private confirmed: Config | null = null;
  private patches: ConfigPatch[] = [];
  private configQueue: Promise<unknown> = Promise.resolve();

  getState(): TvState {
    return this.state;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private update(partial: Partial<TvState>): void {
    const settling = this.patches.length > 0 || this.statusSeq < this.configSeq;
    this.state = { ...this.state, ...partial, settling };
    for (const listener of Array.from(this.listeners)) {
      try {
        listener(this.state);
      } catch (reason) {
        console.warn("tcl-me-remo: a listener failed", reason);
      }
    }
  }

  // ------------------------------------------------------------------ polling

  /** Start (or keep) polling. Returns the function that gives the hold back. */
  acquire(): () => void {
    const generation = this.generation;
    this.holders += 1;
    if (this.holders === 1) {
      // What is still here from before the pause is history, not the current state.
      if (this.state.status && Date.now() - this.state.statusAsOf > STALE_MS) this.update({ checking: true });
      this.timer = setInterval(() => this.tick(), POLL_MS);
      this.poll();
      if (!this.state.bundle) void this.loadConfig();
      if (!this.state.service) void this.refreshService();
    }
    let released = false;
    return () => {
      // A hold from before shutdown() was already given back by it.
      if (released || generation !== this.generation) return;
      released = true;
      this.holders -= 1;
      if (this.holders === 0 && this.timer !== null) {
        clearInterval(this.timer);
        this.timer = null;
      }
    };
  }

  /** Number of mounted users; the preview's tests read it. */
  get activeHolders(): number {
    return this.holders;
  }

  /** The once-a-second tick: skipped while the previous request has not returned. */
  private tick(): void {
    if (!this.polling) {
      this.poll();
    } else if (this.state.status && !this.state.checking && Date.now() - this.pollStartedAt > STALE_MS) {
      // The service has stopped answering in time; what is shown may no longer be true.
      this.update({ checking: true });
    }
  }

  /**
   * Ask for the status now. Requests never overlap: while one is in flight, this one is
   * made as soon as it returns (something has changed that the one in flight may have missed).
   */
  poll(): void {
    if (this.polling) {
      this.pollAgain = true;
      return;
    }
    this.polling = true;
    this.pollAgain = false;
    this.pollStartedAt = Date.now();
    const askedAt = this.pollStartedAt;
    const seq = this.configSeq;
    const generation = this.generation;
    backend
      .rpc<Status>("status")
      .then((reply) => {
        if (generation === this.generation) this.adoptStatus(reply, askedAt, seq);
      })
      .catch((reason) => console.warn("tcl-me-remo: status handling failed", reason))
      .finally(() => {
        this.polling = false;
        if (this.pollAgain && generation === this.generation) this.poll();
      });
  }

  private adoptStatus(reply: RpcReply<Status>, askedAt: number, seq: number): void {
    if (reply.ok && !isStatus(reply.result)) {
      reply = backend.failure(
        "bad_reply",
        "the service answers, but not in a form this version of the panel understands; Repair updates the service",
      );
    }
    if (!reply.ok) {
      // Do not keep showing the last good status: it is no longer known to be true.
      const wentDown = this.state.status !== null || !this.state.started;
      const partial: Partial<TvState> = {
        started: true,
        checking: false,
        status: null,
        statusError: reply.error,
        statusAsOf: 0,
      };
      // Nor what was installed and running back when it still answered.
      const infoIsOld = !this.serviceChecking && Date.now() - this.serviceCheckedAt > SERVICE_FRESH_MS;
      if (wentDown && infoIsOld) partial.service = null;
      this.update(partial);
      if ((wentDown && infoIsOld) || Date.now() - this.serviceCheckedAt >= SERVICE_INFO_MS) void this.refreshService();
      return;
    }
    const wasDown = this.state.started && this.state.status === null;
    this.statusSeq = seq;
    this.update({ started: true, checking: false, status: reply.result, statusError: null, statusAsOf: askedAt });
    if (wasDown) {
      // The service is back: what we knew about it may be out of date.
      void this.loadConfig();
      void this.refreshService();
    } else if (!this.state.bundle) {
      void this.loadConfig();
    } else {
      this.resyncIfChangedElsewhere(reply.result);
    }
  }

  /** Reload the settings when the status shows they were changed behind our back (CLI, pairing). */
  private resyncIfChangedElsewhere(status: Status): void {
    const cfg = this.confirmed;
    if (!cfg || this.patches.length > 0 || this.configLoading) return;
    const seen = [
      status.enabled,
      status.input?.method,
      status.input?.hdmi_port,
      status.wifi?.enabled,
      status.wifi?.host,
      status.bluetooth?.paired ? status.bluetooth.tv_address : "",
    ].join("|");
    const known = [
      cfg.enabled,
      cfg.input.method,
      cfg.input.hdmi_port,
      cfg.wifi.enabled,
      cfg.wifi.host,
      cfg.bluetooth.tv_address,
    ].join("|");
    if (seen === known) {
      this.resyncedFor = "";
      return;
    }
    // Reload once per distinct difference, so two views that never agree cannot cause a loop.
    if (seen === this.resyncedFor || Date.now() - this.configLoadedAt < CONFIG_RESYNC_MS) return;
    this.resyncedFor = seen;
    void this.loadConfig();
  }

  // ------------------------------------------------------------------ service state

  refreshService(): Promise<void> {
    if (this.serviceChecking) return Promise.resolve();
    this.serviceChecking = true;
    this.serviceCheckedAt = Date.now();
    return backend
      .serviceInfo()
      .then((reply) => {
        if (reply.ok) this.update({ service: reply.result, serviceError: "" });
        else this.update({ serviceError: reply.error.message });
      })
      .catch((reason) => console.warn("tcl-me-remo: service_info handling failed", reason))
      .finally(() => {
        this.serviceChecking = false;
        this.serviceCheckedAt = Date.now();
      });
  }

  // ------------------------------------------------------------------ settings

  private shownConfig(): Config | null {
    if (!this.confirmed) return null;
    return this.patches.reduce<Config>((cfg, patch) => merge(cfg, patch), this.confirmed);
  }

  loadConfig(): Promise<void> {
    if (this.configLoading) return Promise.resolve();
    this.configLoading = true;
    return backend
      .rpc<ConfigBundle>("get_config")
      .then((reply) => {
        this.configLoadedAt = Date.now();
        if (reply.ok && isBundle(reply.result)) {
          const bundle: ConfigBundle = {
            ...reply.result,
            presets: list(reply.result.presets),
            keys: list(reply.result.keys),
            link_presets: list(reply.result.link_presets),
          };
          if (JSON.stringify(bundle.config) !== JSON.stringify(this.confirmed)) this.configSeq += 1;
          this.confirmed = bundle.config;
          this.update({ bundle, config: this.shownConfig(), configLoadError: "" });
          if (this.state.settling) this.poll();
        } else if (!reply.ok) {
          this.update({ configLoadError: reply.error.message });
        } else {
          this.update({ configLoadError: "the service sent settings this panel cannot read" });
        }
      })
      .catch((reason) => console.warn("tcl-me-remo: get_config handling failed", reason))
      .finally(() => {
        this.configLoading = false;
      });
  }

  /**
   * Change settings. The change shows at once; if the service rejects it, it is taken back
   * and `configError` carries the service's message, which names the setting.
   */
  setConfig(patch: ConfigPatch): Promise<ConfigResult> {
    if (!this.confirmed) {
      const message = "the settings have not been loaded yet";
      this.update({ configError: message });
      return Promise.resolve({ ok: false, message });
    }
    this.patches.push(patch);
    this.update({ config: this.shownConfig() });
    // One request at a time, in the order the user made the changes.
    const result = this.configQueue.then(() => this.sendPatch(patch));
    this.configQueue = result.catch(() => undefined);
    return result.catch((reason): ConfigResult => {
      console.warn("tcl-me-remo: set_config handling failed", reason);
      return { ok: false, message: "the change could not be sent" };
    });
  }

  private async sendPatch(patch: ConfigPatch): Promise<ConfigResult> {
    let reply: RpcReply<{ config: Config }>;
    try {
      reply = await backend.rpc<{ config: Config }>("set_config", { patch });
    } finally {
      this.patches = this.patches.filter((entry) => entry !== patch);
    }
    if (reply.ok && isBundle({ config: reply.result?.config, schema: {} })) {
      this.confirmed = reply.result.config;
      this.configLoadedAt = Date.now();
      this.configSeq += 1;
      this.update({ config: this.shownConfig(), configError: "" });
      this.poll();
      return { ok: true };
    }
    const message = reply.ok ? "the service sent an unreadable reply" : reply.error.message;
    const unchanged = !reply.ok && reply.error.code === "invalid_config";
    if (!unchanged) this.configSeq += 1;
    // Removing the patch above is the rollback: `config` falls back to what was confirmed.
    this.update({ config: this.shownConfig(), configError: message });
    if (!unchanged) {
      // "save_failed" means the change IS in effect; for anything else we cannot know.
      void this.loadConfig();
      this.poll();
    }
    return { ok: false, message };
  }

  clearConfigError(): void {
    if (this.state.configError) this.update({ configError: "" });
  }

  // ------------------------------------------------------------------ actions

  /**
   * Run one action and record how it went. A second press while the first is in flight is
   * ignored (the returned promise resolves with what is known so far).
   */
  async run<T>(
    key: string,
    title: string,
    call: () => Promise<RpcReply<T>>,
    read: (result: T) => { kind?: OutcomeKind; text: string } = () => ({ text: "Done" }),
  ): Promise<Outcome> {
    if (key in this.state.pending) {
      return this.outcome(key, title, "skipped", "this is already running", ALREADY_RUNNING, null);
    }
    const { [key]: _old, ...keptOutcomes } = this.state.outcomes;
    this.update({ pending: { ...this.state.pending, [key]: title }, outcomes: keptOutcomes });
    let outcome: Outcome;
    try {
      const reply = await call();
      if (!reply.ok) {
        const text = refusalText(reply.error.code, reply.error.message);
        outcome = this.outcome(key, title, "failed", text, reply.error.code, null);
      } else if (isActionRecord(reply.result)) {
        const record = reply.result;
        outcome = this.outcome(key, title, recordKind(record), record.detail, "", record);
      } else {
        const { kind = "ok", text } = read(reply.result);
        outcome = this.outcome(key, title, kind, text, "", null);
      }
    } catch (reason) {
      const text = reason instanceof Error ? reason.message : "unexpected error";
      outcome = this.outcome(key, title, "failed", text, "plugin_error", null);
    }
    const { [key]: _done, ...stillPending } = this.state.pending;
    this.update({ pending: stillPending, outcomes: { ...this.state.outcomes, [key]: outcome } });
    this.poll();
    return outcome;
  }

  private outcome(
    key: string,
    title: string,
    kind: OutcomeKind,
    text: string,
    code: string,
    record: ActionRecord | null,
  ): Outcome {
    return { key, title, kind, text, code, record, at: Date.now() };
  }

  clearOutcome(key: string): void {
    if (!(key in this.state.outcomes)) return;
    const { [key]: _gone, ...rest } = this.state.outcomes;
    this.update({ outcomes: rest });
  }

  /**
   * Stop everything (plugin unload). Components that are still mounted give their holds back
   * later, when they unmount; those are void from here on, so the count cannot go below zero
   * and a later acquire() starts polling as it should.
   */
  shutdown(): void {
    this.generation += 1;
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.holders = 0;
    this.pollAgain = false;
    this.listeners.clear();
  }
}

export const store = new TvStore();

/**
 * Subscribe a component to the store. While `active`, the component also keeps the status
 * polling alive; polling stops when the last such component unmounts.
 *
 * While the store is `checking`, the component is given no status at all (as before the
 * first answer), so nothing out of date can be shown or acted on. A component that wants to
 * keep its layout through that moment passes `maskStale: false` and handles `checking` itself.
 */
export function useTv(active: boolean = true, maskStale: boolean = true): TvState {
  const [state, setState] = useState<TvState>(() => store.getState());
  useEffect(() => {
    setState(store.getState());
    return store.subscribe(setState);
  }, []);
  useEffect(() => (active ? store.acquire() : undefined), [active]);
  if (maskStale && state.checking) return { ...state, started: false, status: null, statusAsOf: 0 };
  return state;
}

/** Keep the status polling alive without re-rendering on every answer. */
export function useHold(): void {
  useEffect(() => store.acquire(), []);
}
