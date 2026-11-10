// Every request the UI can make, in one place. Each function returns a promise that
// resolves to an Outcome (or a ConfigResult) and never rejects.

import { toaster } from "@decky/api";

import * as backend from "./backend";
import { rpc } from "./backend";
import { ALREADY_RUNNING, store } from "./store";
import type { ConfigResult, Outcome } from "./store";
import { kindWord } from "./text";
import type { BluetoothState, ConfigPatch, WifiDevice, WifiStatus, WifiTest } from "./types";

export const ROUTE = "/tcl-me-remo";

export const KEYS = {
  tvOn: "tv.on",
  tvOff: "tv.off",
  switchInput: "tv.input",
  wifiSwitch: "tv.wifi_input",
  testWake: "tv.test_wake",
  testSleep: "tv.test_sleep",
  macro: "tv.macro",
  connect: "bt.connect",
  disconnect: "bt.disconnect",
  forget: "bt.forget",
  install: "svc.install",
  restart: "svc.restart",
  remove: "svc.remove",
  wifiDiscover: "wifi.discover",
  wifiUnpair: "wifi.unpair",
  wifiTest: "wifi.test",
  wifiPairCancel: "wifi.pair_cancel",
} as const;

// ---------------------------------------------------------------------- notifications

function toast(title: string, body: string, failed: boolean): void {
  try {
    toaster.toast({ title, body, critical: failed, duration: failed ? 8000 : 3000 });
  } catch (reason) {
    console.warn("tcl-me-remo: could not show a notification", reason);
  }
}

/** Show an action's outcome as a notification (used by the Quick Access panel). */
export function toastOutcome(outcome: Outcome): void {
  if (outcome.code === ALREADY_RUNNING) return;
  toast(`${outcome.title}: ${kindWord[outcome.kind]}`, outcome.text, outcome.kind === "failed");
}

// ---------------------------------------------------------------------- settings

/** Change settings; a rejected change is taken back and its reason shown. */
export function applyConfig(patch: ConfigPatch): Promise<ConfigResult> {
  return store.setConfig(patch).then((result) => {
    if (!result.ok) toast("Setting not changed", result.message, true);
    return result;
  });
}

// ---------------------------------------------------------------------- TV actions

export function tvOn(): Promise<Outcome> {
  return store.run(KEYS.tvOn, "TV on", () => rpc("tv_on"));
}

export function tvOff(): Promise<Outcome> {
  return store.run(KEYS.tvOff, "TV off", () => rpc("tv_off"));
}

export function switchInput(): Promise<Outcome> {
  return store.run(KEYS.switchInput, "Switch input", () => rpc("switch_input"));
}

/**
 * Ask for the input over Wi-Fi only, whatever input.method is and with no fallback, so the
 * Wi-Fi method can be tried before it is turned on.
 */
export function switchInputOverWifi(): Promise<Outcome> {
  return store.run(KEYS.wifiSwitch, "Switch input over Wi-Fi", () => rpc("switch_input", { via: "wifi" }));
}

/** Run exactly what a real wake or suspend would run, without the event having happened. */
export function testEvent(name: "resume" | "suspend"): Promise<Outcome> {
  const wake = name === "resume";
  return store.run(wake ? KEYS.testWake : KEYS.testSleep, wake ? "Test wake" : "Test sleep", () =>
    rpc("event", { name, simulate: true }),
  );
}

export function runMacro(key: string, macro: string): Promise<Outcome> {
  return store.run(key, "Macro", () => rpc("run_macro", { macro }));
}

// ---------------------------------------------------------------------- Bluetooth

export function connectTv(): Promise<Outcome> {
  return store.run(
    KEYS.connect,
    "Connect",
    () => rpc<BluetoothState & { ready: boolean }>("connect"),
    (result) =>
      result.ready
        ? { text: "The TV is connected" }
        : { kind: "failed", text: result.error || "The TV did not connect. Is it on, or in screenless standby?" },
  );
}

export function disconnectTv(): Promise<Outcome> {
  return store.run(KEYS.disconnect, "Disconnect", () => rpc<BluetoothState>("disconnect"), () => ({
    text: "Disconnected",
  }));
}

export async function forgetTv(): Promise<Outcome> {
  const outcome = await store.run(KEYS.forget, "Forget TV", () => rpc<BluetoothState>("forget_tv"), () => ({
    text: "Forgotten here. Also remove this remote on the TV, under Remotes & Accessories.",
  }));
  await store.loadConfig();
  return outcome;
}

// ---------------------------------------------------------------------- the service itself

async function afterServiceChange(outcome: Outcome): Promise<Outcome> {
  await store.refreshService();
  store.poll();
  void store.loadConfig();
  return outcome;
}

export function installService(): Promise<Outcome> {
  return store
    .run(KEYS.install, "Install service", backend.installService, (result) => ({
      text: (result?.log ?? []).join("; ") || "The service is installed",
    }))
    .then(afterServiceChange);
}

export function restartService(): Promise<Outcome> {
  return store
    .run(KEYS.restart, "Restart service", backend.restartService, () => ({ text: "The service was restarted" }))
    .then(afterServiceChange);
}

/**
 * What a removal is followed by. The page then shows "not installed" with an Install button,
 * which is not the way on for somebody who wants to be rid of it.
 */
export const AFTER_REMOVE =
  "Now uninstall the plugin in Decky (Decky settings > Plugins): otherwise it installs the service again the next time it loads.";

export function removeService(purge: boolean): Promise<Outcome> {
  return store
    .run(KEYS.remove, "Remove service", () => backend.removeService(purge), (result) => ({
      text: `${(result?.log ?? []).join("; ") || "The service was removed"}. ${AFTER_REMOVE}`,
    }))
    .then(afterServiceChange);
}

// ---------------------------------------------------------------------- Wi-Fi assist

export async function wifiDiscover(): Promise<{ outcome: Outcome; devices: WifiDevice[] }> {
  let devices: WifiDevice[] = [];
  const outcome = await store.run(
    KEYS.wifiDiscover,
    "Search",
    () => rpc<{ devices: WifiDevice[] }>("wifi_discover"),
    (result) => {
      devices = Array.isArray(result?.devices) ? result.devices : [];
      return {
        text: devices.length === 1 ? "Found 1 TV" : `Found ${devices.length} TVs`,
        kind: devices.length ? "ok" : "skipped",
      };
    },
  );
  return { outcome, devices };
}

export async function wifiUnpair(): Promise<Outcome> {
  const outcome = await store.run(KEYS.wifiUnpair, "Unpair Wi-Fi", () => rpc<WifiStatus>("wifi_unpair"), () => ({
    text: "The Wi-Fi pairing was removed and the assist turned off",
  }));
  await store.loadConfig();
  return outcome;
}

/** Close a Wi-Fi pairing that was left open (the TV is still showing its code). */
export function wifiPairCancel(): Promise<Outcome> {
  return store.run(KEYS.wifiPairCancel, "Cancel pairing", () => rpc<WifiStatus>("wifi_pair_cancel"), () => ({
    text: "The pairing was cancelled",
  }));
}

export function wifiTest(): Promise<Outcome> {
  return store.run(KEYS.wifiTest, "Check connection", () => rpc<WifiTest>("wifi_test"), (result) => {
    const name = result.name ? `${result.name} answers` : "The TV answers";
    const power = result.on === true ? "; it is on" : result.on === false ? "; it is in standby" : "";
    return { text: `${name} over Wi-Fi${power}` };
  });
}
