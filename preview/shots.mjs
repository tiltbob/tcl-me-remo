// Builds the preview, drives it with Playwright, checks that the UI does what it must, and
// saves the screenshots used in the documentation.
//
//   pnpm run preview            (= node preview/shots.mjs)
//
// Exits non-zero when a check fails, and treats every console error, every unhandled rejection
// and every problem the plugin itself caught and logged ("tcl-me-remo: ...") as a failure.
// Playwright and Chromium are expected to be installed already (globally is fine: it is
// loaded through require, which honours NODE_PATH).
//
// The pages run on a fixed date, time zone and locale, so the screenshots in docs/images/
// come out the same on every run and every machine.

import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { buildPreview, outDir } from "./build.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const docsImages = join(dirname(here), "docs", "images");
const extraShots = join(outDir, "shots");

function loadPlaywright() {
  try {
    return createRequire(import.meta.url)("playwright");
  } catch (error) {
    throw new Error(`Playwright is not installed where Node can find it (NODE_PATH=${process.env.NODE_PATH ?? ""}): ${error.message}`);
  }
}

// Where the pages' clock starts. Time then runs at its normal pace.
const CLOCK_START = Date.UTC(2026, 10, 10, 19, 30, 0);

const QAM = { width: 300, height: 760 };
const SCREEN = { width: 1280, height: 800 };
const ROUTE = "/tcl-me-remo";
// What the panel must show for updates: the address in src/text.ts (the README has the same;
// tests/test_release_plan.py compares the two).
const UPDATE_ADDRESS = /UPDATE_ADDRESS = "([^"]+)"/.exec(readFileSync(join(dirname(here), "src", "text.ts"), "utf8"))[1];

let passed = 0;
const failures = [];

