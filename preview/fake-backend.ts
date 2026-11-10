// An in-memory stand-in for main.py plus the service behind it, for the browser preview.
//
// It starts from what the real service answers (window.__FIXTURE__, produced by
// make_fixture.py from the Python code) and then behaves roughly like a living system: status
// changes over time, settings are validated with the service's own limits and wording,
// pairing completes after a few seconds, actions take a moment and return action records.
//
// Scenarios and knobs come from the page URL:
//   ?scenario=default | unpaired | no-service | stopped | failed | installing
//   &fail=tv_off,send_key   make these calls fail
//   &pair=3                 seconds until pairing completes ("never" to let it time out)
//   &classic=1              another Bluetooth pairing screen is open: pairing mode warns about it
//   &statusDelay=1500       milliseconds a status request takes

import type {
  ActionRecord,
  BluetoothState,
  Config,
  ConfigBundle,
  KeyInfo,
  RpcReply,
  ServiceInfo,
  SettingInfo,
  Status,
} from "../src/types";

declare global {
  interface Window {
    __FIXTURE__: { get_config: ConfigBundle; status: Status };
  }
}

type Json = Record<string, unknown>;

class RpcFailure extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

const TV_NAME = "Living Room TV";
const TV_ADDRESS = "AA:BB:CC:DD:EE:FF";
const TV_HOST = "192.168.1.50";
// What the service puts into bluetooth.error while pairing mode is open and the adapter itself
// is discoverable (bluez/remote.py, CLASSIC_VISIBLE).
const CLASSIC_VISIBLE =
  "this machine is also discoverable over classic Bluetooth (is another Bluetooth pairing screen open?). " +
  "A TV may then try to pair the wrong way and give up; close other pairing screens and try again";

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);

// ---------------------------------------------------------------------- macros (macro.py in small)

const ALIASES: Record<string, string> = { SELECT: "OK", WAKEUP: "WAKE", "VOL+": "VOL_UP", "VOL-": "VOL_DOWN" };

interface Step {
  key: string;
  repeat: number;
  hold: number;
  wait: number;
}

function renderMacro(template: string, port: number): string {
  return template.replace(/\{\s*port\s*(?:([+-])\s*(\d+))?\s*\}/gi, (_all, sign?: string, delta?: string) => {
    const value = sign ? (sign === "+" ? port + Number(delta) : port - Number(delta)) : port;
    return String(Math.max(0, value));
  });
}

function parseMacro(text: string, keys: KeyInfo[]): Step[] {
  const names = new Set(keys.map((key) => key.name));
  const steps: Step[] = [];
  for (const token of text.trim().split(/[\s,;]+/)) {
    if (!token) continue;
    const upper = token.toUpperCase();
    const wait = /^(\d+(?:\.\d+)?)(MS|S)$/.exec(upper);
    if (wait) {
      const amount = Number(wait[1]) / (wait[2] === "MS" ? 1000 : 1);
      if (amount > 30) throw new Error(`wait '${token}' is longer than 30s`);
      if (amount > 0) steps.push({ key: "", repeat: 1, hold: 0, wait: amount });
      continue;
    }
    let body = upper;
    let repeat = 1;
    let hold = 0;
    if (body.includes("*")) {
      const at = body.lastIndexOf("*");
      const count = body.slice(at + 1);
      body = body.slice(0, at);
      if (!/^\d+$/.test(count) || body === "") throw new Error(`bad repeat count in '${token}' (write e.g. DOWN*3)`);
      repeat = Number(count);
      if (repeat > 30) throw new Error(`repeat count in '${token}' is more than 30`);
    }
    if (body.includes("~")) {
      const at = body.indexOf("~");
      hold = Number(body.slice(at + 1));
      body = body.slice(0, at);
      if (!(hold > 0 && hold <= 10)) throw new Error(`hold time in '${token}' must be between 0 and 10 seconds`);
    }
    if (repeat === 0) continue;
    const name = ALIASES[body] ?? body;
    if (!names.has(name) && !/^[KCS]:(?:0X)?[0-9A-F]{1,4}$/.test(name)) {
      if (/^\d/.test(body)) throw new Error(`'${token}' is not a key or a wait (waits need a unit: 2s, 300ms)`);
      throw new Error(
        `unknown key '${body}' (use a name such as HOME, DOWN, OK, WAKE, SLEEP or a raw usage like C:0223, K:3C, S:83)`,
      );
    }
    steps.push({ key: name, repeat, hold, wait: 0 });
  }
  if (steps.length > 64) throw new Error(`macro has ${steps.length} steps; the limit is 64`);
  return steps;
}

