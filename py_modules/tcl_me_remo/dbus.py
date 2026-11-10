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

from dbus_fast import BusType, DBusError, Message, MessageFlag, MessageType, PropertyAccess, Variant  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402
from dbus_fast.service import ServiceInterface, dbus_property, method, signal  # noqa: E402

__all__ = [
    "BusType",
    "DBusError",
    "Message",
    "MessageBus",
    "MessageFlag",
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
    "get_name_owner",
    "get_property",
    "method",
    "on_signal",
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


def _write_failure_seen(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()  # asking for it is what tells asyncio that somebody has looked


class _Bus(MessageBus):
    """The library's bus, without the traceback it leaves in the log when the bus is lost.

    The library reports a failed write in a future of its own, and its method calls throw
    that future away.  A write that fails (the first sign of a bus that has gone, or a call
    made after it, such as the clean-up on the way out) then comes back from the garbage
    collector as "Future exception was never retrieved" with a traceback.  Nothing is lost
    by looking at the future here: a failed write ends the connection, which every pending
    call and ``wait_for_disconnect`` are told about, and whoever awaits the future still
    gets the exception.  A send on a connection that is known to be gone is refused before
    it reaches the socket; callers got an ``OSError`` for that before, and still do.

    ``send`` is the library's own (aio/message_bus.py), plus the additions.
    """

    def send(self, msg: Message) -> asyncio.Future:
        if self._disconnected:
            raise ConnectionError("the D-Bus connection is closed")
        if not msg.serial:
            msg.serial = self.next_serial()
        future = self._loop.create_future()
        future.add_done_callback(_write_failure_seen)
        try:
            self._writer.schedule_write(msg, future)
        except Exception:
            # A write that fails closes the connection, and the library then stumbles over
            # its own closed socket.  The failure is in the future by then, and in the reply
            # of every pending call (this one included): that is where callers get it from.
            # Raising here as well would make the library's ``call`` drop that reply unseen.
            if not future.done() or future.cancelled() or future.exception() is None:
                raise
        return future


async def connect_system_bus() -> MessageBus:
    """Connect to the system bus (honours ``DBUS_SYSTEM_BUS_ADDRESS``, which tests use)."""
    return await _Bus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()


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


async def get_property(
    bus: MessageBus, destination: str, path: str, interface: str, name: str, *, timeout: float = DEFAULT_TIMEOUT
) -> Any:
    body = await call(bus, destination, path, PROPERTIES, "Get", "ss", [interface, name], timeout=timeout)
    return unwrap(body[0])


async def get_all_properties(
    bus: MessageBus, destination: str, path: str, interface: str, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    body = await call(bus, destination, path, PROPERTIES, "GetAll", "s", [interface], timeout=timeout)
    return unwrap(body[0])


async def set_property(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
    name: str,
    value: Variant,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    await call(bus, destination, path, PROPERTIES, "Set", "ssv", [interface, name, value], timeout=timeout)


async def get_managed_objects(
    bus: MessageBus, destination: str, path: str = "/", *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{object_path: {interface: {property: value}}}`` with all Variants unwrapped."""
    body = await call(bus, destination, path, OBJECT_MANAGER, "GetManagedObjects", timeout=timeout)
    return unwrap(body[0])


async def get_name_owner(bus: MessageBus, name: str, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """Unique name of the connection that owns ``name``, or None if nobody does."""
    try:
        body = await call(bus, DBUS_NAME, DBUS_PATH, DBUS_NAME, "GetNameOwner", "s", [name], timeout=timeout)
    except DBusError as exc:
        if exc.type == "org.freedesktop.DBus.Error.NameHasNoOwner":
            return None
        raise
    return str(body[0])


async def add_match(bus: MessageBus, rule: str) -> None:
    await call(bus, DBUS_NAME, DBUS_PATH, DBUS_NAME, "AddMatch", "s", [rule])


async def remove_match(bus: MessageBus, rule: str) -> None:
    try:
        await call(bus, DBUS_NAME, DBUS_PATH, DBUS_NAME, "RemoveMatch", "s", [rule])
    except Exception:  # noqa: BLE001 - best-effort clean-up; the bus may already be gone
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
