"""Pairing mode: the window, the agent, the advertisement, and forgetting a TV."""

import asyncio
import json
import logging

import fake_bluez
import pytest
from fake_bluez import ADAPTER, eventually

from tcl_me_remo import dbus
from tcl_me_remo.bluez import advert
from tcl_me_remo.bluez.agent import AGENT_PATH
from tcl_me_remo.dbus import DBusError
from tcl_me_remo.interfaces import TransportError

TV = "AA:BB:CC:DD:EE:FF"
HCI0 = "/org/bluez/hci0"


@pytest.fixture
async def h(private_bus, tmp_path, monkeypatch):
    async with fake_bluez.harness(tmp_path, monkeypatch) as harness:
        yield harness


async def ask_agent(h, remote, member="RequestAuthorization", signature="o", body=None):
    """Call our Agent1 object directly, the way bluetoothd would."""
    client = await h.bus()
    target = (remote._bus.unique_name, AGENT_PATH, "org.bluez.Agent1")
    return await dbus.call(client, *target, member, signature, body if body is not None else [f"{HCI0}/dev_X"])


def sets(h, name):
    return [value for path, interface, prop, value in h.bluez.property_sets if interface == ADAPTER and prop == name]


# --------------------------------------------------------------------------- the happy path


async def test_full_pairing_flow_with_a_tv_behind_a_private_address(h, caplog):
    caplog.set_level(logging.INFO)
    # Another agent (Steam, a desktop applet) is the default one before we start.
    other = await h.bus()
    await dbus.call(other, "org.bluez", "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os", ["/other", ""])
    assert h.bluez.default_agent == (other.unique_name, "/other", "")

    remote = await h.remote(device_name="Couch Remote")
    tv = h.bluez.add_device(TV, name="Living Room TV", rpa="5A:11:22:33:44:55")

    await remote.start_pairing(30)
    state = remote.state
    assert state.pairing and 28 <= state.pairing_seconds_left <= 30 and state.advertising

    # Our agent took over, with the capability that makes the TV use Just Works.
    assert h.bluez.default_agent == (remote._bus.unique_name, AGENT_PATH, "NoInputNoOutput")
    assert h.bluez.adapter.props["Pairable"] is True
    # The advertisement is the discoverable one; the adapter itself is left alone.
    assert h.bluez.advert == {
        "Type": "peripheral",
        "ServiceUUIDs": ["00001812-0000-1000-8000-00805f9b34fb"],
        "Appearance": 0x03C1,
        "LocalName": "Couch Remote",
        "Discoverable": True,
        "MinInterval": 30,
        "MaxInterval": 60,
    }
    assert h.bluez.adapter.props["Discoverable"] is False and h.bluez.adapter.props["Alias"] == "steamdeck"
    # The HID service was there before anything went on air.
    names = h.bluez.event_names()
    assert names.index("gatt_registered") < names.index("advert_registered")

    assert await tv.pair()
    assert tv.path.endswith("dev_5A_11_22_33_44_55")  # the object path keeps the private address
    await asyncio.sleep(0.1)
    assert remote.state.pairing and h.paired == []  # bonded, but it has not used the HID service yet

    found = await tv.discover()
    assert found["info"] == bytes.fromhex("11010001") and found["c6_system"] == b"\x03\x01"
    await eventually(lambda: h.paired, what="on_paired")
    # The identity address from the Address property, never the one in the object path.
    assert h.paired == [(TV, "Living Room TV")]
    await eventually(lambda: not remote.state.pairing)
    await tv.subscribe()

    state = remote.state
    assert state.paired and state.tv_address == TV and state.tv_name == "Living Room TV"
    assert state.connected and state.subscribed and not state.advertising and state.error == ""
    assert state.pairing_seconds_left == 0
    assert tv.props["Trusted"] is True
    # Everything was put back: the other agent is default again, Pairable is off, nothing on air.
    assert h.bluez.default_agent == (other.unique_name, "/other", "")
    assert remote._bus.unique_name not in h.bluez.agents
    assert h.bluez.adapter.props["Pairable"] is False and sets(h, "Pairable") == [True, False]
    assert h.bluez.advert is None
    assert sets(h, "Discoverable") == [] and sets(h, "Alias") == []
    assert "paired with Living Room TV (AA:BB:CC:DD:EE:FF)" in caplog.text
    # The hint about re-subscribing belongs to the new TV and is still unknown.
    assert json.loads(h.state_path.read_text())["resubscribes"] is None

    # and the TV is usable right away
    assert await remote.ensure_ready(2.0)
    await remote.send_key("OK", 0.01)
    await eventually(lambda: len(tv.keys()) == 2)


