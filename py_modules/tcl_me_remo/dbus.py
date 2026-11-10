"""The one place that imports the vendored D-Bus library (dbus-fast, pure-Python mode).

Everything else in the package imports D-Bus names from here, so the vendoring mechanics
stay in a single file.  A few helpers wrap the low-level ``Message`` API: plain method
calls without an introspection round-trip, which keeps start-up fast and predictable.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
if _VENDOR not in sys.path:
    # First on the path so the pinned copy wins over any system-wide dbus_fast.
    sys.path.insert(0, _VENDOR)

from dbus_fast import BusType, DBusError, Message, MessageType, PropertyAccess, Variant  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402
from dbus_fast.service import ServiceInterface, dbus_property, method, signal  # noqa: E402

__all__ = [
    "BusType",
    "DBusError",
    "Message",
    "MessageBus",
    "MessageType",
    "PropertyAccess",
    "ServiceInterface",
    "Variant",
    "add_match",
    "call",
    "connect_system_bus",
    "dbus_property",
    "get_all_properties",
    "get_managed_objects",
    "get_property",
    "method",
    "remove_match",
    "set_property",
    "signal",
    "unwrap",
]

PROPERTIES = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

DEFAULT_TIMEOUT = 10.0


async def connect_system_bus() -> MessageBus:
    """Connect to the system bus (honours ``DBUS_SYSTEM_BUS_ADDRESS``, which tests use)."""
    return await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()


def unwrap(value: Any) -> Any:
    """Recursively replace ``Variant`` wrappers by their values and bytearrays by bytes."""
    if isinstance(value, Variant):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {unwrap(k): unwrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [unwrap(v) for v in value]
    if isinstance(value, bytearray):
        return bytes(value)
    return value


async def call_message(bus: MessageBus, message: Message, *, timeout: float = DEFAULT_TIMEOUT) -> Message:
    """Send a method call and return the reply message, raising ``DBusError`` on an error reply."""
    reply = await asyncio.wait_for(bus.call(message), timeout)
    if reply is None:  # only for calls flagged "no reply expected"
        raise DBusError("org.freedesktop.DBus.Error.NoReply", "no reply")
    if reply.message_type == MessageType.ERROR:
        text = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
        raise DBusError(reply.error_name or "org.freedesktop.DBus.Error.Failed", text)
    return reply


async def call(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: list[Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[Any]:
    """Call a method and return the reply body (a list of out-arguments, still Variant-wrapped)."""
    message = Message(
        destination=destination,
        path=path,
        interface=interface,
        member=member,
        signature=signature,
        body=list(body or []),
    )
    return (await call_message(bus, message, timeout=timeout)).body


async def get_property(bus: MessageBus, destination: str, path: str, interface: str, name: str) -> Any:
    body = await call(bus, destination, path, PROPERTIES, "Get", "ss", [interface, name])
    return unwrap(body[0])


async def get_all_properties(bus: MessageBus, destination: str, path: str, interface: str) -> dict[str, Any]:
    body = await call(bus, destination, path, PROPERTIES, "GetAll", "s", [interface])
    return unwrap(body[0])


async def set_property(bus: MessageBus, destination: str, path: str, interface: str, name: str, value: Variant) -> None:
    await call(bus, destination, path, PROPERTIES, "Set", "ssv", [interface, name, value])


async def get_managed_objects(
    bus: MessageBus, destination: str, path: str = "/"
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{object_path: {interface: {property: value}}}`` with all Variants unwrapped."""
    body = await call(bus, destination, path, OBJECT_MANAGER, "GetManagedObjects")
    return unwrap(body[0])


async def add_match(bus: MessageBus, rule: str) -> None:
    await call(bus, DBUS_NAME, DBUS_PATH, DBUS_NAME, "AddMatch", "s", [rule])


async def remove_match(bus: MessageBus, rule: str) -> None:
    try:
        await call(bus, DBUS_NAME, DBUS_PATH, DBUS_NAME, "RemoveMatch", "s", [rule])
    except (DBusError, TimeoutError, OSError):
        pass


def on_signal(bus: MessageBus, handler: Callable[[Message], None]) -> Callable[[], None]:
    """Deliver every incoming signal message to ``handler``; returns a function that stops it.

    Use together with :func:`add_match` (the bus only forwards signals that match a rule).
    """

    def dispatch(message: Message) -> None:
        if message.message_type == MessageType.SIGNAL:
            handler(message)

    bus.add_message_handler(dispatch)
    return lambda: bus.remove_message_handler(dispatch)
