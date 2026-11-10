"""A fake ``org.bluez`` for the private test bus, plus a fake TV and fake kernel sockets (raw HCI, management).

The fake follows what bluetoothd 5.79 does where the product depends on it:

* ``GattManager1.RegisterApplication`` reads the caller's objects with ``GetManagedObjects``,
  lays them out IN REPLY ORDER (service, per characteristic: declaration, value, a CCC it
  creates itself when the flags notify, then the exported descriptors), honours a pinned
  service ``Handle``, and writes allocated handles back with ``Properties.Set``.
* Notifications are ``PropertiesChanged(Value)`` on a characteristic, forwarded only to
  connected devices that wrote the CCC.  Everything else is DROPPED SILENTLY.
* CCC state lives in memory: it survives the disconnect of a bonded device, and is lost when
  the application is unregistered, the adapter goes away, or the daemon restarts.
  ``StartNotify`` is called for every CCC write that *changes* the value to "enabled"
  (``gatt_ccc_write_cb``: "If value is identical, then just succeed"), ``StopNotify`` when
  the last subscriber of a characteristic goes.
* ``RemoveDevice`` does not touch CCC state (gatt-database.c has no hook for it): the state of
  the removed device lingers under its identity address, without ``StopNotify``, until the
  next notification finds that its device no longer exists (``send_notification_to_device``)
  and drops it - or until the same TV pairs again, whose CCC writes then change nothing and
  cause no ``StartNotify``.
* The default agent is the head of a queue; the first agent ever registered becomes default,
  ``RequestDefaultAgent`` moves the caller to the head, and unregistering hands the role back.
* LE Secure Connections "Just Works" pairing asks the default agent with
  ``RequestAuthorization``; without a default agent or with ``Pairable`` off pairing fails;
  a device that is already bonded is refused without asking (JustWorksRepairing = never).
* ``RegisterAdvertisement`` records the client at once and answers later (advertising.c
  ``register_advertisement``): until then a second registration of the same object is
  ``AlreadyExists``, and ``UnregisterAdvertisement`` removes the pending client, whose
  registration then fails with "Failed to complete registration" (``client_free``).
* ``Device1.Disconnect`` answers only when the link is down; ``RemoveDevice`` answers before
  the ``InterfacesRemoved`` signal goes out.
* A device that first connected from a private address keeps that address in its object path
  until the daemon restarts, while its ``Address`` property becomes the identity address.

The fake TV models the Android HID host: it connects by itself when it is bonded and sees a
connectable advertisement, subscribes after pairing, and on the first connection after the
HID service was (re)registered it receives a Service Changed indication - after which an
Android 14 host subscribes again, while an Android 11-13 host neither subscribes nor listens
for notifications until its next connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any, Callable

from tcl_me_remo import dbus
from tcl_me_remo.bluez import BleRemote, mgmt, rawhci
from tcl_me_remo.bluez import remote as remote_module
from tcl_me_remo.config import BluetoothConfig
from tcl_me_remo.dbus import DBusError, Message, MessageFlag, MessageType, Variant

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ROOT = "/org/bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
GATT_MANAGER = "org.bluez.GattManager1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
AGENT_MANAGER = "org.bluez.AgentManager1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
GATT_DESC = "org.bluez.GattDescriptor1"
ADVERTISEMENT = "org.bluez.LEAdvertisement1"
AGENT = "org.bluez.Agent1"
PROPERTIES = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"

#: Highest handle of bluetoothd's built-in GAP / GATT / Device Information services.
BUILTIN_LAST_HANDLE = 0x0019

_SIGNATURES = {
    "Address": "s", "AddressType": "s", "Name": "s", "Alias": "s", "Adapter": "o",
    "Powered": "b", "Pairable": "b", "Discoverable": "b", "PairableTimeout": "u",
    "Paired": "b", "Bonded": "b", "Trusted": "b", "Connected": "b", "ServicesResolved": "b",
    "ActiveInstances": "y", "SupportedInstances": "y",
}  # fmt: skip

_CHRC_FLAGS = {
    "broadcast", "read", "write-without-response", "write", "notify", "indicate",
    "authenticated-signed-writes", "extended-properties", "reliable-write", "writable-auxiliaries",
    "encrypt-read", "encrypt-write", "encrypt-notify", "encrypt-indicate",
    "encrypt-authenticated-read", "encrypt-authenticated-write", "encrypt-authenticated-notify",
    "encrypt-authenticated-indicate", "secure-read", "secure-write", "secure-notify", "secure-indicate",
    "authorize",
}  # fmt: skip
_DESC_FLAGS = {
    "read", "write", "encrypt-read", "encrypt-write", "encrypt-authenticated-read",
    "encrypt-authenticated-write", "secure-read", "secure-write", "authorize",
}  # fmt: skip
_WRITABLE = {ADAPTER: {"Powered", "Pairable", "Discoverable", "Alias", "PairableTimeout"}, DEVICE: {"Trusted", "Alias"}}
_NOTIFY_FLAGS = {"notify", "indicate", "encrypt-notify", "encrypt-indicate", "secure-notify", "secure-indicate"}
_CAPABILITIES = {"", "DisplayOnly", "DisplayYesNo", "KeyboardOnly", "NoInputNoOutput", "KeyboardDisplay"}
#: Advertisement properties bluetoothd 5.79 parses, with their D-Bus types.
_ADVERT_PROPERTIES = {
    "Type": "s", "ServiceUUIDs": "as", "SolicitUUIDs": "as", "ManufacturerData": "a{qv}",
    "ServiceData": "a{sv}", "Includes": "as", "LocalName": "s", "Appearance": "q", "Duration": "q",
    "Timeout": "q", "Data": "a{yv}", "Discoverable": "b", "DiscoverableTimeout": "q",
    "SecondaryChannel": "s", "MinInterval": "u", "MaxInterval": "u", "TxPower": "n",
}  # fmt: skip


def bluez_error(short: str, text: str = "") -> DBusError:
    return DBusError(f"org.bluez.Error.{short}", text or short)


class AttError(Exception):
    """What the TV sees when an ATT request is refused (e.g. insufficient encryption)."""


async def eventually(predicate: Callable[[], Any], timeout: float = 3.0, what: str = "condition") -> None:
    """Poll until ``predicate()`` is truthy; fail the test if it never is."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def _short_uuid(uuid: str) -> str:
    """'00002a4d-0000-1000-8000-00805f9b34fb' or '2A4D' -> '2A4D'."""
    text = uuid.lower()
    if len(text) == 36 and text.endswith("-0000-1000-8000-00805f9b34fb") and text.startswith("0000"):
        return text[4:8].upper()
    return uuid.upper()


class _Attribute:
    def __init__(self, handle: int, kind: str, uuid: str, path: str = "", flags: list[str] | None = None):
        self.handle = handle
        self.kind = kind      # "service" | "declaration" | "value" | "ccc" | "descriptor"
        self.uuid = uuid      # short form
        self.path = path      # D-Bus object that backs it ("" for attributes bluetoothd owns)
        self.flags = flags or []

    def row(self) -> tuple[int, str, str, str]:
        return (self.handle, self.kind, self.uuid, self.path)


