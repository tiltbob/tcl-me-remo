"""LE advertising: how the TV finds us.

Facts this module relies on (BlueZ 5.79-5.87 ``src/advertising.c``):

* ``RegisterAdvertisement`` makes bluetoothd read the object's properties with
  ``Properties.GetAll`` and answer only after the kernel accepted the instance.  A property
  it cannot parse fails the whole registration with ``org.bluez.Error.Failed``.
* ``Type = "peripheral"`` makes the advertisement connectable.
* ``Discoverable`` controls the Flags field: true gives "LE General Discoverable" (the TV
  lists us in its pairing screen), false gives a connectable advertisement that pairing
  screens ignore but a bonded TV still connects to.  The adapter's own ``Discoverable``
  property must stay untouched: it turns on classic inquiry scan, and a TV that finds the
  machine over classic Bluetooth pairs with the wrong transport.
* ``MinInterval``/``MaxInterval`` (milliseconds) are regular properties in these versions,
  but they are the newest ones we use, so registration is retried without them if
  bluetoothd refuses the full set.
* ``LocalName`` travels in the scan response; ``Appearance`` 0x03C1 (keyboard) is what makes
  an Android TV offer the device in "pair remote or accessory".
* bluetoothd calls ``Release()`` when it drops the advertisement on its own.

Advertising is strictly on demand: a TV in standby must not be poked while nobody asked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .. import dbus
from ..dbus import DBusError, MessageBus, PropertyAccess, ServiceInterface, dbus_property, method
from .gatt import UUID_HID_SERVICE, uuid16

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADVERTISING_MANAGER = "org.bluez.LEAdvertisingManager1"
ADVERT_PATH = "/org/tclmeremo/advert"
ADVERT_BASIC_PATH = "/org/tclmeremo/advert_basic"

APPEARANCE_KEYBOARD = 0x03C1
#: Advertising intervals in ms: quick while somebody waits for the TV, relaxed in the background.
FAST_INTERVAL = (30, 60)
SLOW_INTERVAL = (150, 250)
#: The scan response holds 31 bytes, two of which are the length and type of the name field.
MAX_NAME_BYTES = 26

CALL_TIMEOUT = 5.0


def fit_name(name: str, limit: int = MAX_NAME_BYTES) -> str:
    """Cut ``name`` to at most ``limit`` UTF-8 bytes without splitting a character."""
    encoded = name.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class AdvertSettings:
    name: str
    discoverable: bool = False
    fast: bool = True

    @property
    def interval(self) -> tuple[int, int]:
        return FAST_INTERVAL if self.fast else SLOW_INTERVAL


class BasicAdvertisement(ServiceInterface):
    """The properties every supported bluetoothd understands."""

    def __init__(self, on_release: Callable[[], None]):
        super().__init__("org.bluez.LEAdvertisement1")
        self.settings = AdvertSettings(name="")
        self._on_release = on_release

    @dbus_property(PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [uuid16(UUID_HID_SERVICE)]

    @dbus_property(PropertyAccess.READ)
    def Appearance(self) -> "q":
        return APPEARANCE_KEYBOARD

    @dbus_property(PropertyAccess.READ)
    def LocalName(self) -> "s":
        return fit_name(self.settings.name)

    @dbus_property(PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return self.settings.discoverable

    @method()
    def Release(self):
        self._on_release()


class TimedAdvertisement(BasicAdvertisement):
    """Adds the advertising interval."""

    @dbus_property(PropertyAccess.READ)
    def MinInterval(self) -> "u":
        return self.settings.interval[0]

    @dbus_property(PropertyAccess.READ)
    def MaxInterval(self) -> "u":
        return self.settings.interval[1]


class Advertiser:
    """Registers and unregisters our single advertisement with one adapter."""

    def __init__(self, bus: MessageBus, on_released: Callable[[], None] | None = None):
        self._bus = bus
        self._on_released = on_released
        self._timed = TimedAdvertisement(self._released)
        self._basic = BasicAdvertisement(self._released)
        self._exported = False
        self._use_basic = False
        self.adapter_path = ""
        self.active_path = ""          # object registered with bluetoothd, "" if none
        self.settings: AdvertSettings | None = None

    @property
    def active(self) -> bool:
        return bool(self.active_path)

    def export(self) -> None:
        if not self._exported:
            self._bus.export(ADVERT_PATH, self._timed)
            self._bus.export(ADVERT_BASIC_PATH, self._basic)
            self._exported = True

    def unexport(self) -> None:
        if self._exported:
            self._bus.unexport(ADVERT_PATH, self._timed)
            self._bus.unexport(ADVERT_BASIC_PATH, self._basic)
            self._exported = False

    def _released(self) -> None:
        if self.active_path:
            log.info("bluetoothd released our advertisement")
        self.active_path = ""
        self.settings = None
        if self._on_released is not None:
            self._on_released()

    def forget(self) -> None:
        """bluetoothd or the adapter went away: nothing is registered any more."""
        self.active_path = ""
        self.settings = None
        self._use_basic = False

    async def _register(self, adapter_path: str, path: str) -> None:
        await dbus.call(
            self._bus, BLUEZ, adapter_path, ADVERTISING_MANAGER, "RegisterAdvertisement", "oa{sv}", [path, {}],
            timeout=CALL_TIMEOUT,
        )  # fmt: skip

    async def _unregister(self, adapter_path: str, path: str) -> None:
        try:
            await dbus.call(
                self._bus, BLUEZ, adapter_path, ADVERTISING_MANAGER, "UnregisterAdvertisement", "o", [path],
                timeout=CALL_TIMEOUT,
            )  # fmt: skip
        except (DBusError, TimeoutError, OSError) as exc:
            # DoesNotExist (already released), bluetoothd gone, ...: either way it is not on air.
            log.debug("UnregisterAdvertisement(%s): %s", path, exc)

    def _unregister_blind(self, adapter_path: str, path: str) -> None:
        """Fire-and-forget unregistration, usable where awaiting is not possible."""
        try:
            self._bus.send(
                dbus.Message(
                    destination=BLUEZ, path=adapter_path, interface=ADVERTISING_MANAGER,
                    member="UnregisterAdvertisement", signature="o", body=[path],
                    flags=dbus.MessageFlag.NO_REPLY_EXPECTED,
                )
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - the bus is gone, and so is the advertisement
            log.debug("could not send UnregisterAdvertisement: %s", exc)

    async def start(self, adapter_path: str, settings: AdvertSettings) -> None:
        """Advertise with ``settings``; a no-op if exactly that is already on air.

        Raises ``DBusError`` / ``TimeoutError`` if bluetoothd refuses.
        """
        if self.active and self.adapter_path == adapter_path and self.settings == settings:
            return
        if self.active:
            await self.stop()
        self.export()
        self._timed.settings = self._basic.settings = settings
        self.adapter_path = adapter_path

        candidates = [ADVERT_BASIC_PATH] if self._use_basic else [ADVERT_PATH, ADVERT_BASIC_PATH]
        error: Exception | None = None
        for path in candidates:
            try:
                try:
                    await self._register(adapter_path, path)
                except DBusError as exc:
                    if exc.type != "org.bluez.Error.AlreadyExists":
                        raise
                    # A registration we lost track of (its reply timed out, say): start clean.
                    await self._unregister(adapter_path, path)
                    await self._register(adapter_path, path)
            except DBusError as exc:
                error = exc
                log.debug("RegisterAdvertisement(%s) failed: %s: %s", path, exc.type, exc.text)
                continue
            except BaseException:
                # Timed out or cancelled with the request possibly accepted: make sure nothing
                # stays on air behind our back.
                self._unregister_blind(adapter_path, path)
                raise
            if path == ADVERT_BASIC_PATH and not self._use_basic:
                log.warning("bluetoothd refused the advertising interval settings; advertising with its defaults")
                self._use_basic = True
            self.active_path = path
            self.settings = settings
            log.info(
                "advertising as %r (%s%s)",
                fit_name(settings.name),
                "discoverable" if settings.discoverable else "bonded TV only",
                "" if path == ADVERT_BASIC_PATH else f", {settings.interval[0]}-{settings.interval[1]} ms",
            )
            return
        assert error is not None
        raise error

    async def stop(self) -> None:
        """Take the advertisement off air.  Never raises."""
        if not self.active:
            return
        path, self.active_path, self.settings = self.active_path, "", None
        await self._unregister(self.adapter_path, path)
        log.info("advertising stopped")
