// The pieces of Steam and Decky Loader that the mocks share: where we have "navigated" to,
// which routes the plugin registered, open modals and shown toasts.

import type { ComponentType, ReactNode } from "react";
import { useEffect, useState } from "react";

export interface Toast {
  id: number;
  title: ReactNode;
  body: ReactNode;
  critical: boolean;
}

export interface OpenModal {
  id: number;
  element: ReactNode;
  resolve: () => void;
}

interface HostState {
  /** "" is the Quick Access Menu; anything else is a full-screen route. */
  location: string;
  routes: Record<string, ComponentType>;
  modals: OpenModal[];
  toasts: Toast[];
  quickAccessVisible: boolean;
  /** False once the loader has unloaded the plugin: nothing of it is rendered any more. */
  pluginLoaded: boolean;
}

let state: HostState = {
  location: "",
  routes: {},
  modals: [],
  toasts: [],
  quickAccessVisible: true,
  pluginLoaded: true,
};
const listeners = new Set<() => void>();
let nextId = 1;

/** Everything a test may want to look at afterwards. */
export const record = {
  toasts: [] as { title: string; body: string; critical: boolean }[],
  navigations: [] as string[],
  sideMenusClosed: 0,
  /** How many different `pages` arrays SidebarNavigation has been given. */
  sidebarPageLists: 0,
};

function update(partial: Partial<HostState>): void {
  state = { ...state, ...partial };
  for (const listener of Array.from(listeners)) listener();
}

export function getHost(): HostState {
  return state;
}

export function useHost(): HostState {
  const [, bump] = useState(0);
  useEffect(() => {
    const listener = () => bump((n) => n + 1);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return state;
}

export function navigate(location: string): void {
  record.navigations.push(location);
  update({ location });
}

export function setQuickAccessVisible(visible: boolean): void {
  update({ quickAccessVisible: visible });
}

/** What the loader does when it unloads a plugin: its panel and its dialogs go away. */
export function unloadPlugin(): void {
  for (const modal of state.modals) modal.resolve();
  update({ pluginLoaded: false, modals: [], location: "" });
}

export function addRoute(path: string, component: ComponentType): void {
  update({ routes: { ...state.routes, [path]: component } });
}

export function removeRoute(path: string): void {
  const { [path]: _gone, ...rest } = state.routes;
  update({ routes: rest });
}

export function openModal(element: ReactNode): { id: number; closed: Promise<void> } {
  const id = nextId++;
  let resolve: () => void = () => undefined;
  const closed = new Promise<void>((done) => {
    resolve = done;
  });
  update({ modals: [...state.modals, { id, element, resolve }] });
  return { id, closed };
}

export function closeModal(id: number): void {
  const modal = state.modals.find((entry) => entry.id === id);
  if (!modal) return;
  update({ modals: state.modals.filter((entry) => entry.id !== id) });
  modal.resolve();
}

export function replaceModal(id: number, element: ReactNode): void {
  update({ modals: state.modals.map((entry) => (entry.id === id ? { ...entry, element } : entry)) });
}

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  return "";
}

export function addToast(title: ReactNode, body: ReactNode, critical: boolean, duration: number): number {
  const id = nextId++;
  record.toasts.push({ title: textOf(title), body: textOf(body), critical });
  update({ toasts: [...state.toasts, { id, title, body, critical }] });
  window.setTimeout(() => dismissToast(id), duration);
  return id;
}

export function dismissToast(id: number): void {
  if (state.toasts.some((toast) => toast.id === id)) update({ toasts: state.toasts.filter((toast) => toast.id !== id) });
}