function validateMacro(text: string, keys: KeyInfo[]): string {
  const cleaned = text.split(/\s+/).filter(Boolean).join(" ");
  for (const port of [1, 2, 3, 4]) parseMacro(renderMacro(cleaned, port), keys);
  return cleaned;
}

function formatSteps(steps: Step[]): string {
  return steps
    .map((step) => {
      if (!step.key) return step.wait < 1 ? `${Math.round(step.wait * 1000)}ms` : `${step.wait}s`;
      return step.key + (step.hold ? `~${step.hold}` : "") + (step.repeat !== 1 ? `*${step.repeat}` : "");
    })
    .join(" ");
}

function estimate(steps: Step[], press: number, gap: number): number {
  return steps.reduce((total, step) => total + (step.key ? step.repeat * ((step.hold || press) + gap) : step.wait), 0);
}

// ---------------------------------------------------------------------- settings (config.py in small)

function checkSpecial(path: string, value: unknown, keys: KeyInfo[]): unknown {
  const fail = (message: string): never => {
    throw new RpcFailure("invalid_config", `${path}: ${message}`);
  };
  if (path === "bluetooth.device_name" && !String(value).trim()) fail("must not be empty");
  if (path === "wifi.host") {
    const text = String(value).trim();
    if (text && !/^[A-Za-z0-9._:\-[\]]+$/.test(text)) fail("must be an IP address or hostname");
    return text;
  }
  if (path === "input.macro" && String(value).trim() !== "") {
    try {
      return validateMacro(String(value), keys);
    } catch (reason) {
      fail((reason as Error).message);
    }
  }
  if (path === "bluetooth.wake_beacon_data") {
    const text = String(value).replace(/[\s:,-]/g, "").toLowerCase();
    if (text === "") return "";
    if (text.length % 2 || !/^[0-9a-f]+$/.test(text)) fail("must be hexadecimal bytes, for example 4600 00 a1b2c3d4e5f6");
    if (text.length / 2 < 3 || text.length / 2 > 29) fail("must be between 3 and 29 bytes (2 bytes company id, then the data)");
    return text;
  }
  return value;
}

function applyPatch(base: Json, patch: unknown, schema: Json, path: string, keys: KeyInfo[]): Json {
  if (!isObject(patch)) throw new RpcFailure("invalid_config", path ? `${path}: must be an object` : "patch must be an object");
  const out: Json = { ...base };
  for (const [key, raw] of Object.entries(patch)) {
    const where = path ? `${path}.${key}` : key;
    if (!(key in base)) throw new RpcFailure("invalid_config", `${where}: unknown setting`);
    const fail = (message: string): never => {
      throw new RpcFailure("invalid_config", `${where}: ${message}`);
    };
    if (isObject(base[key])) {
      out[key] = applyPatch(base[key] as Json, raw, (schema[key] ?? {}) as Json, where, keys);
      continue;
    }
    const info = schema[key] as SettingInfo | undefined;
    let value = raw;
    const shown = JSON.stringify(raw);
    if (info?.type === "bool" && typeof value !== "boolean") fail(`must be true or false, not ${shown}`);
    if (info?.type === "int" && (typeof value !== "number" || !Number.isInteger(value))) fail(`must be a whole number, not ${shown}`);
    if (info?.type === "float" && typeof value !== "number") fail(`must be a number, not ${shown}`);
    if (info?.type === "str" && typeof value !== "string") fail(`must be text, not ${shown}`);
    if (info?.choices && !info.choices.includes(value as string)) fail(`must be one of ${info.choices.join(", ")}, not ${shown}`);
    if (typeof value === "number" && info?.min !== undefined && value < info.min) fail(`must be at least ${info.min}`);
    if (typeof value === "number" && info?.max !== undefined && value > info.max) fail(`must be at most ${info.max}`);
    if (typeof value === "string" && info?.maxlen !== undefined && value.length > info.maxlen) fail(`must be at most ${info.maxlen} characters`);
    value = checkSpecial(where, value, keys);
    out[key] = value;
  }
  return out;
}

// ---------------------------------------------------------------------- the fake

export interface Call {
  fn: string;
  method: string;
  params: Json;
  at: number;
}

type Mode = "running" | "not-installed" | "stopped";

export class FakeBackend {
  /** Every call the UI made, oldest first; the tests read this. */
  calls: Call[] = [];
  mode: Mode = "running";
  failing = new Set<string>();
  pairSeconds: number | null = 3;
  /** How long a status request takes; the tests raise it to prove that polls never overlap. */
  statusDelay = 25;
  statusInFlight = 0;
  maxStatusInFlight = 0;

