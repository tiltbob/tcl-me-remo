"""Suspend and shutdown notifications from systemd-logind.

The service must send a Bluetooth packet *before* the machine sleeps or powers off.  logind
supports exactly that through delay inhibitor locks: while a process holds one, logind
announces the transition (``PrepareForSleep(true)`` / ``PrepareForShutdown(true)``), then
waits - up to ``InhibitDelayMaxSec``, 5 s by default - for the lock to be released before it
carries on.

This module holds one "sleep" and one "shutdown" delay lock, turns the announcements into
callbacks, and releases the matching lock when the callback returns.

It is one of several routes by which lifecycle events reach ``Service`` (the others are the
unit's stop handler and a clock-jump watchdog in ``daemon.py``); ``Service`` de-duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from . import aio, dbus
from .dbus import DBusError, Message, MessageBus

log = logging.getLogger(__name__)

LOGIND_NAME = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
MANAGER = "org.freedesktop.login1.Manager"

DEFAULT_MAX_DELAY = 5.0
# Part of the delay window kept in reserve for the final disconnect and D-Bus round trips.
BUDGET_MARGIN = 1.5
MIN_BUDGET = 1.0
# How long to wait for PrepareForShutdownWithMetadata after a plain PrepareForShutdown.
METADATA_WAIT = 0.15

SleepCallback = Callable[[bool], Awaitable[None]]
ShutdownCallback = Callable[[bool, str], Awaitable[None]]

_MATCH_MANAGER = f"type='signal',sender='{LOGIND_NAME}',interface='{MANAGER}',path='{LOGIND_PATH}'"
_MATCH_OWNER = (
    f"type='signal',sender='{dbus.DBUS_NAME}',interface='{dbus.DBUS_NAME}',"
    f"member='NameOwnerChanged',arg0='{LOGIND_NAME}'"
)


def shutdown_kind_from_type(type_name: str) -> str:
    """Map logind's shutdown "type" metadata to ``shutdown``, ``reboot`` or ``unknown``."""
    name = type_name.lower().replace("-", "").replace("_", "")
    if name in ("poweroff", "halt"):
        return "shutdown"
    if name in ("reboot", "kexec", "softreboot"):
        return "reboot"
    return "unknown"