class _App:
    def __init__(self, owner: str, root: str):
        self.owner = owner
        self.root = root
        self.paths: list[str] = []                  # objects in the order they were listed
        self.attributes: list[_Attribute] = []
        self.chrcs: dict[str, dict[str, Any]] = {}  # path -> {uuid, flags, value_handle, descriptors}
        self.ccc: dict[str, set[str]] = {}          # chrc path -> addresses that enabled notifications

    def table(self) -> list[tuple[int, str, str, str]]:
        return [attribute.row() for attribute in self.attributes]

    def find_chrc(self, name: str) -> str:
        """Characteristic path by path suffix ("c1_map") or short UUID ("2A4B", first match)."""
        for path in self.chrcs:
            if path.endswith("/" + name):
                return path
        for path, info in self.chrcs.items():
            if info["uuid"] == name.upper():
                return path
        raise KeyError(name)


class FakeDevice:
    """A peer device as bluetoothd sees it, driven by the test as if it were the TV."""

    def __init__(
        self,
        bluez: FakeBluez,
        adapter: str,
        address: str,
        *,
        name: str,
        rpa: str,
        android: int,
        identity_type: str = "public",
    ):
        self.bluez = bluez
        self.adapter = adapter
        self.identity = address.upper()
        #: Kind of the identity address: "public", or "random" for a static random address.
        self.identity_type = identity_type
        self.rpa = rpa.upper()
        self.name = name
        self.android = android
        self.path_address = self.rpa or self.identity
        self.props: dict[str, Any] = {
            "Address": self.path_address,
            "AddressType": "random" if self.rpa else identity_type,
            "Name": name,
            "Alias": name or self.path_address.replace(":", "-"),
            "Adapter": adapter,
            "Paired": False,
            "Bonded": False,
            "Trusted": False,
            "Connected": False,
            "ServicesResolved": False,
        }
        self.present = False
        self.conn_handle = 0
        #: The kernel never resolved the TV's private address: it lists the link under that
        #: address although bluetoothd reports the device under its identity address.
        self.kernel_keeps_rpa = False
        #: ``hci_conn.state`` as the kernel's connection list reports it while the link exists.
        #: BT_DISCONN (8) is a link whose HCI Disconnect has been sent and not yet completed.
        self.hci_state = rawhci.BT_CONNECTED
        #: Behave like the Android HID host: reconnect when bonded and a connectable advert appears.
        self.auto_connect = False
        self.connect_delay = 0.02
        #: Android 14+: seconds between the Service Changed indication and the new CCC writes.
        self.resubscribe_delay = 0.05
        #: The Service Changed indication for the current HID registration is still to be delivered.
        self.service_changed_pending = False
        #: The HID host has registered for input notifications on this connection.
        self.listening = False
        self.received: list[tuple[int, bytes, str]] = []   # (value handle, value, "bluez" | "raw")
        self.ignored: list[tuple[int, bytes, str]] = []    # arrived while the HID host was not listening
        self.connections = 0
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ plumbing

    @property
    def path(self) -> str:
        return f"{self.adapter}/dev_{self.path_address.replace(':', '_')}"

    @property
    def connected(self) -> bool:
        return bool(self.props["Connected"])

    @property
    def bonded(self) -> bool:
        return bool(self.props["Bonded"])

    @property
    def hci_address(self) -> str:
        """The address the kernel lists for the link: the identity once the IRK is known."""
        if self.kernel_keeps_rpa and self.rpa:
            return self.rpa
        return self.identity if self.bonded else self.path_address

    @property
    def hci_address_type(self) -> str:
        """The kind of address the kernel tracks the link by ("public" or "random")."""
        return self.identity_type if self.hci_address == self.identity else "random"

    def _set(self, **changes: Any) -> None:
        self.props.update(changes)
        if self.present:
            self.bluez.emit_properties(self.path, DEVICE, changes)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _app(self) -> _App:
        apps = self.bluez.adapters[self.adapter].apps
        if not apps:
            raise AttError("no GATT application is registered")
        return next(iter(apps.values()))

    def keys(self, via: str | None = None) -> list[tuple[int, bytes]]:
        """Notifications the TV's HID host accepted, optionally only those that came one way."""
        return [(handle, value) for handle, value, how in self.received if via is None or how == via]

    # ------------------------------------------------------------------ link

    async def connect(self) -> None:
        adapter = self.bluez.adapters[self.adapter]
        if self.connected or not adapter.present or not adapter.props["Powered"]:
            return
        if not self.present:
            self.bluez.add_device_object(self)
        self.connections += 1
        self.conn_handle = 0x0040 + self.connections
        self._set(Connected=True)
        if self.bonded and self.service_changed_pending and adapter.apps:
            # The indication goes out as soon as the link is up.  Android 11-13 clear the HID
            # host's notification registrations for this connection; Android 14+ rediscover
            # the service and write the CCCs again.
            self.service_changed_pending = False
            self.listening = False
            if self.android >= 14:
                self._spawn(self._resubscribe_later())
        else:
            self.listening = self.bonded

    async def _resubscribe_later(self) -> None:
        await asyncio.sleep(self.resubscribe_delay)
        if self.connected:
            await self.subscribe()

    async def drop(self) -> None:
        """The link goes down (the TV went to standby, moved out of range, ...)."""
        if not self.connected:
            return
        self.listening = False
        self._set(Connected=False)
        if not self.bonded:
            await self.bluez.clear_ccc(self)

    disconnect = drop

    # ------------------------------------------------------------------ pairing

    async def pair(self, *, secure_connections: bool = True) -> bool:
        """Pair as the initiator (the TV's "pair accessory").  Returns whether it worked."""
        bluez = self.bluez
        adapter = bluez.adapters[self.adapter]
        if not self.connected:
            await self.connect()
        if not adapter.props["Pairable"] or bluez.default_agent is None:
            return False  # kernel: not bondable -> "pairing not supported"
        if secure_connections:
            if self.props["Paired"]:
                return False  # JustWorksRepairing = never: refused without asking the agent
            owner, path, _capability = bluez.default_agent
            try:
                await dbus.call(bluez.bus, owner, path, AGENT, "RequestAuthorization", "o", [self.path], timeout=2.0)
            except (DBusError, TimeoutError):
                return False
        if self.rpa:
            # Key distribution delivered the identity address; the object path stays.
            self._set(Address=self.identity, AddressType=self.identity_type)
        self._set(Paired=True, Bonded=True)
        self.listening = True
        return True

    async def pair_like_android(self) -> bool:
        """Pair, then do what the Android HID host does: read the service and subscribe."""
        if not await self.pair():
            return False
        await self.discover()
        await self.subscribe()
        return True

    # ------------------------------------------------------------------ GATT client side

    def _check(self, flags: list[str], plain: str) -> None:
        if plain in flags:
            return
        if f"encrypt-{plain}" in flags:
            if not self.props["Paired"]:
                raise AttError("insufficient encryption")
            return
        raise AttError(f"{plain} not permitted")

    def _options(self, **extra: Variant) -> dict[str, Variant]:
        return {"device": Variant("o", self.path), "link": Variant("s", "LE"), "mtu": Variant("q", 517), **extra}

    async def read(self, name: str, offset: int = 0) -> bytes:
        """Read a characteristic value (by path suffix or short UUID) from the given offset."""
        app = self._app()
        path = app.find_chrc(name)
        self._check(app.chrcs[path]["flags"], "read")
        # bluetoothd passes "offset" only when it is not zero.
        options = self._options(offset=Variant("q", offset)) if offset else self._options()
        body = await dbus.call(self.bluez.bus, app.owner, path, GATT_CHRC, "ReadValue", "a{sv}", [options], timeout=2.0)
        return bytes(body[0])

    async def read_long(self, name: str, chunk: int = 22) -> bytes:
        """Read a value the way a client with a small MTU does: Read, then Read Blob."""
        value = b""
        while True:
            part = await self.read(name, len(value))
            value += part[:chunk]
            if len(part) <= chunk:
                return value

    async def read_descriptor(self, chrc: str, uuid: str = "2908") -> bytes:
        app = self._app()
        info = app.chrcs[app.find_chrc(chrc)]
        path, flags = next((p, f) for p, u, f in info["descriptors"] if u == uuid.upper())
        self._check(flags, "read")
        body = await dbus.call(
            self.bluez.bus, app.owner, path, GATT_DESC, "ReadValue", "a{sv}", [self._options()], timeout=2.0
        )
        return bytes(body[0])

    async def write(self, name: str, value: bytes) -> None:
        app = self._app()
        path = app.find_chrc(name)
        self._check(app.chrcs[path]["flags"], "write")
        options = self._options(type=Variant("s", "command"))
        await dbus.call(
            self.bluez.bus, app.owner, path, GATT_CHRC, "WriteValue", "aya{sv}", [bytes(value), options], timeout=2.0
        )

    async def discover(self) -> dict[str, bytes]:
        """The reads an Android HID host performs after pairing."""
        found = {"info": await self.read("2A4A"), "map": await self.read_long("2A4B")}
        app = self._app()
        for path, info in app.chrcs.items():
            if info["uuid"] == "2A4D":
                found[path.rsplit("/", 1)[-1]] = await self.read_descriptor(path.rsplit("/", 1)[-1])
        return found

    def _notifying_chrcs(self, app: _App) -> list[str]:
        return [path for path, info in app.chrcs.items() if _NOTIFY_FLAGS & set(info["flags"])]

    async def subscribe(self, only: str | None = None) -> None:
        """Write the CCC of every notifying characteristic, like the HID host does.

        ``only`` (a path suffix such as "c4_kbd") writes just that one: the state between the
        first and the last of the host's three writes.
        """
        app = self._app()
        if not self.connected:
            raise AttError("not connected")
        for path in self._notifying_chrcs(app):
            if only is not None and not path.endswith("/" + only):
                continue
            flags = app.chrcs[path]["flags"]
            if "encrypt-notify" in flags and not self.props["Paired"]:
                raise AttError("insufficient encryption")
            subscribers = app.ccc.setdefault(path, set())
            if self.identity in subscribers:
                continue  # "If value is identical, then just succeed": no StartNotify
            subscribers.add(self.identity)
            # "Always call StartNotify for an incoming enable"
            await dbus.call(self.bluez.bus, app.owner, path, GATT_CHRC, "StartNotify", timeout=2.0)
        self.listening = True

    async def unsubscribe(self) -> None:
        await self.bluez.clear_ccc(self)