  private bundle: ConfigBundle = clone(window.__FIXTURE__.get_config);
  private base: Status = clone(window.__FIXTURE__.status);
  private config: Config = clone(window.__FIXTURE__.get_config.config);
  private bluetooth: BluetoothState;
  private wifi = { paired: false, pairing: false, pairing_host: "" };
  private busy = "";
  private history: ActionRecord[] = [];
  private logs: string[] = [];
  private startedAt = Date.now() - 512_000;
  private pairingUntil = 0;
  /** The adapter itself is discoverable (knob "classic"): an open pairing mode says so. */
  private classicVisible = false;
  private pairTimer = 0;
  private configProblems: string[] = [];
  /** True while the pretend start-up install runs (scenario "installing"). */
  private installing = false;

  constructor() {
    this.bluetooth = {
      ...this.base.bluetooth,
      available: true,
      adapter: "hci0",
      adapter_address: "11:22:33:44:55:66",
      powered: true,
      registered: true,
    };
    const query = new URLSearchParams(window.location.search);
    for (const name of (query.get("fail") ?? "").split(",")) if (name) this.failing.add(name);
    const pair = query.get("pair");
    if (pair === "never") this.pairSeconds = null;
    else if (pair) this.pairSeconds = Number(pair);
    const slow = query.get("statusDelay");
    if (slow) this.statusDelay = Number(slow);
    this.classicVisible = query.get("classic") === "1";
    this.enter(query.get("scenario") ?? "default");
  }

  private enter(scenario: string): void {
    const now = Date.now() / 1000;
    this.log("INFO", "service started");
    if (scenario === "no-service") {
      this.mode = "not-installed";
      return;
    }
    if (scenario === "installing") {
      // main.py installs the service in the background when the plugin loads. It does not
      // report that yet; this scenario shows what the panel does once service_info() carries
      // `installing: true` for as long as that install runs.
      this.mode = "not-installed";
      this.installing = true;
      window.setTimeout(() => {
        this.installing = false;
        this.mode = "running";
        this.startedAt = Date.now();
      }, 2500);
    }
    if (scenario === "stopped") this.mode = "stopped";
    if (scenario === "unpaired") return;

    Object.assign(this.bluetooth, {
      paired: true,
      tv_address: TV_ADDRESS,
      tv_name: TV_NAME,
      connected: true,
      ready: true,
      subscribed: true,
      delivery: "bluez",
    });
    this.config.bluetooth.tv_address = TV_ADDRESS;
    this.config.bluetooth.tv_name = TV_NAME;
    this.config.input.method = "macro";
    this.config.input.hdmi_port = 2;
    this.history.push(
      {
        name: "tv_off",
        trigger: "suspend",
        ok: true,
        skipped: false,
        detail: "TV off (Bluetooth)",
        steps: ["display check: DP-1 (TCL 65Q750G) is connected", "Bluetooth: sent SLEEP"],
        time: now - 7400,
        seconds: 0.6,
      },
      {
        name: "tv_on",
        trigger: "resume",
        ok: true,
        skipped: false,
        detail: "TV on (Bluetooth), input switched (Bluetooth macro)",
        steps: [
          "display check: DP-1 (TCL 65Q750G) is connected",
          "Bluetooth: TV connected after 2.3s",
          "Bluetooth: sent WAKE",
          "waiting 8s for the TV to start up",
          "Bluetooth: playing macro: F3 1.5s LEFT*8 RIGHT*2 OK",
        ],
        time: now - 190,
        seconds: 16.9,
      },
    );
    this.log("INFO", "tv_on (resume): TV on (Bluetooth), input switched (Bluetooth macro)");
    if (scenario === "failed") {
      Object.assign(this.bluetooth, { connected: false, ready: false, delivery: "", advertising: true });
      this.history.push(this.failedOff("suspend", now - 45));
    }
  }

  private failedOff(trigger: ActionRecord["trigger"], time: number): ActionRecord {
    this.log("WARNING", `tv_off (${trigger}): FAILED: Bluetooth: the TV did not connect within 3s`);
    return {
      name: "tv_off",
      trigger,
      ok: false,
      skipped: false,
      detail: "Bluetooth: the TV did not connect within 3s",
      steps: ["display check: DP-1 (TCL 65Q750G) is connected", "Bluetooth: the TV did not connect within 3s"],
      time,
      seconds: 3.5,
    };
  }

  private log(level: string, message: string): void {
    const stamp = new Date().toISOString().replace("T", " ").slice(0, 19);
    this.logs.push(`${stamp} ${level} ${message}`);
    if (this.logs.length > 400) this.logs.shift();
  }

  // ------------------------------------------------------------------ status

