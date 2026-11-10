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
    ``not_subscribed`` (the TV is connected but will not receive keys: it has to be paired
    again, or direct delivery has to be allowed - the message says which applies),
    ``bluetooth_error``, ``wifi_not_configured``, ``wifi_not_paired``, ``wifi_unreachable``,
    ``wifi_error``, ``pairing_failed``.
    """


@dataclass
class BleState:
    """Snapshot of the Bluetooth remote, shown verbatim in the UI.

    ``subscribed`` is about the paired TV only: it is False whenever no TV is paired, even if
    BlueZ still holds the subscription of a TV that has been forgotten since.

    ``error`` is one human-readable sentence, "" when there is nothing to say.  It carries,
    in this order of precedence: the last problem that stopped the TV from being reached
    (the message of the :class:`TransportError` that was raised, or why a wait ended without
    the TV, e.g. "the TV did not connect over Bluetooth"; for ``not_subscribed`` it says
    whether pairing again or allowing direct delivery is the way out); otherwise something
    about pairing mode - while it is open, a warning that needs acting on *now* (this machine
    is also discoverable over classic Bluetooth), and after it closed without a TV, how it
    ended (timed out, interrupted because Bluetooth went away, the TV's address was not
    reported).  A window the user cancelled, and one that paired a TV, leave nothing.  All of
    it is cleared when the TV becomes ready for keys and when pairing mode is opened.  A
    request that is refused only because pairing mode is open is not a problem in this sense
    and leaves no trace here: the warning about the open window stays visible.
    """

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
    subscribed: bool = False       # BlueZ holds the paired TV's notification subscription (see above)
    delivery: str = ""             # how keys travel while ready: "bluez" or "raw" ("" if not ready)
    error: str = ""                # last problem or pairing note, human readable (see above)

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

    async def ensure_ready(self, timeout: float, *, wake: bool = False) -> bool:
        """Make the TV connect to us and subscribe to key notifications.

        Advertises to the bonded TV if there is no link.  Returns True as soon as keys can
        be sent, False if the TV did not show up within ``timeout`` seconds.  Raises
        :class:`TransportError` when waiting is pointless (no bonded TV, adapter missing or
        off).

        ``wake`` says the caller is about to turn the TV on, which allows measures that
        rouse a suspended TV (the wake beacon).  It must be False for everything else:
        waking a TV in order to tell it to sleep would leave it on.
        """

    async def send_key(self, spec: str, hold: float) -> None:
        """Press the key, keep it down ``hold`` seconds, release it.

        Raises :class:`TransportError` (``not_connected``) if the link is not ready.
        """

    async def start_pairing(self, timeout: float) -> None:
        """Open pairing mode: become discoverable and accept one bond for ``timeout`` seconds."""

    async def cancel_pairing(self) -> None:
        """Close pairing mode and return once everything it changed has been put back.

        Usually that is immediate.  It takes about two seconds when a TV bonded in the
        window without finishing: its bond is removed again (it would block that TV's next
        attempt), and BlueZ drops the TV's link before it answers.  A TV that has just
        finished pairing is still stored.  Does nothing when pairing mode is not open.
        """

    async def forget(self) -> None:
        """Remove the bond with the TV."""

    async def disconnect(self, *, fast: bool = False) -> None:
        """Drop the link to the TV (and stop advertising) if there is one.

        The link is then "parked": the TV is not called again until the next
        :meth:`ensure_ready` or :meth:`resume_link`, whatever ``reconnect_on_drop`` says.

        Without ``fast`` this returns when the link is down, which takes about two seconds
        (BlueZ's own delay).  ``fast=True`` is for the moments before the machine sleeps or
        powers off: the link is ended at once and with the reason code a remote control gives
        (0x13, "remote user terminated connection") - if the machine went down with the link
        up, the kernel would end it with "power off" (0x15).  Going by Android's source, the
        HID host of Android 13 and 14 arms its connection to a remote again after 0x13 and
        not after 0x15 (though another layer of those versions may reconnect regardless),
        Android 11 and 12 after neither, Android 16 after either; whether a TV behaves
        differently for the two has not been observed.  The request to end the link
        is made within a fraction of a second (a report sent just before is given that time
        to go out) and is not taken back if the caller stops waiting; the call returns when
        the link is down or shortly after the request was made, whichever comes first.  Where
        the fast way is not available it falls back to the normal one.
        """

    def resume_link(self) -> None:
        """The machine is awake (again): a parked link may be brought up in the background.

        Called by the service at every boot and resume, before it decides whether anything
        is sent to the TV.  Synchronous, cheap, never raises.  It only lifts what
        :meth:`disconnect` (or the idle time-out) imposed: nothing happens unless
        ``reconnect_on_drop`` is on and a TV is bonded.  The TV is then called whether or not
        anything will be sent to it.  With ``idle_disconnect_seconds`` set, the link this
        brings up is dropped again after that time without a key, like any other link.
        """


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

    async def probe(self, host: str, *, timeout: float) -> dict[str, Any]:
        """Connect, complete the handshake and report what the TV says.

        Returns ``{"on": bool | None, "name": str}``: whether the TV's screen is on (None if
        it did not say).  Raises :class:`TransportError` if the TV cannot be reached or does
        not accept us.
        """

    async def send_key(self, host: str, keycode: int, *, timeout: float) -> None:
        """Send one Android key code (press and release)."""

    async def launch_app_link(self, host: str, link: str, *, timeout: float) -> None:
        """Ask the TV to open an app link / URI."""
