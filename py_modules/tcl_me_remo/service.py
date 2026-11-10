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
import json
import logging
import math
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
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

# Seconds the "TV off" attempt of a down event may take when the caller gives no budget.
# logind lets delay inhibitors hold a suspend/shutdown for InhibitDelayMaxSec (5 s by
# default).  The budget covers the attempt only; the final disconnect (DISCONNECT_SECONDS)
# comes on top, and the two together must stay under logind's limit with room for the lock
# to be released: 3.5 + 0.5 = 4.0 of 5 s.
DEFAULT_DOWN_BUDGET = 3.5
# Budget for a manually requested "TV off".
MANUAL_OFF_BUDGET = 20.0
# When the off key is a toggle, the Wi-Fi assist may only be tried after Bluetooth has given
# up (see Service._power); this much of the attempt's time is kept for it.
WIFI_AFTER_BLE_SECONDS = 1.5
# A down event that the machine did not follow (a "tcl-me-remo event suspend" typed by hand,
# a suspend that failed without anybody saying so) must not make every later one look like a
# duplicate.  If the service is still running this long after it handled a down event, and
# the clocks show no sleep, the machine is taken to be up.  Between the event and the moment
# the kernel freezes us (or systemd stops us) there are at most other inhibitors (5 s), the
# sleep hook (10 s) and the kernel's own freeze time-out (20 s); two minutes is well clear
# of that.  A shutdown has no such limit (a stop job ahead of ours can hang for longer), so
# the daemon asks systemd whether the system is stopping before it lets the rule act.
STALE_DOWN_SECONDS = 120.0
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
# How long a down event waits for a key held by hand to end once it has told it to.  The
# shipped transport gives way at once; this is so that one that does not cannot use up the
# time the power key has.
CUT_KEY_SECONDS = 0.2
HISTORY_LENGTH = 20
# The longest budget a down event may be given.  A request with more is refused; the
# daemon's own (derived from logind's setting, which can be anything) is cut to it.
MAX_EVENT_BUDGET = 120.0
# Upper lengths of what an action record keeps.  Records are held in memory (the history) and
# sent whole with every status reply, which has to stay well under ipc.MAX_LINE whatever
# text a request or a TV managed to get into a message.
MAX_DETAIL_CHARS = 600
MAX_STEP_CHARS = 400
MAX_STEPS_KEPT = 40
# The most a "logs" reply carries, in bytes of JSON; older lines are left out beyond that.
MAX_LOGS_BYTES = 768 * 1024

Trace = list

_METHOD_RE = re.compile(r"^[a-z][a-z_]*$")


class Phase(StrEnum):
    RUNNING = "running"        # normal operation
    GOING_DOWN = "going_down"  # a down event is being handled
    DOWN = "down"              # down event handled; waiting for resume (or power loss)


