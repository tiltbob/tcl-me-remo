"""Contracts between the service core and its transports.

``service.Service`` only ever talks to these protocols, so the Bluetooth stack and the Wi-Fi
assist can be replaced by fakes in tests and evolve independently.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Protocol

from .config import BluetoothConfig
from .ipc import RpcError

# Android key codes used over the Wi-Fi assist (android.view.KeyEvent).
KEYCODE_POWER = 26
KEYCODE_SLEEP = 223
KEYCODE_WAKEUP = 224
KEYCODE_TV_INPUT = 178
KEYCODE_TV_INPUT_HDMI_1 = 243  # HDMI n is 242 + n, for n in 1..4


class TransportError(RpcError):
    """A transport cannot do what was asked, for a reason the user can act on.

    Codes used: ``not_paired``, ``no_adapter``, ``bluetooth_off``, ``not_connected``,
    ``not_subscribed`` (the TV is connected but will not receive keys until it is paired
    again), ``bluetooth_error``, ``wifi_not_configured``, ``wifi_not_paired``,
    ``wifi_unreachable``, ``wifi_error``, ``pairing_failed``.
    """


@dataclass
class BleState:
    """Snapshot of the Bluetooth remote, shown verbatim in the UI."""

    available: bool = False        # BlueZ is reachable and an adapter exists
    adapter: str = ""              # e.g. "hci0"
    adapter_address: str = ""
    powered: bool = False
    registered: bool = False       # our HID GATT service is registered with BlueZ
    advertising: bool = False
    pairing: bool = False          # pairing mode is open
    pairing_seconds_left: int = 0
    paired: bool = False           # a TV is bonded and known to BlueZ
    tv_address: str = ""
    tv_name: str = ""
    connected: bool = False        # the TV currently holds a link to us
    ready: bool = False            # connected and settled: keys can be sent right now
    subscribed: bool = False       # BlueZ holds the TV's notification subscription
    delivery: str = ""             # how keys travel while ready: "bluez" or "raw" ("" if not ready)
    error: str = ""                # last problem, human readable ("" when fine)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Called by the remote when pairing mode produced a bonded TV: (address, name).
PairedCallback = Callable[[str, str], Awaitable[None]]


class Remote(Protocol):
    """The emulated Bluetooth LE remote control."""

    # Set by the daemon once the service exists; called when pairing mode bonded a TV.
    on_paired: PairedCallback | None

    @property
    def state(self) -> BleState: ...

    def apply_config(self, cfg: BluetoothConfig) -> None:
        """Adopt new settings (TV address, device name, timings).  Must not block."""

    async def ensure_ready(self, timeout: float) -> bool:
        """Make the TV connect to us and subscribe to key notifications.

        Advertises to the bonded TV if there is no link.  Returns True as soon as keys can
        be sent, False if the TV did not show up within ``timeout`` seconds.  Raises
        :class:`TransportError` when waiting is pointless (no bonded TV, adapter missing or
        off).
        """

    async def send_key(self, spec: str, hold: float) -> None:
        """Press the key, keep it down ``hold`` seconds, release it.

        Raises :class:`TransportError` (``not_connected``) if the link is not ready.
        """

    async def start_pairing(self, timeout: float) -> None:
        """Open pairing mode: become discoverable and accept one bond for ``timeout`` seconds."""

    async def cancel_pairing(self) -> None: ...

    async def forget(self) -> None:
        """Remove the bond with the TV."""

    async def disconnect(self) -> None:
        """Drop the link to the TV (and stop advertising) if there is one."""


class WifiAssist(Protocol):
    """Opt-in Google TV remote protocol client (Android TV Remote v2)."""

    def status(self) -> dict[str, Any]:
        """``{"paired": bool, "pairing": bool, "pairing_host": str}``"""

    async def discover(self, timeout: float) -> list[dict[str, Any]]:
        """Find TVs on the LAN: ``[{"name": str, "host": str, "port": int}, ...]``."""

    async def start_pairing(self, host: str) -> None:
        """Begin pairing; the TV shows a six character code."""

    async def finish_pairing(self, code: str) -> None:
        """Complete pairing with the code from the TV screen."""

    async def cancel_pairing(self) -> None: ...

    async def unpair(self) -> None:
        """Forget the client certificate so the TV no longer accepts us."""

    async def send_key(self, host: str, keycode: int, *, timeout: float) -> None:
        """Send one Android key code (press and release)."""

    async def launch_app_link(self, host: str, link: str, *, timeout: float) -> None:
        """Ask the TV to open an app link / URI."""