function check(name, ok, detail = "") {
  if (ok) {
    passed += 1;
    console.log(`  ok    ${name}`);
  } else {
    failures.push(`${name}${detail ? `: ${detail}` : ""}`);
    console.log(`  FAIL  ${name}${detail ? `: ${detail}` : ""}`);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const url = await buildPreview();
  mkdirSync(docsImages, { recursive: true });
  mkdirSync(extraShots, { recursive: true });
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();

  /** Open the preview in a fresh page; page problems become failures. */
  async function open(label, viewport, params) {
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2, timezoneId: "UTC", locale: "en-US" });
    await context.addInitScript((start) => {
      const RealDate = Date;
      const offset = start - RealDate.now();
      class ShiftedDate extends RealDate {
        constructor(...args) {
          if (args.length === 0) super(RealDate.now() + offset);
          else super(...args);
        }
        static now() {
          return RealDate.now() + offset;
        }
      }
      window.Date = ShiftedDate;
    }, CLOCK_START);
    const page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") check(`[${label}] no console errors`, false, message.text());
      // Whatever the plugin catches it logs as a warning; a caught problem is still a problem.
      if (message.type() === "warning" && message.text().includes("tcl-me-remo:")) {
        check(`[${label}] nothing went wrong behind the scenes`, false, message.text());
      }
    });
    page.on("pageerror", (error) => check(`[${label}] no uncaught errors`, false, String(error)));
    await page.goto(`${url}?${new URLSearchParams(params)}`);
    return page;
  }

  /** True when `text` shows up within the timeout. */
  async function sees(page, text, timeout = 5000, exact = false) {
    try {
      await page.getByText(text, { exact }).first().waitFor({ state: "visible", timeout });
      return true;
    } catch {
      return false;
    }
  }

  async function gone(page, text, timeout = 5000) {
    try {
      await page.getByText(text).first().waitFor({ state: "hidden", timeout });
      return true;
    } catch {
      return false;
    }
  }

  const button = (page, name) => page.getByRole("button", { name, exact: true });
  const toggle = (page, name) => page.getByRole("switch", { name, exact: true });
  const calls = (page, method) =>
    page.evaluate((wanted) => window.__preview.fake.calls.filter((call) => (call.method || call.fn) === wanted), method);
  const lastPatch = async (page) => {
    const sent = await calls(page, "set_config");
    return sent.length ? JSON.stringify(sent[sent.length - 1].params.patch) : "(none)";
  };
  const statusCalls = (page) => page.evaluate(() => window.__preview.fake.calls.filter((call) => call.method === "status").length);
  const bodyText = (page) => page.evaluate(() => document.body.innerText);
  const where = (page) => page.evaluate(() => ({ location: window.__preview.host().location, closed: window.__preview.record.sideMenusClosed }));
  /** Poll `probe` until it returns something truthy; false when it never does. */
  async function until(probe, timeout = 6000) {
    const end = Date.now() + timeout;
    for (;;) {
      if (await probe()) return true;
      if (Date.now() > end) return false;
      await sleep(100);
    }
  }

  /** Screenshot for the docs (what the screen shows) plus a full-length one for review. */
  async function shot(page, name, docs = true) {
    await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
    await page.mouse.move(0, 0);
    if (docs) await page.screenshot({ path: join(docsImages, `${name}.png`), fullPage: page.viewportSize().width === QAM.width });
    await page.screenshot({ path: join(extraShots, `${name}-full.png`), fullPage: true });
  }

  // ------------------------------------------------------------------ the Quick Access panel
  console.log("Quick Access panel");
  {
    const page = await open("panel", QAM, { scenario: "default" });
    check("shows that the service runs", await sees(page, "Service running"));
    check("shows the paired TV and that it is connected", await sees(page, "Living Room TV: connected"));
    check("shows how the last action went", await sees(page, "TV on: done (wake"));
    check("offers TV on / TV off / Switch input", (await button(page, "TV on").isVisible()) && (await button(page, "TV off").isVisible()) && (await button(page, "Switch to HDMI 2").isVisible()));
    await toggle(page, "TV off on suspend").waitFor();
    await shot(page, "panel");

    await toggle(page, "TV off on suspend").click();
    await sleep(300);
    check("a toggle sends the matching set_config patch", (await lastPatch(page)) === '{"events":{"suspend_tv_off":false}}', await lastPatch(page));
    check("the toggle shows the new value", (await toggle(page, "TV off on suspend").getAttribute("aria-checked")) === "false");
    await toggle(page, "TV on at start and wake").click();
    await sleep(300);
    check("'TV on at start and wake' sets both events", (await lastPatch(page)) === '{"events":{"boot_tv_on":false,"resume_tv_on":false}}', await lastPatch(page));
    await toggle(page, "Automatic actions").click();
    await sleep(300);
    check("the master switch sends enabled", (await lastPatch(page)) === '{"enabled":false}', await lastPatch(page));
    check("the panel says that automatic actions are off", await sees(page, "Automatic actions are off"));

    await button(page, "TV on").click();
    check("a running action is shown as in progress", await sees(page, "Working: TV on"));
    check("action buttons are disabled while the action runs", (await button(page, "TV off").isDisabled()) && (await page.getByRole("button", { name: "Working…" }).isDisabled()));
    check("the panel shows the action record", await sees(page, "TV on: done (manual", 8000));
    check("the action's outcome arrives as a notification", await page.evaluate(() => window.__preview.record.toasts.some((toast) => toast.title === "TV on: done" && toast.body.startsWith("TV on (Bluetooth)"))));
    check("the backend received tv_on", (await calls(page, "tv_on")).length === 1);
    check("buttons are enabled again afterwards", await button(page, "TV off").isEnabled());

    // Polling: about once a second, and it stops when the panel goes away.
    const before = (await calls(page, "status")).length;
    await sleep(3100);
    const during = (await calls(page, "status")).length - before;
    check("status is polled about once a second", during >= 2 && during <= 5, `${during} requests in 3.1 s`);
    await page.evaluate(() => window.__preview.setQuickAccessVisible(false));
    await sleep(400);
    const hidden = (await calls(page, "status")).length;
    await sleep(2300);
    check("polling stops when the Quick Access Menu closes", (await calls(page, "status")).length === hidden && (await page.evaluate(() => window.__preview.liveIntervals())) === 0);
    await page.evaluate(() => window.__preview.setQuickAccessVisible(true));
    check("and resumes when it opens again", await sees(page, "Service running"));
    await sleep(1200);
    await page.evaluate(() => window.__preview.unmount());
    await sleep(400);
    const after = (await calls(page, "status")).length;
    await sleep(2500);
    const state = await page.evaluate(() => ({ intervals: window.__preview.liveIntervals(), holders: window.__preview.store.activeHolders, status: window.__preview.fake.calls.filter((call) => call.method === "status").length }));
    check("after unmount no status request is made", state.status === after, `${state.status - after} more`);
    check("after unmount no timer is left behind", state.intervals === 0 && state.holders === 0, JSON.stringify(state));
    await page.context().close();
  }
  {
    const page = await open("slow status", QAM, { scenario: "default", statusDelay: "2600" });
    await sees(page, "Service running", 8000);
    await sleep(6000);
    const seen = await page.evaluate(() => ({ max: window.__preview.fake.maxStatusInFlight, count: window.__preview.fake.calls.filter((call) => call.method === "status").length }));
    check("status requests never overlap (a tick is skipped while one is in flight)", seen.max === 1 && seen.count <= 4, JSON.stringify(seen));
    await page.context().close();
  }

  console.log("Quick Access panel, reopened later");
  {
    const page = await open("panel reopened", QAM, { scenario: "default" });
    await sees(page, "Living Room TV: connected");
    await sleep(1500);
    await page.evaluate(() => window.__preview.setQuickAccessVisible(false));
    // Let a request that was on its way come back, so the panel's last word is "connected".
    await sleep(600);
    // While the menu is closed the service stops, and it is slow to say so.
    await page.evaluate(() => {
      window.__preview.fake.mode = "stopped";
      window.__preview.fake.statusDelay = 3000;
    });
    await sleep(3400);
    await page.evaluate(() => window.__preview.setQuickAccessVisible(true));
    await sleep(1200);
    const early = await bodyText(page);
    check(
      "a reopened panel does not pass the old status off as current",
      early.includes("Checking the background service") && !early.includes("connected") && !early.includes("Service running"),
      early.replace(/\n+/g, " | "),
    );
    check("and offers no TV action until the service has answered", (await button(page, "TV on").isDisabled()) && (await button(page, "TV off").isDisabled()));
    await shot(page, "panel-checking", false);
    const seen = [];
    const settled = await until(async () => {
      seen.push(await bodyText(page));
      return seen[seen.length - 1].includes("The background service is not running");
    }, 8000);
    check("then it says what is true now", settled, seen[seen.length - 1].replace(/\n+/g, " | "));
    check(
      "without quoting what was installed and running earlier",
      !seen.some((text) => text.includes("is running but does not answer")),
    );
    await page.context().close();
  }
  {
    const page = await open("panel busy", QAM, { scenario: "default" });
    await sees(page, "Service running");
    // An automatic action starts and "TV on" is pressed before the panel has heard of it.
    await page.evaluate(() => {
      void window.__preview.fake.rpc("event", { name: "resume", simulate: true });
      Array.from(document.querySelectorAll("button")).find((element) => element.textContent === "TV on").click();
    });
    check("a request refused because the service is busy says so in plain words", await sees(page, "TV on was not started. Busy with TV on (wake)"));
    const refused = await bodyText(page);
    check("not as an alarm, and not in the service's raw wording", !refused.includes("did not start") && !refused.includes("'tv_on'"), refused.replace(/\n+/g, " | "));
    check("the note goes away when the other action has finished", (await sees(page, "TV on: done (wake, just now", 8000)) && (await gone(page, "was not started")));
    await sleep(1500);
    const later = await bodyText(page);
    check("and no failure is left next to the success", !later.includes("was not started") && !later.includes("did not start"), later.replace(/\n+/g, " | "));
    await page.context().close();
  }

  console.log("Quick Access panel, ways out");
  {
    const page = await open("panel navigation", QAM, { scenario: "default" });
    await sees(page, "Service running");
    await button(page, "Remote").click();
    check("Remote opens the remote and closes the menu", (await sees(page, "Source list")) && JSON.stringify(await where(page)) === JSON.stringify({ location: `${ROUTE}/remote`, closed: 1 }), JSON.stringify(await where(page)));
    await page.evaluate(() => window.__preview.navigate(""));
    await button(page, "Settings").click();
    check("Settings opens the settings on Setup", (await sees(page, "1. The background service")) && JSON.stringify(await where(page)) === JSON.stringify({ location: `${ROUTE}/setup`, closed: 2 }), JSON.stringify(await where(page)));
    await page.evaluate(() => window.__preview.navigate(""));
    await page.locator(".qam-header button").click();
    check("the gear in the title does the same", (await sees(page, "1. The background service")) && JSON.stringify(await where(page)) === JSON.stringify({ location: `${ROUTE}/setup`, closed: 3 }), JSON.stringify(await where(page)));
    // The settings page keeps polling without being redrawn by it.
    await sleep(600);
    const lists = await page.evaluate(() => window.__preview.record.sidebarPageLists);
    const polls = await statusCalls(page);
    await sleep(2600);
    check(
      "the settings frame is not handed a new list of pages with every status",
      (await page.evaluate(() => window.__preview.record.sidebarPageLists)) === lists && (await statusCalls(page)) - polls >= 2,
      `${(await page.evaluate(() => window.__preview.record.sidebarPageLists)) - lists} new lists in ${(await statusCalls(page)) - polls} polls`,
    );

    // Unloading the plugin while its settings are open.
    const holdersBefore = await page.evaluate(() => window.__preview.store.activeHolders);
    await page.evaluate(() => window.__preview.dismount());
    await sleep(500);
    const afterDismount = await statusCalls(page);
    await sleep(2500);
    const left = await page.evaluate(() => ({
      routes: Object.keys(window.__preview.host().routes),
      holders: window.__preview.store.activeHolders,
      intervals: window.__preview.liveIntervals(),
    }));
    check("onDismount removes the route", holdersBefore >= 2 && left.routes.length === 0, JSON.stringify({ holdersBefore, ...left }));
    check("and stops polling: no timer, no holder, no further status request", left.holders === 0 && left.intervals === 0 && (await statusCalls(page)) === afterDismount, JSON.stringify(left));
    // A hold taken afterwards must work as on the first day (the count must not have gone negative).
    await page.evaluate(() => {
      window.__release = window.__preview.store.acquire();
    });
    await sleep(2300);
    const revived = await page.evaluate(() => ({ holders: window.__preview.store.activeHolders, intervals: window.__preview.liveIntervals() }));
    check("a later hold starts polling again", revived.holders === 1 && revived.intervals === 1 && (await statusCalls(page)) - afterDismount >= 2, JSON.stringify(revived));
    await page.evaluate(() => window.__release());
    check("and giving it back stops it", (await page.evaluate(() => window.__preview.liveIntervals())) === 0);
    await page.context().close();
  }
  {
    const page = await open("panel unloaded", QAM, { scenario: "default" });
    await sees(page, "Service running");
    await page.evaluate(() => window.__preview.dismount());
    await sleep(500);
    const afterDismount = await statusCalls(page);
    await sleep(2200);
    const left = await page.evaluate(() => ({ holders: window.__preview.store.activeHolders, intervals: window.__preview.liveIntervals() }));
    check("unloading the plugin with the panel open leaves nothing running", left.holders === 0 && left.intervals === 0 && (await statusCalls(page)) === afterDismount, JSON.stringify(left));
    await page.context().close();
  }

  console.log("Quick Access panel, no TV paired");
  {
    const page = await open("panel-unpaired", QAM, { scenario: "unpaired", pair: "2" });
    check("says that no TV is paired", await sees(page, "No TV paired"));
    check("offers Pair TV instead of the power buttons", (await button(page, "Pair TV").isVisible()) && (await button(page, "TV on").count()) === 0);
    await shot(page, "panel-unpaired");
    await button(page, "Pair TV").click();
    check("pairing opens with instructions for the TV", (await sees(page, "Pairing mode is open")) && (await sees(page, "Pair accessory")));
    check("the backend received pair_start", (await calls(page, "pair_start")).length === 1);
    await shot(page, "pair-dialog", false);
    check("pairing completes and names the TV", await sees(page, "Paired with Living Room TV", 8000));
    await shot(page, "pair-dialog-done", false);
    await button(page, "Done").click();
    check("a finished pairing is not cancelled", (await calls(page, "pair_cancel")).length === 0);
    check("the panel switches to the power buttons", (await sees(page, "TV on", 5000, true)) && (await button(page, "Pair TV").count()) === 0);
    await page.context().close();
  }
  {
    const page = await open("panel-unpaired navigation", QAM, { scenario: "unpaired" });
    await button(page, "Setup").click();
    check("Setup opens the checklist", (await sees(page, "No TV is paired")) && (await where(page)).location === `${ROUTE}/setup`);
    await page.context().close();
  }
  {
    // Pairing mode was opened elsewhere (the command line) and is still open.
    const page = await open("pairing open elsewhere", QAM, { scenario: "unpaired", pair: "never" });
    await sees(page, "No TV paired");
    await page.evaluate(() => window.__preview.fake.rpc("pair_start", {}));
    check("the panel says that pairing mode is open", await sees(page, "Pairing mode is open"));
    check("and Pair TV stays usable, so pairing can be followed or cancelled", await button(page, "Pair TV").isEnabled());
    await button(page, "Pair TV").click();
    await sees(page, "On the TV:");
    await button(page, "Cancel pairing").click();
    check("cancelling from there closes pairing mode", (await sees(page, "No TV paired")) && (await calls(page, "pair_cancel")).length === 1);
    await page.context().close();
  }
  {
    const page = await open("pairing cancelled", QAM, { scenario: "unpaired", pair: "never" });
    await button(page, "Pair TV").click();
    await sees(page, "Pairing mode is open");
    await button(page, "Cancel pairing").click();
    await sleep(300);
    check("Cancel pairing closes pairing mode on the service", (await calls(page, "pair_cancel")).length === 1 && (await page.getByRole("dialog").count()) === 0);
    await button(page, "Pair TV").click();
    await sees(page, "Pairing mode is open");
    await page.keyboard.press("Escape");
    await sleep(300);
    check("leaving the dialog with B / Escape cancels pairing too", (await calls(page, "pair_cancel")).length === 2 && (await page.getByRole("dialog").count()) === 0);
    await page.context().close();
  }
  {
    // Another Bluetooth pairing screen is open on this machine: the service warns while its
    // own pairing mode is open, which is when the warning can still be acted on.
    const page = await open("pairing next to another pairing screen", QAM, { scenario: "unpaired", pair: "never", classic: "1" });
    await button(page, "Pair TV").click();
    check("an open pairing mode shows what the service warns about", (await sees(page, "Pairing mode is open")) && (await sees(page, "Bluetooth problem: this machine is also discoverable over classic Bluetooth")));
    await shot(page, "pair-dialog-warning", false);
    await button(page, "Cancel pairing").click();
    await sleep(300);
    check("and the warning goes away with pairing mode", (await page.getByRole("dialog").count()) === 0 && (await page.getByText("discoverable over classic Bluetooth").count()) === 0);
    await page.context().close();
  }
  {
    const page = await open("pairing refused", QAM, { scenario: "unpaired", fail: "pair_start" });
    await button(page, "Pair TV").click();
    check("a refused pair_start shows the service's reason", (await sees(page, "Pairing mode could not be opened")) && (await sees(page, "Bluetooth is turned off in SteamOS")));
    check("and the dialog can be left or retried", (await button(page, "Try again").isVisible()) && (await button(page, "Close").isVisible()));
    await button(page, "Close").click();
    await page.context().close();
  }

  console.log("Quick Access panel, service missing");
  {
    const page = await open("panel-no-service", QAM, { scenario: "no-service" });
    check("says in plain words that the service is not installed", await sees(page, "The background service is not installed"));
    check("offers to install it", await button(page, "Install service").isVisible());
    check("and says that the plugin may still be installing it", await sees(page, "The plugin installs it when it loads"));
    await shot(page, "panel-no-service");
    await button(page, "Install service").click();
    check("Install calls install_service and the panel comes alive", (await sees(page, "Service running", 8000)) && (await calls(page, "install_service")).length === 1);
    check("the result is announced", await sees(page, "Install service: done"));
    await page.context().close();
  }
  {
    const page = await open("panel-no-service navigation", QAM, { scenario: "no-service" });
    await sees(page, "The background service is not installed");
    await button(page, "Diagnostics").click();
    check("Diagnostics is reachable without the service", (await sees(page, "Repair, restart, remove")) && JSON.stringify(await where(page)) === JSON.stringify({ location: `${ROUTE}/diagnostics`, closed: 1 }), JSON.stringify(await where(page)));
    await page.context().close();
  }
  {
    const page = await open("setup without the service", SCREEN, { scenario: "no-service", path: `${ROUTE}/setup` });
    await sees(page, "The background service is not installed");
    const text = await bodyText(page);
    check(
      "without the service, Setup does not claim to know the TV's state",
      text.includes("Pairing: not known while the service is not answering") &&
        text.includes("Connection: not known while the service is not answering") &&
        !text.includes("No TV is paired") &&
        !text.includes("The TV is not connected right now") &&
        !text.includes("Pairing mode stays open"),
    );
    check("and says why the tests cannot run", text.includes("The tests need the background service") && (await button(page, "Test wake").isDisabled()));
    await shot(page, "setup-no-service", false);
    await page.context().close();
  }
  {
    // What the panel shows once the plugin backend reports a running install (see the report:
    // main.py does not send `installing` yet).
    const page = await open("panel-installing", QAM, { scenario: "installing" });
    check("a running install is shown as progress", await sees(page, "Installing the background service"));
    const text = await bodyText(page);
    check("not as a missing service with an Install button", !text.includes("is not installed") && (await button(page, "Install service").count()) === 0, text.replace(/\n+/g, " | "));
    await shot(page, "panel-installing", false);
    check("and the panel comes alive when it is done", (await sees(page, "Service running", 10000)) && (await calls(page, "install_service")).length === 0);
    await page.context().close();
  }
  {
    const page = await open("panel-stopped", QAM, { scenario: "stopped" });
    check("a stopped service is reported with the installer's finding", (await sees(page, "The background service is not running")) && (await sees(page, "the service is inactive")));
    check("and Repair is offered", await button(page, "Repair service").isVisible());
    await shot(page, "panel-stopped", false);
    await page.context().close();
  }

  console.log("Failures");
  {
    const page = await open("panel-failed", QAM, { scenario: "failed" });
    check("a failed automatic action is shown with its reason", (await sees(page, "TV off failed")) && (await sees(page, "the TV did not connect within 3s")));
    await shot(page, "panel-failed", false);
    await button(page, "Show what was tried").click();
    check("the step trace is one press away", (await page.getByRole("dialog").getByText("display check: DP-1").count()) === 1);
    await button(page, "Close").click();
    await page.context().close();
  }
  {
    const page = await open("failing action", QAM, { scenario: "default", fail: "tv_off" });
    await sees(page, "Service running");
    await button(page, "TV off").click();
    check("a failing action is announced as failed", await sees(page, "TV off: failed", 8000));
    check("and stays on the panel with the reason", (await sees(page, "TV off failed")) && (await page.getByText("the TV did not connect within 3s").count()) >= 1);
    check("the notification is marked critical", await page.evaluate(() => window.__preview.record.toasts.some((toast) => toast.critical && toast.title === "TV off: failed")));
    await page.context().close();
  }

  // ------------------------------------------------------------------ the settings pages
  const settings = (label, page, extra = {}) => open(label, SCREEN, { scenario: "default", path: `${ROUTE}/${page}`, ...extra });
  const sidebar = (page, name) => page.locator(".sidebar-item", { hasText: name }).click();

  console.log("Settings: Setup");
  {
    const page = await settings("setup", "setup", { pair: "2" });
    check("lists the TV settings to change", (await sees(page, "Quick start and Screenless service: On")) && (await sees(page, "Power On Behavior: Last Input Used")));
    check("ticks what is known: service, pairing, connection", (await sees(page, "The service is running")) && (await sees(page, "Paired with Living Room TV")) && (await sees(page, "The TV is connected")));
    await shot(page, "setup");
    await button(page, "Test wake").click();
    check("Test wake runs event resume with simulate", await (async () => {
      await sees(page, "Test wake: done", 8000);
      const sent = await calls(page, "event");
      return sent.length === 1 && sent[0].params.name === "resume" && sent[0].params.simulate === true;
    })());
    await page.getByRole("button", { name: /^Show steps/ }).first().click();
    check("and shows the action record with its steps", (await sees(page, "Bluetooth: sent WAKE")) && (await sees(page, "display check: DP-1")));
    await button(page, "Test sleep").click();
    check("Test sleep runs event suspend with simulate", await (async () => {
      await sees(page, "Test sleep: done", 8000);
      const sent = await calls(page, "event");
      return sent.length === 2 && sent[1].params.name === "suspend" && sent[1].params.simulate === true;
    })());
    await shot(page, "setup-tested", false);
    await button(page, "Forget TV").click();
    check("Forget TV asks first and says to remove the remote on the TV", (await sees(page, "Forget Living Room TV?")) && (await sees(page, "Remotes & Accessories")) && (await calls(page, "forget_tv")).length === 0);
    await shot(page, "forget-dialog", false);
    await button(page, "Forget TV").last().click();
    check("confirming forgets the TV", (await sees(page, "No TV is paired")) && (await calls(page, "forget_tv")).length === 1);
    check("the result is shown with its age", (await sees(page, "Forget TV: done")) && (await sees(page, "just now")));
    await button(page, "Pair TV").click();
    await sees(page, "Paired with Living Room TV.", 8000);
    await button(page, "Done").click();
    check("a result the state has since overtaken is not left standing", (await sees(page, "Paired with Living Room TV")) && (await gone(page, "Forget TV: done")));
    await page.context().close();
  }

  console.log("Settings: Automation");
  {
    const page = await settings("automation", "automation");
    await toggle(page, "Turn the TV off on restart").waitFor();
    for (const name of ["Turn the TV on at start", "Switch the TV input after start", "Turn the TV on at wake", "Switch the TV input after wake", "Turn the TV off on suspend", "Turn the TV off on shutdown", "Turn the TV off on restart"]) {
      check(`has a switch for "${name}"`, (await toggle(page, name).count()) === 1);
    }
    await shot(page, "automation");
    await toggle(page, "Turn the TV off on restart").click();
    await sleep(300);
    check("an event toggle sends its own patch", (await lastPatch(page)) === '{"events":{"reboot_tv_off":true}}', await lastPatch(page));
    await page.getByRole("combobox", { name: "Require an external display" }).selectOption({ label: "Always" });
    await sleep(300);
    check("the display requirement is a choice", (await lastPatch(page)) === '{"display":{"require_external":"yes"}}', await lastPatch(page));
    await page.getByRole("combobox", { name: "Only for this display" }).selectOption({ label: "DP-1 (TCL 65Q750G)" });
    await sleep(300);
    check("the display is chosen from the connected ones", (await lastPatch(page)) === '{"display":{"edid_match":"TCL 65Q750G"}}', await lastPatch(page));
    const slider = page.getByRole("slider", { name: "Wait for the display" });
    check("the slider takes its limits from the schema", (await slider.getAttribute("min")) === "0" && (await slider.getAttribute("max")) === "60");
    await slider.fill("12");
    await sleep(900);
    check("a slider sends one patch when it comes to rest", (await lastPatch(page)) === '{"display":{"wait_seconds":12}}', await lastPatch(page));
    await page.evaluate(() => window.__preview.store.setConfig({ display: { wait_seconds: 999 } }));
    check("a rejected setting shows the service's message", (await sees(page, "That change was not accepted")) && (await sees(page, "display.wait_seconds: must be at most 60")));
    check("and the value goes back to what the service has", (await slider.inputValue()) === "12");
    await button(page, "Dismiss").click();
    await page.getByRole("combobox", { name: "Require an external display" }).selectOption({ label: "Never" });
    await sees(page, "display check is off");
    check(
      "with the display check off, the settings that refine it are disabled and say why",
      (await page.getByRole("combobox", { name: "Only for this display" }).isDisabled()) &&
        (await slider.isDisabled()) &&
        (await page.getByText("Not used while Require an external display is set to Never.").count()) === 2,
    );
    check("the headline says actions may run", await sees(page, "Automatic actions may run right now"));
    await toggle(page, "Automatic actions").click();
    check("with the master switch off, the headline says so instead", (await sees(page, "Automatic actions are turned off")) && (await gone(page, "Automatic actions may run right now")));
    await shot(page, "automation-off", false);
    await page.context().close();
  }

  console.log("Settings: Input");
  {
    const page = await settings("input", "input");
    check("recommends Last Input Used", await sees(page, "Power On Behavior to Last Input Used"));
    check("shows each preset with the macro for the chosen port", (await sees(page, "TCL source row (2023 and newer)")) && (await sees(page, "F3 1.5s LEFT*8 RIGHT*2 OK")) && (await sees(page, "F3 1.5s UP*8 DOWN*2 OK")));
    check("says the presets are not confirmed", await sees(page, "Reported, not confirmed"));
    await shot(page, "input");

    const macro = page.getByRole("textbox", { name: "Macro" });
    await macro.fill("HOME 1s BANANA");
    check("a bad macro shows the service's validation error", (await sees(page, "This macro is not valid")) && (await sees(page, "unknown key 'BANANA'")));
    check("a bad macro cannot be tried or saved", (await button(page, "Try it now").isDisabled()) && (await button(page, "Save").isDisabled()));
    await shot(page, "input-bad-macro", false);
    await macro.fill("HOME 2s F3 1.5s LEFT*8");
    await button(page, "RIGHT").click();
    await button(page, "Repeat once per HDMI number").click();
    await button(page, "OK").click();
    check("the key palette appends steps", (await macro.inputValue()) === "HOME 2s F3 1.5s LEFT*8 RIGHT*{port} OK", await macro.inputValue());
    check("a good macro is validated with its duration", (await sees(page, "This macro is valid")) && (await sees(page, "HOME 2s F3 1.5s LEFT*8 RIGHT*2 OK")));
    await button(page, "Try it now").click();
    check("Try it now runs run_macro with the text", await (async () => {
      await sees(page, "Macro: done", 8000);
      const sent = await calls(page, "run_macro");
      return sent.length === 1 && sent[0].params.macro === "HOME 2s F3 1.5s LEFT*8 RIGHT*{port} OK";
    })());
    await button(page, "Save").click();
    await sleep(400);
    check("Save stores the macro and selects it", (await lastPatch(page)) === '{"input":{"macro":"HOME 2s F3 1.5s LEFT*8 RIGHT*{port} OK","preset":"custom"}}', await lastPatch(page));

    const method = page.getByRole("combobox", { name: "Switch the input" });
    await method.selectOption({ label: "Off" });
    await sleep(300);
    check("turning input switching off needs no confirmation", (await lastPatch(page)) === '{"input":{"method":"none"}}', await lastPatch(page));
    check("while it is off, the chosen macro is 'selected', not 'in use'", (await sees(page, "Nothing. Input switching is off.")) && (await sees(page, "Your macro is selected")) && !/\bin use\b/i.test(await bodyText(page)));
    check("Switch input now is disabled while it is off", await button(page, "Switch input now").isDisabled());
    await method.selectOption({ label: "Bluetooth macro" });
    check("turning the macro on first shows what will be sent", (await sees(page, "Switch input with a Bluetooth macro?")) && (await page.getByRole("dialog").getByText("HOME 2s F3 1.5s LEFT*8 RIGHT*2 OK").count()) === 1);
    check("nothing is changed before the user agrees", (await lastPatch(page)) === '{"input":{"method":"none"}}');
    const whenText = await page.getByRole("dialog").innerText();
    check("the dialog says when: TV on, Switch input, and by itself after start or wake", whenText.includes("when you press TV on or Switch input, and by itself after SteamOS starts or wakes."), whenText.replace(/\n+/g, " | "));
    await shot(page, "input-confirm", false);
    await button(page, "Turn on").click();
    await sleep(300);
    check("agreeing sends the method", (await lastPatch(page)) === '{"input":{"method":"macro"}}', await lastPatch(page));
    check("and now the macro is 'in use'", await sees(page, "Your macro is in use"));
    await method.selectOption({ label: "Wi-Fi" });
    check("Wi-Fi cannot be chosen before the assist is paired", (await sees(page, "The Wi-Fi assist is not set up yet")) && (await button(page, "Turn on").isDisabled()));
    await button(page, "Cancel").click();
    await sleep(300);
    check("cancelling leaves the method alone", (await lastPatch(page)) === '{"input":{"method":"macro"}}' && (await method.evaluate((element) => element.selectedOptions[0].label)) === "Bluetooth macro");
    // The stand-in dropdown keeps showing its own last pick, as Steam's may; the row must not.
    const shownMethod = await page.locator('.field[data-label="Switch the input"] .dropdown-value').innerText();
    check("and the row shows the setting, not the pick that was cancelled", shownMethod === "Bluetooth macro", shownMethod);
    await page.getByRole("combobox", { name: "This machine is on" }).selectOption({ label: "HDMI 3" });
    check("changing the port updates every preset's macro", (await sees(page, "F3 1.5s LEFT*8 RIGHT*3 OK")) && (await lastPatch(page)) === '{"input":{"hdmi_port":3}}');
    await button(page, "Switch input now").click();
    check("Switch input now shows the action record", await sees(page, "Switch input: done", 8000));
    await page.context().close();
  }
  {
    // A slow service: the status the page has is from before the last settings change.
    const page = await settings("input, slow status", "input", { statusDelay: "900" });
    const method = page.getByRole("combobox", { name: "Switch the input" });
    await method.waitFor();
    await sees(page, "F3 1.5s LEFT*8 RIGHT*2 OK");
    await method.selectOption({ label: "Off" });
    await page.evaluate(() => window.__preview.store.setConfig({ events: { resume_switch_input: false } }));
    await page.getByRole("combobox", { name: "This machine is on" }).selectOption({ label: "HDMI 4" });
    await method.selectOption({ label: "Bluetooth macro" });
    const dialog = page.getByRole("dialog");
    await dialog.waitFor();
    const turnOn = dialog.getByRole("button", { name: "Turn on", exact: true });
    const first = { text: await dialog.innerText(), enabled: await turnOn.isEnabled() };
    check(
      "the confirmation cannot be agreed to while what will be sent is still the old answer",
      !first.enabled && first.text.includes("Checking what will be sent") && !first.text.includes("RIGHT*2"),
      JSON.stringify(first),
    );
    check("it becomes available once the service has caught up", await until(() => turnOn.isEnabled(), 6000));
    const final = await dialog.innerText();
    check("and then shows the keys for the port it names", final.includes("for HDMI 4:") && final.includes("F3 1.5s LEFT*8 RIGHT*4 OK") && !final.includes("RIGHT*2"), final.replace(/\n+/g, " | "));
    check("and which of start and wake are switched on", final.includes("and by itself after SteamOS starts. After a wake it is turned off (see Automation)."), final.replace(/\n+/g, " | "));
    await turnOn.click();
    await sleep(300);
    check("agreeing then sends the method", (await lastPatch(page)) === '{"input":{"method":"macro"}}', await lastPatch(page));

    // The same with another preset on the same port: only the keys change.
    await method.selectOption({ label: "Off" });
    await page.getByRole("button", { name: "Use", exact: true }).nth(3).click();
    await method.selectOption({ label: "Bluetooth macro" });
    await dialog.waitFor();
    const second = { text: await dialog.innerText(), enabled: await turnOn.isEnabled() };
    check(
      "nor while a newly chosen preset has not arrived in what the service reports",
      !second.enabled && second.text.includes("Checking what will be sent") && !second.text.includes("LEFT*8"),
      JSON.stringify(second),
    );
    await until(() => turnOn.isEnabled(), 6000);
    const secondFinal = await dialog.innerText();
    check("it then shows the new preset's keys", secondFinal.includes("F3 1.5s UP*8 DOWN*4 OK") && !secondFinal.includes("LEFT*8"), secondFinal.replace(/\n+/g, " | "));
    await button(page, "Cancel").click();
    await page.context().close();
  }

  console.log("Settings: Remote");
  {
    const page = await settings("remote", "remote");
    await button(page, "OK").waitFor();
    check("has no power toggle", (await page.getByRole("button", { name: /power/i }).count()) === 0);
    await shot(page, "remote");
    await button(page, "OK").click();
    check("a key press sends send_key", await (async () => {
      await sees(page, "Sent OK");
      const sent = await calls(page, "send_key");
      return sent.length === 1 && sent[0].params.key === "OK";
    })());
    await button(page, "Source list").click();
    await sees(page, "Sent Source list (F3)");
    await button(page, "Wake").click();
    await sees(page, "Sent Wake");
    check("Source list sends F3 and Wake sends WAKE", JSON.stringify((await calls(page, "send_key")).map((call) => call.params.key)) === '["OK","F3","WAKE"]');
    await page.context().close();
  }
  {
    const page = await settings("remote, TV unreachable", "remote", { fail: "send_key" });
    await button(page, "OK").waitFor();
    await button(page, "OK").click();
    check("a key that fails says why, on the page", await sees(page, "OK was not sent: the TV is not connected over Bluetooth"));
    await shot(page, "remote-error", false);
    await page.context().close();
  }

  console.log("Settings: Bluetooth");
  {
    const page = await settings("bluetooth", "bluetooth");
    check("shows the live Bluetooth state", (await sees(page, "hci0 (11:22:33:44:55:66)")) && (await sees(page, "Ready for keys")) && (await sees(page, "TV subscribed")) && (await sees(page, "Delivery")));
    check("groups the rarely needed settings under Advanced", (await sees(page, "Advanced", 2000, true)) && (await sees(page, "Wake beacon (experimental)")));
    check(
      "the two settings that need a restart say so",
      (await page.getByText("A change takes effect when the service restarts (Diagnostics).").count()) === 2 && (await sees(page, "more than one adapter. A change takes effect when the service restarts", 2000)),
    );
    await shot(page, "bluetooth");
    await toggle(page, "Keep calling the TV").click();
    await sleep(300);
    check("Keep calling the TV is bluetooth.reconnect_on_drop", (await lastPatch(page)) === '{"bluetooth":{"reconnect_on_drop":false}}', await lastPatch(page));
    const delivery = page.getByRole("combobox", { name: "Direct delivery" });
    const offered = (await delivery.locator("option").allTextContents()).map((text) => text.trim());
    check("Direct delivery offers its four choices, the careful one first", JSON.stringify(offered) === JSON.stringify(["Automatic", "On", "Off", "Always"]), JSON.stringify(offered));
    check("and says in plain words what each one does", (await sees(page, "only on Linux 6.17 or newer", 2000)) && (await sees(page, "On: Used when the TV needs it, on any kernel.", 2000)) && (await sees(page, "has to be paired again after each restart", 2000)) && (await sees(page, "Always: Used for every key", 2000)));
    await delivery.selectOption({ label: "On" });
    await sleep(300);
    const shownDelivery = await delivery.evaluate((element) => element.selectedOptions[0].label);
    check("On is bluetooth.raw_notify = on, a value the service accepts", (await lastPatch(page)) === '{"bluetooth":{"raw_notify":"on"}}' && shownDelivery === "On" && (await page.getByText("bluetooth.raw_notify: must be").count()) === 0, `${await lastPatch(page)} / ${shownDelivery}`);
    await page.getByRole("combobox", { name: "Wake beacon (experimental)" }).selectOption({ label: "Custom" });
    await sleep(300);
    await page.getByRole("button", { name: "Edit", exact: true }).last().click();
    await page.getByRole("dialog").getByRole("textbox").fill("xyz");
    await button(page, "Save").click();
    check("text is entered in a dialog; bad input is rejected with the reason", await sees(page, "bluetooth.wake_beacon_data: must be hexadecimal bytes"));
    await button(page, "Dismiss").click();
    await page.getByRole("button", { name: "Edit", exact: true }).first().click();
    await page.getByRole("dialog").getByRole("textbox").fill("Deck Remote");
    await button(page, "Save").click();
    await sleep(300);
    check("the pairing name is saved from its dialog", (await lastPatch(page)) === '{"bluetooth":{"device_name":"Deck Remote"}}', await lastPatch(page));
    await button(page, "Disconnect").click();
    check("Disconnect drops the link", (await sees(page, "Disconnect: done")) && (await calls(page, "disconnect")).length === 1);
    await sidebar(page, "Remote");
    await button(page, "OK").waitFor();
    await sidebar(page, "Bluetooth");
    await sees(page, "Ready for keys");
    check("a result that was seen is gone when the user comes back to the page", (await page.getByText("Disconnect: done").count()) === 0);
    await page.context().close();
  }

  console.log("Settings: Wi-Fi assist");
  {
    const page = await settings("wifi", "wifi");
    check("explains that the assist is optional", (await sees(page, "Optional", 5000, true)) && (await sees(page, "Not paired", 5000, true)));
    const claims = await bodyText(page);
    check(
      "what is only reported is worded as reported",
      claims.includes("Reported to work on TCL TVs.") && !claims.includes("Works on TCL") && !claims.includes("TCL TVs ignore it"),
    );
    const sectionOf = (title) => page.locator(".dialog-section").filter({ has: page.locator(".dialog-section-header", { hasText: title }) });
    check(
      "the Wi-Fi timeout sits with input switching, which is what it is used for",
      (await sectionOf("Switching the input").getByRole("slider", { name: "Keep trying to switch for" }).count()) === 1 &&
        (await sectionOf("Power").getByRole("slider").count()) === 0,
    );
    check("the test button says what is missing before the assist is paired", (await sees(page, "Pair the Wi-Fi assist first.")) && (await button(page, "Switch input now").isDisabled()));
    await button(page, "Search").click();
    check("Search lists the TVs that answered", (await sees(page, "Address: 192.168.1.50", 8000)) && (await calls(page, "wifi_discover")).length === 1);
    await shot(page, "wifi");
    await button(page, "Pair").first().click();
    check("pairing asks the TV for a code", (await sees(page, "Enter it here")) && (await calls(page, "wifi_pair_start"))[0]?.params.host === "192.168.1.50");
    await shot(page, "wifi-code", false);
    await button(page, "Cancel").click();
    await sleep(300);
    check("cancelling calls wifi_pair_cancel", (await calls(page, "wifi_pair_cancel")).length === 1);

    // Closing the dialog while the code is being checked, and the TV turns the code down.
    await button(page, "Pair").first().click();
    await sees(page, "Enter it here");
    await page.getByRole("dialog").getByRole("textbox").fill("000000");
    await button(page, "Finish pairing").click();
    await button(page, "Cancel").click();
    check(
      "closing the dialog while the code is checked still closes the pairing on the service",
      (await until(async () => (await calls(page, "wifi_pair_cancel")).length === 2)) &&
        (await page.evaluate(() => {
          const made = window.__preview.fake.calls.map((call) => call.method);
          return made.lastIndexOf("wifi_pair_cancel") > made.lastIndexOf("wifi_pair_finish");
        })),
    );
    check("so the page is not left waiting on a pairing nobody can reach", (await sees(page, "Not paired", 5000, true)) && (await button(page, "Pair").first().isEnabled()));

    // A pairing that was left open elsewhere (the command line).
    await page.evaluate(() => window.__preview.fake.rpc("wifi_pair_start", { host: "192.168.1.50" }));
    check("a pairing left open elsewhere is shown", await sees(page, "Pairing with 192.168.1.50"));
    check("it does not lock the Pair buttons", await button(page, "Pair").first().isEnabled());
    await button(page, "Cancel pairing").click();
    check("and it can be cancelled from the page", (await sees(page, "Not paired", 5000, true)) && (await calls(page, "wifi_pair_cancel")).length === 3);

    await button(page, "Pair").first().click();
    await sees(page, "Enter it here");
    await page.getByRole("dialog").getByRole("textbox").fill("000000");
    await button(page, "Finish pairing").click();
    check("a wrong code shows the reason and can be retried", (await sees(page, "the TV did not accept the code")) && (await button(page, "Start again").isVisible()));
    await button(page, "Start again").click();
    await sees(page, "Enter it here");
    await page.getByRole("dialog").getByRole("textbox").fill("12ab34");
    await button(page, "Finish pairing").click();
    check("the right code finishes the pairing", await (async () => {
      await sees(page, "over Wi-Fi.", 8000);
      const sent = await calls(page, "wifi_pair_finish");
      return sent.length === 3 && sent[2].params.code === "12ab34";
    })());
    await button(page, "Done").click();
    check("the page shows the pairing and the assist is on", (await sees(page, "Paired with Living Room TV")) && (await toggle(page, "Use the Wi-Fi assist").getAttribute("aria-checked")) === "true");
    await button(page, "Check connection").click();
    check("Check connection reports what wifi_test found", await sees(page, "Living Room TV answers over Wi-Fi; it is on"));
    check("a chosen link set is 'selected' while the input is not switched over Wi-Fi", (await sees(page, "Selected", 5000, true)) && (await page.getByText("In use", { exact: true }).count()) === 0);
    await page.getByRole("button", { name: "Use", exact: true }).nth(1).click();
    await sleep(300);
    check("a link set can be chosen", (await lastPatch(page)) === '{"wifi":{"link_preset":"tcl-realtek"}}', await lastPatch(page));
    // Wi-Fi switching can be tried before it is turned on: input.method is still the macro.
    await until(() => button(page, "Switch input now").isEnabled());
    await button(page, "Switch input now").click();
    check("the test asks over Wi-Fi only, whatever the method is", await (async () => {
      await sees(page, "Switch input over Wi-Fi: done", 8000);
      const sent = await calls(page, "switch_input");
      const methods = (await calls(page, "set_config")).filter((call) => call.params.patch.input?.method);
      return sent.length === 1 && sent[0].params.via === "wifi" && methods.length === 0;
    })());
    await page.getByRole("button", { name: /^Show steps/ }).first().click();
    check("and shows the link that was opened", (await page.getByText(/^Wi-Fi: opened /).count()) === 1);
    await shot(page, "wifi-paired", false);
    await button(page, "Unpair").click();
    await button(page, "Unpair").last().click();
    check("Unpair asks first, then unpairs", (await sees(page, "Not paired", 5000, true)) && (await calls(page, "wifi_unpair")).length === 1);
    await page.context().close();
  }

  console.log("Settings: Diagnostics");
  {
    const page = await settings("diagnostics", "diagnostics");
    check("shows what is installed and running", (await sees(page, "Yes, version")) && (await sees(page, "Starts with SteamOS")));
    check("shows the action history", (await sees(page, "TV on: done")) && (await sees(page, "TV off: done")));
    check("shows recent log lines", await sees(page, "service started"));
    check(
      "says which version the plugin is and how an update is installed, with the address to type",
      (await sees(page, "This plugin is version 0.1.0")) &&
        (await sees(page, "Decky settings > Developer > Install Plugin from URL")) &&
        (await sees(page, UPDATE_ADDRESS, 1000, true)),
    );
    check("and that nothing looks for updates by itself", await sees(page, "It does not look for updates, and nothing is updated by itself."));
    check(
      "and that the address has to be carried to Decky's page, with the way to notice a mistyped ending",
      (await sees(page, "The address is not on screen while you type it: write it down or have it on a phone")) &&
        (await sees(page, 'Decky then asks "Install tcl-me-remo"; if it shows any other name, the end of the address')),
    );
    check(
      "and that an update can restart the service, and where to look if keys then do not arrive",
      (await sees(page, "An update that changes the service restarts it")) && (await sees(page, "the Bluetooth page says why")),
    );
    check("the log of a running service is its own, not the journal", (await calls(page, "service_log")).length === 0);
    await shot(page, "diagnostics");
    {
      // The same page a little later, in a fresh browser context, must give the same picture.
      await sleep(1300);
      const again = await settings("diagnostics again", "diagnostics");
      await sees(again, "Yes, version");
      await sees(again, "TV off: done");
      await sees(again, "service started");
      await again.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
      await again.mouse.move(0, 0);
      const second = await again.screenshot();
      check("the documentation screenshots do not depend on when they are taken", second.equals(readFileSync(join(docsImages, "diagnostics.png"))));
      await again.context().close();
    }
    const logsBefore = (await calls(page, "logs")).length;
    await button(page, "Refresh").click();
    await sleep(400);
    check("Refresh reads the log again", (await calls(page, "logs")).length === logsBefore + 1);
    await page.getByRole("button", { name: /^Show steps/ }).first().click();
    check("history entries expand to their steps", await sees(page, "Bluetooth: playing macro: F3 1.5s LEFT*8 RIGHT*2 OK"));
    const entries = await page.getByRole("button", { name: /^(Show|Hide) steps/ }).count();
    await page.evaluate(() => window.__preview.fake.rpc("tv_off", {}));
    await until(async () => (await page.getByRole("button", { name: /^(Show|Hide) steps/ }).count()) === entries + 1);
    check(
      "an opened entry stays open when a new action is recorded",
      (await page.getByRole("button", { name: "Hide steps", exact: true }).count()) === 1 && (await sees(page, "Bluetooth: playing macro: F3 1.5s LEFT*8 RIGHT*2 OK", 1000)),
    );
    check("no settings problem is shown when there is none", (await page.getByText("Settings problems").count()) === 0);

    // The doctor's report, which otherwise takes "tcl-me-remo doctor" in a terminal.
    check("the set-up check is not run until it is asked for", (await calls(page, "doctor")).length === 0 && (await button(page, "Run check").isVisible()));
    await button(page, "Run check").click();
    check("while it runs the button says so and cannot be pressed twice", await page.getByRole("button", { name: "Checking…" }).isDisabled());
    check(
      "Run check shows the doctor's report: every check with its verdict",
      (await sees(page, "All 12 checks passed.")) && (await sees(page, "OK · TV link")) && (await sees(page, "Living Room TV is connected (BlueZ)")) && (await sees(page, "OK · suspend lock")),
    );
    check(
      "with what it ran on and what is paired, as the command prints them",
      (await sees(page, "tcl-me-remo 0.1.0 on SteamOS 3.7.13")) && (await sees(page, "Device AA:BB:CC:DD:EE:FF Living Room TV")) && (await sees(page, "Connected displays: HDMI-A-1")),
    );
    check(
      "a setting the report tells the reader to change is named as the panel labels it",
      (await sees(page, "or set Direct delivery (bluetooth.raw_notify; in the panel: Bluetooth page, Advanced) to 'on'")) &&
        (await page.getByText("set raw_notify").count()) === 0,
    );
    check("it was one request to the backend", (await calls(page, "doctor")).length === 1 && (await button(page, "Check again").isEnabled()));
    await shot(page, "diagnostics-check", false);

    await button(page, "Remove").click();
    check("Remove asks first and offers to delete the settings", (await sees(page, "Remove the background service?")) && (await toggle(page, "Also delete settings and the pairing").count()) === 1 && (await calls(page, "remove_service")).length === 0);
    check(
      "deleting the settings is said to remove the Bluetooth pairing on this machine as well",
      await sees(page, "removes the Bluetooth pairing with the TV from this machine", 1000),
    );
    await toggle(page, "Also delete settings and the pairing").click();
    await shot(page, "remove-dialog", false);
    await button(page, "Remove service and settings").click();
    check("confirming calls remove_service with the choice", await (async () => {
      await sees(page, "The background service is not installed", 8000);
      const sent = await calls(page, "remove_service");
      return sent.length === 1 && sent[0].params.purge === true;
    })());
    check(
      "the result of the removal says what comes next: uninstalling the plugin in Decky",
      (await sees(page, "Remove service: done")) &&
        (await sees(page, "removed settings and state in /var/lib/tcl-me-remo. Now uninstall the plugin in Decky (Decky settings > Plugins): otherwise it installs the service again")),
    );
    check("without the service the page shows its state, not stale data", (await gone(page, "What happened")) && (await button(page, "Install").isVisible()));
    check(
      "without the service the log is read from the system journal, and the page says so",
      (await sees(page, "The service's last lines in the system journal")) && (await sees(page, "Stopped tcl-me-remo.service")) && (await calls(page, "service_log")).length >= 1,
    );
    await button(page, "Check again").click();
    check(
      "and the set-up check still works, and names the problem",
      (await sees(page, "8 checks: 1 problem, 1 not known.")) && (await sees(page, "Problem · service")) && (await sees(page, "not reachable: cannot reach the tcl-me-remo service")),
    );
    await shot(page, "diagnostics-no-service", false);
    await button(page, "Install service").click();
    check("and the service can be installed again from there", await sees(page, "Yes, version", 8000));
    check("after which the log is the service's own again", (await sees(page, "Recent log lines")) && (await gone(page, "The service's last lines in the system journal")));
    await page.context().close();
  }
  {
    const page = await settings("diagnostics, a stopped service", "diagnostics", { scenario: "stopped" });
    check("a stopped service's journal is shown without being asked for", (await sees(page, "The background service is not running")) && (await sees(page, "Stopped tcl-me-remo.service")));
    await page.context().close();

    const never = await settings("diagnostics, a service that never was", "diagnostics", { scenario: "no-service" });
    check("a journal without lines from the service is said to be that", await sees(never, "The system journal has no lines from the service"));
    await never.context().close();

    const failing = await settings("diagnostics, the tools fail", "diagnostics", { scenario: "stopped", fail: "doctor,service_log" });
    check("a journal that cannot be read is reported, not hidden", await sees(failing, "The log could not be read: [Errno 2] No such file or directory: 'journalctl'"));
    await button(failing, "Run check").click();
    check("a set-up check that fails says why and can be tried again", (await sees(failing, "The check could not be made: timed out after 75s")) && (await button(failing, "Run check").isEnabled()));
    await failing.context().close();
  }

  console.log("Settings: Diagnostics, a problem with the settings file");
  {
    const setting = { events: { reboot_tv_off: true } };
    const save = (page) => page.evaluate((patch) => window.__preview.fake.rpc("set_config", { patch }), setting);

    const value = await settings("diagnostics, a bad value", "diagnostics", { configProblem: "value" });
    check("a value that could not be used is shown", (await sees(value, "Settings problems")) && (await sees(value, "input.hdmi_port: must be at most 4")));
    check("with the way to settle it", await sees(value, "Changing any setting writes a clean settings file."));
    await save(value);
    check("and it is settled by the next save", await gone(value, "Settings problems"));
    await value.context().close();

    const kept = await settings("diagnostics, the file was moved aside", "diagnostics", { configProblem: "kept" });
    check("a settings file that was moved aside is shown with where it is", (await sees(kept, "Settings problems")) && (await sees(kept, "/var/lib/tcl-me-remo/config.json.broken")));
    check(
      "the page says that the service runs on defaults, and the way on that needs no terminal",
      (await sees(kept, "The service is running on default settings")) && (await sees(kept, "set things up again on these pages, and pair the TV again if")) && (await sees(kept, "ends when the service next restarts (Restart, at the top of")),
    );
    check("and that getting the old file back is the one thing that takes a terminal", await sees(kept, "mending that file by hand, which takes a"));
    check(
      "and names the README section that has those steps, under the heading the README gives it",
      (await sees(kept, 'see "A settings file that was moved aside" in the README')) &&
        /^### A settings file that was moved aside$/m.test(readFileSync(join(dirname(here), "README.md"), "utf8")),
    );
    check("and does not claim that changing a setting clears it", (await kept.getByText("Changing any setting writes a clean settings file.").count()) === 0);
    await shot(kept, "diagnostics-settings-kept", false);
    const statusBefore = await statusCalls(kept);
    await save(kept);
    await until(async () => (await statusCalls(kept)) >= statusBefore + 2);
    check("which is true: the entry is still there after a setting was saved", (await sees(kept, "config.json.broken", 1000)) && (await sees(kept, "The service is running on default settings", 1000)));
    await kept.context().close();
  }

  await browser.close();
}

try {
  await main();
} catch (error) {
  failures.push(`the run stopped early: ${error.stack ?? error}`);
}

console.log("");
if (failures.length) {
  console.log(`${failures.length} FAILED, ${passed} passed`);
  for (const failure of failures) console.log(`  - ${failure}`);
  process.exit(1);
}
console.log(`all ${passed} checks passed; screenshots are in docs/images/ (more in preview/.build/shots/)`);