async def test_pairing_completes_on_start_notify_alone_and_with_legacy_pairing(h):
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    await remote.start_pairing(30)
    # LE legacy Just Works: the kernel does not consult the agent at all.
    assert await tv.pair(secure_connections=False)
    await asyncio.sleep(0.05)
    assert remote.state.pairing
    await tv.subscribe()  # StartNotify carries no device, a single new bonded peer is enough
    await eventually(lambda: h.paired == [(TV, "Living Room TV")])
    await eventually(lambda: not remote.state.pairing)


async def test_pairable_left_on_if_it_was_on(h):
    h.bluez.adapter.props["Pairable"] = True
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    await h.pair(remote, tv)
    assert h.bluez.adapter.props["Pairable"] is True and sets(h, "Pairable") == []


async def test_tv_without_a_name_gets_an_empty_name_not_its_address(h):
    remote = await h.remote()
    tv = h.bluez.add_device(TV, name="")
    await h.pair(remote, tv)
    assert h.paired == [(TV, "")]


async def test_on_paired_failure_does_not_break_pairing(h, caplog):
    remote = await h.remote()

    async def boom(address, name):
        raise RuntimeError("disk full")

    remote.on_paired = boom
    tv = h.bluez.add_device(TV)
    await h.pair(remote, tv)
    assert remote.state.paired and remote.state.tv_address == TV and not remote.state.pairing
    assert "the on_paired callback failed" in caplog.text
    assert h.bluez.default_agent is None and h.bluez.advert is None


# --------------------------------------------------------------------------- who gets in


async def test_everything_is_rejected_outside_the_window(h):
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    # We hold no agent, so bluetoothd has nobody to ask (and the adapter is not bondable).
    assert h.bluez.default_agent is None
    assert not await tv.pair()
    # Even a direct question to our agent object is answered with "no".
    for member, signature, body in (
        ("RequestAuthorization", "o", [tv.path]),
        ("RequestConfirmation", "ou", [tv.path, 123456]),
        ("AuthorizeService", "os", [tv.path, "00001812-0000-1000-8000-00805f9b34fb"]),
        ("RequestPinCode", "o", [tv.path]),
        ("RequestPasskey", "o", [tv.path]),
    ):
        with pytest.raises(DBusError) as err:
            await ask_agent(h, remote, member, signature, body)
        assert err.value.type == "org.bluez.Error.Rejected", member
    assert not remote.state.paired and h.paired == []


async def test_only_one_new_device_is_accepted(h):
    gamepad = h.bluez.add_device("11:11:11:11:11:11", name="Gamepad", bonded=True)  # bonded long ago
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    intruder = h.bluez.add_device("22:22:22:22:22:22", name="Someone Else")

    await remote.start_pairing(30)
    with pytest.raises(DBusError):  # a device that was bonded before the window is not "the new TV"
        await ask_agent(h, remote, body=[gamepad.path])
    with pytest.raises(DBusError):  # nothing has been accepted yet, so no service authorisation either
        await ask_agent(h, remote, "AuthorizeService", "os", [tv.path, "1812"])

    assert await tv.pair()
    assert not await intruder.pair()  # the one slot is taken
    await ask_agent(h, remote, body=[tv.path])  # the accepted device may ask again
    await ask_agent(h, remote, "RequestConfirmation", "ou", [tv.path, 0])
    await ask_agent(h, remote, "AuthorizeService", "os", [tv.path, "1812"])
    with pytest.raises(DBusError):
        await ask_agent(h, remote, "RequestPinCode", "o", [tv.path])

    # The intruder reading our service does not make it the TV either.
    await intruder.connect()
    await tv.discover()
    await eventually(lambda: h.paired == [(TV, "Living Room TV")])
    await eventually(lambda: not remote.state.pairing)
    with pytest.raises(DBusError):  # window closed
        await ask_agent(h, remote, body=[tv.path])


