"""In-memory stand-ins for the transports, shared by the test modules."""

from __future__ import annotations

import asyncio
from typing import Any

from tcl_me_remo.interfaces import BleState, TransportError

TV_ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeRemote:
    """A Bluetooth remote whose TV connects after ``connect_after`` seconds (None = never)."""

    def __init__(self, *, connect_after: float | None = 0.0, paired: bool = True):
        self._state = BleState(
            available=True,
            adapter="hci0",
            adapter_address="11:22:33:44:55:66",
            powered=True,
            registered=True,
            paired=paired,
            tv_address=TV_ADDRESS if paired else "",
            tv_name="Living Room TV" if paired else "",
        )
        self.connect_after = connect_after
        self.on_paired = None
        self.fail_code: str | None = None
        self.keys: list[tuple[str, float]] = []
        self.ensure_calls: list[float] = []
        self.wake_flags: list[bool] = []
        self.disconnects = 0
        self.pairing_calls: list[float] = []
        self.forgotten = 0
        self.config: Any = None
        self.key_delay = 0.0

    @property
    def state(self) -> BleState:
        return self._state

    def apply_config(self, cfg) -> None:
        self.config = cfg

    async def ensure_ready(self, timeout: float, *, wake: bool = False) -> bool:
        self.ensure_calls.append(timeout)
        self.wake_flags.append(wake)
        if self.fail_code:
            raise TransportError(self.fail_code, f"bluetooth problem: {self.fail_code}")
        if self._state.ready:
            return True
        if self.connect_after is None or self.connect_after > timeout:
            await asyncio.sleep(timeout)
            return False
        await asyncio.sleep(self.connect_after)
        self._state.connected = self._state.ready = True
        return True

    async def send_key(self, spec: str, hold: float) -> None:
        if not self._state.ready:
            raise TransportError("not_connected", "the TV is not connected")
        await asyncio.sleep(self.key_delay)
        self.keys.append((spec, hold))

    async def start_pairing(self, timeout: float) -> None:
        self.pairing_calls.append(timeout)
        self._state.pairing = True

    async def cancel_pairing(self) -> None:
        self._state.pairing = False

    async def forget(self) -> None:
        self.forgotten += 1
        self._state.paired = False
        self._state.tv_address = self._state.tv_name = ""
        self._state.connected = self._state.ready = False

    async def disconnect(self) -> None:
        self.disconnects += 1
        self._state.connected = self._state.ready = False

    def key_names(self) -> list[str]:
        return [spec for spec, _ in self.keys]


class FakeWifi:
    """A Wi-Fi assist that fails ``unreachable_times`` attempts before working."""

    def __init__(self, *, paired: bool = True):
        self.paired = paired
        self.pairing_host = ""
        self.keys: list[tuple[str, int]] = []
        self.links: list[tuple[str, str]] = []
        self.unreachable_times = 0
        self.fail_code: str | None = None
        self.attempts = 0
        self.codes: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"paired": self.paired, "pairing": bool(self.pairing_host), "pairing_host": self.pairing_host}

    def _maybe_fail(self) -> None:
        self.attempts += 1
        if self.fail_code:
            raise TransportError(self.fail_code, f"wifi problem: {self.fail_code}")
        if self.unreachable_times > 0:
            self.unreachable_times -= 1
            raise TransportError("wifi_unreachable", "the TV did not answer")

    async def discover(self, timeout: float) -> list[dict[str, Any]]:
        return [{"name": "Living Room TV", "host": "192.168.1.50", "port": 6466}]

    async def start_pairing(self, host: str) -> None:
        self.pairing_host = host

    async def finish_pairing(self, code: str) -> None:
        if not self.pairing_host:
            raise TransportError("pairing_failed", "pairing was not started")
        if code != "A1B2C3":
            raise TransportError("pairing_failed", "wrong code")
        self.codes.append(code)
        self.paired = True
        self.pairing_host = ""

    async def cancel_pairing(self) -> None:
        self.pairing_host = ""

    async def unpair(self) -> None:
        self.paired = False

    async def probe(self, host: str, *, timeout: float) -> dict[str, Any]:
        self._maybe_fail()
        if not self.paired:
            raise TransportError("wifi_not_paired", "the TV does not accept this machine: pair first")
        return {"on": True, "name": "Living Room TV"}

    async def send_key(self, host: str, keycode: int, *, timeout: float) -> None:
        self._maybe_fail()
        self.keys.append((host, keycode))

    async def launch_app_link(self, host: str, link: str, *, timeout: float) -> None:
        self._maybe_fail()
        self.links.append((host, link))