  private connectors(): Status["display"]["connectors"] {
    return [
      { name: "eDP-1", card: "card0", status: "connected", internal: true, maker: "VLV", model: "ANX7530 U", connected: true, label: "eDP-1 (VLV ANX7530 U)" },
      { name: "DP-1", card: "card0", status: "connected", internal: false, maker: "TCL", model: "65Q750G", connected: true, label: "DP-1 (TCL 65Q750G)" },
      { name: "HDMI-A-1", card: "card0", status: "disconnected", internal: false, maker: "", model: "", connected: false, label: "HDMI-A-1" },
    ];
  }

  private gate(): { ok: boolean; reason: string } {
    const display = this.config.display;
    if (display.require_external === "no") return { ok: true, reason: "display check is off" };
    const needle = display.edid_match.trim().toLowerCase();
    const externals = this.connectors().filter((connector) => connector.connected && !connector.internal);
    if (needle) {
      const match = externals.find((connector) => `${connector.maker} ${connector.model}`.toLowerCase().includes(needle));
      if (match) return { ok: true, reason: `${match.label} is connected` };
      const seen = externals.map((connector) => connector.label).join(", ");
      return { ok: false, reason: `no connected display matches '${display.edid_match}' (connected: ${seen})` };
    }
    return { ok: true, reason: `${externals[0].label} is connected` };
  }

  private effectiveMacro(): string {
    const preset = this.bundle.presets.find((entry) => entry.name === this.config.input.preset);
    return renderMacro(preset ? preset.template : this.config.input.macro, this.config.input.hdmi_port);
  }

  private wifiLink(): string {
    const preset = this.bundle.link_presets.find((entry) => entry.name === this.config.wifi.link_preset);
    const port = String(this.config.input.hdmi_port);
    return preset ? (preset.links[port] ?? "") : this.config.wifi.app_link.replace("{port}", port).trim();
  }

  private timings(): { press: number; gap: number } {
    return { press: this.config.bluetooth.key_press_ms / 1000, gap: this.config.bluetooth.key_gap_ms / 1000 };
  }

  private status(): Status {
    if (this.bluetooth.pairing) {
      this.bluetooth.pairing_seconds_left = Math.max(0, Math.round((this.pairingUntil - Date.now()) / 1000));
      if (Date.now() >= this.pairingUntil) this.stopPairing();
    }
    const macro = this.effectiveMacro();
    let macroError = "";
    let macroSeconds = 0;
    try {
      const { press, gap } = this.timings();
      macroSeconds = Math.round(estimate(parseMacro(macro, this.bundle.keys), press, gap) * 10) / 10;
    } catch (reason) {
      macroError = (reason as Error).message;
    }
    const gate = this.gate();
    return {
      ...this.base,
      uptime: Math.round((Date.now() - this.startedAt) / 100) / 10,
      enabled: this.config.enabled,
      phase: "running",
      busy: this.busy,
      bluetooth: { ...this.bluetooth },
      wifi: {
        available: true,
        enabled: this.config.wifi.enabled,
        host: this.config.wifi.host,
        name: this.config.wifi.name,
        ...this.wifi,
      },
      display: { gate_ok: gate.ok, gate_reason: gate.reason, has_internal: true, connectors: this.connectors() },
      input: {
        method: this.config.input.method,
        hdmi_port: this.config.input.hdmi_port,
        wifi_link: this.config.wifi.input_mode === "app_link" ? this.wifiLink() : "",
        macro,
        macro_error: macroError,
        macro_seconds: macroSeconds,
      },
      last_action: this.history.length ? this.history[this.history.length - 1] : null,
      history: this.history.slice(-20),
      config_problems: this.configProblems,
    };
  }

  // ------------------------------------------------------------------ actions

  private async action(
    name: ActionRecord["name"],
    trigger: ActionRecord["trigger"],
    queue: boolean,
    body: (steps: string[]) => Promise<string>,
  ): Promise<ActionRecord> {
    if (this.busy && !queue) throw new RpcFailure("busy", `busy with ${this.busy.replace(/^(\S+) /, "'$1' ")}; try again shortly`);
    while (this.busy) await sleep(50);
    this.busy = `${name} (${trigger})`;
    const started = Date.now();
    const steps: string[] = [];
    let ok = true;
    let skipped = false;
    let detail = "";
    try {
      detail = await body(steps);
    } catch (reason) {
      if (reason instanceof Skip) {
        skipped = true;
        detail = reason.message;
      } else {
        ok = false;
        detail = (reason as Error).message;
      }
    } finally {
      this.busy = "";
    }
    const record: ActionRecord = {
      name,
      trigger,
      ok,
      skipped,
      detail,
      steps,
      time: started / 1000,
      seconds: Math.round((Date.now() - started) / 10) / 100,
    };
    this.history.push(record);
    this.log(ok ? "INFO" : "WARNING", `${name} (${trigger}): ${skipped ? "skipped: " : ok ? "" : "FAILED: "}${detail}`);
    return record;
  }

