// Shapes of the service's control protocol (see docs/control-protocol.md).
// These mirror py_modules/tcl_me_remo/{config,interfaces,service}.py; keep them in step.

export type EventName = "boot" | "resume" | "suspend" | "shutdown" | "reboot";

export interface EventsConfig {
  boot_tv_on: boolean;
  boot_switch_input: boolean;
  resume_tv_on: boolean;
  resume_switch_input: boolean;
  suspend_tv_off: boolean;
  shutdown_tv_off: boolean;
  reboot_tv_off: boolean;
}

export interface DisplayConfig {
  require_external: "auto" | "yes" | "no";
  edid_match: string;
  wait_seconds: number;
}

export interface PowerConfig {
  on_key: string;
  off_key: string;
  on_repeat: number;
  off_repeat: number;
  repeat_gap: number;
}

export interface InputConfig {
  method: "macro" | "wifi" | "none";
  hdmi_port: number;
  delay_after_wake: number;
  preset: string;
  macro: string;
  wifi_fallback_to_macro: boolean;
}

export interface BluetoothConfig {
  adapter: string;
  device_name: string;
  tv_address: string;
  tv_name: string;
  connect_timeout: number;
  off_connect_timeout: number;
  settle_ms: number;
  resubscribe_wait: number;
  raw_notify: "auto" | "off" | "always";
  reconnect_on_drop: boolean;
  wake_beacon: "off" | "mediatek" | "custom";
  wake_beacon_data: string;
  idle_disconnect_seconds: number;
  gatt_security: "encrypt" | "plain";
  key_press_ms: number;
  key_gap_ms: number;
  power_on_adapter: boolean;
}

export interface WifiConfig {
  enabled: boolean;
  host: string;
  name: string;
  input_mode: "app_link" | "keycode";
  link_preset: string;
  app_link: string;
  power_fallback: boolean;
  connect_timeout: number;
}

export interface Config {
  version: number;
  enabled: boolean;
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  events: EventsConfig;
  display: DisplayConfig;
  power: PowerConfig;
  input: InputConfig;
  bluetooth: BluetoothConfig;
  wifi: WifiConfig;
}

/** A nested object of changes, e.g. `{input: {hdmi_port: 2}}`. */
export type ConfigPatch = { [K in keyof Config]?: Config[K] extends object ? Partial<Config[K]> : Config[K] };

export interface SettingInfo {
  type: "bool" | "int" | "float" | "str";
  default: boolean | number | string;
  help?: string;
  min?: number;
  max?: number;
  maxlen?: number;
  choices?: string[];
}

export interface Preset {
  name: string;
  title: string;
  description: string;
  template: string;
}

export interface KeyInfo {
  name: string;
  label: string;
  page: "keyboard" | "consumer" | "system";
  usage: string;
  android: string;
}

export interface LinkPreset {
  name: string;
  title: string;
  description: string;
  /** HDMI port number (as a string key) to app link. */
  links: Record<string, string>;
}

export interface ConfigBundle {
  config: Config;
  schema: Record<string, unknown>;
  presets: Preset[];
  keys: KeyInfo[];
  link_presets: LinkPreset[];
}

export interface BluetoothState {
  available: boolean;
  adapter: string;
  adapter_address: string;
  powered: boolean;
  registered: boolean;
  advertising: boolean;
  pairing: boolean;
  pairing_seconds_left: number;
  paired: boolean;
  tv_address: string;
  tv_name: string;
  connected: boolean;
  ready: boolean;
  subscribed: boolean;
  delivery: "" | "bluez" | "raw";
  error: string;
}

export interface WifiStatus {
  available: boolean;
  enabled: boolean;
  host: string;
  name: string;
  paired: boolean;
  pairing: boolean;
  pairing_host: string;
}

export interface Connector {
  name: string;
  card: string;
  status: string;
  internal: boolean;
  maker: string;
  model: string;
  connected: boolean;
  label: string;
}

export interface DisplayStatus {
  gate_ok: boolean;
  gate_reason: string;
  has_internal: boolean;
  connectors: Connector[];
}

export interface InputStatus {
  method: InputConfig["method"];
  hdmi_port: number;
  wifi_link: string;
  macro: string;
  macro_error: string;
  macro_seconds: number;
}

export interface ActionRecord {
  name: "tv_on" | "tv_off" | "switch_input" | "macro";
  trigger: EventName | "manual";
  ok: boolean;
  skipped: boolean;
  detail: string;
  steps: string[];
  time: number;
  seconds: number;
}

export interface Status {
  version: string;
  uptime: number;
  enabled: boolean;
  phase: "running" | "going_down" | "down";
  busy: string;
  bluetooth: BluetoothState;
  wifi: WifiStatus;
  display: DisplayStatus;
  input: InputStatus;
  last_action: ActionRecord | null;
  history: ActionRecord[];
  config_problems: string[];
}

export interface MacroCheck {
  ok: boolean;
  error?: string;
  normalized?: string;
  rendered?: string;
  seconds?: number;
}

export interface WifiDevice {
  name: string;
  host: string;
  port: number;
}

/** Result of `wifi_test`. `on` is null when the TV did not report its power state. */
export interface WifiTest {
  reachable: boolean;
  on: boolean | null;
  name: string;
}

export interface RpcError {
  code: string;
  message: string;
}

/** What the plugin backend returns for every bridged call. */
export type RpcReply<T> = { ok: true; result: T } | { ok: false; error: RpcError };

/** State of the systemd service as seen by the plugin backend. */
export interface ServiceInfo {
  installed: boolean;
  active: boolean;
  enabled: boolean;
  installed_version: string;
  bundled_version: string;
  needs_update: boolean;
  /** Where the service's code is installed (empty when it is not). */
  install_dir?: string;
  problems: string[];
}

/** What `install_service` reports on success: what was done, plus the resulting state. */
export interface InstallResult extends Partial<ServiceInfo> {
  changed?: boolean;
  restarted?: boolean;
  log?: string[];
}
