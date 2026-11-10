"""A live mirror of bluetoothd's object tree (adapters and devices).

BlueZ publishes everything through ``org.freedesktop.DBus.ObjectManager`` at ``/``.  The
mirror is filled by one ``GetManagedObjects`` call and kept current by three signals
(InterfacesAdded, InterfacesRemoved, PropertiesChanged) plus NameOwnerChanged, which tells us
when bluetoothd itself stops or starts.  The rest of the package asks the mirror instead of
making round-trips, and is told through one callback whenever anything changed.

The owner of ``org.bluez`` is tracked by its unique bus name because signals carry the unique
name as their sender, and because the bus connection is shared with other parts of the
service whose match rules deliver unrelated signals to the same handlers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .. import dbus
from ..dbus import DBusError, Message, MessageBus

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
BLUEZ_ROOT = "/org/bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
GATT_MANAGER = "org.bluez.GattManager1"
ADVERTISING_MANAGER = "org.bluez.LEAdvertisingManager1"
AGENT_MANAGER = "org.bluez.AgentManager1"

SYNC_TIMEOUT = 5.0
SYNC_RETRY_SECONDS = 2.0

_MATCH_RULES = (
    f"type='signal',sender='{dbus.DBUS_NAME}',interface='{dbus.DBUS_NAME}',member='NameOwnerChanged',arg0='{BLUEZ}'",
    f"type='signal',sender='{BLUEZ}',interface='{dbus.OBJECT_MANAGER}'",
    f"type='signal',sender='{BLUEZ}',interface='{dbus.PROPERTIES}',member='PropertiesChanged',arg0='{ADAPTER}'",
    f"type='signal',sender='{BLUEZ}',interface='{dbus.PROPERTIES}',member='PropertiesChanged',arg0='{DEVICE}'",
)

Objects = dict[str, dict[str, dict[str, Any]]]


class BluezWatcher:
    def __init__(
        self,
        bus: MessageBus,
        *,
        on_change: Callable[[], None],
        on_owner_change: Callable[[str | None], None],
    ):
        self._bus = bus
        self._on_change = on_change
        self._on_owner_change = on_owner_change
        self.owner: str | None = None   # unique bus name of bluetoothd, None while it is not running
        self.synced = False             # ``objects`` reflects the current bluetoothd
        self.objects: Objects = {}
        self._queued: list[Message] | None = None  # signals held back while a sync is in flight
        self._sync_task: asyncio.Task[None] | None = None
        self._stop_signals: Callable[[], None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------ life cycle

    async def start(self) -> None:
        """Subscribe and take the first snapshot.  Does not raise if bluetoothd is absent."""
        self._stopped = False
        self._stop_signals = dbus.on_signal(self._bus, self._handle_signal)
        for rule in _MATCH_RULES:
            await dbus.add_match(self._bus, rule)
        try:
            owner = await dbus.get_name_owner(self._bus, BLUEZ, timeout=SYNC_TIMEOUT)
        except (DBusError, TimeoutError, OSError) as exc:
            log.warning("could not ask the bus who owns %s: %s", BLUEZ, exc)
            owner = None
        if owner != self.owner:
            self._set_owner(owner, start_sync=False)
        if self.owner is not None and not self.synced and not await self._sync_once():
            self._kick_sync()

    async def stop(self) -> None:
        self._stopped = True
        if self._stop_signals is not None:
            self._stop_signals()
            self._stop_signals = None
        task, self._sync_task = self._sync_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for rule in _MATCH_RULES:
            await dbus.remove_match(self._bus, rule)

    # ------------------------------------------------------------------ queries

    def interface(self, path: str, interface: str) -> dict[str, Any] | None:
        return self.objects.get(path, {}).get(interface)

    def adapters(self) -> list[str]:
        """Paths of adapters that can host a GATT server and advertise, lowest index first."""

        def usable(interfaces: dict[str, Any]) -> bool:
            return ADAPTER in interfaces and GATT_MANAGER in interfaces and ADVERTISING_MANAGER in interfaces

        def index(path: str) -> tuple[int, str]:
            digits = "".join(ch for ch in path.rsplit("/", 1)[-1] if ch.isdigit())
            return (int(digits) if digits else 1 << 30, path)

        return sorted((path for path, interfaces in self.objects.items() if usable(interfaces)), key=index)

    def devices(self, adapter_path: str) -> dict[str, dict[str, Any]]:
        """``{device path: Device1 properties}`` of one adapter."""
        prefix = adapter_path + "/"
        return {
            path: interfaces[DEVICE]
            for path, interfaces in self.objects.items()
            if DEVICE in interfaces and path.startswith(prefix)
        }

    def forget_object(self, path: str) -> None:
        """Drop an object we know bluetoothd has just removed, ahead of its signal."""
        self.objects.pop(path, None)

    # ------------------------------------------------------------------ synchronisation

    def _set_owner(self, owner: str | None, *, start_sync: bool = True) -> None:
        self.owner = owner
        self.objects = {}
        self.synced = False
        self._queued = None
        log.info("bluetoothd %s", f"is running ({owner})" if owner else "is not running")
        self._notify(self._on_owner_change, owner)
        if owner is not None and start_sync:
            self._kick_sync()

    def _kick_sync(self) -> None:
        if self._stopped or (self._sync_task is not None and not self._sync_task.done()):
            return
        self._sync_task = asyncio.ensure_future(self._sync_loop())

    async def _sync_loop(self) -> None:
        while not self._stopped and self.owner is not None and not self.synced:
            if await self._sync_once():
                return
            await asyncio.sleep(SYNC_RETRY_SECONDS)

    async def _sync_once(self) -> bool:
        owner = self.owner
        if owner is None:
            return True
        # Signals that arrive while the snapshot is under way are replayed on top of it.  That
        # is correct whether bluetoothd emitted them before or after building the reply: the
        # last value signalled for a property is always its current one.
        self._queued = []
        try:
            objects = await dbus.get_managed_objects(self._bus, BLUEZ, "/", timeout=SYNC_TIMEOUT)
        except (DBusError, TimeoutError, OSError) as exc:
            if self.owner == owner:
                self._queued = None
                log.warning("could not read bluetoothd's objects: %s", exc)
            return False
        if self.owner != owner:
            return False  # bluetoothd restarted meanwhile; the new owner triggered its own sync
        queued, self._queued = self._queued or [], None
        self.objects = objects
        for message in queued:
            self._apply(message)
        self.synced = True
        self._notify(self._on_change)
        return True

    # ------------------------------------------------------------------ signals

    def _handle_signal(self, message: Message) -> None:
        try:
            if (
                message.member == "NameOwnerChanged"
                and message.sender == dbus.DBUS_NAME
                and message.path == dbus.DBUS_PATH
            ):
                if len(message.body) == 3 and message.body[0] == BLUEZ:
                    new_owner = message.body[2] or None
                    if new_owner != self.owner:
                        self._set_owner(new_owner)
                return
            if self.owner is None or message.sender != self.owner:
                return
            if message.interface not in (dbus.OBJECT_MANAGER, dbus.PROPERTIES):
                return
            if self._queued is not None:
                self._queued.append(message)
                return
            if not self.synced:
                return
            if self._apply(message):
                self._notify(self._on_change)
        except Exception:  # noqa: BLE001 - a malformed signal must never take the service down
            log.exception("failed to process a signal from bluetoothd")

    def _apply(self, message: Message) -> bool:
        """Fold one signal into ``objects``; True if it changed anything we mirror."""
        body = message.body
        if message.interface == dbus.OBJECT_MANAGER:
            if message.member == "InterfacesAdded" and len(body) == 2:
                path, interfaces = body[0], dbus.unwrap(body[1])
                if not isinstance(path, str) or not isinstance(interfaces, dict):
                    return False
                self.objects.setdefault(path, {}).update(interfaces)
                return True
            if message.member == "InterfacesRemoved" and len(body) == 2:
                path, names = body
                known = self.objects.get(path)
                if known is None:
                    return False
                for name in names:
                    known.pop(name, None)
                if not known:
                    del self.objects[path]
                return True
            return False
        if message.member == "PropertiesChanged" and len(body) == 3 and message.path:
            interface, changed, invalidated = body
            properties = self.objects.get(message.path, {}).get(interface)
            if properties is None:
                return False
            properties.update(dbus.unwrap(changed))
            for name in invalidated:
                properties.pop(name, None)
            return True
        return False

    @staticmethod
    def _notify(callback: Callable[..., None], *args: Any) -> None:
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - keep the mirror alive whatever a listener does
            log.exception("BlueZ change listener failed")