  private async reach(steps: string[], what: string, timeout: number): Promise<void> {
    if (!this.bluetooth.paired) throw new Error("Bluetooth: no TV is paired");
    if (this.failing.has(what)) {
      await sleep(700);
      steps.push(`Bluetooth: the TV did not connect within ${timeout}s`);
      throw new Error(`Bluetooth: the TV did not connect within ${timeout}s`);
    }
    if (!this.bluetooth.ready) {
      await sleep(500);
      Object.assign(this.bluetooth, { connected: true, ready: true, subscribed: true, delivery: "bluez", advertising: false });
      steps.push("Bluetooth: TV connected after 0.5s");
    }
  }

  private async playMacro(steps: string[], text: string): Promise<void> {
    let parsed: Step[];
    try {
      parsed = parseMacro(text, this.bundle.keys);
    } catch (reason) {
      throw new Error((reason as Error).message);
    }
    if (!parsed.length) throw new Error("the macro is empty");
    await this.reach(steps, "run_macro", this.config.bluetooth.connect_timeout);
    steps.push(`Bluetooth: playing macro: ${formatSteps(parsed)}`);
    this.log("INFO", steps[steps.length - 1]);
    await sleep(900);
  }

  private async wifiSwitch(steps: string[]): Promise<void> {
    const problem = !this.config.wifi.enabled
      ? "the Wi-Fi assist is turned off"
      : !this.config.wifi.host
        ? "no TV address is set for the Wi-Fi assist"
        : !this.wifi.paired
          ? "the Wi-Fi assist is not paired with the TV"
          : this.config.wifi.input_mode === "app_link" && !this.wifiLink()
            ? "no app link is set for Wi-Fi input switching"
            : "";
    if (problem) throw new Error(problem);
    await sleep(400);
    steps.push(
      this.config.wifi.input_mode === "app_link"
        ? `Wi-Fi: opened ${this.wifiLink()}`
        : `Wi-Fi: sent HDMI ${this.config.input.hdmi_port} key`,
    );
  }

  private async switchInput(steps: string[]): Promise<string> {
    const input = this.config.input;
    if (input.method === "none") throw new Error("input switching is turned off");
    if (input.method === "wifi") {
      try {
        await this.wifiSwitch(steps);
        return "Wi-Fi";
      } catch (reason) {
        if (!(input.wifi_fallback_to_macro && this.effectiveMacro().trim())) throw reason;
        steps.push(`Wi-Fi: ${(reason as Error).message}; falling back to the Bluetooth macro`);
      }
    }
    const text = this.effectiveMacro().trim();
    if (!text) throw new Error("no input macro is set: choose a preset or enter a macro");
    await this.playMacro(steps, text);
    return "Bluetooth macro";
  }

  private async tvOn(steps: string[], power: boolean, wantInput: boolean): Promise<string> {
    const parts: string[] = [];
    if (power) {
      await this.reach(steps, "tv_on", this.config.bluetooth.connect_timeout);
      await sleep(300);
      steps.push(`Bluetooth: sent ${this.config.power.on_key}`);
      parts.push("TV on (Bluetooth)");
    }
    if (wantInput) {
      if (power && this.config.input.delay_after_wake > 0) {
        steps.push(`waiting ${this.config.input.delay_after_wake}s for the TV to start up`);
        await sleep(500);
      }
      parts.push(`input switched (${await this.switchInput(steps)})`);
    }
    return parts.join(", ") || "nothing to do";
  }

  private async tvOff(steps: string[]): Promise<string> {
    await this.reach(steps, "tv_off", this.config.bluetooth.off_connect_timeout);
    await sleep(400);
    steps.push(`Bluetooth: sent ${this.config.power.off_key}`);
    return "TV off (Bluetooth)";
  }

