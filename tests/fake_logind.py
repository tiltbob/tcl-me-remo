"""A fake systemd-logind for the private test bus: inhibitor locks and Prepare* signals."""

from __future__ import annotations

import asyncio
import os

from tcl_me_remo import dbus
from tcl_me_remo.dbus import PropertyAccess, ServiceInterface, Variant, dbus_property, method, signal
from tcl_me_remo.lifecycle import LOGIND_NAME, LOGIND_PATH, MANAGER


class _Manager(ServiceInterface):
    def __init__(self, max_delay_usec: int):
        super().__init__(MANAGER)
        self._max_delay_usec = max_delay_usec
        self.locks: list[dict] = []

    @dbus_property(PropertyAccess.READ)
    def InhibitDelayMaxUSec(self) -> "t":
        return self._max_delay_usec

    @method()
    def Inhibit(self, what: "s", who: "s", why: "s", mode: "s") -> "h":
        # The caller gets the write end; we keep the read end and see EOF once the caller
        # (the only holder after our own copy is closed) releases the lock.
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        self.locks.append({"what": what, "who": who, "why": why, "mode": mode, "fd": read_end})
        asyncio.get_running_loop().call_later(0.2, os.close, write_end)
        return write_end

    @signal()
    def PrepareForSleep(self, start: "b") -> "b":
        return start

    @signal()
    def PrepareForShutdown(self, start: "b") -> "b":
        return start

    @signal()
    def PrepareForShutdownWithMetadata(self, start: "b", metadata: "a{sv}") -> "ba{sv}":
        return [start, metadata]


class FakeLogind:
    def __init__(self, max_delay_usec: int = 5_000_000):
        self.manager = _Manager(max_delay_usec)
        self.bus = None

    async def start(self) -> None:
        self.bus = await dbus.connect_system_bus()
        self.bus.export(LOGIND_PATH, self.manager)
        await self.bus.request_name(LOGIND_NAME)

    async def stop(self) -> None:
        for lock in self.manager.locks:
            try:
                os.close(lock["fd"])
            except OSError:
                pass
        if self.bus is not None:
            self.bus.disconnect()

    def held(self, what: str) -> int:
        """How many locks of this kind are currently held by clients."""
        count = 0
        for lock in self.manager.locks:
            if lock["what"] != what:
                continue
            try:
                if os.read(lock["fd"], 1) == b"":
                    continue  # EOF: every write end is closed, so the lock was released
            except BlockingIOError:
                count += 1
            except OSError:
                continue
        return count

    def prepare_for_sleep(self, start: bool) -> None:
        self.manager.PrepareForSleep(start)

    def prepare_for_shutdown(self, start: bool, kind: str | None = None) -> None:
        if kind is not None:
            self.manager.PrepareForShutdownWithMetadata(start, {"type": Variant("s", kind)})
        self.manager.PrepareForShutdown(start)


async def eventually(predicate, timeout: float = 3.0, interval: float = 0.01):
    """Poll ``predicate`` until it is truthy; fail the test if it never is."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(interval)
