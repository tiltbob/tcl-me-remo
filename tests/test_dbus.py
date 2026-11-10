import asyncio
import os

import pytest

from tcl_me_remo import dbus
from tcl_me_remo.dbus import (
    DBusError,
    PropertyAccess,
    ServiceInterface,
    Variant,
    dbus_property,
    method,
)


class Thing(ServiceInterface):
    def __init__(self):
        super().__init__("com.example.Thing")
        self._value = b"\x01\x02"
        self._level = 3

    @dbus_property(PropertyAccess.READ)
    def Value(self) -> "ay":  # noqa: F821
        return self._value

    @dbus_property()
    def Level(self) -> "u":  # noqa: F821
        return self._level

    @Level.setter
    def Level(self, value: "u"):  # noqa: F821
        self._level = value

    @method()
    def Echo(self, options: "a{sv}") -> "a{sv}":  # noqa: F821
        return options

    @method()
    def Fail(self):
        raise DBusError("com.example.Error.Nope", "not today")

    @method()
    def OpenFd(self) -> "h":  # noqa: F821
        read_end, write_end = os.pipe()
        os.close(write_end)
        return read_end

    def push(self, value: bytes):
        self._value = value
        self.emit_properties_changed({"Value": value})


def test_vendored_copy_is_used_and_pure_python():
    import dbus_fast
    import dbus_fast.message

    assert "_vendor" in dbus_fast.__file__
    assert dbus_fast.message.__file__.endswith(".py")


async def test_round_trip_on_private_bus(private_bus):
    server = await dbus.connect_system_bus()
    client = await dbus.connect_system_bus()
    try:
        await server.request_name("com.example.App")
        thing = Thing()
        server.export("/app/sub/thing", thing)

        # ObjectManager is served on every ancestor path of an exported object.  BlueZ relies
        # on this when it calls GetManagedObjects on a registered GATT application root.
        for root in ("/app", "/app/sub", "/"):
            objects = await dbus.get_managed_objects(client, "com.example.App", root)
            assert objects == {"/app/sub/thing": {"com.example.Thing": {"Value": b"\x01\x02", "Level": 3}}}

        assert await dbus.get_property(client, "com.example.App", "/app/sub/thing", "com.example.Thing", "Level") == 3
        target = ("com.example.App", "/app/sub/thing", "com.example.Thing")
        await dbus.set_property(client, *target, "Level", Variant("u", 9))
        assert await dbus.get_all_properties(client, "com.example.App", "/app/sub/thing", "com.example.Thing") == {
            "Value": b"\x01\x02",
            "Level": 9,
        }

        body = await dbus.call(
            client,
            "com.example.App",
            "/app/sub/thing",
            "com.example.Thing",
            "Echo",
            "a{sv}",
            [{"offset": Variant("q", 7), "device": Variant("o", "/org/bluez/hci0/dev_AA")}],
        )
        assert dbus.unwrap(body[0]) == {"offset": 7, "device": "/org/bluez/hci0/dev_AA"}

        with pytest.raises(DBusError) as err:
            await dbus.call(client, "com.example.App", "/app/sub/thing", "com.example.Thing", "Fail")
        assert err.value.type == "com.example.Error.Nope" and "not today" in err.value.text

        with pytest.raises(DBusError):
            await dbus.call(client, "com.example.Nobody", "/", "com.example.Thing", "Fail")

        # signals: match rule + handler
        seen = []
        stop = dbus.on_signal(client, lambda m: seen.append((m.path, m.interface, m.member, dbus.unwrap(m.body))))
        rule = "type='signal',sender='com.example.App',interface='org.freedesktop.DBus.Properties'"
        await dbus.add_match(client, rule)
        thing.push(b"\x00\x51")
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen == [
            (
                "/app/sub/thing",
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                ["com.example.Thing", {"Value": b"\x00\x51"}, []],
            )
        ]
        stop()
        await dbus.remove_match(client, rule)

        # file descriptors arrive as real fds (logind's Inhibit() hands out its lock this way)
        reply = await dbus.call_message(
            client,
            dbus.Message(
                destination="com.example.App", path="/app/sub/thing", interface="com.example.Thing", member="OpenFd"
            ),
        )
        fd = reply.unix_fds[reply.body[0]]
        assert os.read(fd, 1) == b""  # the far end is closed: EOF proves this is a live pipe fd
        os.close(fd)
    finally:
        server.disconnect()
        client.disconnect()