async def test_window_times_out_and_cleans_up(h):
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    await remote.start_pairing(0.3)
    assert remote.state.pairing and remote.state.pairing_seconds_left == 1
    await eventually(lambda: not remote.state.pairing, what="pairing timeout")
    await eventually(lambda: h.bluez.advert is None and h.bluez.default_agent is None)
    assert "timed out" in remote.state.error
    assert h.bluez.adapter.props["Pairable"] is False and sets(h, "Pairable") == [True, False]
    assert not remote.state.advertising
    assert not await tv.pair() and h.paired == []


async def test_cancel_closes_the_window_at_once(h):
    remote = await h.remote()
    await remote.start_pairing(30)
    assert remote.state.pairing
    await remote.cancel_pairing()
    assert not remote.state.pairing and remote.state.pairing_seconds_left == 0
    assert h.bluez.advert is None and h.bluez.default_agent is None
    assert h.bluez.adapter.props["Pairable"] is False
    await remote.cancel_pairing()  # idempotent
    # opening again works, and re-opening replaces the running window
    await remote.start_pairing(30)
    await remote.start_pairing(30)
    assert remote.state.pairing and h.bluez.event_names().count("agent_registered") == 3
    await remote.cancel_pairing()


async def test_window_holds_on_to_agent_role_and_pairability(h):
    remote = await h.remote()
    other = await h.bus()
    manager = ("org.bluez", "/org/bluez", "org.bluez.AgentManager1")
    await remote.start_pairing(30)
    # Another program grabs the default-agent role, and a PairableTimeout switches Pairable off.
    await dbus.call(other, *manager, "RegisterAgent", "os", ["/other", "KeyboardDisplay"])
    await dbus.call(other, *manager, "RequestDefaultAgent", "o", ["/other"])
    h.bluez.adapter.props["Pairable"] = False
    h.bluez.emit_properties(HCI0, ADAPTER, {"Pairable": False})
    await h.bluez.release_advertisement()
    await eventually(lambda: h.bluez.default_agent[0] == remote._bus.unique_name, what="default agent re-assertion")
    await eventually(lambda: h.bluez.adapter.props["Pairable"] is True)
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["Discoverable"], what="advert back on air")
    await remote.cancel_pairing()
    assert h.bluez.default_agent[0] == other.unique_name


# --------------------------------------------------------------------------- pairing again


async def test_pairing_again_removes_the_stale_bond_first(h):
    remote, tv = await h.paired_remote()
    assert tv.connected and tv.path == f"{HCI0}/dev_AA_BB_CC_DD_EE_FF"
    h.bluez.events.clear()

    await remote.start_pairing(30)
    names = h.bluez.event_names()
    # Without this BlueZ would refuse the new pairing without asking anybody.
    assert names.index("device_removed") < names.index("agent_registered") < names.index("advert_registered")
    assert not remote.state.paired and not remote.state.connected

    # The same TV pairs again and gets the very same object path as before.
    again = h.bluez.add_device(TV, name="Living Room TV")
    assert again.path == tv.path
    assert await again.pair_like_android()
    await eventually(lambda: len(h.paired) == 2 and not remote.state.pairing)
    assert remote.state.paired and remote.state.connected


async def test_ensure_ready_and_pairing_do_not_overlap(h):
    remote, tv = await h.paired_remote()
    await remote.disconnect()
    tv.auto_connect = False

    waiting = asyncio.ensure_future(remote.ensure_ready(10))
    await eventually(lambda: h.bluez.advert is not None and not h.bluez.advert["Discoverable"])
    await remote.start_pairing(30)  # the user opens pairing mode: the connection attempt gives way
    assert await asyncio.wait_for(waiting, 1.0) is False
    assert h.bluez.advert["Discoverable"] is True

    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(1.0)
    assert err.value.code == "not_paired" and "pairing mode is open" in err.value.message
    await remote.cancel_pairing()


