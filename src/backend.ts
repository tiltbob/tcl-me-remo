// The only place that talks to the plugin backend (main.py).
//
// Every function here resolves to an RpcReply and never rejects: a backend that throws, a
// reply of the wrong shape and a call that is never answered (Decky does not settle calls
// that are in flight when the backend stops) all become `{ok: false, error}`.

import { callable } from "@decky/api";

import type { DoctorReport, InstallResult, RpcReply, ServiceInfo } from "./types";

const callRpc = callable<[method: string, params: Record<string, unknown>], unknown>("rpc");
const callServiceInfo = callable<[], unknown>("service_info");
const callInstallService = callable<[], unknown>("install_service");
const callRemoveService = callable<[purge: boolean], unknown>("remove_service");
const callRestartService = callable<[], unknown>("restart_service");
const callDoctor = callable<[], unknown>("doctor");
const callServiceLog = callable<[lines: number], unknown>("service_log");

// Safety nets only. main.py gives up first (its TIMEOUTS table: 240 s at most for the slow
// methods, 45 s or less for the rest); these fire when the backend itself has gone away.
const SLOW_METHODS = new Set(["tv_on", "switch_input", "run_macro", "event", "connect"]);
const SLOW_SECONDS = 260;
const NORMAL_SECONDS = 60;
const STATUS_SECONDS = 20;
// install_service and remove_service wait for an install that is already running (main.py
// installs the service in the background when the plugin loads; up to 120 s) and then run
// their own (up to another 120 s).
const INSTALLER_SECONDS = 260;
// The doctor asks a dozen system tools, each with its own time limit; main.py gives it 75 s.
const DOCTOR_SECONDS = 90;

export function failure(code: string, message: string): RpcReply<never> {
  return { ok: false, error: { code, message } };
}

function describe(reason: unknown): string {
  // A Python exception arrives with an empty message (see the Decky notes), so do not
  // pretend to know more than "it failed".
  const text = reason instanceof Error ? reason.message : typeof reason === "string" ? reason : "";
  return text ? `the plugin backend failed: ${text}` : "the plugin backend failed (details are in the Decky log)";
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asReply<T>(value: unknown): RpcReply<T> {
  if (isObject(value) && value.ok === true) {
    return { ok: true, result: value.result as T };
  }
  if (isObject(value) && value.ok === false) {
    const error = isObject(value.error) ? value.error : {};
    return failure(String(error.code ?? "error"), String(error.message ?? "unknown error"));
  }
  return failure("bad_reply", "the plugin backend sent an unreadable reply");
}

function settle<T>(
  what: string,
  seconds: number,
  start: () => Promise<unknown>,
  read: (value: unknown) => RpcReply<T> = asReply,
): Promise<RpcReply<T>> {
  return new Promise((resolve) => {
    let done = false;
    const finish = (reply: RpcReply<T>) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(reply);
    };
    const timer = setTimeout(
      () => finish(failure("timeout", `the plugin backend did not answer '${what}' within ${seconds}s`)),
      seconds * 1000,
    );
    try {
      start().then(
        (value) => {
          let reply: RpcReply<T>;
          try {
            reply = read(value);
          } catch (reason) {
            reply = failure("bad_reply", describe(reason));
          }
          finish(reply);
        },
        (reason) => finish(failure("plugin_error", describe(reason))),
      );
    } catch (reason) {
      finish(failure("plugin_error", describe(reason)));
    }
  });
}

/** One request to the service (docs/control-protocol.md). */
export function rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<RpcReply<T>> {
  const seconds = method === "status" ? STATUS_SECONDS : SLOW_METHODS.has(method) ? SLOW_SECONDS : NORMAL_SECONDS;
  return settle<T>(method, seconds, () => callRpc(method, params));
}

/** What is installed and whether it runs. Works when the service itself does not. */
export function serviceInfo(): Promise<RpcReply<ServiceInfo>> {
  return settle<ServiceInfo>("service_info", NORMAL_SECONDS, callServiceInfo, (value) => {
    if (isObject(value) && typeof value.installed === "boolean") {
      const problems = Array.isArray(value.problems) ? value.problems.map(String) : [];
      return { ok: true, result: { ...(value as unknown as ServiceInfo), problems } };
    }
    return failure("bad_reply", "the plugin backend sent an unreadable reply");
  });
}

export function installService(): Promise<RpcReply<InstallResult>> {
  return settle<InstallResult>("install_service", INSTALLER_SECONDS, callInstallService);
}

export function removeService(purge: boolean): Promise<RpcReply<{ log?: string[] }>> {
  return settle<{ log?: string[] }>("remove_service", INSTALLER_SECONDS, () => callRemoveService(purge));
}

export function restartService(): Promise<RpcReply<Record<string, never>>> {
  return settle<Record<string, never>>("restart_service", 90, callRestartService);
}

/** The report of "tcl-me-remo doctor". Read-only; works when the service itself does not. */
export function doctor(): Promise<RpcReply<DoctorReport>> {
  return settle<DoctorReport>("doctor", DOCTOR_SECONDS, callDoctor, (value) => {
    const reply = asReply<DoctorReport>(value);
    if (!reply.ok) return reply;
    const result: unknown = reply.result;
    if (!isObject(result) || !Array.isArray(result.checks)) {
      return failure("bad_reply", "the plugin backend sent an unreadable report");
    }
    const lines = (list: unknown) => (Array.isArray(list) ? list.map(String) : []);
    return {
      ok: true,
      result: {
        worst: String(result.worst ?? "unknown") as DoctorReport["worst"],
        system: lines(result.system),
        checks: result.checks.filter(isObject).map((check) => ({
          name: String(check.name ?? ""),
          status: String(check.status ?? "unknown") as DoctorReport["worst"],
          detail: String(check.detail ?? ""),
        })),
        details: lines(result.details),
      },
    };
  });
}

/**
 * The service's lines in the system journal, oldest first. For when the service does not
 * answer; while it does, its own "logs" request is the better source.
 */
export function serviceLog(lines: number): Promise<RpcReply<{ lines: string[] }>> {
  return settle<{ lines: string[] }>("service_log", NORMAL_SECONDS, () => callServiceLog(lines));
}