  private event(name: string): Promise<ActionRecord> {
    const up = name === "boot" || name === "resume";
    const trigger = name as ActionRecord["trigger"];
    const events = this.config.events as unknown as Record<string, boolean>;
    return this.action(up ? "tv_on" : "tv_off", trigger, up, async (steps) => {
      if (!this.config.enabled) throw new Skip("automatic actions are turned off");
      const gate = this.gate();
      if (up) {
        const power = events[`${name}_tv_on`];
        const input = events[`${name}_switch_input`] && this.config.input.method !== "none";
        if (!power && !input) throw new Skip(`nothing is set to happen on ${name}`);
        if (!gate.ok) throw new Skip(gate.reason);
        steps.push(`display check: ${gate.reason}`);
        return this.tvOn(steps, power, input);
      }
      if (!events[`${name}_tv_off`]) throw new Skip(`turning the TV off on ${name} is disabled`);
      if (!gate.ok) throw new Skip(gate.reason);
      steps.push(`display check: ${gate.reason}`);
      return this.tvOff(steps);
    });
  }

  // ------------------------------------------------------------------ pairing

  private stopPairing(): void {
    window.clearTimeout(this.pairTimer);
    Object.assign(this.bluetooth, { pairing: false, pairing_seconds_left: 0, advertising: this.bluetooth.paired && !this.bluetooth.connected });
    // A warning about the open pairing mode goes away with it.
    if (this.bluetooth.error === CLASSIC_VISIBLE) this.bluetooth.error = "";
  }

  private startPairing(timeout: number): void {
    this.pairingUntil = Date.now() + timeout * 1000;
    Object.assign(this.bluetooth, {
      pairing: true,
      pairing_seconds_left: timeout,
      advertising: true,
      error: this.classicVisible ? CLASSIC_VISIBLE : "",
    });
    this.log("INFO", `pairing mode open for ${timeout}s`);
    window.clearTimeout(this.pairTimer);
    if (this.pairSeconds === null) return;
    this.pairTimer = window.setTimeout(() => {
      if (!this.bluetooth.pairing) return;
      Object.assign(this.bluetooth, { paired: true, tv_address: TV_ADDRESS, tv_name: TV_NAME });
      this.config.bluetooth.tv_address = TV_ADDRESS;
      this.config.bluetooth.tv_name = TV_NAME;
      this.stopPairing();
      this.log("INFO", `paired with ${TV_NAME} (${TV_ADDRESS})`);
      window.setTimeout(() => {
        if (this.bluetooth.paired) {
          Object.assign(this.bluetooth, { connected: true, ready: true, subscribed: true, delivery: "bluez", advertising: false });
        }
      }, 800);
    }, this.pairSeconds * 1000);
  }

  // ------------------------------------------------------------------ dispatch