@dataclass
class _OffAttempt:
    """One "TV off" attempt: what it may use, and what it got done (for whoever judges it)."""

    deadline: float | None = None         # time.monotonic() by which the attempt must be over
    connect_timeout: float | None = None  # wait this long for the TV instead of off_connect_timeout
    cut_held_key: bool = False            # a down event: a key held by hand must not delay the power key
    sent: int = 0                         # power keys that went out, on either transport
    via: str = ""                         # the transport of the first of them
    unreached: bool = False               # it ended because the TV could not be reached or readied in time
    cut: bool = False                     # it was ended with the key down (counted in ``sent``: see _power_ble)
    unsure: bool = False                  # ... or waiting behind a key held by hand: whether it went out is not known


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
        power_state_path: Path | str | None = None,
        lasting_config_problems: list[str] | None = None,
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
        # Those of them that a later save does not settle (the settings file was unusable and
        # has been moved aside): they stay in the status until the service restarts.
        self._lasting_problems: list[str] = list(lasting_config_problems or [])
        # Which way the last power key we sent switched the TV: "on", "off", or "" when that
        # is not known.  Kept in a file (when a path is given) because it has to survive a
        # reboot; see _toggle_held_back.
        self._power_state_path = Path(power_state_path) if power_state_path is not None else None
        self._last_power = _read_last_power(self._power_state_path)

        self.phase = Phase.RUNNING
        self._offset_seen = self._sleep_offset()
        self._boot_handled = False
        self._down_epoch = 0  # counts real down events; lets a queued up event notice one
        self._down_since = 0.0  # time.monotonic() when the phase last became DOWN
        # The last down event was given up as stale (check_still_up): if the resume of that
        # very suspend turns up after all, it is a real one.
        self._late_resume_due = False
        # The down action of this cycle failed to reach the TV: a second route may try again.
        self._retry_possible = False
        self._action: asyncio.Task[dict[str, Any]] | None = None
        self._action_name = ""
        self._action_trigger = ""
        self._action_is_down = False
        self._start_lock = asyncio.Lock()
        self._key_lock = asyncio.Lock()
        self._key_task: asyncio.Task[None] | None = None  # the manual key being sent right now
        self._key_cut: asyncio.Task[None] | None = None   # ... if a down event has ended it
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LENGTH)
        self._background: set[asyncio.Task[Any]] = set()
        self._started_at = time.time()
        self.remote.apply_config(cfg.bluetooth)

    # ------------------------------------------------------------------ lifecycle events

    async def handle_event(self, name: str, *, budget: float | None = None, simulate: bool = False) -> dict[str, Any]:
        """Handle a lifecycle event and return the resulting action record.

        ``simulate`` runs the same decisions and actions without touching the bookkeeping
        that de-duplicates real events; the UI uses it for "test what happens on wake".

        ``budget`` must be a positive, finite number of seconds, whoever the caller is: it
        becomes a timer's deadline, and one that is not a number (NaN) leaves the event
        loop's timers out of order for everything else.  More than MAX_EVENT_BUDGET is cut
        to that.
        """
        if budget is not None:
            if not _is_positive_number(budget):
                raise RpcError("bad_request", "'budget' must be a positive number of seconds")
            budget = float(min(budget, MAX_EVENT_BUDGET))
        if name in UP_EVENTS:
            return await self._handle_up(name, simulate)
        if name in DOWN_EVENTS:
            return await self._handle_down(name, budget, simulate)
        raise RpcError("bad_request", f"unknown event {macro.quote(str(name))} (expected one of: {', '.join(EVENTS)})")

    def slept_since_last_event(self) -> bool:
        """True if the machine has been suspended since the service last accounted for it."""
        return self._sleep_offset() - self._offset_seen > SLEEP_JUMP_SECONDS

    def cancel_shutdown(self) -> None:
        """A shutdown was called off (logind PrepareForShutdown(false)): we are running again."""
        went_down = self.phase != Phase.RUNNING
        self.phase = Phase.RUNNING
        self._retry_possible = False
        if went_down:
            self._resume_link()  # the down action closed the link for a shutdown that is not coming

    def down_event_is_stale(self, now: float | None = None) -> bool:
        """Was a down event handled STALE_DOWN_SECONDS ago, with the service still running?

        Only the clocks are looked at.  Whether the machine is merely slow on its way down
        is for the caller to find out (the daemon asks systemd) before it has
        ``check_still_up`` act on the answer.
        """
        if self.phase != Phase.DOWN or self.slept_since_last_event():
            return False  # a sleep the clocks show is a resume, and is handled as one
        return (time.monotonic() if now is None else now) - self._down_since >= STALE_DOWN_SECONDS

    def down_event_stands(self) -> None:
        """The machine is still on its way down, only slowly: look again STALE_DOWN_SECONDS from now."""
        if self.phase == Phase.DOWN:
            self._down_since = time.monotonic()

    def check_still_up(self, now: float | None = None) -> bool:
        """A down event that has gone stale stops counting: the machine is taken to be up.

        See STALE_DOWN_SECONDS and ``down_event_is_stale``.  Returns True if the phase was
        put back to "running".
        """
        if not self.down_event_is_stale(now):
            return False
        log.info(
            "still running %gs after a suspend or shutdown was handled, and the machine has not slept: "
            "it did not go down, the next suspend or shutdown counts again",
            STALE_DOWN_SECONDS,
        )
        self.phase = Phase.RUNNING
        self._retry_possible = False
        # A suspend can take longer than this to fail.  Its resume, if one is still reported,
        # finds the phase "running" and would be taken for a duplicate: the TV we turned off
        # would stay off.
        self._late_resume_due = True
        self._resume_link()  # the down action closed the link
        return True

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
                # A resume is real if a down event has been handled to its end, or the clocks
                # show a sleep (announced or not).  Anything else is a second notification of
                # a resume that was already handled.  That includes one arriving while a down
                # action is still running and the clocks show no sleep: the machine cannot
                # have slept and woken for this suspend yet, so it is the late second
                # notification of the previous wake, overtaken by the next suspend.  Taking it
                # for a new wake would send "TV on" right behind the "TV off".
                # One exception: a suspend whose down event was given up as stale.  Its
                # resume, late as it is, is the first word of that wake (_late_resume_due).
                if self.phase != Phase.DOWN and not self.slept_since_last_event() and not self._late_resume_due:
                    return self._skipped("tv_on", name, "duplicate resume event ignored")
                self._late_resume_due = False
            self.phase = Phase.RUNNING
            self._retry_possible = False
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
            power = want_on
            held_back = self._toggle_held_back() if want_on else ""
            if held_back:
                if not want_input:
                    raise _Skip(held_back)
                self._note(trace, f"TV on: not sent: {held_back}")
                power = False
            return await self._do_tv_on(trace, power=power, switch=want_input)

        return await self._run_action("tv_on", name, action, mode="queue")

    def _toggle_held_back(self) -> str:
        """Why an *automatic* "TV on" must not send the on key ("" if it may).

        Only a toggle is ever held back.  A toggle turns a TV that is on off, and the service
        cannot see the TV.  The one case in which it is known to be off is right after this
        service turned it off itself, so the toggle is sent on boot and resume only if the
        last power key sent was an "off": not after a reboot that left the TV on, not twice,
        not before the first "off".  (The TV can still have been switched with its own remote
        since; nothing here can know that.)  A manual "TV on" always sends the key.
        """
        key = self.cfg.power.on_key
        if not keys.is_power_toggle(key) or self._last_power == "off":
            return ""
        return (
            f"the on key ({key}) is a toggle, and the TV was not turned off by this service last, "
            "so it is taken to be on already (the toggle would turn it off); 'TV on' by hand sends it"
        )

    async def _handle_down(self, name: str, budget: float | None, simulate: bool) -> dict[str, Any]:
        if budget is None:
            budget = DEFAULT_DOWN_BUDGET
        if simulate:
            try:
                return await self._down_action(name, budget, real=False)
            finally:
                # "Test sleep" closes the link as the real thing does, but the machine stays
                # up and no wake will follow: without this the TV would not be called again
                # until something is sent, and the next real suspend would find the link down.
                if self.phase == Phase.RUNNING:
                    self._resume_link()
        retry = False
        if self.phase != Phase.RUNNING and not self.slept_since_last_event():
            # Second notification of the same suspend/shutdown: let the first one finish.
            while True:
                current = self._action
                if current is None or current.done() or not self._action_is_down:
                    break
                await asyncio.gather(asyncio.shield(current), return_exceptions=True)
            if not self._second_attempt_due():
                return self._skipped("tv_off", name, f"duplicate {name} event ignored")
            retry = True
        else:
            self._down_epoch += 1
            self._offset_seen = self._sleep_offset()
            self._late_resume_due = False  # a new cycle: the resume that was still owed is not coming
        self._retry_possible = False  # one second attempt per cycle
        self.phase = Phase.GOING_DOWN
        epoch = self._down_epoch
        try:
            return await self._down_action(name, budget, real=True, retry=retry)
        finally:
            self._down_handled(epoch)

    def _second_attempt_due(self) -> bool:
        """Should a second notification of a down event try "TV off" again, in its own time?

        The second route (the sleep hook after logind, the stop signal during shutdown) has
        time of its own, but using it holds the machine awake that much longer.  That is
        worth it only if the first attempt failed because the TV could not be reached or
        readied in time (never after a key went out, a skip, or a closed display check), AND
        the link was down because the settings have it down: ``reconnect_on_drop`` off or an
        idle time-out set.  With the link kept up by design, a link that is down means the TV
        is not answering (off already, unplugged, out of range), and trying longer would only
        make every suspend with such a TV slower.
        """
        return self.phase == Phase.DOWN and self._retry_possible and self._link_down_by_design()

    def _link_down_by_design(self) -> bool:
        """Do the settings have the link down at times when the TV would answer a call?"""
        bluetooth = self.cfg.bluetooth
        return not bluetooth.reconnect_on_drop or bluetooth.idle_disconnect_seconds > 0

    def _down_handled(self, epoch: int) -> None:
        """The down action of down event number ``epoch`` has ended: note where that leaves us.

        Normally the machine is now down.  But the action can outlive the event: logind stops
        waiting after its delay, and a machine that slept with the action unfinished wakes up
        with it still running; a shutdown can be called off half-way.  The up event (or the
        cancellation) has then been handled already, and it is the newer truth.  Marking the
        machine "down" over it would make the next real suspend look like a duplicate, and
        the action's last step has just parked the link that the up event un-parked.
        """
        if self._down_epoch != epoch:
            return  # a newer down event is being handled; it will say how things stand
        if self.phase == Phase.GOING_DOWN:
            self.phase = Phase.DOWN
            self._down_since = time.monotonic()
        elif self.phase == Phase.RUNNING:
            log.info("the machine came back up while the down action was still running")
            self._resume_link()

    async def _down_action(self, name: str, budget: float, *, real: bool, retry: bool = False) -> dict[str, Any]:
        """Run the "TV off" of a down event, then close the link.

        ``budget`` is the time for the attempt itself.  The outcome is fixed when the attempt
        ends; closing the link comes after it, takes up to DISCONNECT_SECONDS more, and
        always runs.  ``real`` is False for a simulated event; ``retry`` marks the second
        attempt of a cycle (see _second_attempt_due), which waits for the TV for as long as
        its budget allows: the first attempt's last step stopped calling the TV, so this one
        starts the call from scratch.

        So does a first attempt that has more than the usual time while the link is down by
        the settings' choice.  That is a longer route reporting first: the sleep hook or the
        stop signal when logind's route did not act (logind not available, or one that
        cannot tell a reboot from a power-off).  It is the only attempt this cycle may get,
        and waiting ``off_connect_timeout`` of six or eight seconds would waste the rest.
        With the usual budget nothing changes, whatever the settings: see the condition.
        """
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

        attempt = _OffAttempt(cut_held_key=True)
        if retry or (budget > DEFAULT_DOWN_BUDGET and self._link_down_by_design()):
            press = cfg.bluetooth.key_press_ms / 1000.0
            attempt.connect_timeout = max(cfg.bluetooth.off_connect_timeout, budget - press - FLUSH_SECONDS - 0.1)

        async def action(trace: Trace) -> str:
            if reason:
                raise _Skip(reason)
            self._note(trace, f"display check: {gate.reason}")
            if retry:
                self._note(trace, "second attempt: the first one could not reach the TV in its time")
            try:
                return await self._tv_off_within(trace, budget, attempt)
            except RpcError:
                # Could not reach the TV, and nothing went out: a second route may try again.
                self._retry_possible = real and not retry and attempt.unreached and not attempt.sent
                raise

        # The clean-up: leave no half-open link behind.  After a suspend the radio state is
        # gone and a stale "connected" would swallow the wake key.  And end it ourselves, at
        # once: a link the kernel ends as part of suspend or power-off is ended with reason
        # "power off" (0x15), while a remote itself gives 0x13.  From Android's source, the
        # HID host of Android 13 and 14 re-arms its connection to a remote after 0x13 and not
        # after 0x15 (another layer there may reconnect anyway), Android 11 and 12 after
        # neither, Android 16 after any.  Whether it matters on a real TV is not known; 0x13
        # is never the worse of the two.  It runs whatever became of the action (done,
        # skipped, out of time, pre-empted), after the outcome is fixed.
        return await self._run_action("tv_off", name, action, mode="preempt", cleanup=self._disconnect_quietly)

    async def _tv_off_within(self, trace: Trace, budget: float, attempt: _OffAttempt) -> str:
        """Try to turn the TV off, giving up after ``budget`` seconds.

        What counts is whether the key went out.  If the time ends after it (in the gap
        before a repeat, or in the pause that lets the key-up onto the air), the TV has been
        told and the attempt is a success; the trace says that the time ran out.  The same
        goes for a time that ends while the key is down: the transport releases it, the TV
        has had a whole key press, and _power_ble has counted it.

        Only an attempt that got nothing out because the TV could not be reached is marked
        ``unreached``, which is what allows a second route to try again.
        """
        attempt.deadline = time.monotonic() + budget
        try:
            return await asyncio.wait_for(self._do_tv_off(trace, attempt), budget)
        except TimeoutError:
            if attempt.sent:
                when = "with the key down; it was released at once" if attempt.cut else "right after the key went out"
                self._note(trace, f"out of time ({budget:g}s) {when}")
                return f"TV off ({attempt.via})"
            if attempt.unsure:
                raise RpcError(
                    "tv_unreachable",
                    f"ran out of time ({budget:g}s) with the power key waiting behind a key held by hand "
                    "that did not let go",
                ) from None
            attempt.unreached = True
            raise RpcError("tv_unreachable", f"ran out of time ({budget:g}s)") from None

    async def _cut_held_key(self, trace: Trace) -> None:
        """End a key that is being sent or held by hand (on-screen remote, ``key --hold``).

        The transport sends one key at a time, so a key held for seconds would keep the power
        key of a down event waiting until the machine is asleep.  The transport releases a
        key whose press is cancelled; the caller gets an error reply (see send_key).

        The key's task is given CUT_KEY_SECONDS to end, and the power key goes out whether
        or not it has: a wait without a bound of its own would not end with the attempt's
        time either (a cancelled ``gather`` waits for its child; ``asyncio.wait`` does not).
        """
        task = self._key_task
        if task is None or task.done():
            return
        self._key_cut = task
        task.cancel()
        ended, _ = await asyncio.wait({task}, timeout=CUT_KEY_SECONDS)
        if ended:
            self._note(trace, "a key that was being held by hand was cut short")
        else:
            self._note(trace, f"a key that was being held by hand did not end within {CUT_KEY_SECONDS:g}s of being cut")

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

    async def _do_tv_off(self, trace: Trace, attempt: _OffAttempt) -> str:
        cfg = self.cfg
        if attempt.cut_held_key:
            await self._cut_held_key(trace)
        connect_timeout = attempt.connect_timeout or cfg.bluetooth.off_connect_timeout
        via = await self._power(trace, on=False, connect_timeout=connect_timeout, wifi_timeout=2.5, attempt=attempt)
        await self._sleep(FLUSH_SECONDS)
        return f"TV off ({via})"

    async def _power(
        self,
        trace: Trace,
        *,
        on: bool,
        connect_timeout: float,
        wifi_timeout: float,
        attempt: _OffAttempt | None = None,
    ) -> str:
        """Send the power key.  Returns the transport that delivered it.

        Bluetooth is tried first and Wi-Fi is the fallback.  The exception is turning the TV
        off with a discrete off key while the Bluetooth link is down: the time before the
        machine sleeps is too short to try one transport after the other, so both run at once
        and the first to succeed wins.  The TV may then get the key twice, which is harmless
        for SLEEP and only for a discrete key.

        An off key that is a toggle is therefore never raced: the Wi-Fi assist's SLEEP
        followed by a Bluetooth toggle would turn the TV off and on again.  Bluetooth gets
        the first part of the time, and Wi-Fi (always the discrete SLEEP) is tried only once
        Bluetooth has given up.
        """
        cfg = self.cfg
        wifi_ok = cfg.wifi.power_fallback and self._wifi_configured()
        deadline = attempt.deadline if attempt is not None else None
        if not on and wifi_ok and not self.remote.state.ready:
            if not keys.is_power_toggle(cfg.power.off_key):
                return await self._power_race(
                    trace, connect_timeout=connect_timeout, wifi_timeout=wifi_timeout, attempt=attempt
                )
            if deadline is not None:
                left = deadline - time.monotonic() - FLUSH_SECONDS
                connect_timeout = max(0.1, min(connect_timeout, max(left - WIFI_AFTER_BLE_SECONDS, left / 2)))
        try:
            await self._power_ble(trace, on=on, connect_timeout=connect_timeout, attempt=attempt)
            return "Bluetooth"
        except RpcError as exc:
            self._note(trace, f"Bluetooth: {exc.message}")
            if not wifi_ok:
                raise RpcError("tv_unreachable", f"Bluetooth: {exc.message}") from None
            ble_problem = exc.message
        if deadline is not None:
            wifi_timeout = max(0.1, min(wifi_timeout, deadline - time.monotonic() - FLUSH_SECONDS))
        try:
            await self._power_wifi(trace, on=on, timeout=wifi_timeout, attempt=attempt)
        except RpcError as exc:
            self._note(trace, f"Wi-Fi: {exc.message}")
            raise RpcError("tv_unreachable", f"Bluetooth: {ble_problem}; Wi-Fi: {exc.message}") from None
        return "Wi-Fi"

    async def _power_ble(
        self, trace: Trace, *, on: bool, connect_timeout: float, attempt: _OffAttempt | None = None
    ) -> None:
        cfg = self.cfg
        key = cfg.power.on_key if on else cfg.power.off_key
        repeat = cfg.power.on_repeat if on else cfg.power.off_repeat
        if keys.is_power_toggle(key):
            repeat = 1  # a toggle sent twice undoes itself
        if not await self._ble_ready(trace, connect_timeout, wake=on):
            if attempt is not None:
                attempt.unreached = True
            raise RpcError(
                "tv_unreachable", self._why_not_ready(f"the TV did not connect within {connect_timeout:g}s")
            )
        press = cfg.bluetooth.key_press_ms / 1000.0
        deadline = attempt.deadline if attempt is not None else None
        direction = "on" if on else "off"
        sent, no_time, cut = 0, False, False
        try:
            for index in range(repeat):
                if index:
                    # A repeat is for a TV that missed the first press.  One that cannot be
                    # finished (gap, press, and the pause that lets it onto the air) in the
                    # time the attempt has left is not started: the key has gone out.
                    if deadline is not None and (
                        time.monotonic() + cfg.power.repeat_gap + press + FLUSH_SECONDS > deadline
                    ):
                        no_time = True
                        break
                    await self._sleep(cfg.power.repeat_gap)
                try:
                    await self.remote.send_key(key, press)
                except asyncio.CancelledError:
                    # Ended with the key down: the attempt's time ran out, or a newer action
                    # took over.  The press is with the TV, and the transport releases a key
                    # whose press is cancelled, so the TV has had a whole key press, a little
                    # shorter than asked for.  It counts as sent.  Calling it "TV not
                    # reached" would have a second route send it again, and a toggle sent
                    # twice turns the TV off and back on; and since the TV was most probably
                    # switched, that is what is remembered: forgetting the direction would
                    # leave a TV we turned off dark at the next wake (a toggle as on key is
                    # sent only after an "off"), in order to be right in the rarer case.
                    #
                    # Not so while a key held by hand is still with the transport, which
                    # sends one key at a time: ours may never have begun.  Then nothing is
                    # known, nothing is remembered, and no second attempt is made either.
                    held = self._key_task
                    if held is not None and not held.done():
                        self._remember_power("")
                        if attempt is not None:
                            attempt.unsure = True
                    else:
                        sent, cut = sent + 1, True
                        self._remember_power(direction)
                        if attempt is not None:
                            attempt.sent += 1
                            attempt.via = attempt.via or "Bluetooth"
                            attempt.cut = True
                    raise
                sent += 1
                self._remember_power(direction)
                if attempt is not None:
                    attempt.sent += 1
                    attempt.via = attempt.via or "Bluetooth"
        finally:
            if sent:
                count = "" if repeat == 1 else f" x{sent}" if sent == repeat else f" x{sent} of {repeat}"
                note = " (no time for more)" if no_time else " (cut short, and released)" if cut else ""
                self._note(trace, f"Bluetooth: sent {key}{count}{note}")

    async def _power_wifi(self, trace: Trace, *, on: bool, timeout: float, attempt: _OffAttempt | None = None) -> None:
        assert self.wifi is not None
        await self.wifi.send_key(self.cfg.wifi.host, KEYCODE_WAKEUP if on else KEYCODE_SLEEP, timeout=timeout)
        self._remember_power("on" if on else "off")
        if attempt is not None:
            attempt.sent += 1
            attempt.via = attempt.via or "Wi-Fi"
        self._note(trace, f"Wi-Fi: sent {'WAKEUP' if on else 'SLEEP'}")

    def _remember_power(self, direction: str) -> None:
        """Note which way the power key just sent switched the TV ("" = cannot be said)."""
        if direction == self._last_power:
            return
        self._last_power = direction
        if self._power_state_path is None:
            return
        try:
            _write_last_power(self._power_state_path, direction)
        except OSError as exc:
            log.warning("could not store the direction of the last power key (%s): %s", self._power_state_path, exc)

    async def _send_key_noting(self, spec: str, hold: float) -> None:
        """Send a key for the on-screen remote or a macro; if it acts on the TV's power, remember how."""
        effect = keys.power_effect(spec)
        if effect == "unknown":
            self._remember_power("")  # a toggle: which way it switched the TV cannot be said
        try:
            await self.remote.send_key(spec, hold)
        except asyncio.CancelledError:
            if effect:
                self._remember_power("")  # cut short: it may or may not have taken effect
            raise
        if effect in ("on", "off"):
            self._remember_power(effect)

    async def _power_race(
        self, trace: Trace, *, connect_timeout: float, wifi_timeout: float, attempt: _OffAttempt | None = None
    ) -> str:
        attempts = {
            asyncio.ensure_future(
                self._power_ble(trace, on=False, connect_timeout=connect_timeout, attempt=attempt)
            ): "Bluetooth",
            asyncio.ensure_future(self._power_wifi(trace, on=False, timeout=wifi_timeout, attempt=attempt)): "Wi-Fi",
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

    def _why_not_ready(self, not_connected: str) -> str:
        """What to say when the wait for the TV ended without it being ready for keys.

        ``not_connected`` is the caller's sentence for a TV that did not connect.  A TV that
        did connect, and was not ready in time all the same (it had not written all of its
        subscriptions yet, say), is a different matter with a different way out, and the
        transport has said which in ``state.error``.
        """
        state = self.remote.state
        if not state.connected:
            return not_connected
        return state.error or "the TV connected but was not ready for keys in time"

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
            reason = self._why_not_ready(f"the TV did not connect within {cfg.bluetooth.connect_timeout:g}s")
            raise RpcError("tv_unreachable", f"Bluetooth: {reason}")
        self._note(trace, f"Bluetooth: playing macro: {macro.format_steps(steps)}")
        await macro.run(steps, self._send_key_noting, key_press=press, key_gap=gap, sleep=self._sleep)

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
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Run ``fn`` as the one-and-only current action and record its outcome.

        ``mode`` says what to do when another action is still running:
          ``reject``  - fail with ``busy`` (manual requests)
          ``queue``   - wait for it (up events)
          ``preempt`` - cancel it (down events: the machine is about to sleep)

        ``cleanup`` runs once the outcome is fixed and recorded, however the action ended,
        and the action counts as running until it is through.  It must bound its own time.
        """
        is_down = mode == "preempt"

        async def runner() -> dict[str, Any]:
            trace: Trace = []
            started_wall, started = time.time(), time.monotonic()
            ok, skipped, detail, error_code = False, False, "", ""
            try:
                detail = await fn(trace)
                ok = True
            except TimeoutError:
                detail, error_code = "timed out", "timeout"
            except asyncio.CancelledError:
                # Only a newer action (or service shutdown) cancels this task.  Swallowing the
                # cancellation is deliberate: the outcome must still be recorded and returned.
                detail, error_code = "interrupted by a newer request", "interrupted"
            except _Skip as exc:
                ok, skipped, detail = True, True, str(exc)
            except RpcError as exc:
                detail, error_code = exc.message, exc.code
            except Exception as exc:  # noqa: BLE001 - never let an action crash the service
                log.exception("%s (%s) crashed", name, trigger)
                detail, error_code = f"internal error: {type(exc).__name__}: {exc}", "internal"
            detail = _clip(detail, MAX_DETAIL_CHARS)
            record = {
                "name": name,
                "trigger": trigger,
                "ok": ok,
                "skipped": skipped,
                "detail": detail,
                "error_code": error_code,
                "steps": _clip_steps(trace),
                "time": started_wall,
                "seconds": round(time.monotonic() - started, 2),
            }
            self._history.append(record)
            level = logging.INFO if ok else logging.WARNING
            log.log(level, "%s (%s): %s%s", name, trigger, "skipped: " if skipped else "" if ok else "FAILED: ", detail)
            if cleanup is not None:
                cleaning = asyncio.ensure_future(cleanup())
                while not cleaning.done():
                    try:
                        await asyncio.shield(cleaning)
                    except asyncio.CancelledError:
                        # A newer action is taking over (or the service is stopping).  The
                        # outcome above stands, and the clean-up is seen through first.
                        continue
                    except Exception:  # noqa: BLE001 - never let a clean-up crash the service
                        log.exception("clean-up after %s (%s) failed", name, trigger)
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
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if not task.cancelled() or (me is not None and me.cancelling()):
                raise
            # A down event pre-empted the action before it took its first step, so it left no
            # record of its own.  Our caller is still there and gets the usual answer.
            record = {
                "name": name,
                "trigger": trigger,
                "ok": False,
                "skipped": False,
                "detail": "interrupted by a newer request",
                "error_code": "interrupted",
                "steps": [],
                "time": time.time(),
                "seconds": 0.0,
            }
            self._history.append(record)
            log.warning("%s (%s): FAILED: %s", name, trigger, record["detail"])
            return record

    def _busy(self) -> RpcError:
        return RpcError("busy", f"busy with '{self._action_name}' ({self._action_trigger}); try again shortly")

    def _skipped(self, name: str, trigger: str, reason: str) -> dict[str, Any]:
        record = {
            "name": name,
            "trigger": trigger,
            "ok": True,
            "skipped": True,
            "detail": reason,
            "error_code": "",
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
        message = _clip(message, MAX_STEP_CHARS)
        trace.append(message)
        log.info("%s", message)

    # ------------------------------------------------------------------ manual operations

    async def tv_on(self, *, switch_input: bool = True) -> dict[str, Any]:
        switch = switch_input and self.cfg.input.method != "none"
        return await self._run_action(
            "tv_on", "manual", lambda trace: self._do_tv_on(trace, power=True, switch=switch), mode="reject"
        )

    async def tv_off(self) -> dict[str, Any]:
        return await self._run_action(
            "tv_off",
            "manual",
            lambda trace: self._tv_off_within(trace, MANUAL_OFF_BUDGET, _OffAttempt()),
            mode="reject",
        )

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
        if len(text) > macro.MAX_TEXT:
            # Before anything looks at it: the same limit as the input.macro setting.
            raise RpcError("bad_macro", f"the macro is {len(text)} characters long; the limit is {macro.MAX_TEXT}")
        rendered = macro.render(text, self.cfg.input.hdmi_port)

        async def action(trace: Trace) -> str:
            await self._play_macro(trace, rendered)
            return "macro played"

        return await self._run_action("macro", "manual", action, mode="reject")

    async def send_key(self, spec: str, hold: float | None = None) -> dict[str, Any]:
        """Send one key right away (the on-screen remote).  Not recorded in the history.

        A suspend or shutdown does not wait for a key that is being held: it ends the key
        (the transport releases it) and this call fails with ``busy``.
        """
        try:
            key = keys.resolve(spec)
        except keys.UnknownKey as exc:
            raise RpcError("bad_key", str(exc)) from None
        if hold is not None and not 0 < hold <= macro.MAX_HOLD:
            raise RpcError("bad_request", f"hold must be between 0 and {macro.MAX_HOLD:g} seconds")
        if self._action is not None and not self._action.done():
            raise self._busy()
        cfg = self.cfg

        async def press() -> None:
            if not self.remote.state.ready:
                if not await self.remote.ensure_ready(min(cfg.bluetooth.connect_timeout, 10.0)):
                    raise RpcError("tv_unreachable", self._why_not_ready("the TV is not connected over Bluetooth"))
            await self._send_key_noting(key.spec, hold if hold is not None else cfg.bluetooth.key_press_ms / 1000.0)

        async with self._key_lock:
            if self._action is not None and not self._action.done():
                raise self._busy()  # an action started while this key waited for the one before it
            # In a task of its own, so that a down event can end the key without ending our
            # caller: that is the control connection's handler, which still has to answer.
            task = asyncio.ensure_future(press())
            self._key_task = task
            try:
                await task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if self._key_cut is not task or (me is not None and me.cancelling()):
                    task.cancel()  # our caller went away: the key goes with it
                    raise
                raise RpcError(
                    "busy", "the key was cut short: the machine is going to sleep or shutting down"
                ) from None
            finally:
                if self._key_task is task:
                    self._key_task = None
                if self._key_cut is task:
                    self._key_cut = None
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
            # What was wrong with single values is settled by the file just written.  Not so
            # that the old file was unusable and has been moved aside: the user still has to
            # hear where their settings went.
            self.config_problems = list(self._lasting_problems)

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
        press, gap = cfg.bluetooth.key_press_ms / 1000.0, cfg.bluetooth.key_gap_ms / 1000.0
        macro_error, macro_seconds = "", 0.0
        try:
            steps = macro.parse(text)
            macro_seconds = macro.estimate_seconds(steps, key_press=press, key_gap=gap)
            # What playing it would refuse, said here and not only when it is due: a macro
            # that takes too long with the configured key timings.
            macro.check_runnable(steps, key_press=press, key_gap=gap)
        except macro.MacroError as exc:
            macro_error = str(exc)
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
            raise RpcError("unknown_method", f"unknown method {macro.quote(method)}")
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
        if hold is not None and not (_is_positive_number(hold) and hold <= macro.MAX_HOLD):
            raise RpcError("bad_request", f"hold must be between 0 and {macro.MAX_HOLD:g} seconds")
        key = _str(params, "key")
        if len(key) > macro.QUOTE_CHARS:
            raise RpcError("bad_key", f"unknown key {macro.quote(key)}")  # no key is that long
        return await self.send_key(key, float(hold) if hold is not None else None)

    async def _rpc_run_macro(self, params: dict[str, Any]) -> Any:
        return await self.run_macro(_str(params, "macro"))

    async def _rpc_check_macro(self, params: dict[str, Any]) -> Any:
        """Validate macro text without running it (live feedback while the user types)."""
        cfg = self.cfg
        port = params.get("port", cfg.input.hdmi_port)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 4:
            raise RpcError("bad_request", "'port' must be an HDMI port number from 1 to 4")
        text = _str(params, "macro", allow_empty=True)
        if len(text) > macro.MAX_TEXT:
            # Before parsing: this runs in the event loop that also has to answer logind.
            return {"ok": False, "error": f"the macro is {len(text)} characters long; the limit is {macro.MAX_TEXT}"}
        press, gap = cfg.bluetooth.key_press_ms / 1000.0, cfg.bluetooth.key_gap_ms / 1000.0
        try:
            cleaned = macro.validate(text)
            steps = macro.parse(macro.render(cleaned, port))
            # The limit on the running time too, with the key timings that are set: it is
            # what playing the macro would be refused for.
            macro.check_runnable(steps, key_press=press, key_gap=gap)
        except macro.MacroError as exc:
            return {"ok": False, "error": str(exc)}
        seconds = macro.estimate_seconds(steps, key_press=press, key_gap=gap)
        return {"ok": True, "normalized": cleaned, "rendered": macro.format_steps(steps), "seconds": round(seconds, 1)}

    async def _rpc_event(self, params: dict[str, Any]) -> Any:
        budget = params.get("budget")
        # NaN compares false with everything and 1e999 is an infinity: test for what is
        # wanted, not for what is not.  (handle_event checks again, for its other callers.)
        if budget is not None and not (_is_positive_number(budget) and budget <= MAX_EVENT_BUDGET):
            raise RpcError(
                "bad_request", f"'budget' must be a number of seconds, more than 0 and at most {MAX_EVENT_BUDGET:g}"
            )
        name = _str(params, "name")
        if name not in EVENTS:
            raise RpcError("bad_request", f"unknown event {macro.quote(name)} (expected one of: {', '.join(EVENTS)})")
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
        code = _str(params, "code")
        if len(code) > 32:
            raise RpcError("bad_request", "'code' is the short code the TV shows")
        await wifi.finish_pairing(code)
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
        try:
            host = config_mod.apply_patch(self.cfg, {"wifi": {"host": host}}).wifi.host
        except ConfigError as exc:
            raise RpcError("bad_request", str(exc)) from None
        result = await wifi.probe(host, timeout=6.0)
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
        found = self._recent_logs(lines) if self._recent_logs is not None else []
        # The newest lines that fit into a reply (a line of 500 characters can take 3000
        # bytes as JSON if none of them is ASCII).
        kept, size = len(found), 0
        while kept > 0:
            size += len(json.dumps(found[kept - 1])) + 1
            if size > MAX_LOGS_BYTES:
                break
            kept -= 1
        return {"lines": found[kept:]}


class _Skip(Exception):
    """Raised inside an action to end it as 'skipped' (a normal outcome, not a failure)."""


def _read_last_power(path: Path | None) -> str:
    """The stored direction of the last power key: "on", "off", or "" (nothing usable stored)."""
    if path is None:
        return ""
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if value in ("on", "off") else ""


def _write_last_power(path: Path, direction: str) -> None:
    """Store it so that it is there after a reboot (temporary file, fsync, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"last": direction, "time": round(time.time())}) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_positive_number(value: Any) -> bool:
    """A real number above zero: not a bool, not NaN, not an infinity (JSON's 1e999 is one)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return value > 0


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _clip_steps(trace: list) -> list[str]:
    """The steps of an action for its record: all of them, unless there are absurdly many."""
    steps = [str(step) for step in trace]
    if len(steps) > MAX_STEPS_KEPT:
        left_out = len(steps) - (MAX_STEPS_KEPT - 1)
        steps = [*steps[: MAX_STEPS_KEPT - 1], f"... and {left_out} more steps"]
    return steps


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
