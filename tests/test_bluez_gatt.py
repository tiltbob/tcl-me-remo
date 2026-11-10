"""The frozen HID GATT table and its registration with (fake) bluetoothd."""

import logging

import fake_bluez
import pytest
from fake_bluez import AttError, eventually

from tcl_me_remo import dbus, hid
from tcl_me_remo.bluez import advert, agent, gatt
from tcl_me_remo.bluez.gatt import HidApplication
from tcl_me_remo.dbus import DBusError

S = "/org/tclmeremo/hid_app/service"

# Spelled out on purpose: this must only ever change together with a "re-pair your TV" notice.
REPORT_MAP_HEX = (
    "05010906a10185010507191e29f1151e26f100750895028100c0"
    "050c0901a101850219012aff03150126ff03751095028100c0"
    "05010980a10185031500250175019503098109820983810295058101c0"
)

FROZEN_TABLE = [
    {"path": S, "uuid": "1812", "kind": "service", "handle": 0x0100},
    {"path": f"{S}/c0_info", "uuid": "2A4A", "kind": "characteristic", "flags": ["encrypt-read"], "value": "11010001"},
    {"path": f"{S}/c1_map", "uuid": "2A4B", "kind": "characteristic", "flags": ["encrypt-read"], "value": REPORT_MAP_HEX},
    {
        "path": f"{S}/c2_ctrl", "uuid": "2A4C", "kind": "characteristic",
        "flags": ["write-without-response", "encrypt-write"], "value": "00",
    },
    {
        "path": f"{S}/c3_proto", "uuid": "2A4E", "kind": "characteristic",
        "flags": ["encrypt-read", "write-without-response", "encrypt-write"], "value": "01",
    },
    {
        "path": f"{S}/c4_kbd", "uuid": "2A4D", "kind": "characteristic",
        "flags": ["encrypt-read", "encrypt-notify"], "value": "0000",
    },
    {"path": f"{S}/c4_kbd/d0", "uuid": "2908", "kind": "descriptor", "flags": ["encrypt-read"], "value": "0101"},
    {
        "path": f"{S}/c5_consumer", "uuid": "2A4D", "kind": "characteristic",
        "flags": ["encrypt-read", "encrypt-notify"], "value": "00000000",
    },
    {"path": f"{S}/c5_consumer/d0", "uuid": "2908", "kind": "descriptor", "flags": ["encrypt-read"], "value": "0201"},
    {
        "path": f"{S}/c6_system", "uuid": "2A4D", "kind": "characteristic",
        "flags": ["encrypt-read", "encrypt-notify"], "value": "00",
    },
    {"path": f"{S}/c6_system/d0", "uuid": "2908", "kind": "descriptor", "flags": ["encrypt-read"], "value": "0301"},
]  # fmt: skip

# What bluetoothd makes of it: (handle, kind, uuid, backing object).
FROZEN_ATTRIBUTES = [
    (0x0100, "service", "1812", S),
    (0x0101, "declaration", "2803", f"{S}/c0_info"),
    (0x0102, "value", "2A4A", f"{S}/c0_info"),
    (0x0103, "declaration", "2803", f"{S}/c1_map"),
    (0x0104, "value", "2A4B", f"{S}/c1_map"),
    (0x0105, "declaration", "2803", f"{S}/c2_ctrl"),
    (0x0106, "value", "2A4C", f"{S}/c2_ctrl"),
    (0x0107, "declaration", "2803", f"{S}/c3_proto"),
    (0x0108, "value", "2A4E", f"{S}/c3_proto"),
    (0x0109, "declaration", "2803", f"{S}/c4_kbd"),
    (0x010A, "value", "2A4D", f"{S}/c4_kbd"),
    (0x010B, "ccc", "2902", ""),
    (0x010C, "descriptor", "2908", f"{S}/c4_kbd/d0"),
    (0x010D, "declaration", "2803", f"{S}/c5_consumer"),
    (0x010E, "value", "2A4D", f"{S}/c5_consumer"),
    (0x010F, "ccc", "2902", ""),
    (0x0110, "descriptor", "2908", f"{S}/c5_consumer/d0"),
    (0x0111, "declaration", "2803", f"{S}/c6_system"),
    (0x0112, "value", "2A4D", f"{S}/c6_system"),
    (0x0113, "ccc", "2902", ""),
    (0x0114, "descriptor", "2908", f"{S}/c6_system/d0"),
]


@pytest.fixture
async def h(private_bus, tmp_path, monkeypatch):
    async with fake_bluez.harness(tmp_path, monkeypatch) as harness:
        yield harness


# --------------------------------------------------------------------------- the table itself


def test_frozen_table_snapshot():
    assert hid.REPORT_MAP.hex() == REPORT_MAP_HEX
    assert HidApplication("encrypt").snapshot() == FROZEN_TABLE
    assert gatt.APP_ROOT == "/org/tclmeremo/hid_app" and gatt.SERVICE_HANDLE == 0x0100


