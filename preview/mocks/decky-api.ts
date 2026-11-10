// Stand-in for @decky/api: the same exports the plugin uses, wired to the fake backend and
// to the preview's own idea of routes and toasts.

import type * as Real from "@decky/api";

import { fake } from "../fake-backend";
import { addRoute, addToast, dismissToast, removeRoute, useHost } from "../host";

type Callables = Record<string, (...args: unknown[]) => Promise<unknown>>;

function invoke(route: string, args: unknown[]): Promise<unknown> {
  const method = (fake as unknown as Callables)[route];
  if (typeof method !== "function" || route.startsWith("_")) {
    // What the loader does for a method the backend does not have.
    return Promise.reject(new Error(`plugin has no method ${route}`));
  }
  // Arguments and results cross a JSON boundary in the real thing.
  const sent = JSON.parse(JSON.stringify(args)) as unknown[];
  return method.apply(fake, sent).then((result) => (result === undefined ? null : JSON.parse(JSON.stringify(result))));
}

export const call = ((route: string, ...args: unknown[]) => invoke(route, args)) as typeof Real.call;

export const callable = ((route: string) =>
  (...args: unknown[]) =>
    invoke(route, args)) as typeof Real.callable;

export const definePlugin: typeof Real.definePlugin = (fn) => fn;

export const routerHook = {
  addRoute: (path: string, component: Parameters<typeof addRoute>[1]) => addRoute(path, component),
  removeRoute: (path: string) => removeRoute(path),
  addPatch: (_path: string, patch: unknown) => patch,
  removePatch: () => undefined,
  addGlobalComponent: () => undefined,
  removeGlobalComponent: () => undefined,
} as unknown as typeof Real.routerHook;

export const toaster: typeof Real.toaster = {
  toast(data) {
    const id = addToast(data.title, data.body, data.critical === true, data.duration ?? 5000);
    return { data, dismiss: () => dismissToast(id) };
  },
};

export const useQuickAccessVisible: typeof Real.useQuickAccessVisible = () => useHost().quickAccessVisible;

export const addEventListener = ((_event: string, listener: unknown) => listener) as typeof Real.addEventListener;
export const removeEventListener = (() => undefined) as typeof Real.removeEventListener;