  private async dispatch(method: string, params: Json): Promise<unknown> {
    const text = (name: string, allowEmpty = false): string => {
      const value = params[name];
      if (typeof value !== "string" || (!allowEmpty && !value.trim())) throw new RpcFailure("bad_request", `missing or invalid '${name}'`);
      return value;
    };
    switch (method) {
      case "status":
        return this.status();
      case "get_config":
        return { ...this.bundle, config: clone(this.config) };
      case "set_config":
        this.config = applyPatch(this.config as unknown as Json, params.patch, this.bundle.schema as Json, "", this.bundle.keys) as unknown as Config;
        this.configProblems = [];
        return { config: clone(this.config) };
      case "tv_on":
        return this.action("tv_on", "manual", false, (steps) =>
          this.tvOn(steps, true, params.switch_input !== false && this.config.input.method !== "none"),
        );
      case "tv_off":
        return this.action("tv_off", "manual", false, (steps) => this.tvOff(steps));
      case "switch_input": {
        const via = params.via;
        if (via !== undefined && via !== null && via !== "macro" && via !== "wifi") {
          throw new RpcFailure("bad_request", "'via' must be 'macro' or 'wifi'");
        }
        return this.action("switch_input", "manual", false, async (steps) => {
          // With `via`, exactly that method is tried, whatever input.method is, with no fallback.
          if (via === "wifi") {
            await this.wifiSwitch(steps);
            return "input switched (Wi-Fi)";
          }
          if (via === "macro") {
            const text = this.effectiveMacro().trim();
            if (!text) throw new Error("no input macro is set: choose a preset or enter a macro");
            await this.playMacro(steps, text);
            return "input switched (Bluetooth macro)";
          }
          return `input switched (${await this.switchInput(steps)})`;
        });
      }
      case "run_macro": {
        const rendered = renderMacro(text("macro"), this.config.input.hdmi_port);
        return this.action("macro", "manual", false, async (steps) => {
          await this.playMacro(steps, rendered);
          return "macro played";
        });
      }
      case "check_macro": {
        const port = params.port ?? this.config.input.hdmi_port;
        if (typeof port !== "number" || !Number.isInteger(port) || port < 1 || port > 4) {
          throw new RpcFailure("bad_request", "'port' must be an HDMI port number from 1 to 4");
        }
        try {
          const cleaned = validateMacro(text("macro", true), this.bundle.keys);
          const steps = parseMacro(renderMacro(cleaned, port), this.bundle.keys);
          const { press, gap } = this.timings();
          return { ok: true, normalized: cleaned, rendered: formatSteps(steps), seconds: Math.round(estimate(steps, press, gap) * 10) / 10 };
        } catch (reason) {
          return { ok: false, error: (reason as Error).message };
        }
      }
      case "event": {
        const name = text("name");
        if (!["boot", "resume", "suspend", "shutdown", "reboot"].includes(name)) {
          throw new RpcFailure("bad_request", `unknown event '${name}' (expected one of: boot, resume, suspend, shutdown, reboot)`);
        }
        return this.event(name);
      }
      case "send_key": {
        const key = text("key").toUpperCase();
        const name = ALIASES[key] ?? key;
        if (!this.bundle.keys.some((entry) => entry.name === name) && !/^[KCS]:[0-9A-F]{1,4}$/.test(name)) {
          throw new RpcFailure("bad_key", `unknown key '${key}' (use a name such as HOME, DOWN, OK, WAKE, SLEEP or a raw usage like C:0223, K:3C, S:83)`);
        }
        if (this.busy) throw new RpcFailure("busy", `busy with ${this.busy.replace(/^(\S+) /, "'$1' ")}; try again shortly`);
        if (!this.bluetooth.paired) throw new RpcFailure("not_paired", "no TV is paired");
        if (this.failing.has("send_key")) {
          await sleep(600);
          throw new RpcFailure("tv_unreachable", "the TV is not connected over Bluetooth");
        }
        await sleep(90);
        this.log("DEBUG", `sent ${name}`);
        return { key: name };
      }
      case "pair_start":
        if (!this.bluetooth.available) throw new RpcFailure("no_adapter", "no Bluetooth adapter was found");
        if (this.failing.has("pair_start")) throw new RpcFailure("bluetooth_off", "Bluetooth is turned off in SteamOS");
        this.startPairing(typeof params.timeout === "number" ? params.timeout : 120);
        return { ...this.bluetooth };
      case "pair_cancel":
        this.stopPairing();
        return { ...this.bluetooth };
      case "forget_tv":
        Object.assign(this.bluetooth, { paired: false, tv_address: "", tv_name: "", connected: false, ready: false, subscribed: false, delivery: "", advertising: false });
        this.config.bluetooth.tv_address = "";
        this.config.bluetooth.tv_name = "";
        this.log("INFO", "forgot the TV");
        return { ...this.bluetooth };
      case "connect": {
        if (!this.bluetooth.paired) throw new RpcFailure("not_paired", "no TV is paired");
        await sleep(600);
        if (!this.failing.has("connect")) {
          Object.assign(this.bluetooth, { connected: true, ready: true, subscribed: true, delivery: "bluez", advertising: false });
        }
        return { ...this.bluetooth };
      }
      case "disconnect":
        Object.assign(this.bluetooth, { connected: false, ready: false, delivery: "", advertising: this.config.bluetooth.reconnect_on_drop });
        return { ...this.bluetooth };
      case "wifi_discover":
        await sleep(700);
        return { devices: this.failing.has("wifi_discover") ? [] : [{ name: TV_NAME, host: TV_HOST, port: 6466 }] };
      case "wifi_pair_start": {
        const host = text("host").trim();
        if (!/^[A-Za-z0-9._:\-[\]]+$/.test(host)) throw new RpcFailure("bad_request", "wifi.host: must be an IP address or hostname");
        await sleep(400);
        if (host !== TV_HOST) throw new RpcFailure("wifi_unreachable", `no answer from ${host} on port 6467`);
        Object.assign(this.wifi, { pairing: true, pairing_host: host });
        return { paired: this.wifi.paired, pairing: true, pairing_host: host };
      }
      case "wifi_pair_finish": {
        const code = text("code").trim();
        if (!this.wifi.pairing) throw new RpcFailure("pairing_failed", "no pairing is in progress");
        await sleep(400);
        if (!/^[0-9a-fA-F]{6}$/.test(code) || code === "000000") {
          // Like the real assist: a code the TV turned down leaves the pairing open, so it
          // can be tried again; only wifi_pair_cancel (or two minutes) closes it.
          throw new RpcFailure("pairing_failed", "the TV did not accept the code");
        }
        this.config.wifi.enabled = true;
        this.config.wifi.host = this.wifi.pairing_host;
        if (typeof params.name === "string" && params.name) this.config.wifi.name = params.name;
        Object.assign(this.wifi, { paired: true, pairing: false, pairing_host: "" });
        this.log("INFO", `Wi-Fi assist paired with ${this.config.wifi.host}`);
        return { ...this.wifi };
      }
      case "wifi_pair_cancel":
        Object.assign(this.wifi, { pairing: false, pairing_host: "" });
        return { ...this.wifi };
      case "wifi_unpair":
        Object.assign(this.wifi, { paired: false });
        this.config.wifi.enabled = false;
        return { ...this.wifi };
      case "wifi_test":
        if (!this.config.wifi.host) throw new RpcFailure("wifi_not_configured", "no TV address is set for the Wi-Fi assist");
        if (!this.wifi.paired) throw new RpcFailure("wifi_not_paired", "the Wi-Fi assist is not paired with the TV");
        await sleep(300);
        return { reachable: true, on: true, name: this.config.wifi.name || TV_NAME };
      case "logs": {
        const lines = typeof params.lines === "number" ? params.lines : 100;
        return { lines: this.logs.slice(-lines) };
      }
      default:
        throw new RpcFailure("unknown_method", `unknown method '${method}'`);
    }
  }