class Logind:
    """Delay-inhibitor holder and signal listener.

    ``on_sleep(True)`` runs before suspend and ``on_sleep(False)`` after resume.
    ``on_shutdown(True, kind)`` runs before power-off/reboot with ``kind`` being
    ``"shutdown"``, ``"reboot"`` or ``"unknown"`` (systemd older than 255 does not say);
    ``on_shutdown(False, "")`` means a shutdown was called off.
    """

    def __init__(
        self,
        bus: MessageBus,
        *,
        on_sleep: SleepCallback,
        on_shutdown: ShutdownCallback,
        who: str = "tcl-me-remo",
        why: str = "Turn the TV off first",
    ):
        self._bus = bus
        self._on_sleep = on_sleep
        self._on_shutdown = on_shutdown
        self._who = who
        self._why = why
        self._sleep_fd: int | None = None
        self._shutdown_fd: int | None = None
        self._owner: str | None = None
        self._stop_signals: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # PrepareForSleep signals are handled one at a time, in the order they arrived.
        self._sleep_order = asyncio.Lock()
        self._pending_kind = ""
        self._metadata_seen = asyncio.Event()
        self._shutdown_in_progress = False
        self.available = False
        self.max_delay = DEFAULT_MAX_DELAY

    @property
    def down_budget(self) -> float:
        """Seconds a pre-sleep / pre-shutdown action may take before logind stops waiting."""
        return max(MIN_BUDGET, self.max_delay - BUDGET_MARGIN)

    # ------------------------------------------------------------------ start / stop

    async def start(self) -> bool:
        """Subscribe and take the locks.  Returns False (and logs) if logind is not usable."""
        try:
            self._owner = await self._get_owner()
            await dbus.add_match(self._bus, _MATCH_MANAGER)
            await dbus.add_match(self._bus, _MATCH_OWNER)
            self._stop_signals = dbus.on_signal(self._bus, self._on_message)
            try:
                usec = await dbus.get_property(self._bus, LOGIND_NAME, LOGIND_PATH, MANAGER, "InhibitDelayMaxUSec")
                self.max_delay = max(0.0, float(usec) / 1_000_000)
            except (DBusError, TimeoutError, TypeError, ValueError) as exc:
                log.debug("could not read InhibitDelayMaxUSec (%s); assuming %gs", exc, DEFAULT_MAX_DELAY)
            await self._take("sleep")
            await self._take("shutdown")
        except (DBusError, TimeoutError, OSError) as exc:
            log.warning("logind is not available (%s): suspend/shutdown will only be seen by the fallbacks", exc)
            self.available = False
            return False
        self.available = True
        log.info("holding suspend and shutdown delay locks (logind waits up to %gs)", self.max_delay)
        return True

    async def stop(self) -> None:
        if self._stop_signals is not None:
            self._stop_signals()
            self._stop_signals = None
        await dbus.remove_match(self._bus, _MATCH_MANAGER)
        await dbus.remove_match(self._bus, _MATCH_OWNER)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._release("sleep")
        self._release("shutdown")
        self.available = False

    # ------------------------------------------------------------------ locks

    async def _get_owner(self) -> str:
        bus_driver = (dbus.DBUS_NAME, dbus.DBUS_PATH, dbus.DBUS_NAME)
        body = await dbus.call(self._bus, *bus_driver, "GetNameOwner", "s", [LOGIND_NAME])
        return str(body[0])

    async def _take(self, what: str) -> None:
        if self._fd(what) is not None:
            return
        reply = await dbus.call_message(
            self._bus,
            Message(
                destination=LOGIND_NAME,
                path=LOGIND_PATH,
                interface=MANAGER,
                member="Inhibit",
                signature="ssss",
                body=[what, self._who, self._why, "delay"],
            ),
        )
        fd = reply.unix_fds[reply.body[0]]
        if what == "sleep":
            self._sleep_fd = fd
        else:
            self._shutdown_fd = fd

    def _fd(self, what: str) -> int | None:
        return self._sleep_fd if what == "sleep" else self._shutdown_fd

    def _release(self, what: str) -> None:
        fd = self._fd(what)
        if fd is None:
            return
        if what == "sleep":
            self._sleep_fd = None
        else:
            self._shutdown_fd = None
        try:
            os.close(fd)
        except OSError:
            pass

    async def _retake(self, what: str) -> None:
        try:
            await self._take(what)
        except (DBusError, TimeoutError, OSError) as exc:
            log.warning("could not re-take the %s delay lock: %s", what, exc)

    # ------------------------------------------------------------------ signals

    def _on_message(self, message: Message) -> None:
        # Signals can be sent straight to our connection by any local process, so trust only
        # the bus driver for ownership changes and only logind's current owner for the rest.
        if message.interface == dbus.DBUS_NAME and message.member == "NameOwnerChanged":
            if message.sender == dbus.DBUS_NAME and len(message.body) == 3 and message.body[0] == LOGIND_NAME:
                self._owner = str(message.body[2]) or None
            return
        if message.interface != MANAGER or message.path != LOGIND_PATH:
            return
        if self._owner is None or message.sender != self._owner:
            log.debug("ignoring %s from %s (not logind)", message.member, message.sender)
            return
        body = message.body
        if message.member == "PrepareForSleep" and len(body) == 1 and isinstance(body[0], bool):
            self._spawn(self._handle_sleep(body[0]))
        elif message.member == "PrepareForShutdownWithMetadata" and len(body) == 2 and isinstance(body[0], bool):
            metadata = dbus.unwrap(body[1]) if isinstance(body[1], dict) else {}
            self._pending_kind = shutdown_kind_from_type(str(metadata.get("type", "")))
            self._metadata_seen.set()
        elif message.member == "PrepareForShutdown" and len(body) == 1 and isinstance(body[0], bool):
            self._spawn(self._handle_shutdown(body[0]))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_sleep(self, going_down: bool) -> None:
        # In arrival order.  Each signal is handled in a task of its own, and reporting a
        # resume starts with a round trip to logind (the lock is taken back), so without this
        # the "suspend" of a machine sent back to sleep at once would be reported before the
        # "resume" it followed, and the TV would be turned on just as the machine goes down.
        # (An asyncio.Lock serves its waiters first come, first served.)
        async with self._sleep_order:
            if going_down:
                try:
                    await self._on_sleep(True)
                except Exception:  # noqa: BLE001 - the lock must be released whatever happens
                    log.exception("pre-suspend handler failed")
                finally:
                    self._release("sleep")
            else:
                # Take the lock back first so a quick second suspend is still delayed for us.
                await self._retake("sleep")
                try:
                    await self._on_sleep(False)
                except Exception:  # noqa: BLE001
                    log.exception("resume handler failed")

    async def _handle_shutdown(self, going_down: bool) -> None:
        if going_down:
            if self._shutdown_in_progress:
                return
            self._shutdown_in_progress = True
            if not self._metadata_seen.is_set():
                # Newer logind sends the metadata variant alongside; give it a moment.
                try:
                    await aio.wait_for(self._metadata_seen.wait(), METADATA_WAIT)
                except TimeoutError:
                    pass
            kind = self._pending_kind or "unknown"
            try:
                await self._on_shutdown(True, kind)
            except Exception:  # noqa: BLE001
                log.exception("pre-shutdown handler failed")
            finally:
                self._release("shutdown")
        else:
            self._shutdown_in_progress = False
            self._pending_kind = ""
            self._metadata_seen.clear()
            await self._retake("shutdown")
            try:
                await self._on_shutdown(False, "")
            except Exception:  # noqa: BLE001
                log.exception("shutdown-cancelled handler failed")