def test_plain_security_only_swaps_flags():
    def plain(flags):
        return [{"encrypt-read": "read", "encrypt-write": "write", "encrypt-notify": "notify"}.get(f, f) for f in flags]

    expected = [{**row, "flags": plain(row["flags"])} if "flags" in row else row for row in FROZEN_TABLE]
    assert HidApplication("plain").snapshot() == expected
    flags = {row["path"].rsplit("/", 1)[-1]: row["flags"] for row in expected if "flags" in row}
    assert flags["c2_ctrl"] == ["write-without-response", "write"]
    assert flags["c3_proto"] == ["read", "write-without-response", "write"]
    assert flags["c5_consumer"] == ["read", "notify"]
    with pytest.raises(ValueError):
        HidApplication("secure")


def test_no_ccc_descriptor_and_no_extra_services():
    rows = HidApplication().snapshot()
    assert all(row["uuid"] != "2902" for row in rows)
    assert [row["uuid"] for row in rows if row["kind"] == "service"] == ["1812"]


def test_expected_handles_and_report_value_handles():
    app = HidApplication()
    layout = app.expected_handles(0x0100)
    assert layout == {path: handle for handle, kind, _uuid, path in FROZEN_ATTRIBUTES if kind not in ("declaration", "ccc")}
    # Nothing written back yet: computed from the pinned service handle.
    assert [app.report_value_handle(i) for i in (1, 2, 3)] == [
        (0x010A, "computed"),
        (0x010E, "computed"),
        (0x0112, "computed"),
    ]
    app.reports[2].handle = 0x0042  # bluetoothd reported something else: believe it
    assert app.report_value_handle(2) == (0x0042, "bluez")
    app.reset_for_registration(0)  # automatic handles and nothing reported: unknown
    assert app.report_value_handle(2) == (0, "unknown")


async def test_managed_objects_are_listed_in_table_order(private_bus):
    """bluetoothd allocates handles in the order of the GetManagedObjects reply."""
    server = await dbus.connect_system_bus()
    client = await dbus.connect_system_bus()
    try:
        # Other objects of ours are exported first and must not leak into the application.
        advertiser = advert.Advertiser(server)
        advertiser.export()
        server.export(agent.AGENT_PATH, agent.PairingAgent(lambda kind, device: False))
        app = HidApplication()
        app.export(server)

        body = await dbus.call(client, server.unique_name, gatt.APP_ROOT, dbus.OBJECT_MANAGER, "GetManagedObjects")
        listed = body[0]
        assert list(listed) == [row["path"] for row in FROZEN_TABLE]

        service = dbus.unwrap(listed[S]["org.bluez.GattService1"])
        assert service == {"UUID": "00001812-0000-1000-8000-00805f9b34fb", "Primary": True, "Handle": 0x0100}
        assert listed[S]["org.bluez.GattService1"]["Handle"].signature == "q"

        chrc = listed[f"{S}/c5_consumer"]["org.bluez.GattCharacteristic1"]
        assert dbus.unwrap(chrc) == {
            "UUID": "00002a4d-0000-1000-8000-00805f9b34fb",
            "Service": S,
            "Flags": ["encrypt-read", "encrypt-notify"],
            "Value": bytes(4),
            "Handle": 0,
        }
        assert chrc["Service"].signature == "o" and chrc["Handle"].signature == "q"
        # Their presence would make bluetoothd use AcquireWrite / AcquireNotify.
        assert "WriteAcquired" not in chrc and "NotifyAcquired" not in chrc

        desc = dbus.unwrap(listed[f"{S}/c5_consumer/d0"]["org.bluez.GattDescriptor1"])
        assert desc == {
            "UUID": "00002908-0000-1000-8000-00805f9b34fb",
            "Characteristic": f"{S}/c5_consumer",
            "Flags": ["encrypt-read"],
            "Value": b"\x02\x01",
            "Handle": 0,
        }
    finally:
        server.disconnect()
        client.disconnect()


# --------------------------------------------------------------------------- registration


async def test_registration_layout_and_handle_write_back(h, caplog):
    caplog.set_level(logging.INFO)
    remote = await h.remote()
    state = remote.state
    assert state.available and state.registered and state.adapter == "hci0" and state.powered
    assert state.adapter_address == "11:22:33:44:55:00"

    assert h.bluez.app.root == gatt.APP_ROOT
    assert h.bluez.app.paths == [row["path"] for row in FROZEN_TABLE]
    assert h.bluez.app.table() == FROZEN_ATTRIBUTES

    # bluetoothd wrote the value handles back before it answered RegisterApplication.
    app = remote._app
    assert [app.report_value_handle(i) for i in (1, 2, 3)] == [(0x010A, "bluez"), (0x010E, "bluez"), (0x0112, "bluez")]
    assert {path: obj.handle for path, obj in app.objects()} == app.expected_handles(0x0100)
    assert "report value handles 0x010A, 0x010E, 0x0112 (reported by BlueZ)" in caplog.text

    # Registered, and nothing else: no advertisement, no agent, nothing set on the adapter.
    assert h.bluez.event_names() == ["gatt_registered"]
    assert h.bluez.advert is None and h.bluez.default_agent is None and h.bluez.property_sets == []
    assert not state.advertising