  // ------------------------------------------------------------------ what main.py exposes

  async rpc(method: string, params?: Json | null): Promise<RpcReply<unknown>> {
    const sent = params ?? {};
    this.calls.push({ fn: "rpc", method, params: sent, at: Date.now() });
    if (method === "status") {
      this.statusInFlight += 1;
      this.maxStatusInFlight = Math.max(this.maxStatusInFlight, this.statusInFlight);
      try {
        await sleep(this.statusDelay);
        return await this.answer(method, sent);
      } finally {
        this.statusInFlight -= 1;
      }
    }
    await sleep(45);
    return this.answer(method, sent);
  }

  private async answer(method: string, sent: Json): Promise<RpcReply<unknown>> {
    if (this.mode !== "running") {
      return {
        ok: false,
        error: {
          code: "unavailable",
          message: "cannot reach the tcl-me-remo service: [Errno 2] No such file or directory",
        },
      };
    }
    try {
      return { ok: true, result: await this.dispatch(method, sent) };
    } catch (reason) {
      if (reason instanceof RpcFailure) return { ok: false, error: { code: reason.code, message: reason.message } };
      return { ok: false, error: { code: "internal", message: String(reason) } };
    }
  }

  async service_info(): Promise<ServiceInfo> {
    this.calls.push({ fn: "service_info", method: "", params: {}, at: Date.now() });
    await sleep(150);
    const installed = this.mode !== "not-installed";
    const active = this.mode === "running";
    return {
      installed,
      active,
      enabled: installed,
      installed_version: installed ? this.base.version : "",
      bundled_version: this.base.version,
      needs_update: !installed,
      install_dir: installed ? "/opt/tcl-me-remo" : "",
      ...(this.installing ? { installing: true } : {}),
      problems: installed && !active ? ["the service is inactive"] : [],
    };
  }

  async install_service(): Promise<RpcReply<unknown>> {
    this.calls.push({ fn: "install_service", method: "", params: {}, at: Date.now() });
    await sleep(1200);
    if (this.failing.has("install_service")) {
      return { ok: false, error: { code: "install_failed", message: "could not write to /opt/tcl-me-remo: [Errno 30] Read-only file system" } };
    }
    const log = this.mode === "not-installed"
      ? [`installed version ${this.base.version} to /opt/tcl-me-remo`, "enabled tcl-me-remo.service", "service started"]
      : this.mode === "stopped" ? ["service started"] : ["already up to date"];
    this.mode = "running";
    this.startedAt = Date.now();
    return { ok: true, result: { changed: true, restarted: true, log, ...(await this.service_info()) } };
  }

  async remove_service(purge: boolean = false): Promise<RpcReply<unknown>> {
    this.calls.push({ fn: "remove_service", method: "", params: { purge }, at: Date.now() });
    await sleep(800);
    this.mode = "not-installed";
    return { ok: true, result: { log: purge ? ["service removed", "settings deleted"] : ["service removed"] } };
  }

  async restart_service(): Promise<RpcReply<unknown>> {
    this.calls.push({ fn: "restart_service", method: "", params: {}, at: Date.now() });
    await sleep(900);
    if (this.mode === "not-installed") {
      return { ok: false, error: { code: "restart_failed", message: "Failed to restart tcl-me-remo.service: Unit tcl-me-remo.service not found." } };
    }
    this.mode = "running";
    this.startedAt = Date.now();
    Object.assign(this.bluetooth, { connected: false, ready: false, subscribed: false, delivery: "" });
    return { ok: true, result: {} };
  }
}

class Skip extends Error {}

export const fake = new FakeBackend();
