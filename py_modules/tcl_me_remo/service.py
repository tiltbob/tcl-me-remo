"""The brain: decides what to do on each lifecycle event and runs the TV actions.

``Service`` owns the configuration and talks to two transports through the protocols in
``interfaces.py``: the emulated Bluetooth remote (primary) and the optional Wi-Fi assist.
It knows nothing about D-Bus, sockets or systemd, which keeps every decision in this file
testable with fakes.

Lifecycle events
    boot, resume            -> turn the TV on, then switch its input   ("up" events)
    suspend, shutdown,
    reboot                  -> turn the TV off                          ("down" events)

Events can reach the service by more than one route (logind signals, a systemd unit hooked
to sleep.target, a clock-jump watchdog).  Every route may fire for the same real event, so
event handling is idempotent: see ``_handle_up`` and ``_handle_down``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__, display, keys, macro, tvdata
from . import config as config_mod
from .config import Config, ConfigError
from .interfaces import (
    KEYCODE_SLEEP,
    KEYCODE_TV_INPUT_HDMI_1,
    KEYCODE_WAKEUP,
    Remote,
    TransportError,
    WifiAssist,
)
from .ipc import RpcError

log = logging.getLogger(__name__)

UP_EVENTS = ("boot", "resume")
DOWN_EVENTS = ("suspend", "shutdown", "reboot")
EVENTS = UP_EVENTS + DOWN_EVENTS

# Seconds a "down" action may take when the caller gives no budget.  logind lets delay
# inhibitors hold a suspend/shutdown for InhibitDelayMaxSec (5 s by default); the budget plus
# the final disconnect (DISCONNECT_SECONDS) must stay under that, with room for the lock to be
# released: 3.5 + 0.5 = 4.0 of 5 s.
DEFAULT_DOWN_BUDGET = 3.5
# Budget for a manually requested "TV off".
MANUAL_OFF_BUDGET = 20.0
# Pause after the last key of a down action: the kernel drops every Bluetooth link as part
# of suspend, so the key-up report must be on the air before the suspend lock is released.
FLUSH_SECONDS = 0.4
# A change of more than this in (CLOCK_BOOTTIME - CLOCK_MONOTONIC) means the machine slept.
SLEEP_JUMP_SECONDS = 1.0
# How long the final disconnect of a down action is waited for.  It is a fast one (the
# transport asks the kernel, which ends the link at once and with the reason code a remote
# control uses); the transport makes that request within the first fraction of this time and
# does not take it back when the wait ends, so the wait only bounds how long we look on.
DISCONNECT_SECONDS = 0.5
HISTORY_LENGTH = 20

Trace = list

_METHOD_RE = re.compile(r"^[a-z][a-z_]*$")


class Phase(StrEnum):
    RUNNING = "running"        # normal operation
    GOING_DOWN = "going_down"  # a down event is being handled
    DOWN = "down"              # down event handled; waiting for resume (or power loss)


def sleep_offset() -> float:
    """Total time the machine has spent suspended since boot, in seconds."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


def effective_macro(cfg: Config) -> str:
    """The input-switching macro that will actually run, placeholders filled in."""
    preset = macro.PRESETS.get(cfg.input.preset)
    template = preset.template if preset is not None else cfg.input.macro
    return macro.render(template, cfg.input.hdmi_port)


def wifi_input_link(cfg: Config) -> str:
    """The app link the Wi-Fi assist opens to show the configured HDMI input ("" if none)."""
    preset = tvdata.LINK_PRESETS.get(cfg.wifi.link_preset)
    if preset is not None:
        return preset.links.get(cfg.input.hdmi_port, "")
    return cfg.wifi.app_link.replace("{port}", str(cfg.input.hdmi_port)).strip()