async def test_pinned_handle_refused_falls_back_to_automatic_handles(h, caplog):
    caplog.set_level(logging.INFO)
    h.bluez.reject_pinned_handle = True
    remote = await h.remote()
    assert remote.state.registered
    first = fake_bluez.BUILTIN_LAST_HANDLE + 1
    assert h.bluez.app.table()[0] == (first, "service", "1812", S)
    # The real handles come from bluetoothd's write-back, so direct delivery would still be right.
    assert remote._app.service.handle == first
    assert remote._app.report_value_handle(2) == (first + 14, "bluez")
    assert "refused the HID service at its fixed handle 0x0100" in caplog.text
    assert "differ from the fixed layout" in caplog.text


async def test_registration_is_retried_until_it_works(h):
    h.bluez.fail_register_application = 2  # the pinned attempt and the automatic one of the first round
    remote = await h.remote()
    assert not remote.state.registered
    assert "could not register the HID service" in remote.state.error
    await eventually(lambda: remote.state.registered, what="registration retry")
    assert remote.state.error == ""
    assert h.bluez.app.table() == FROZEN_ATTRIBUTES  # the pin survived the hiccup


async def test_config_changes_never_touch_the_registration(h):
    remote = await h.remote()
    remote.apply_config(fake_bluez.quick_config(device_name="Other Name", gatt_security="plain", settle_ms=5))
    remote.apply_config(fake_bluez.quick_config(tv_address="AA:BB:CC:DD:EE:FF", raw_notify="off"))
    await remote.disconnect()
    assert h.bluez.event_names() == ["gatt_registered"]
    # gatt_security is read once, at start
    assert h.bluez.app.chrcs[f"{S}/c0_info"]["flags"] == ["encrypt-read"]


# --------------------------------------------------------------------------- attribute access


async def test_read_value_honours_offset(h):
    remote, tv = await h.paired_remote()
    assert await tv.read("c1_map") == hid.REPORT_MAP
    assert await tv.read("c1_map", 30) == hid.REPORT_MAP[30:]
    assert await tv.read("c1_map", len(hid.REPORT_MAP)) == b""
    assert await tv.read_long("c1_map", chunk=22) == hid.REPORT_MAP  # Read + Read Blob with a 23 byte MTU
    with pytest.raises(DBusError) as err:
        await tv.read("c1_map", len(hid.REPORT_MAP) + 1)
    assert err.value.type == "org.bluez.Error.InvalidOffset"

    assert await tv.read("c0_info") == bytes.fromhex("11010001")
    assert await tv.read("c3_proto") == b"\x01"
    assert [await tv.read_descriptor(name) for name in ("c4_kbd", "c5_consumer", "c6_system")] == [
        b"\x01\x01",
        b"\x02\x01",
        b"\x03\x01",
    ]
    assert await tv.read("c5_consumer") == bytes(4)


async def test_control_point_and_protocol_mode_store_writes(h):
    remote, tv = await h.paired_remote()
    await tv.write("c3_proto", b"\x00")
    assert await tv.read("c3_proto") == b"\x00"
    await tv.write("c2_ctrl", b"\x01")
    assert remote._app.characteristics[2].value == b"\x01"
    with pytest.raises(DBusError) as err:
        await tv.write("c3_proto", b"\x00\x01")
    assert err.value.type == "org.bluez.Error.InvalidValueLength"
    with pytest.raises(AttError):
        await tv.write("c1_map", b"\x00")  # bluetoothd itself refuses: no write flag
    with pytest.raises(AttError):
        await tv.read("c2_ctrl")


async def test_encrypt_flags_keep_strangers_out_and_plain_lets_them_in(h):
    remote = await h.remote()
    stranger = h.bluez.add_device("11:11:11:11:11:11", name="Phone")
    await stranger.connect()
    with pytest.raises(AttError, match="insufficient encryption"):
        await stranger.read("c1_map")
    with pytest.raises(AttError, match="insufficient encryption"):
        await stranger.subscribe()
    await remote.stop()

    plain = await h.remote(gatt_security="plain")
    assert plain.state.registered
    assert await stranger.read("c1_map") == hid.REPORT_MAP
    await stranger.subscribe()
    assert plain.state.subscribed
