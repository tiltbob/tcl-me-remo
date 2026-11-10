// Wording shared by the panel and the settings pages.

import type { OutcomeKind } from "./store";
import type { ActionRecord } from "./types";

export const colors = {
  ok: "#59bf40",
  warn: "#ffc82c",
  bad: "#ff6b6b",
  badBackground: "rgba(209, 28, 28, 0.22)",
  dim: "#8b929a",
  text: "#dcdedf",
};

export const kindColor: Record<OutcomeKind, string> = {
  ok: colors.ok,
  skipped: colors.warn,
  failed: colors.bad,
};

export const kindWord: Record<OutcomeKind, string> = {
  ok: "done",
  skipped: "skipped",
  failed: "failed",
};

const ACTION_NAMES: Record<string, string> = {
  tv_on: "TV on",
  tv_off: "TV off",
  switch_input: "Switch input",
  macro: "Macro",
};

const TRIGGER_NAMES: Record<string, string> = {
  boot: "start",
  resume: "wake",
  suspend: "suspend",
  shutdown: "shutdown",
  reboot: "restart",
  manual: "manual",
};

/** The service's `busy` text ("tv_on (boot)") in the panel's words ("TV on (start)"). */
export function busyName(busy: string): string {
  const match = /^(\w+) \((\w+)\)$/.exec(busy);
  if (!match) return busy;
  return `${ACTION_NAMES[match[1]] ?? match[1]} (${TRIGGER_NAMES[match[2]] ?? match[2]})`;
}

/**
 * A refused request in the panel's words. The service says "busy with 'tv_on' (resume); try
 * again shortly"; everything else is already plain.
 */
export function refusalText(code: string, message: string): string {
  if (code !== "busy") return message;
  const match = /'(\w+)' \((\w+)\)/.exec(message);
  if (!match) return message;
  return `Busy with ${busyName(`${match[1]} (${match[2]})`)}. Try again when that has finished.`;
}

export function actionName(record: ActionRecord): string {
  return ACTION_NAMES[record.name] ?? String(record.name);
}

export function triggerName(record: ActionRecord): string {
  return TRIGGER_NAMES[record.trigger] ?? String(record.trigger);
}

/** "just now", "3 min ago", "2 h ago", or a date for older things. `time` is in seconds. */
export function timeAgo(time: number, now: number = Date.now()): string {
  const seconds = Math.max(0, now / 1000 - time);
  if (seconds < 20) return "just now";
  if (seconds < 90) return "a minute ago";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 36 * 3600) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(time * 1000).toLocaleDateString();
}

/** The time of day, with the date in front when it was not today. `time` is in seconds. */
export function clockTime(time: number, now: number = Date.now()): string {
  const when = new Date(time * 1000);
  const today = when.toDateString() === new Date(now).toDateString();
  return today ? when.toLocaleTimeString() : `${when.toLocaleDateString()} ${when.toLocaleTimeString()}`;
}

export function seconds(value: number): string {
  return `${Math.round(value * 10) / 10} s`;
}

export function duration(value: number): string {
  if (value < 90) return `${Math.round(value)} s`;
  if (value < 5400) return `${Math.round(value / 60)} min`;
  if (value < 36 * 3600) return `${Math.round(value / 360) / 10} h`;
  return `${Math.round(value / 8640) / 10} days`;
}