class Service:
    def __init__(
        self,
        cfg: Config,
        *,
        remote: Remote,
        wifi: WifiAssist | None = None,
        save_config: Callable[[Config], None] | None = None,
        on_config_changed: Callable[[Config], None] | None = None,
        recent_logs: Callable[[int], list[str]] | None = None,
        sys_root: Path | str = "/sys",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        sleep_offset_fn: Callable[[], float] = sleep_offset,
        config_problems: list[str] | None = None,
    ):
        self.cfg = cfg
        self.remote = remote
        self.wifi = wifi
        self._save_config = save_config
        self._on_config_changed = on_config_changed
        self._recent_logs = recent_logs
        self._sys_root = sys_root
        self._sleep = sleep
        self._sleep_offset = sleep_offset_fn
        self.config_problems: list[str] = list(config_problems or [])

        self.phase = Phase.RUNNING
        self._offset_seen = self._sleep_offset()
        self._boot_handled = False
        self._down_epoch = 0  # counts real down events; lets a queued up event notice one
        self._action: asyncio.Task[dict[str, Any]] | None = None
        self._action_name = ""
        self._action_trigger = ""
        self._action_is_down = False
        self._start_lock = asyncio.Lock()
        self._key_lock = asyncio.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LENGTH)
        self._background: set[asyncio.Task[Any]] = set()
        self._started_at = time.time()
        self.remote.apply_config(cfg.bluetooth)

    # ------------------------------------------------------------------ lifecycle events

    async def handle_event(self, name: str, *, budget: float | None = None, simulate: bool = False) -> dict[str, Any]:
        """Handle a lifecycle event and return the resulting action record.

        ``simulate`` runs the same decisions and actions without touching the bookkeeping
        that de-duplicates real events; the UI uses it for "test what happens on wake".
        """
        if name in UP_EVENTS:
            return await self._handle_up(name, simulate)
        if name in DOWN_EVENTS:
            return await self._handle_down(name, budget, simulate)
        raise RpcError("bad_request", f"unknown event {name!r} (expected one of: {', '.join(EVENTS)})")

    def slept_since_last_event(self) -> bool:
        """True if the machine has been suspended since the service last accounted for it."""
        return self._sleep_offset() - self._offset_seen > SLEEP_JUMP_SECONDS

    def cancel_shutdown(self) -> None:
        """A shutdown was called off (logind PrepareForShutdown(false)): we are running again."""
        went_down = self.phase != Phase.RUNNING
        self.phase = Phase.RUNNING
        if went_down:
            self._resume_link()  # the down action closed the link for a shutdown that is not coming

    def _resume_link(self) -> None:
        """Tell the Bluetooth remote that the machine is up (again): it may call the TV again.

        The link the last down action closed stays closed until something is sent, and on a
        wake where nothing is (display check, settings) that would be the next suspend, which
        then has three seconds to get the TV back.
        """
        try:
            self.remote.resume_link()
        except Exception:  # noqa: BLE001 - never raises by contract; a lifecycle event must not die of it
            log.exception("could not resume the Bluetooth link")

    async def _handle_up(self, name: str, simulate: bool) -> dict[str, Any]:
        if not simulate:
            if name == "boot":
                if self._boot_handled:
                    return self._skipped("tv_on", name, "duplicate boot event ignored")
                self._boot_handled = True
            else:
                # A resume is real if we handled a down event, or the clocks show a sleep we
                # were never told about.  Anything else is a second notification of a resume
                # that was already handled.
                if self.phase == Phase.RUNNING and not self.slept_since_last_event():
                    return self._skipped("tv_on", name, "duplicate resume event ignored")
            self.phase = Phase.RUNNING
            self._offset_seen = self._sleep_offset()
            # A real boot or wake, whatever is decided below about the TV.
            self._resume_link()

        cfg = self.cfg
        want_on = bool(getattr(cfg.events, f"{name}_tv_on"))
        want_input = bool(getattr(cfg.events, f"{name}_switch_input")) and cfg.input.method != "none"
        if not cfg.enabled:
            return self._skipped("tv_on", name, "automatic actions are turned off")
        if not (want_on or want_input):
            return self._skipped("tv_on", name, f"nothing is set to happen on {name}")

        epoch = self._down_epoch

        async def action(trace: Trace) -> str:
            if self._down_epoch != epoch:
                # This event waited behind another action and the machine has started going
                # down since: turning the TV on now would undo the "TV off" just sent.
                raise _Skip(f"superseded: the machine went down before {name} was handled")
            gate = await display.wait_for_gate(cfg.display, sys_root=self._sys_root)
            if not gate.ok:
                raise _Skip(gate.reason)
            self._note(trace, f"display check: {gate.reason}")
            return await self._do_tv_on(trace, power=want_on, switch=want_input)

        return await self._run_action("tv_on", name, action, mode="queue")

    async def _handle_down(self, name: str, budget: float | None, simulate: bool) -> dict[str, Any]:
        if not simulate:
            if self.phase != Phase.RUNNING and not self.slept_since_last_event():
                # Second notification of the same suspend/shutdown: let the first one finish.
                current = self._action
                if current is not None and not current.done() and self._action_is_down:
                    await asyncio.gather(asyncio.shield(current), return_exceptions=True)
                return self._skipped("tv_off", name, f"duplicate {name} event ignored")
            self.phase = Phase.GOING_DOWN
            self._down_epoch += 1
            self._offset_seen = self._sleep_offset()
        try:
            return await self._down_action(name, budget if budget is not None else DEFAULT_DOWN_BUDGET)
        finally:
            if not simulate:
                self.phase = Phase.DOWN

    async def _down_action(self, name: str, budget: float) -> dict[str, Any]:
        cfg = self.cfg
        wanted = bool(getattr(cfg.events, f"{name}_tv_off"))
        gate = display.evaluate(cfg.display, display.read_display_state(self._sys_root))
        if not cfg.enabled:
            reason = "automatic actions are turned off"
        elif not wanted:
            reason = f"turning the TV off on {name} is disabled"
        elif not gate.ok:
            reason = gate.reason
        else:
            reason = ""

        async def action(trace: Trace) -> str:
            try:
                if reason:
                    raise _Skip(reason)
                self._note(trace, f"display check: {gate.reason}")
                return await self._do_tv_off(trace)
            finally:
                # Leave no half-open link behind: after a suspend the radio state is gone and
                # a stale "connected" would swallow the wake key.  And end it ourselves, at
                # once: a link the kernel ends as part of suspend or power-off is ended with
                # reason "power off" (0x15), after which several Android versions do not call
                # a remote back, while the reason a remote itself gives (0x13) is the one
                # they all handle.  Shielded so that running out of budget does not skip it.
                await asyncio.shield(self._disconnect_quietly())

        return await self._run_action("tv_off", name, action, mode="preempt", budget=budget)

    async def _disconnect_quietly(self) -> None:
        try:
            await asyncio.wait_for(self.remote.disconnect(fast=True), DISCONNECT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - best effort on the way down
            log.debug("disconnect before going down failed: %s", exc)

    # ------------------------------------------------------------------ TV actions

    async def _do_tv_on(self, trace: Trace, *, power: bool, switch: bool) -> str:
        cfg = self.cfg
        parts: list[str] = []
        if power:
            via = await self._power(trace, on=True, connect_timeout=cfg.bluetooth.connect_timeout, wifi_timeout=6.0)
            parts.append(f"TV on ({via})")
        if switch:
            if power and cfg.input.delay_after_wake > 0:
                self._note(trace, f"waiting {cfg.input.delay_after_wake:g}s for the TV to start up")
                await self._sleep(cfg.input.delay_after_wake)
            via = await self._do_switch_input(trace)
            parts.append(f"input switched ({via})")
        return ", ".join(parts) or "nothing to do"

    async def _do_tv_off(self, trace: Trace) -> str:
        cfg = self.cfg
        via = await self._power(trace, on=False, connect_timeout=cfg.bluetooth.off_connect_timeout, wifi_timeout=2.5)
        await self._sleep(FLUSH_SECONDS)
        return f"TV off ({via})"

    async def _power(self, trace: Trace, *, on: bool, connect_timeout: float, wifi_timeout: float) -> str:
        """Send the power key.  Returns the transport that delivered it.

        Bluetooth is tried first and Wi-Fi is the fallback.  The exception is turning the TV
        off while the Bluetooth link is down: the time before the machine sleeps is too short
        to try one transport after the other, so both run at once and the first to succeed
        wins (SLEEP is idempotent, so a double delivery is harmless).
        """
        cfg = self.cfg
        wifi_ok = cfg.wifi.power_fallback and self._wifi_configured()
        if not on and wifi_ok and not self.remote.state.ready:
            return await self._power_race(trace, connect_timeout=connect_timeout, wifi_timeout=wifi_timeout)
        try:
            await self._power_ble(trace, on=on, connect_timeout=connect_timeout)
            return "Bluetooth"
        except RpcError as exc:
            self._note(trace, f"Bluetooth: {exc.message}")
            if not wifi_ok:
                raise RpcError("tv_unreachable", f"Bluetooth: {exc.message}") from None
            ble_problem = exc.message
        try:
            await self._power_wifi(trace, on=on, timeout=wifi_timeout)
        except RpcError as exc:
            self._note(trace, f"Wi-Fi: {exc.message}")
            raise RpcError("tv_unreachable", f"Bluetooth: {ble_problem}; Wi-Fi: {exc.message}") from None
        return "Wi-Fi"

    async def _power_ble(self, trace: Trace, *, on: bool, connect_timeout: float) -> None:
        cfg = self.cfg
        key = cfg.power.on_key if on else cfg.power.off_key
        repeat = cfg.power.on_repeat if on else cfg.power.off_repeat
        if keys.resolve(key).spec == "POWER":
            repeat = 1  # a toggle sent twice undoes itself, and TCL shows a shutdown dialog
        if not await self._ble_ready(trace, connect_timeout, wake=on):
            raise RpcError("tv_unreachable", f"the TV did not connect within {connect_timeout:g}s")
        for index in range(repeat):
            if index:
                await self._sleep(cfg.power.repeat_gap)
            await self.remote.send_key(key, cfg.bluetooth.key_press_ms / 1000.0)
        self._note(trace, f"Bluetooth: sent {key}" + (f" x{repeat}" if repeat > 1 else ""))

    async def _power_wifi(self, trace: Trace, *, on: bool, timeout: float) -> None:
        assert self.wifi is not None
        await self.wifi.send_key(self.cfg.wifi.host, KEYCODE_WAKEUP if on else KEYCODE_SLEEP, timeout=timeout)
        self._note(trace, f"Wi-Fi: sent {'WAKEUP' if on else 'SLEEP'}")

    async def _power_race(self, trace: Trace, *, connect_timeout: float, wifi_timeout: float) -> str:
        attempts = {
            asyncio.ensure_future(self._power_ble(trace, on=False, connect_timeout=connect_timeout)): "Bluetooth",
            asyncio.ensure_future(self._power_wifi(trace, on=False, timeout=wifi_timeout)): "Wi-Fi",
        }
        pending = set(attempts)
        problems: list[str] = []
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    error = task.exception()
                    if error is None:
                        return attempts[task]
                    if not isinstance(error, RpcError):
                        raise error
                    problems.append(f"{attempts[task]}: {error.message}")
                    self._note(trace, problems[-1])
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        raise RpcError("tv_unreachable", "; ".join(sorted(problems)))

    async def _ble_ready(self, trace: Trace, timeout: float, *, wake: bool = False) -> bool:
        if self.remote.state.ready:
            return True
        started = time.monotonic()
        ready = await self.remote.ensure_ready(timeout, wake=wake)
        if ready:
            self._note(trace, f"Bluetooth: TV connected after {time.monotonic() - started:.1f}s")
        return ready

    async def _do_switch_input(self, trace: Trace) -> str:
        cfg = self.cfg
        if cfg.input.method == "none":
            raise RpcError("not_configured", "input switching is turned off")
        if cfg.input.method == "wifi":
            try:
                await self._wifi_switch_input(trace)
                return "Wi-Fi"
            except RpcError as exc:
                if not (cfg.input.wifi_fallback_to_macro and effective_macro(cfg).strip()):
                    raise
                self._note(trace, f"Wi-Fi: {exc.message}; falling back to the Bluetooth macro")
        await self._macro_switch_input(trace)
        return "Bluetooth macro"

    async def _macro_switch_input(self, trace: Trace) -> None:
        cfg = self.cfg
        text = effective_macro(cfg).strip()
        if not text:
            raise RpcError("not_configured", "no input macro is set: choose a preset or enter a macro")
        await self._play_macro(trace, text)

    async def _play_macro(self, trace: Trace, text: str) -> None:
        cfg = self.cfg
        try:
            steps = macro.parse(text)
            press, gap = cfg.bluetooth.key_press_ms / 1000.0, cfg.bluetooth.key_gap_ms / 1000.0
            macro.check_runnable(steps, key_press=press, key_gap=gap)
        except macro.MacroError as exc:
            raise RpcError("bad_macro", str(exc)) from None
        if not steps:
            raise RpcError("bad_macro", "the macro is empty")
        if not await self._ble_ready(trace, cfg.bluetooth.connect_timeout):
            raise RpcError(
                "tv_unreachable", f"Bluetooth: the TV did not connect within {cfg.bluetooth.connect_timeout:g}s"
            )
        self._note(trace, f"Bluetooth: playing macro: {macro.format_steps(steps)}")
        await macro.run(steps, self.remote.send_key, key_press=press, key_gap=gap, sleep=self._sleep)

    async def _wifi_switch_input(self, trace: Trace) -> None:
        cfg = self.cfg
        if self.wifi is None:
            raise TransportError("wifi_not_configured", "the Wi-Fi assist is not available")
        if not cfg.wifi.enabled:
            raise TransportError("wifi_not_configured", "the Wi-Fi assist is turned off")
        if not cfg.wifi.host:
            raise TransportError("wifi_not_configured", "no TV address is set for the Wi-Fi assist")
        if not self.wifi.status().get("paired"):
            raise TransportError("wifi_not_paired", "the Wi-Fi assist is not paired with the TV")
        if cfg.wifi.input_mode == "app_link":
            link = wifi_input_link(cfg)
            if not link:
                raise RpcError("not_configured", "no app link is set for Wi-Fi input switching")
            what = f"opened {link}"
        else:
            link = ""
            what = f"sent HDMI {cfg.input.hdmi_port} key"
        keycode = KEYCODE_TV_INPUT_HDMI_1 + cfg.input.hdmi_port - 1

        # Right after a wake the TV's network can take a while to answer, so retry while it
        # is merely unreachable; any other failure (not paired, refused) is final.
        deadline = time.monotonic() + cfg.wifi.connect_timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                attempt_timeout = max(1.0, min(4.0, remaining))
                if link:
                    await self.wifi.launch_app_link(cfg.wifi.host, link, timeout=attempt_timeout)
                else:
                    await self.wifi.send_key(cfg.wifi.host, keycode, timeout=attempt_timeout)
                break
            except TransportError as exc:
                if exc.code != "wifi_unreachable" or deadline - time.monotonic() <= 1.0:
                    raise
                await self._sleep(1.0)
        self._note(trace, f"Wi-Fi: {what}")

    def _wifi_configured(self) -> bool:
        cfg = self.cfg
        return bool(self.wifi is not None and cfg.wifi.enabled and cfg.wifi.host and self.wifi.status().get("paired"))

    # ------------------------------------------------------------------ action runner

    async def _run_action(
        self,
        name: str,
        trigger: str,
        fn: Callable[[Trace], Awaitable[str]],
        *,
        mode: str,
        budget: float | None = None,
    ) -> dict[str, Any]:
        """Run ``fn`` as the one-and-only current action and record its outcome.

        ``mode`` says what to do when another action is still running:
          ``reject``  - fail with ``busy`` (manual requests)
          ``queue``   - wait for it (up events)
          ``preempt`` - cancel it (down events: the machine is about to sleep)
        """
        is_down = mode == "preempt"

        async def runner() -> dict[str, Any]:
            trace: Trace = []
            started_wall, started = time.time(), time.monotonic()
            ok, skipped, detail = False, False, ""
            try:
                if budget is not None:
                    detail = await asyncio.wait_for(fn(trace), budget)
                else:
                    detail = await fn(trace)
                ok = True
            except TimeoutError:
                detail = f"ran out of time ({budget:g}s)" if budget is not None else "timed out"
            except asyncio.CancelledError:
                # Only a newer action (or service shutdown) cancels this task.  Swallowing the
                # cancellation is deliberate: the outcome must still be recorded and returned.
                detail = "interrupted by a newer request"
            except _Skip as exc:
                ok, skipped, detail = True, True, str(exc)
            except RpcError as exc:
                detail = exc.message
            except Exception as exc:  # noqa: BLE001 - never let an action crash the service
                log.exception("%s (%s) crashed", name, trigger)
                detail = f"internal error: {type(exc).__name__}: {exc}"
            record = {
                "name": name,
                "trigger": trigger,
                "ok": ok,
                "skipped": skipped,
                "detail": detail,
                "steps": list(trace),
                "time": started_wall,
                "seconds": round(time.monotonic() - started, 2),
            }
            self._history.append(record)
            level = logging.INFO if ok else logging.WARNING
            log.log(level, "%s (%s): %s%s", name, trigger, "skipped: " if skipped else "" if ok else "FAILED: ", detail)
            return record

        while True:
            async with self._start_lock:
                current = self._action
                busy = current is not None and not current.done()
                if busy and mode == "reject":
                    raise self._busy()
                if busy and mode == "preempt":
                    assert current is not None
                    current.cancel()
                    await asyncio.gather(current, return_exceptions=True)
                    busy = False
                if not busy:
                    task: asyncio.Task[dict[str, Any]] = asyncio.ensure_future(runner())
                    self._action, self._action_name, self._action_trigger = task, name, trigger
                    self._action_is_down = is_down
                    break
            # "queue": wait for the running action outside the lock, so that a down event
            # arriving meanwhile can still pre-empt it, then try again.
            assert current is not None
            await asyncio.gather(asyncio.shield(current), return_exceptions=True)

        # shield: if *our caller* goes away (a UI closing its socket) the action carries on
        # and still records its outcome.
        return await asyncio.shield(task)

    def _busy(self) -> RpcError:
        return RpcError("busy", f"busy with '{self._action_name}' ({self._action_trigger}); try again shortly")

    def _skipped(self, name: str, trigger: str, reason: str) -> dict[str, Any]:
        record = {
            "name": name,
            "trigger": trigger,
            "ok": True,
            "skipped": True,
            "detail": reason,
            "steps": [],
            "time": time.time(),
            "seconds": 0.0,
        }
        # Duplicate notifications are routine; keep them out of the history the user sees.
        if "duplicate" not in reason:
            self._history.append(record)
        log.info("%s (%s): skipped: %s", name, trigger, reason)
        return record

    @staticmethod
    def _note(trace: Trace, message: str) -> None:
        trace.append(message)
        log.info("%s", message)

    # ------------------------------------------------------------------ manual operations

    async def tv_on(self, *, switch_input: bool = True) -> dict[str, Any]:
        switch = switch_input and self.cfg.input.method != "none"
        return await self._run_action(
            "tv_on", "manual", lambda trace: self._do_tv_on(trace, power=True, switch=switch), mode="reject"
        )

    async def tv_off(self) -> dict[str, Any]:
        return await self._run_action("tv_off", "manual", self._do_tv_off, mode="reject", budget=MANUAL_OFF_BUDGET)

    async def switch_input(self, via: str | None = None) -> dict[str, Any]:
        """Switch the TV's input now.

        ``via`` ("macro" or "wifi") tries exactly that method, whatever ``input.method`` says
        and without falling back, so a method can be tested before it is turned on.
        """
        if via not in (None, "macro", "wifi"):
            raise RpcError("bad_request", "'via' must be 'macro' or 'wifi'")

        async def action(trace: Trace) -> str:
            if via == "wifi":
                await self._wifi_switch_input(trace)
                return "input switched (Wi-Fi)"
            if via == "macro":
                await self._macro_switch_input(trace)
                return "input switched (Bluetooth macro)"
            return f"input switched ({await self._do_switch_input(trace)})"

        return await self._run_action("switch_input", "manual", action, mode="reject")

    async def run_macro(self, text: str) -> dict[str, Any]:
        rendered = macro.render(text, self.cfg.input.hdmi_port)

        async def action(trace: Trace) -> str:
            await self._play_macro(trace, rendered)
            return "macro played"

        return await self._run_action("macro", "manual", action, mode="reject")

    async def send_key(self, spec: str, hold: float | None = None) -> dict[str, Any]:
        """Send one key right away (the on-screen remote).  Not recorded in the history."""
        try:
            key = keys.resolve(spec)
        except keys.UnknownKey as exc:
            raise RpcError("bad_key", str(exc)) from None
        if hold is not None and not 0 < hold <= macro.MAX_HOLD:
            raise RpcError("bad_request", f"hold must be between 0 and {macro.MAX_HOLD:g} seconds")
        if self._action is not None and not self._action.done():
            raise self._busy()
        cfg = self.cfg
        async with self._key_lock:
            if not self.remote.state.ready:
                if not await self.remote.ensure_ready(min(cfg.bluetooth.connect_timeout, 10.0)):
                    raise RpcError("tv_unreachable", "the TV is not connected over Bluetooth")
            await self.remote.send_key(key.spec, hold if hold is not None else cfg.bluetooth.key_press_ms / 1000.0)
        return {"key": key.spec}

    # ------------------------------------------------------------------ configuration

    def set_config(self, patch: Any) -> Config:
        try:
            new_cfg = config_mod.apply_patch(self.cfg, patch)
        except ConfigError as exc:
            raise RpcError("invalid_config", str(exc)) from None
        self._adopt_config(new_cfg)
        return new_cfg

    def _adopt_config(self, new_cfg: Config) -> None:
        self.cfg = new_cfg
        self.remote.apply_config(new_cfg.bluetooth)
        if self._on_config_changed is not None:
            self._on_config_changed(new_cfg)
        if self._save_config is not None:
            try:
                self._save_config(new_cfg)
            except OSError as exc:
                raise RpcError("save_failed", f"settings are in effect but could not be saved: {exc}") from None
            self.config_problems = []

    async def on_paired(self, address: str, name: str) -> None:
        """Callback for the Bluetooth remote: pairing mode produced a bonded TV."""
        log.info("paired with %s (%s)", name or "TV", address)
        try:
            patch = {"bluetooth": {"tv_address": address, "tv_name": (name or "")[:64]}}
            self._adopt_config(config_mod.apply_patch(self.cfg, patch))
        except (RpcError, ConfigError) as exc:
            log.error("could not store the paired TV: %s", exc)
            self.config_problems.append(f"could not store the paired TV: {exc}")

    async def forget_tv(self) -> None:
        await self.remote.forget()
        self._adopt_config(config_mod.apply_patch(self.cfg, {"bluetooth": {"tv_address": "", "tv_name": ""}}))

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        cfg = self.cfg
        state = display.read_display_state(self._sys_root)
        gate = display.evaluate(cfg.display, state)
        running = self._action is not None and not self._action.done()
        text = effective_macro(cfg)
        try:
            steps = macro.parse(text)
            macro_error = ""
            macro_seconds = macro.estimate_seconds(
                steps, key_press=cfg.bluetooth.key_press_ms / 1000.0, key_gap=cfg.bluetooth.key_gap_ms / 1000.0
            )
        except macro.MacroError as exc:
            macro_error, macro_seconds = str(exc), 0.0
        wifi_status = self.wifi.status() if self.wifi is not None else {}
        return {
            "version": __version__,
            "uptime": round(time.time() - self._started_at, 1),
            "enabled": cfg.enabled,
            "phase": self.phase.value,
            "busy": f"{self._action_name} ({self._action_trigger})" if running else "",
            "bluetooth": self.remote.state.to_dict(),
            "wifi": {
                "available": self.wifi is not None,
                "enabled": cfg.wifi.enabled,
                "host": cfg.wifi.host,
                "name": cfg.wifi.name,
                "paired": bool(wifi_status.get("paired")),
                "pairing": bool(wifi_status.get("pairing")),
                "pairing_host": str(wifi_status.get("pairing_host", "")),
            },
            "display": {"gate_ok": gate.ok, "gate_reason": gate.reason, **state.to_dict()},
            "input": {
                "method": cfg.input.method,
                "hdmi_port": cfg.input.hdmi_port,
                "wifi_link": wifi_input_link(cfg) if cfg.wifi.input_mode == "app_link" else "",
                "macro": text,
                "macro_error": macro_error,
                "macro_seconds": round(macro_seconds, 1),
            },
            "last_action": self._history[-1] if self._history else None,
            "history": list(self._history),
            "config_problems": list(self.config_problems),
        }

    # ------------------------------------------------------------------ control channel

    async def rpc(self, method: str, params: dict[str, Any]) -> Any:
        """Dispatch one control-channel request (see ipc.py for the wire format)."""
        handler = getattr(self, f"_rpc_{method}", None) if _METHOD_RE.match(method) else None
        if handler is None:
            raise RpcError("unknown_method", f"unknown method {method!r}")
        return await handler(params)

    async def _rpc_status(self, params: dict[str, Any]) -> Any:
        return self.status()

    async def _rpc_get_config(self, params: dict[str, Any]) -> Any:
        return {
            "config": self.cfg.to_dict(),
            "schema": config_mod.describe(),
            "presets": [
                {"name": p.name, "title": p.title, "description": p.description, "template": p.template}
                for p in macro.PRESETS.values()
            ],
            "keys": keys.catalog(),
            "link_presets": tvdata.link_preset_catalog(),
        }

    async def _rpc_set_config(self, params: dict[str, Any]) -> Any:
        return {"config": self.set_config(params.get("patch")).to_dict()}

    async def _rpc_tv_on(self, params: dict[str, Any]) -> Any:
        return await self.tv_on(switch_input=_bool(params, "switch_input", True))

    async def _rpc_tv_off(self, params: dict[str, Any]) -> Any:
        return await self.tv_off()

    async def _rpc_switch_input(self, params: dict[str, Any]) -> Any:
        via = params.get("via")
        if via is not None and not isinstance(via, str):
            raise RpcError("bad_request", "'via' must be 'macro' or 'wifi'")
        return await self.switch_input(via)

    async def _rpc_send_key(self, params: dict[str, Any]) -> Any:
        hold = params.get("hold")
        if hold is not None and (isinstance(hold, bool) or not isinstance(hold, (int, float))):
            raise RpcError("bad_request", "'hold' must be a number of seconds")
        return await self.send_key(_str(params, "key"), float(hold) if hold is not None else None)

    async def _rpc_run_macro(self, params: dict[str, Any]) -> Any:
        return await self.run_macro(_str(params, "macro"))

    async def _rpc_check_macro(self, params: dict[str, Any]) -> Any:
        """Validate macro text without running it (live feedback while the user types)."""
        cfg = self.cfg
        port = params.get("port", cfg.input.hdmi_port)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 4:
            raise RpcError("bad_request", "'port' must be an HDMI port number from 1 to 4")
        try:
            cleaned = macro.validate(_str(params, "macro", allow_empty=True))
            steps = macro.parse(macro.render(cleaned, port))
        except macro.MacroError as exc:
            return {"ok": False, "error": str(exc)}
        seconds = macro.estimate_seconds(
            steps, key_press=cfg.bluetooth.key_press_ms / 1000.0, key_gap=cfg.bluetooth.key_gap_ms / 1000.0
        )
        return {"ok": True, "normalized": cleaned, "rendered": macro.format_steps(steps), "seconds": round(seconds, 1)}

    async def _rpc_event(self, params: dict[str, Any]) -> Any:
        budget = params.get("budget")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0):
            raise RpcError("bad_request", "'budget' must be a positive number of seconds")
        name = _str(params, "name")
        if name not in EVENTS:
            raise RpcError("bad_request", f"unknown event {name!r} (expected one of: {', '.join(EVENTS)})")
        simulate, wait = _bool(params, "simulate", False), _bool(params, "wait", True)
        coro = self.handle_event(name, budget=float(budget) if budget is not None else None, simulate=simulate)
        if wait:
            return await coro
        # Fire and forget: a caller such as a systemd hook must not be held up by an action
        # that can take a minute (connect, wait for the TV, play a macro).
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return {"queued": True, "name": name}

    async def _rpc_pair_start(self, params: dict[str, Any]) -> Any:
        timeout = params.get("timeout", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 10 <= timeout <= 600:
            raise RpcError("bad_request", "'timeout' must be between 10 and 600 seconds")
        await self.remote.start_pairing(float(timeout))
        return self.remote.state.to_dict()

    async def _rpc_pair_cancel(self, params: dict[str, Any]) -> Any:
        await self.remote.cancel_pairing()
        return self.remote.state.to_dict()

    async def _rpc_forget_tv(self, params: dict[str, Any]) -> Any:
        await self.forget_tv()
        return self.remote.state.to_dict()

    async def _rpc_disconnect(self, params: dict[str, Any]) -> Any:
        await self.remote.disconnect()
        return self.remote.state.to_dict()

    async def _rpc_connect(self, params: dict[str, Any]) -> Any:
        ready = await self.remote.ensure_ready(self.cfg.bluetooth.connect_timeout)
        return {"ready": ready, **self.remote.state.to_dict()}

    def _need_wifi(self) -> WifiAssist:
        if self.wifi is None:
            raise RpcError("wifi_unavailable", "the Wi-Fi assist is not available in this build")
        return self.wifi

    async def _rpc_wifi_discover(self, params: dict[str, Any]) -> Any:
        return {"devices": await self._need_wifi().discover(3.0)}

    async def _rpc_wifi_pair_start(self, params: dict[str, Any]) -> Any:
        wifi = self._need_wifi()
        host = _str(params, "host")
        try:
            checked = config_mod.apply_patch(self.cfg, {"wifi": {"host": host}}).wifi.host
        except ConfigError as exc:
            raise RpcError("bad_request", str(exc)) from None
        await wifi.start_pairing(checked)
        return wifi.status()

    async def _rpc_wifi_pair_finish(self, params: dict[str, Any]) -> Any:
        wifi = self._need_wifi()
        host = str(wifi.status().get("pairing_host", ""))
        await wifi.finish_pairing(_str(params, "code"))
        patch: dict[str, Any] = {"wifi": {"enabled": True}}
        if host:
            patch["wifi"]["host"] = host
        name = params.get("name")
        if isinstance(name, str) and name:
            patch["wifi"]["name"] = name[:64]
        self.set_config(patch)
        return wifi.status()

    async def _rpc_wifi_pair_cancel(self, params: dict[str, Any]) -> Any:
        wifi = self._need_wifi()
        await wifi.cancel_pairing()
        return wifi.status()

    async def _rpc_wifi_test(self, params: dict[str, Any]) -> Any:
        """Check that the TV answers over Wi-Fi and accepts us, without changing anything on it."""
        wifi = self._need_wifi()
        host = params.get("host", self.cfg.wifi.host)
        if not isinstance(host, str) or not host.strip():
            raise RpcError("wifi_not_configured", "no TV address is set for the Wi-Fi assist")
        result = await wifi.probe(host.strip(), timeout=6.0)
        return {"reachable": True, "on": result.get("on"), "name": str(result.get("name", ""))}

    async def _rpc_wifi_unpair(self, params: dict[str, Any]) -> Any:
        wifi = self._need_wifi()
        await wifi.unpair()
        self.set_config({"wifi": {"enabled": False}})
        return wifi.status()

    async def _rpc_logs(self, params: dict[str, Any]) -> Any:
        lines = params.get("lines", 100)
        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 1000:
            raise RpcError("bad_request", "'lines' must be between 1 and 1000")
        return {"lines": self._recent_logs(lines) if self._recent_logs is not None else []}


class _Skip(Exception):
    """Raised inside an action to end it as 'skipped' (a normal outcome, not a failure)."""


def _str(params: dict[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = params.get(name)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RpcError("bad_request", f"missing or invalid '{name}'")
    return value


def _bool(params: dict[str, Any], name: str, default: bool) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise RpcError("bad_request", f"'{name}' must be true or false")
    return value
