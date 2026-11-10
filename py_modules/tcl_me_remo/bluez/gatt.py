"""The HID-over-GATT service we register with bluetoothd, as D-Bus objects.

THE TABLE IS FROZEN.  A bonded TV caches attribute handles together with the report map, and
Android before 14 never reads them again.  Changing the order of the objects, adding or
removing one, or moving the service handle silently breaks every existing pairing.
``tests/test_bluez_gatt.py`` holds a snapshot of the whole table for that reason.

How bluetoothd turns these objects into attributes (BlueZ 5.79 ``src/gatt-database.c``,
``src/shared/gatt-db.c``):

* it calls ``ObjectManager.GetManagedObjects`` on the application root and lays the objects
  out in the order of the reply (dbus-fast answers in export order);
* a service whose ``Handle`` property is non-zero is inserted at exactly that handle
  (``gatt_db_insert_service``); with 0 it goes after the highest handle ever used, which
  moves on every registration - hence the pin;
* each characteristic takes two handles (declaration, value), one more for the Client
  Characteristic Configuration descriptor that bluetoothd creates itself when the flags
  contain notify/indicate (``database_add_ccc`` - never export a 0x2902 descriptor), then one
  per descriptor we export;
* for every object that has a ``Handle`` property with value 0 it writes the allocated handle
  back with ``Properties.Set``.  For a characteristic that is the handle of the *value*
  attribute (``service_insert_characteristic`` returns the value attribute), which is the one
  an ATT notification carries.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .. import hid
from ..dbus import DBusError, MessageBus, PropertyAccess, ServiceInterface, Variant, dbus_property, method

log = logging.getLogger(__name__)

APP_ROOT = "/org/tclmeremo/hid_app"
SERVICE_PATH = APP_ROOT + "/service"
#: Well above bluetoothd's built-in GAP/GATT/Device Information services.
SERVICE_HANDLE = 0x0100

UUID_HID_SERVICE = 0x1812
UUID_HID_INFORMATION = 0x2A4A
UUID_REPORT_MAP = 0x2A4B
UUID_HID_CONTROL_POINT = 0x2A4C
UUID_REPORT = 0x2A4D
UUID_PROTOCOL_MODE = 0x2A4E
UUID_REPORT_REFERENCE = 0x2908
UUID_CCC = 0x2902

PROTOCOL_MODE_REPORT = 0x01

ERR_FAILED = "org.bluez.Error.Failed"
ERR_INVALID_OFFSET = "org.bluez.Error.InvalidOffset"
ERR_INVALID_VALUE_LENGTH = "org.bluez.Error.InvalidValueLength"
ERR_NOT_PERMITTED = "org.bluez.Error.NotPermitted"
ERR_NOT_SUPPORTED = "org.bluez.Error.NotSupported"

_PLAIN_FLAG = {"encrypt-read": "read", "encrypt-write": "write", "encrypt-notify": "notify"}

#: ``(characteristic, device object path or "")`` - the TV read one of our attributes.
TouchCallback = Callable[["GattCharacteristic", str], None]
#: ``(characteristic, enabled)`` - bluetoothd called StartNotify / StopNotify.
NotifyCallback = Callable[["GattCharacteristic", bool], None]


def uuid16(short: int) -> str:
    """The 128-bit string form of a 16-bit Bluetooth UUID."""
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


def _flags(flags: list[str], security: str) -> list[str]:
    if security == "plain":
        return [_PLAIN_FLAG.get(flag, flag) for flag in flags]
    return list(flags)


def _option(options: dict[str, Any], name: str, default: Any) -> Any:
    value = options.get(name, default)
    return value.value if isinstance(value, Variant) else value


def _read_with_offset(value: bytes, options: dict[str, Any]) -> bytes:
    # bluetoothd only passes "offset" when it is non-zero (ATT Read Blob): the report map is
    # longer than a default MTU and is fetched in slices.
    offset = int(_option(options, "offset", 0))
    if offset > len(value):
        raise DBusError(ERR_INVALID_OFFSET, f"offset {offset} is beyond the value ({len(value)} bytes)")
    return value[offset:]


class GattService(ServiceInterface):
    def __init__(self, path: str, uuid: int, handle: int):
        super().__init__("org.bluez.GattService1")
        self.path = path
        self.uuid = uuid
        self.handle = handle

    @dbus_property(PropertyAccess.READ)
    def UUID(self) -> "s":
        return uuid16(self.uuid)

    @dbus_property(PropertyAccess.READ)
    def Primary(self) -> "b":
        return True

    @dbus_property()
    def Handle(self) -> "q":
        return self.handle

    @Handle.setter
    def Handle(self, value: "q"):
        # bluetoothd reports the handle it allocated when we asked for 0.
        self.handle = int(value)


class GattCharacteristic(ServiceInterface):
    def __init__(
        self,
        path: str,
        service: GattService,
        uuid: int,
        flags: list[str],
        value: bytes,
        *,
        writable_length: int = 0,
        report_id: int = 0,
    ):
        super().__init__("org.bluez.GattCharacteristic1")
        self.path = path
        self.service = service
        self.uuid = uuid
        self.flags = flags
        self.value = bytes(value)
        self.writable_length = writable_length
        self.report_id = report_id
        self.handle = 0  # the VALUE handle once bluetoothd has written it back
        self.notifying = False
        self.descriptors: list[GattDescriptor] = []
        self.on_touch: TouchCallback | None = None
        self.on_notify: NotifyCallback | None = None

    @property
    def can_notify(self) -> bool:
        return any(flag in ("notify", "encrypt-notify") for flag in self.flags)

    @property
    def can_write(self) -> bool:
        return any(flag in ("write", "encrypt-write", "write-without-response") for flag in self.flags)

    def notify(self, value: bytes) -> None:
        """Send ``value`` as a notification through bluetoothd.

        bluetoothd watches the ``Value`` property and forwards a change to every connected
        client that has written the CCC descriptor.  A client without CCC state is skipped
        silently (``send_notification_to_device``), so this cannot fail and cannot tell.
        """
        self.value = bytes(value)
        self.emit_properties_changed({"Value": self.value})

    def _touched(self, options: dict[str, Any]) -> None:
        if self.on_touch is not None:
            try:
                self.on_touch(self, str(_option(options, "device", "")))
            except Exception:  # noqa: BLE001 - a bookkeeping bug must not fail the TV's read
                log.exception("read callback failed for %s", self.path)

    @dbus_property(PropertyAccess.READ)
    def UUID(self) -> "s":
        return uuid16(self.uuid)

    @dbus_property(PropertyAccess.READ)
    def Service(self) -> "o":
        return self.service.path

    @dbus_property(PropertyAccess.READ)
    def Flags(self) -> "as":
        return list(self.flags)

    @dbus_property(PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @dbus_property()
    def Handle(self) -> "q":
        return self.handle

    @Handle.setter
    def Handle(self, value: "q"):
        self.handle = int(value)

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        self._touched(options)
        return _read_with_offset(self.value, options)

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        if not self.can_write:
            raise DBusError(ERR_NOT_PERMITTED, "this characteristic cannot be written")
        data = bytes(value)
        if len(data) != self.writable_length or int(_option(options, "offset", 0)) != 0:
            raise DBusError(ERR_INVALID_VALUE_LENGTH, f"expected {self.writable_length} byte(s)")
        # HID Control Point (suspend / exit suspend) and Protocol Mode: nothing to act on, the
        # host just needs the write to succeed and a later read to return what it wrote.
        self.value = data
        log.debug("%s written: %s", self.path, data.hex())

    def _notify_changed(self, enabled: bool) -> None:
        self.notifying = enabled
        if self.on_notify is not None:
            try:
                self.on_notify(self, enabled)
            except Exception:  # noqa: BLE001 - bluetoothd ignores the reply anyway; keep our state sane
                log.exception("subscription callback failed for %s", self.path)

    @method()
    def StartNotify(self):
        if not self.can_notify:
            raise DBusError(ERR_NOT_SUPPORTED, "this characteristic does not notify")
        self._notify_changed(True)

    @method()
    def StopNotify(self):
        self._notify_changed(False)


class GattDescriptor(ServiceInterface):
    def __init__(self, path: str, characteristic: GattCharacteristic, uuid: int, flags: list[str], value: bytes):
        super().__init__("org.bluez.GattDescriptor1")
        self.path = path
        self.characteristic = characteristic
        self.uuid = uuid
        self.flags = flags
        self.value = bytes(value)
        self.handle = 0

    @dbus_property(PropertyAccess.READ)
    def UUID(self) -> "s":
        return uuid16(self.uuid)

    @dbus_property(PropertyAccess.READ)
    def Characteristic(self) -> "o":
        return self.characteristic.path

    @dbus_property(PropertyAccess.READ)
    def Flags(self) -> "as":
        return list(self.flags)

    @dbus_property(PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @dbus_property()
    def Handle(self) -> "q":
        return self.handle

    @Handle.setter
    def Handle(self, value: "q"):
        self.handle = int(value)

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        self.characteristic._touched(options)
        return _read_with_offset(self.value, options)

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        raise DBusError(ERR_NOT_PERMITTED, "this descriptor is read-only")


GattObject = GattService | GattCharacteristic | GattDescriptor


class HidApplication:
    """The frozen HID table: one service, seven characteristics, three descriptors.

    ``security`` is ``"encrypt"`` (every attribute needs an encrypted link, which is what
    HOGP asks for and what a Just Works bond provides) or ``"plain"`` (bring-up aid).  Flags
    do not move handles, so switching does not require pairing again.
    """

    def __init__(self, security: str = "encrypt", *, service_handle: int = SERVICE_HANDLE):
        if security not in ("encrypt", "plain"):
            raise ValueError(f"unknown GATT security mode {security!r}")
        self.security = security
        self.service = GattService(SERVICE_PATH, UUID_HID_SERVICE, service_handle)
        self.characteristics: list[GattCharacteristic] = []
        self.reports: dict[int, GattCharacteristic] = {}

        def add(name: str, uuid: int, flags: list[str], value: bytes, **kwargs: Any) -> GattCharacteristic:
            char = GattCharacteristic(
                f"{SERVICE_PATH}/{name}", self.service, uuid, _flags(flags, security), value, **kwargs
            )
            self.characteristics.append(char)
            return char

        add("c0_info", UUID_HID_INFORMATION, ["encrypt-read"], hid.HID_INFORMATION)
        add("c1_map", UUID_REPORT_MAP, ["encrypt-read"], hid.REPORT_MAP)
        add("c2_ctrl", UUID_HID_CONTROL_POINT, ["write-without-response", "encrypt-write"], b"\x00", writable_length=1)
        add(
            "c3_proto",
            UUID_PROTOCOL_MODE,
            ["encrypt-read", "write-without-response", "encrypt-write"],
            bytes([PROTOCOL_MODE_REPORT]),
            writable_length=1,
        )
        for name, report_id in (
            ("c4_kbd", hid.REPORT_ID_KEYBOARD),
            ("c5_consumer", hid.REPORT_ID_CONSUMER),
            ("c6_system", hid.REPORT_ID_SYSTEM),
        ):
            char = add(
                name,
                UUID_REPORT,
                ["encrypt-read", "encrypt-notify"],
                hid.release_report(report_id),
                report_id=report_id,
            )
            # Report Reference: Android only treats a Report characteristic as an input report
            # (and caches it) after reading exactly these two bytes.
            char.descriptors.append(
                GattDescriptor(
                    f"{char.path}/d0",
                    char,
                    UUID_REPORT_REFERENCE,
                    _flags(["encrypt-read"], security),
                    bytes([report_id, hid.REPORT_TYPE_INPUT]),
                )
            )
            self.reports[report_id] = char

    # ------------------------------------------------------------------ export

    def objects(self) -> list[tuple[str, GattObject]]:
        """Every object with its path, in the order bluetoothd must see them."""
        out: list[tuple[str, GattObject]] = [(self.service.path, self.service)]
        for char in self.characteristics:
            out.append((char.path, char))
            out.extend((desc.path, desc) for desc in char.descriptors)
        return out

    def export(self, bus: MessageBus) -> None:
        for path, obj in self.objects():
            bus.export(path, obj)

    def unexport(self, bus: MessageBus) -> None:
        for path, obj in self.objects():
            bus.unexport(path, obj)

    def set_callbacks(self, on_touch: TouchCallback | None, on_notify: NotifyCallback | None) -> None:
        for char in self.characteristics:
            char.on_touch = on_touch
            char.on_notify = on_notify

    # ------------------------------------------------------------------ registration life cycle

    def reset_for_registration(self, service_handle: int) -> None:
        """Forget everything a previous registration left behind.

        bluetoothd keeps subscriptions in memory only, so a new registration starts with
        nobody subscribed, and handles are written back again.
        """
        self.service.handle = service_handle
        for char in self.characteristics:
            char.handle = 0
            char.notifying = False
            for desc in char.descriptors:
                desc.handle = 0
        for report_id, char in self.reports.items():
            char.value = hid.release_report(report_id)

    @property
    def subscribed(self) -> bool:
        """The TV has subscribed to at least one of the report characteristics."""
        return any(char.notifying for char in self.reports.values())

    @property
    def fully_subscribed(self) -> bool:
        """...to every one of them.  A host writes these subscriptions one after another, and
        bluetoothd forwards a report only once the one of its own characteristic has arrived."""
        return all(char.notifying for char in self.reports.values())

    def expected_handles(self, service_handle: int) -> dict[str, int]:
        """Handles bluetoothd allocates when the service sits at ``service_handle``.

        Keyed by object path; for a characteristic this is the value handle.  Mirrors
        ``database_add_chrc``: declaration, value, CCC if the characteristic notifies, then
        the exported descriptors.
        """
        handles = {self.service.path: service_handle}
        cursor = service_handle
        for char in self.characteristics:
            cursor += 2  # declaration, value
            handles[char.path] = cursor
            if char.can_notify:
                cursor += 1  # CCC, created by bluetoothd
            for desc in char.descriptors:
                cursor += 1
                handles[desc.path] = cursor
        return handles

    def report_value_handle(self, report_id: int) -> tuple[int, str]:
        """``(value handle, source)`` of a report characteristic; handle 0 if unknown.

        ``source`` is ``"bluez"`` when bluetoothd wrote the handle back and ``"computed"``
        when it had to be derived from the service handle.
        """
        char = self.reports[report_id]
        if char.handle:
            return char.handle, "bluez"
        if self.service.handle:
            return self.expected_handles(self.service.handle)[char.path], "computed"
        return 0, "unknown"

    def snapshot(self) -> list[dict[str, Any]]:
        """The table as plain data (tests and diagnostics)."""
        rows: list[dict[str, Any]] = []
        for path, obj in self.objects():
            row: dict[str, Any] = {"path": path, "uuid": f"{obj.uuid:04X}"}
            if isinstance(obj, GattService):
                row.update(kind="service", handle=obj.handle)
            else:
                kind = "characteristic" if isinstance(obj, GattCharacteristic) else "descriptor"
                row.update(kind=kind, flags=list(obj.flags), value=obj.value.hex())
            rows.append(row)
        return rows