class _Adapter:
    def __init__(self, name: str, address: str):
        self.name = name
        self.path = f"{ROOT}/{name}"
        self.present = True
        self.props: dict[str, Any] = {
            "Address": address,
            "AddressType": "public",
            "Name": "steamdeck",
            "Alias": "steamdeck",
            "Powered": True,
            "Pairable": False,
            "Discoverable": False,
            "PairableTimeout": 0,
        }
        self.reset()

    def reset(self) -> None:
        """Everything bluetoothd keeps in memory for this adapter."""
        self.apps: dict[tuple[str, str], _App] = {}
        self.adverts: dict[tuple[str, str], dict[str, Any]] = {}
        #: Registrations received and not answered yet, each with a token of its own: bluetoothd
        #: keeps one client object per registration, so a registration that was unregistered
        #: while pending and a new one for the same path are two things (src/advertising.c).
        self.pending_adverts: dict[tuple[str, str], object] = {}
        self.ranges: list[tuple[int, int, tuple[str, str]]] = []
        self.last_handle = BUILTIN_LAST_HANDLE


class FakeBluez:
    def __init__(self, adapters: tuple[str, ...] = ("hci0",)):
        self.bus: Any = None
        made = [_Adapter(name, f"11:22:33:44:55:{index:02X}") for index, name in enumerate(adapters)]
        self.adapters = {adapter.path: adapter for adapter in made}
        self.devices: list[FakeDevice] = []
        self.agents: dict[str, tuple[str, str]] = {}   # owner -> (path, capability)
        self.default_agents: list[str] = []            # owners, head is the default agent
        self.objects: dict[str, dict[str, dict[str, Any]]] = {}
        #: Chronological log: ("gatt_registered", root), ("advert_registered", props), ...
        self.events: list[tuple[str, Any]] = []
        #: Every Properties.Set a client performed: (path, interface, name, value).
        self.property_sets: list[tuple[str, str, str, Any]] = []
        self.dropped_notifications = 0
        # knobs
        self.reject_pinned_handle = False
        self.fail_register_application = 0          # fail this many RegisterApplication calls
        self.unsupported_advert_properties: set[str] = set()
        self.max_adverts = 5
        self.disconnect_delay = 0.03                # real bluetoothd: 2 s
        self.disconnect_calls = 0                   # Device1.Disconnect calls received
        self.power_on_delay = 0.02
        self.advert_register_delay = 0.0            # a busy bluetoothd answers RegisterAdvertisement late
        self._tasks: set[asyncio.Task[Any]] = set()
        self._matched: set[str] = set()
        self.running = False

    # ------------------------------------------------------------------ daemon life cycle

    async def start(self) -> None:
        self.bus = await dbus.connect_system_bus()
        self.bus.add_message_handler(self._on_message)
        await dbus.add_match(
            self.bus, "type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg2=''"
        )
        self.objects = {ROOT: {AGENT_MANAGER: {}}}
        for adapter in self.adapters.values():
            if adapter.present:
                self._publish_adapter(adapter, announce=False)
        self.running = True
        await self.bus.request_name(BLUEZ)

    async def stop(self) -> None:
        """bluetoothd exits: every registration, subscription and unbonded device is gone."""
        self.running = False
        tasks = list(self._tasks) + [task for device in self.devices for task in device._tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None
        self.agents.clear()
        self.default_agents.clear()
        self._matched.clear()
        for adapter in self.adapters.values():
            adapter.reset()
        for device in list(self.devices):
            device.present = False
            device.props["Connected"] = False
            device.listening = False
            if device.bonded:
                device.path_address = device.identity   # the path follows the identity from now on
                device.service_changed_pending = True   # restored from the bond file at start-up
            else:
                self.devices.remove(device)
        self.objects = {}

    async def restart(self, downtime: float = 0.02) -> None:
        await self.stop()
        await asyncio.sleep(downtime)
        await self.start()

    def _publish_adapter(self, adapter: _Adapter, *, announce: bool) -> None:
        interfaces = {
            ADAPTER: adapter.props,
            GATT_MANAGER: {},
            ADV_MANAGER: {"ActiveInstances": 0, "SupportedInstances": self.max_adverts},
        }
        self._add_object(adapter.path, interfaces, announce=announce)
        for device in self.devices:
            if device.adapter == adapter.path and device.bonded:
                self.add_device_object(device, announce=announce)

    async def remove_adapter(self, name: str = "hci0") -> None:
        """The controller disappears (USB re-enumeration, driver reload)."""
        adapter = self.adapters[f"{ROOT}/{name}"]
        adapter.present = False
        adapter.reset()
        for device in list(self.devices):
            if device.adapter != adapter.path:
                continue
            device.props["Connected"] = False
            device.listening = False
            if device.present:
                self._remove_object(device.path)
                device.present = False
            if device.bonded:
                device.service_changed_pending = True
            else:
                self.devices.remove(device)
        self._remove_object(adapter.path)

    async def add_adapter(self, name: str = "hci0") -> None:
        adapter = self.adapters[f"{ROOT}/{name}"]
        adapter.present = True
        self._publish_adapter(adapter, announce=True)

    def set_powered(self, powered: bool, name: str = "hci0") -> None:
        adapter = self.adapters[f"{ROOT}/{name}"]
        adapter.props["Powered"] = powered
        self.emit_properties(adapter.path, ADAPTER, {"Powered": powered})
        if not powered:
            for device in self.devices:
                if device.adapter == adapter.path and device.connected:
                    device.listening = False
                    device._set(Connected=False)

    # ------------------------------------------------------------------ devices

    def add_device(
        self,
        address: str = "AA:BB:CC:DD:EE:FF",
        *,
        name: str = "Living Room TV",
        rpa: str = "",
        android: int = 12,
        adapter: str = "hci0",
        bonded: bool = False,
        identity_type: str = "public",
    ) -> FakeDevice:
        """Create a peer.  ``bonded=True`` gives a TV that paired in an earlier daemon life."""
        device = FakeDevice(
            self, f"{ROOT}/{adapter}", address, name=name, rpa=rpa, android=android, identity_type=identity_type
        )
        self.devices.append(device)
        if bonded:
            device.path_address = device.identity
            device.props.update(
                Address=device.identity, AddressType=identity_type, Paired=True, Bonded=True, Trusted=True
            )
            device.service_changed_pending = True
            if self.running:
                self.add_device_object(device)
        return device

    def add_device_object(self, device: FakeDevice, *, announce: bool = True) -> None:
        device.present = True
        self._add_object(device.path, {DEVICE: device.props}, announce=announce)

    def device_at(self, path: str) -> FakeDevice | None:
        return next((device for device in self.devices if device.present and device.path == path), None)

    async def clear_ccc(self, device: FakeDevice) -> None:
        """Drop the device's subscriptions; StopNotify where it was the last subscriber."""
        for adapter in self.adapters.values():
            for app in adapter.apps.values():
                for path in self._drop_device_state(app, device.identity):
                    if self.bus is not None:
                        with contextlib.suppress(DBusError, TimeoutError):
                            await dbus.call(self.bus, app.owner, path, GATT_CHRC, "StopNotify", timeout=2.0)

    @staticmethod
    def _drop_device_state(app: _App, address: str) -> list[str]:
        """Forget every CCC of one device; returns the characteristics left without a subscriber."""
        orphaned = []
        for path, subscribers in app.ccc.items():
            if address in subscribers:
                subscribers.discard(address)
                if not subscribers:
                    orphaned.append(path)
        return orphaned

    # ------------------------------------------------------------------ views for tests

    @property
    def adapter(self) -> _Adapter:
        return next(iter(self.adapters.values()))

    @property
    def app(self) -> _App | None:
        return next(iter(self.adapter.apps.values()), None)

    @property
    def advert(self) -> dict[str, Any] | None:
        """Properties of the connectable advertisement that is on air, or None."""
        return next((props for props in self.adapter.adverts.values() if props["Type"] == "peripheral"), None)

    @property
    def beacon(self) -> dict[str, Any] | None:
        """Properties of the broadcast (non-connectable) advertisement that is on air, or None."""
        return next((props for props in self.adapter.adverts.values() if props["Type"] == "broadcast"), None)

    @property
    def default_agent(self) -> tuple[str, str, str] | None:
        if not self.default_agents:
            return None
        owner = self.default_agents[0]
        return (owner, *self.agents[owner])

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    async def release_advertisement(self) -> None:
        """bluetoothd drops our advertisement on its own and tells us with Release()."""
        for (owner, path), _props in list(self.adapter.adverts.items()):
            del self.adapter.adverts[(owner, path)]
            self.events.append(("advert_released", path))
            self.bus.send(
                Message(
                    destination=owner, path=path, interface=ADVERTISEMENT, member="Release",
                    flags=MessageFlag.NO_REPLY_EXPECTED,
                )
            )  # fmt: skip

    # ------------------------------------------------------------------ object tree and signals

    def _wire(self, props: dict[str, Any]) -> dict[str, Variant]:
        return {name: Variant(_SIGNATURES[name], value) for name, value in props.items()}

    def _add_object(self, path: str, interfaces: dict[str, dict[str, Any]], *, announce: bool) -> None:
        self.objects[path] = interfaces
        if announce and self.bus is not None:
            body = [path, {name: self._wire(props) for name, props in interfaces.items()}]
            self.bus.send(Message.new_signal("/", OBJECT_MANAGER, "InterfacesAdded", "oa{sa{sv}}", body))

    def _remove_object(self, path: str) -> None:
        interfaces = self.objects.pop(path, None)
        if interfaces is not None and self.bus is not None:
            self.bus.send(
                Message.new_signal("/", OBJECT_MANAGER, "InterfacesRemoved", "oas", [path, list(interfaces)])
            )

    def emit_properties(self, path: str, interface: str, changes: dict[str, Any]) -> None:
        if self.bus is not None and path in self.objects:
            body = [interface, self._wire(changes), []]
            self.bus.send(Message.new_signal(path, PROPERTIES, "PropertiesChanged", "sa{sv}as", body))

    # ------------------------------------------------------------------ message handling

    def _on_message(self, msg: Message) -> bool | None:
        if msg.message_type == MessageType.SIGNAL:
            self._on_signal(msg)
            return None
        if msg.message_type != MessageType.METHOD_CALL:
            return None
        task = asyncio.ensure_future(self._dispatch(msg))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _dispatch(self, msg: Message) -> None:
        after: list[Callable[[], None]] = []
        try:
            signature, body = await self._call(msg, after)
            reply = Message.new_method_return(msg, signature, body)
        except DBusError as exc:
            reply = Message.new_error(msg, exc.type, exc.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bug in the fake must surface in the test log
            log.exception("fake bluez failed handling %s.%s", msg.interface, msg.member)
            reply = Message.new_error(msg, "org.bluez.Error.Failed", f"fake bluez bug: {exc!r}")
        if self.bus is not None:
            if not msg.flags & MessageFlag.NO_REPLY_EXPECTED:
                self.bus.send(reply)
            for action in after:
                action()

    async def _call(self, msg: Message, after: list[Callable[[], None]]) -> tuple[str, list[Any]]:
        path, interface, member, body = msg.path, msg.interface, msg.member, msg.body
        if interface == OBJECT_MANAGER and member == "GetManagedObjects" and path == "/":
            tree = {
                obj: {name: self._wire(props) for name, props in interfaces.items()}
                for obj, interfaces in self.objects.items()
            }
            return "a{oa{sa{sv}}}", [tree]
        if interface == PROPERTIES:
            return await self._properties(msg)
        if path == ROOT and interface == AGENT_MANAGER:
            return self._agent_manager(msg)
        adapter = self.adapters.get(path)
        if adapter is not None and adapter.present:
            if interface == GATT_MANAGER and member == "RegisterApplication":
                return await self._register_application(adapter, msg)
            if interface == GATT_MANAGER and member == "UnregisterApplication":
                if adapter.apps.pop((msg.sender, body[0]), None) is None:
                    raise bluez_error("DoesNotExist")
                adapter.ranges = [entry for entry in adapter.ranges if entry[2] != (msg.sender, body[0])]
                self.events.append(("gatt_unregistered", body[0]))
                return "", []
            if interface == ADV_MANAGER and member == "RegisterAdvertisement":
                return await self._register_advertisement(adapter, msg)
            if interface == ADV_MANAGER and member == "UnregisterAdvertisement":
                key = (msg.sender, body[0])
                if key in adapter.pending_adverts:
                    del adapter.pending_adverts[key]  # the registration under way fails
                    return "", []
                if adapter.adverts.pop(key, None) is None:
                    raise bluez_error("DoesNotExist")
                self.events.append(("advert_unregistered", body[0]))
                return "", []
            if interface == ADAPTER and member == "RemoveDevice":
                return await self._remove_device(adapter, body[0], after)
        device = self.device_at(path) if path else None
        if device is not None and interface == DEVICE and member == "Disconnect":
            self.disconnect_calls += 1
            if device.connected:
                await asyncio.sleep(self.disconnect_delay)  # the reply comes once the link is down
                await device.drop()
            return "", []
        raise DBusError("org.freedesktop.DBus.Error.UnknownMethod", f"no {interface}.{member} at {path}")

    async def _properties(self, msg: Message) -> tuple[str, list[Any]]:
        interfaces = self.objects.get(msg.path)
        if interfaces is None:
            raise DBusError("org.freedesktop.DBus.Error.UnknownObject", f"no object at {msg.path}")
        props = interfaces.get(msg.body[0])
        if props is None:
            raise DBusError("org.freedesktop.DBus.Error.UnknownInterface", msg.body[0])
        if msg.member == "GetAll":
            return "a{sv}", [self._wire(props)]
        name = msg.body[1]
        if name not in props:
            raise DBusError("org.freedesktop.DBus.Error.UnknownProperty", name)
        if msg.member == "Get":
            return "v", [Variant(_SIGNATURES[name], props[name])]
        if msg.member != "Set":
            raise DBusError("org.freedesktop.DBus.Error.UnknownMethod", msg.member)
        value = msg.body[2].value
        self.property_sets.append((msg.path, msg.body[0], name, value))
        if name not in _WRITABLE.get(msg.body[0], set()):
            raise DBusError("org.freedesktop.DBus.Error.PropertyReadOnly", name)
        if msg.body[0] == ADAPTER and name == "Powered" and value and not props["Powered"]:
            await asyncio.sleep(self.power_on_delay)
        if props[name] != value:
            if msg.body[0] == ADAPTER and name == "Powered":
                self.set_powered(bool(value), msg.path.rsplit("/", 1)[-1])
            else:
                props[name] = value
                self.emit_properties(msg.path, msg.body[0], {name: value})
        return "", []

    def _on_signal(self, msg: Message) -> None:
        if msg.member == "NameOwnerChanged" and msg.sender == "org.freedesktop.DBus":
            name, _old, new = msg.body
            if name.startswith(":") and not new:
                self._client_gone(name)
            return
        if msg.interface == PROPERTIES and msg.member == "PropertiesChanged" and msg.body[0] == GATT_CHRC:
            value = msg.body[1].get("Value")
            if value is not None:
                self._forward_notification(msg.sender, msg.path, bytes(value.value))

    def _client_gone(self, owner: str) -> None:
        """A client left the bus: bluetoothd drops everything it had registered."""
        if owner in self.agents:
            del self.agents[owner]
            self.default_agents.remove(owner)
        for adapter in self.adapters.values():
            for key in [key for key in adapter.apps if key[0] == owner]:
                del adapter.apps[key]
                adapter.ranges = [entry for entry in adapter.ranges if entry[2] != key]
            for key in [key for key in adapter.adverts if key[0] == owner]:
                del adapter.adverts[key]

    def _forward_notification(self, owner: str, path: str, value: bytes) -> None:
        for adapter in self.adapters.values():
            for app in adapter.apps.values():
                if app.owner != owner or path not in app.chrcs:
                    continue
                handle = app.chrcs[path]["value_handle"]
                subscribers = app.ccc.get(path, set())
                delivered = False
                known = {device.identity: device for device in self.devices if device.adapter == adapter.path}
                for address in sorted(subscribers):
                    # send_notification_to_device(): no CCC state for the device -> return.
                    device = known.get(address)
                    if device is not None and device.connected:
                        (device.received if device.listening else device.ignored).append((handle, value, "bluez"))
                        delivered = True
                    elif device is None or not device.bonded:
                        # "Remove device state if device no longer exists or is not paired":
                        # this is where the state of a removed device finally goes, and with
                        # it StopNotify for whatever it was the last subscriber of.
                        for orphan in self._drop_device_state(app, address):
                            self.bus.send(
                                Message(
                                    destination=app.owner, path=orphan, interface=GATT_CHRC, member="StopNotify",
                                    flags=MessageFlag.NO_REPLY_EXPECTED,
                                )
                            )  # fmt: skip
                if not delivered:
                    self.dropped_notifications += 1

    # ------------------------------------------------------------------ AgentManager1

    def _agent_manager(self, msg: Message) -> tuple[str, list[Any]]:
        owner, member, body = msg.sender, msg.member, msg.body
        if member == "RegisterAgent":
            if owner in self.agents:
                raise bluez_error("AlreadyExists")
            if body[1] not in _CAPABILITIES:
                raise bluez_error("InvalidArguments")
            self.agents[owner] = (body[0], body[1])
            self.default_agents.append(owner)  # the first agent ever becomes default, others queue up
            self.events.append(("agent_registered", body[1]))
            return "", []
        if member in ("RequestDefaultAgent", "UnregisterAgent"):
            if owner not in self.agents or self.agents[owner][0] != body[0]:
                raise bluez_error("DoesNotExist")
            self.default_agents.remove(owner)
            if member == "RequestDefaultAgent":
                self.default_agents.insert(0, owner)
            else:
                del self.agents[owner]
                self.events.append(("agent_unregistered", body[0]))
            return "", []
        raise DBusError("org.freedesktop.DBus.Error.UnknownMethod", member)

    # ------------------------------------------------------------------ Adapter1.RemoveDevice

    async def _remove_device(
        self, adapter: _Adapter, path: str, after: list[Callable[[], None]]
    ) -> tuple[str, list[Any]]:
        device = self.device_at(path)
        if device is None or device.adapter != adapter.path:
            raise bluez_error("DoesNotExist")
        if not adapter.props["Powered"]:
            raise bluez_error("NotReady")
        if device.connected:
            # The link goes down while the device is still bonded, so its CCC state is kept
            # (att_disconnected); nothing looks at it again when the device is then removed.
            await asyncio.sleep(self.disconnect_delay)
            await device.drop()
        device.props.update(Paired=False, Bonded=False, Trusted=False)
        device.service_changed_pending = False
        device.present = False
        self.devices.remove(device)
        self.events.append(("device_removed", path))
        # bluetoothd sends InterfacesRemoved from an idle callback, i.e. after the method reply.
        after.append(lambda: self._remove_object(path))
        self.objects[path] = self.objects.get(path, {})
        return "", []

    # ------------------------------------------------------------------ GattManager1

    async def _register_application(self, adapter: _Adapter, msg: Message) -> tuple[str, list[Any]]:
        owner, root = msg.sender, msg.body[0]
        if (owner, root) in adapter.apps:
            raise bluez_error("AlreadyExists")
        if self.fail_register_application > 0:
            self.fail_register_application -= 1
            raise bluez_error("Failed", "No object received")
        body = await dbus.call(self.bus, owner, root, OBJECT_MANAGER, "GetManagedObjects", timeout=2.0)
        listed: dict[str, dict[str, dict[str, Variant]]] = body[0]
        app = _App(owner, root)
        app.paths = list(listed)

        def prop(props: dict[str, Variant], name: str, signature: str, default: Any = None) -> Any:
            variant = props.get(name)
            if variant is None:
                if default is None:
                    raise bluez_error("Failed", f'Failed to read "{name}" property')
                return default
            if variant.signature != signature:
                raise bluez_error("Failed", f'"{name}" has type {variant.signature}, expected {signature}')
            return variant.value

        services = [(path, ifs[GATT_SERVICE]) for path, ifs in listed.items() if GATT_SERVICE in ifs]
        chrcs = [(path, ifs[GATT_CHRC]) for path, ifs in listed.items() if GATT_CHRC in ifs]
        descs = [(path, ifs[GATT_DESC]) for path, ifs in listed.items() if GATT_DESC in ifs]
        if not services:
            raise bluez_error("Failed", "No object received")
        write_back: list[tuple[str, str, int]] = []
        for service_path, service in services:
            uuid = _short_uuid(prop(service, "UUID", "s"))
            if uuid in ("1800", "1801"):
                raise bluez_error("Failed", "GAP and GATT services must be handled by BlueZ")
            prop(service, "Primary", "b")
            mine = [(path, c) for path, c in chrcs if prop(c, "Service", "o") == service_path]
            layout: list[tuple[str, str, str, list[str]]] = []  # (kind, uuid, path, flags) after the service
            for chrc_path, chrc in mine:
                flags = list(prop(chrc, "Flags", "as"))
                if unknown := set(flags) - _CHRC_FLAGS:
                    raise bluez_error("Failed", f"Invalid characteristic flag: {sorted(unknown)[0]}")
                chrc_uuid = _short_uuid(prop(chrc, "UUID", "s"))
                layout.append(("declaration", "2803", chrc_path, flags))
                layout.append(("value", chrc_uuid, chrc_path, flags))
                if _NOTIFY_FLAGS & set(flags):
                    layout.append(("ccc", "2902", "", []))  # created by bluetoothd itself
                for desc_path, desc in descs:
                    if prop(desc, "Characteristic", "o") != chrc_path:
                        continue
                    desc_uuid = _short_uuid(prop(desc, "UUID", "s"))
                    desc_flags = list(prop(desc, "Flags", "as"))
                    if desc_uuid == "2902":
                        raise bluez_error("Failed", "applications must not export a CCC (0x2902) descriptor")
                    if unknown := set(desc_flags) - _DESC_FLAGS:
                        raise bluez_error("Failed", f"Invalid descriptor flag: {sorted(unknown)[0]}")
                    layout.append(("descriptor", desc_uuid, desc_path, desc_flags))
            pinned = prop(service, "Handle", "q", 0)
            if pinned and self.reject_pinned_handle:
                raise bluez_error("Failed", "Failed to create entry in database")
            start = pinned or adapter.last_handle + 1
            end = start + len(layout)
            if end > 0xFFFF or any(start <= hi and lo <= end for lo, hi, _ in adapter.ranges) or (
                pinned and start <= BUILTIN_LAST_HANDLE
            ):
                raise bluez_error("Failed", "Failed to create entry in database")
            adapter.ranges.append((start, end, (owner, root)))
            adapter.last_handle = max(adapter.last_handle, end)

            app.attributes.append(_Attribute(start, "service", uuid, service_path))
            if "Handle" in service and not pinned:
                write_back.append((service_path, GATT_SERVICE, start))
            for offset, (kind, attr_uuid, path, flags) in enumerate(layout, start=1):
                handle = start + offset
                app.attributes.append(_Attribute(handle, kind, attr_uuid, path, flags))
                if kind == "value":
                    app.chrcs[path] = {"uuid": attr_uuid, "flags": flags, "value_handle": handle, "descriptors": []}
                    if "Handle" in listed[path][GATT_CHRC] and not listed[path][GATT_CHRC]["Handle"].value:
                        write_back.append((path, GATT_CHRC, handle))  # the VALUE handle
                elif kind == "descriptor":
                    owner_chrc = prop(listed[path][GATT_DESC], "Characteristic", "o")
                    app.chrcs[owner_chrc]["descriptors"].append((path, attr_uuid, flags))
                    if "Handle" in listed[path][GATT_DESC] and not listed[path][GATT_DESC]["Handle"].value:
                        write_back.append((path, GATT_DESC, handle))

        adapter.apps[(owner, root)] = app
        rule = f"type='signal',sender='{owner}',interface='{PROPERTIES}',member='PropertiesChanged'"
        if rule not in self._matched:
            self._matched.add(rule)
            await dbus.add_match(self.bus, rule)
        # Like g_dbus_proxy_set_property_basic(): fire the Set calls, do not wait for replies.
        # They leave before the RegisterApplication reply does.
        for path, interface, handle in write_back:
            self.bus.send(
                Message(
                    destination=owner, path=path, interface=PROPERTIES, member="Set", signature="ssv",
                    body=[interface, "Handle", Variant("q", handle)],
                )
            )  # fmt: skip
        self.events.append(("gatt_registered", root))
        # A changed database means a Service Changed indication for every bonded device,
        # sent at once if it is connected and on its next connection otherwise.
        for device in self.devices:
            if device.adapter == adapter.path and device.bonded:
                if device.connected:
                    device.listening = False
                    if device.android >= 14:
                        device._spawn(device._resubscribe_later())
                else:
                    device.service_changed_pending = True
        return "", []

    # ------------------------------------------------------------------ LEAdvertisingManager1

    async def _register_advertisement(self, adapter: _Adapter, msg: Message) -> tuple[str, list[Any]]:
        owner, path = msg.sender, msg.body[0]
        key = (owner, path)
        if key in adapter.adverts or key in adapter.pending_adverts:
            raise bluez_error("AlreadyExists")
        if len(adapter.adverts) + len(adapter.pending_adverts) >= self.max_adverts:
            raise bluez_error("NotPermitted", "Maximum advertisements reached")
        mine = adapter.pending_adverts[key] = object()
        try:
            props = await self._parse_advertisement(owner, path)
            if adapter.pending_adverts.get(key) is not mine:
                # Unregistered meanwhile (client_free: "Failed to complete registration"),
                # whether or not the same path has been registered anew since.
                raise bluez_error("Failed", "Failed to complete registration")
        finally:
            if adapter.pending_adverts.get(key) is mine:
                del adapter.pending_adverts[key]
        adapter.adverts[key] = props
        self.events.append(("advert_registered", dict(props)))
        if props["Type"] == "peripheral":
            for device in self.devices:
                if device.adapter == adapter.path and device.auto_connect and device.bonded and not device.connected:
                    device._spawn(self._connect_if_advertising(adapter, device))
        return "", []

    async def _parse_advertisement(self, owner: str, path: str) -> dict[str, Any]:
        if self.advert_register_delay > 0:
            await asyncio.sleep(self.advert_register_delay)
        body = await dbus.call(self.bus, owner, path, PROPERTIES, "GetAll", "s", [ADVERTISEMENT], timeout=2.0)
        raw: dict[str, Variant] = body[0]
        parse_error = bluez_error("Failed", "Failed to parse advertisement.")
        for name, variant in raw.items():
            expected = _ADVERT_PROPERTIES.get(name)
            if expected is None:
                continue  # bluetoothd only looks at the properties it knows
            if name in self.unsupported_advert_properties or variant.signature != expected:
                raise parse_error
        props = dbus.unwrap(raw)
        if props.get("Type") not in ("peripheral", "broadcast"):
            raise parse_error
        for name in ("MinInterval", "MaxInterval"):
            if name in props and not 20 <= props[name] <= 10_485_759:
                raise parse_error
        if props.get("MinInterval", 0) > props.get("MaxInterval", 1 << 32):
            raise parse_error
        if "LocalName" in props and "local-name" in props.get("Includes", []):
            raise parse_error
        if props["Type"] == "broadcast" and props.get("Discoverable"):
            raise parse_error  # "Broadcast cannot set flags"
        payload = 3 if props["Type"] == "peripheral" else 0                      # Flags
        if props.get("ServiceUUIDs"):
            payload += 2 + 2 * len(props["ServiceUUIDs"])
        payload += 4 if "Appearance" in props else 0
        payload += sum(4 + len(data) for data in props.get("ManufacturerData", {}).values())
        if payload > 31 or 2 + len(props.get("LocalName", "").encode()) > 31:
            raise bluez_error("InvalidLength")
        return props

    async def _connect_if_advertising(self, adapter: _Adapter, device: FakeDevice) -> None:
        await asyncio.sleep(device.connect_delay)
        # Only a connectable advertisement can be answered; the wake beacon is a broadcast.
        connectable = any(props["Type"] == "peripheral" for props in adapter.adverts.values())
        if connectable and adapter.present and device in self.devices:
            await device.connect()


# --------------------------------------------------------------------------- fake raw HCI


class FakeRawHub:
    """Stands in for the kernel side of a raw HCI socket; shared by all backends it hands out."""

    def __init__(self, bluez: FakeBluez):
        self.bluez = bluez
        self.open_error: OSError | None = None
        self.send_error: OSError | None = None
        self.opened = 0
        self.closed = 0
        self.packets: list[bytes] = []    # every ACL data packet written
        self.commands: list[bytes] = []   # every HCI command packet written
        #: What the controller answers to LE Read Buffer Size: its number of LE data buffers
        #: (0 = one pool shared with classic Bluetooth), or None for no answer at all.
        self.le_buffers: int | None = 8
        self.filters: list[bytes] = []    # every event filter set on a socket

    def backend(self) -> _FakeRawBackend:
        return _FakeRawBackend(self)

    def link(self, **kwargs: Any) -> rawhci.RawLink:
        return rawhci.RawLink(self.backend, **kwargs)

    def connections(self, dev_id: int) -> list[rawhci.HciConnection]:
        adapter = f"{ROOT}/hci{dev_id}"
        return [
            rawhci.HciConnection(device.conn_handle, device.hci_address, rawhci.LE_LINK, False, device.hci_state)
            for device in self.bluez.devices
            if device.adapter == adapter and device.connected
        ]

    def write(self, dev_id: int, packet: bytes) -> None:
        if self.send_error is not None:
            raise self.send_error
        if packet[0] == rawhci.HCI_COMMAND_PKT:
            self.commands.append(packet)
            return
        self.packets.append(packet)
        conn_handle = int.from_bytes(packet[1:3], "little") & 0x0FFF
        cid = int.from_bytes(packet[7:9], "little")
        if cid != rawhci.L2CAP_CID_ATT or packet[9] != rawhci.ATT_OP_HANDLE_VALUE_NOTIFICATION:
            return
        value_handle = int.from_bytes(packet[10:12], "little")
        for device in self.bluez.devices:
            if device.adapter == f"{ROOT}/hci{dev_id}" and device.connected and device.conn_handle == conn_handle:
                (device.received if device.listening else device.ignored).append((value_handle, packet[12:], "raw"))


class _FakeRawBackend:
    def __init__(self, hub: FakeRawHub):
        self._hub = hub
        self._dev_id = -1
        self._watched = 0
        self._events: list[bytes] = []

    def open(self, dev_id: int) -> None:
        if self._hub.open_error is not None:
            raise self._hub.open_error
        self._dev_id = dev_id
        self._hub.opened += 1

    def connections(self) -> list[rawhci.HciConnection]:
        return self._hub.connections(self._dev_id)

    def send(self, packet: bytes) -> None:
        self._hub.write(self._dev_id, packet)
        if packet == rawhci.LE_READ_BUFFER_SIZE_COMMAND and self._hub.le_buffers is not None:
            # Command Complete as a raw socket delivers it: one command slot, opcode, status 0,
            # LE packet length 251, number of LE buffers.
            params = bytes([0x01, 0x02, 0x20, 0x00, 0xFB, 0x00, self._hub.le_buffers])
            self._events.append(bytes([rawhci.HCI_EVENT_PKT, rawhci.EVT_CMD_COMPLETE, len(params)]) + params)

    def watch(self, opcode: int) -> None:
        self._watched = opcode
        self._hub.filters.append(rawhci.command_complete_filter(opcode))

    def reply(self, opcode: int) -> bytes | None:
        while self._events:
            params = rawhci.parse_command_complete(self._events.pop(0), opcode)
            if params is not None and self._watched == opcode:  # unfiltered events never reach the socket
                return params
        return None

    def close(self) -> None:
        if self._dev_id >= 0:
            self._hub.closed += 1
            self._dev_id = -1


# --------------------------------------------------------------------------- fake management socket


class FakeMgmtHub:
    """Stands in for the kernel side of a management control socket (mgmt.py).

    "Disconnect" is looked up the way the kernel does it: by the address *and* the address
    type it tracks the link by.  A link that is found goes down a moment later, as it would
    once the controller reports Disconnection Complete, and bluetoothd (the fake one) then
    says so on D-Bus; the command's own answer follows that, as in the kernel.
    """

    def __init__(self, bluez: FakeBluez):
        self.bluez = bluez
        self.open_error: OSError | None = None
        self.send_error: OSError | None = None
        #: Answer every Disconnect with this status instead of carrying it out (0x0A = busy).
        self.refuse_status: int | None = None
        #: Seconds until that refusal arrives.  Kernels from 6.11 on look the link up in a work
        #: item, so "no such link" (status 0x0E there) comes a moment after the command was accepted.
        self.refuse_delay = 0.0
        #: False: the controller never reports the link down (the TV stopped answering).
        self.completes = True
        self.disconnect_delay = 0.01
        self.opened = 0
        self.closed = 0
        self.commands: list[bytes] = []   # every packet written to a control socket
        self._tasks: set[asyncio.Task[Any]] = set()

    def backend(self) -> _FakeMgmtBackend:
        return _FakeMgmtBackend(self)

    def closer(self, raw: FakeRawHub) -> mgmt.LinkCloser:
        return mgmt.LinkCloser(self.backend, raw.backend)

    def find(self, dev_id: int, address: bytes, address_type: int) -> FakeDevice | None:
        wanted = rawhci.format_bdaddr(address)
        kind = {mgmt.BDADDR_LE_PUBLIC: "public", mgmt.BDADDR_LE_RANDOM: "random"}.get(address_type)
        for device in self.bluez.devices:
            if device.adapter == f"{ROOT}/hci{dev_id}" and device.connected:
                if device.hci_address == wanted and device.hci_address_type == kind:
                    return device
        return None

    def spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class _FakeMgmtBackend:
    def __init__(self, hub: FakeMgmtHub):
        self._hub = hub
        self._open = False
        self._events: list[bytes] = []

    def open(self) -> None:
        if self._hub.open_error is not None:
            raise self._hub.open_error
        self._open = True
        self._hub.opened += 1

    def _answer(self, dev_id: int, opcode: int, status: int, params: bytes = b"") -> None:
        body = opcode.to_bytes(2, "little") + bytes([status]) + params
        self._events.append(mgmt.EV_CMD_COMPLETE.to_bytes(2, "little") + dev_id.to_bytes(2, "little")
                            + len(body).to_bytes(2, "little") + body)  # fmt: skip

    def send(self, packet: bytes) -> None:
        if self._hub.send_error is not None:
            raise self._hub.send_error
        self._hub.commands.append(bytes(packet))
        opcode, dev_id, length = (int.from_bytes(packet[i : i + 2], "little") for i in (0, 2, 4))
        params = packet[6:]
        if opcode != mgmt.OP_DISCONNECT or length != len(params) or length != 7:
            self._answer(dev_id, opcode, 0x0D)  # invalid parameters
            return
        if self._hub.refuse_status is not None:
            if self._hub.refuse_delay > 0:
                self._hub.spawn(self._refuse_later(dev_id, self._hub.refuse_status, params))
            else:
                self._answer(dev_id, opcode, self._hub.refuse_status, params)
            return
        device = self._hub.find(dev_id, params[:6], params[6])
        if device is None:
            self._answer(dev_id, opcode, 0x02, params)  # not connected
        elif self._hub.completes:
            self._hub.spawn(self._carry_out(device, dev_id, params))

    async def _refuse_later(self, dev_id: int, status: int, params: bytes) -> None:
        await asyncio.sleep(self._hub.refuse_delay)
        if self._open:
            self._answer(dev_id, mgmt.OP_DISCONNECT, status, params)

    async def _carry_out(self, device: FakeDevice, dev_id: int, params: bytes) -> None:
        await asyncio.sleep(self._hub.disconnect_delay)
        await device.drop()
        if self._open:
            self._answer(dev_id, mgmt.OP_DISCONNECT, 0x00, params)

    def receive(self) -> bytes | None:
        return self._events.pop(0) if self._events else None

    def close(self) -> None:
        if self._open:
            self._open = False
            self._hub.closed += 1


@contextlib.asynccontextmanager
async def running(**kwargs: Any):
    """``async with fake_bluez.running() as bluez`` - a started fake that is stopped afterwards."""
    bluez = FakeBluez(**kwargs)
    await bluez.start()
    try:
        yield bluez
    finally:
        await bluez.stop()


# --------------------------------------------------------------------------- test harness

#: remote.py timing constants shortened so that the suite runs in seconds.
FAST_TIMINGS: dict[str, Any] = {
    "ADAPTER_GRACE": 0.25,
    "ADVERT_RETRY": 0.05,
    "PAIRING_TICK": 0.05,
    "KNOWN_SILENT_WAIT": 0.05,
    "POST_SUBSCRIBE_SETTLE": 0.02,
    "REGISTER_RETRY": (0.05, 0.2),
    "POWER_ON_WAIT": 1.0,
    "DISCONNECT_WAIT": 1.0,
}


def quick_config(**overrides: Any) -> BluetoothConfig:
    """A BluetoothConfig with test-sized waits."""
    values: dict[str, Any] = {
        "settle_ms": 40,
        "resubscribe_wait": 0.3,
        "key_press_ms": 10,
        "key_gap_ms": 20,
        # Most tests want to see exactly the advertising they asked for.
        "reconnect_on_drop": False,
        # The reboot tests are about direct delivery itself, on the kind of kernel that needs
        # its traces repaired (Harness.kernel).  What "auto" makes of a kernel is tested by name.
        "raw_notify": "on",
    }
    values.update(overrides)
    return BluetoothConfig(**values)


#: Kernel releases for the two cases the transport tells apart (rawhci.FIXED_KERNEL).
OLD_KERNEL = "6.11.11-valve19-1-neptune-611"
NEW_KERNEL = "6.17.2-arch1-1"


class Harness:
    """A fake bluetoothd, fake kernel sockets and a factory for remotes wired to them."""

    def __init__(self, bluez: FakeBluez, tmp_path: Any):
        self.bluez = bluez
        self.raw = FakeRawHub(bluez)
        self.mgmt = FakeMgmtHub(bluez)
        #: The kernel the remotes believe they run on, unless a test names another.
        self.kernel = OLD_KERNEL
        self.state_path = tmp_path / "bluetooth.json"
        self.paired: list[tuple[str, str]] = []   # what on_paired was called with
        self._buses: list[Any] = []
        self._remotes: list[BleRemote] = []

    async def bus(self) -> Any:
        bus = await dbus.connect_system_bus()
        self._buses.append(bus)
        return bus

    async def remote(
        self,
        cfg: BluetoothConfig | None = None,
        *,
        start: bool = True,
        kernel: str | None = None,
        **overrides: Any,
    ) -> BleRemote:
        """A started remote on its own bus connection; ``overrides`` are config fields."""
        cfg = dataclasses.replace(cfg, **overrides) if cfg is not None else quick_config(**overrides)
        kernel = kernel if kernel is not None else self.kernel
        remote = BleRemote(
            await self.bus(),
            cfg,
            state_path=self.state_path,
            raw_link=self.raw.link(repair_credits=rawhci.kernel_needs_credit_repair(kernel)),
            link_closer=self.mgmt.closer(self.raw),
            kernel_release=kernel,
        )

        async def on_paired(address: str, name: str) -> None:
            # What Service.on_paired does: store the TV and hand the new settings back.
            self.paired.append((address, name))
            remote.apply_config(dataclasses.replace(remote._cfg, tv_address=address, tv_name=name))

        remote.on_paired = on_paired
        self._remotes.append(remote)
        if start:
            await remote.start()
        return remote

    async def pair(self, remote: BleRemote, tv: FakeDevice, *, timeout: float = 5.0) -> None:
        """Run the whole pairing flow: window, Just Works, discovery, subscription."""
        await remote.start_pairing(timeout)
        assert await tv.pair_like_android(), "the fake TV could not pair"
        await eventually(lambda: not remote.state.pairing and remote.state.paired, what="pairing to complete")

    async def paired_remote(
        self, *, android: int = 12, rpa: str = "", identity_type: str = "public", **overrides: Any
    ) -> tuple[BleRemote, FakeDevice]:
        """A remote and a TV that paired with it in this bluetoothd lifetime; the link is up."""
        remote = await self.remote(**overrides)
        tv = self.bluez.add_device(android=android, rpa=rpa, identity_type=identity_type)
        tv.auto_connect = True
        await self.pair(remote, tv)
        return remote, tv

    async def close(self) -> None:
        for remote in self._remotes:
            await remote.stop()
        for bus in self._buses:
            bus.disconnect()


@contextlib.asynccontextmanager
async def harness(tmp_path: Any, monkeypatch: Any, **bluez_kwargs: Any):
    """``async with fake_bluez.harness(tmp_path, monkeypatch) as h`` (needs the private_bus fixture)."""
    for name, value in FAST_TIMINGS.items():
        monkeypatch.setattr(remote_module, name, value)
    # The fake controller answers at once; do not make every closed raw link wait for the poll.
    monkeypatch.setattr(rawhci, "REPLY_WAIT", 0.2)
    monkeypatch.setattr(rawhci, "REPLY_POLL", 0.005)
    async with running(**bluez_kwargs) as bluez:
        h = Harness(bluez, tmp_path)
        try:
            yield h
        finally:
            await h.close()