async def test_start_pairing_structural_errors(h):
    remote = await h.remote()
    h.bluez.set_powered(False)
    await eventually(lambda: not remote.state.powered)
    with pytest.raises(TransportError) as err:
        await remote.start_pairing(30)
    assert err.value.code == "bluetooth_off" and not remote.state.pairing

    remote.apply_config(fake_bluez.quick_config(power_on_adapter=True))
    await remote.start_pairing(30)
    assert remote.state.pairing and h.bluez.adapter.props["Powered"] is True
    await remote.cancel_pairing()

    await h.bluez.remove_adapter()
    await eventually(lambda: not remote.state.available)
    with pytest.raises(TransportError) as err:
        await remote.start_pairing(30)
    assert err.value.code == "no_adapter"


# --------------------------------------------------------------------------- the advertisement


def test_advertised_name_is_cut_on_a_character_boundary():
    assert advert.fit_name("SteamOS Remote") == "SteamOS Remote"
    assert advert.fit_name("x" * 40) == "x" * 26
    assert advert.fit_name("Wohnzimmerfernbedienung-äöüß") == "Wohnzimmerfernbedienung-ä"  # "ö" would be cut in half
    assert advert.fit_name("Wohnzimmer-Fernbedienung äöü") == "Wohnzimmer-Fernbedienung"
    assert len(advert.fit_name("日本語" * 10).encode()) <= 26


async def test_advertising_falls_back_when_interval_properties_are_refused(h, caplog):
    h.bluez.unsupported_advert_properties = {"MinInterval", "MaxInterval"}
    remote = await h.remote(device_name="Couch Remote")
    await remote.start_pairing(30)
    assert h.bluez.advert == {
        "Type": "peripheral",
        "ServiceUUIDs": ["00001812-0000-1000-8000-00805f9b34fb"],
        "Appearance": 0x03C1,
        "LocalName": "Couch Remote",
        "Discoverable": True,
    }
    assert "refused the advertising interval settings" in caplog.text
    await remote.cancel_pairing()
    assert h.bluez.advert is None
    # The lesson is remembered: the next advertisement goes straight to the basic object.
    caplog.clear()
    await remote.start_pairing(30)
    assert "MinInterval" not in h.bluez.advert and "refused" not in caplog.text
    await remote.cancel_pairing()


async def test_advertising_failure_is_reported_and_rolled_back(h):
    h.bluez.max_adverts = 0
    remote = await h.remote()
    with pytest.raises(TransportError) as err:
        await remote.start_pairing(30)
    assert err.value.code == "bluetooth_error" and "could not open pairing mode" in err.value.message
    assert not remote.state.pairing and h.bluez.default_agent is None
    assert h.bluez.adapter.props["Pairable"] is False
    assert remote.state.error == err.value.message


# --------------------------------------------------------------------------- forgetting


async def test_forget_removes_the_bond_and_local_state(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    await remote.forget()
    state = remote.state
    assert not state.paired and state.tv_address == "" and state.tv_name == ""
    assert not state.connected and not state.ready and not state.advertising
    assert ("device_removed", tv.path) in h.bluez.events and tv not in h.bluez.devices
    assert json.loads(h.state_path.read_text())["tv"] == ""
    await remote.forget()  # nothing to forget: fine
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(1.0)
    assert err.value.code == "not_paired"


async def test_forget_when_bluez_no_longer_knows_the_tv(h):
    remote = await h.remote(tv_address=TV, tv_name="Old TV")
    assert not remote.state.paired
    await remote.forget()  # DoesNotExist / unknown device is not an error
    assert remote.state.tv_address == ""


async def test_forget_with_bluetooth_off_removes_the_bond_later(h):
    remote, tv = await h.paired_remote()
    h.bluez.set_powered(False)
    await eventually(lambda: not remote.state.powered and not remote.state.connected)
    await remote.forget()  # BlueZ answers NotReady: remembered instead of failing
    assert remote.state.tv_address == "" and tv in h.bluez.devices
    assert json.loads(h.state_path.read_text())["stale"] == [TV]

    h.bluez.set_powered(True)
    await eventually(lambda: tv not in h.bluez.devices, what="stale bond removal")
    await eventually(lambda: json.loads(h.state_path.read_text())["stale"] == [])
