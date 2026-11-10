"""Pairing mode: the window, the agent, the advertisement, and forgetting a TV."""

import asyncio
import json
import logging

import fake_bluez
import pytest
from fake_bluez import ADAPTER, eventually

from tcl_me_remo import dbus
from tcl_me_remo.bluez import advert
from tcl_me_remo.bluez import remote as remote_module
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


def stored(h, name):
    try:
        return json.loads(h.state_path.read_text())[name]
    except FileNotFoundError:
        return None


def slow_down(h, monkeypatch, member, delay, *, argument=None):
    """Make the fake bluetoothd take ``delay`` over one method; the event says "it is in there now"."""
    entered = asyncio.Event()
    real = fake_bluez.FakeBluez._call

    async def call(self, msg, after):
        if msg.member == member and (argument is None or argument in msg.body):
            entered.set()
            if member == "RegisterAdvertisement":
                # bluetoothd notes the registration when it reads the request and answers
                # later; the fake does its waiting after that point, like the real thing.
                h.bluez.advert_register_delay = delay
            else:
                await asyncio.sleep(delay)
        return await real(self, msg, after)

    monkeypatch.setattr(fake_bluez.FakeBluez, "_call", call)
    return entered


async def assert_nothing_left_of_pairing_mode(h, remote):
    """Agent, advertisement and Pairable are as they were, now and a few ticks later."""
    for _ in range(2):
        assert not remote.state.pairing and not remote.state.advertising
        assert h.bluez.advert is None and h.bluez.default_agent is None and h.bluez.agents == {}
        assert h.bluez.adapter.props["Pairable"] is False
        assert remote._pairing_task is None or remote._pairing_task.done()
        await asyncio.sleep(4 * remote_module.PAIRING_TICK)
    assert not remote._tasks and not remote._op_lock.locked()


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


@pytest.mark.parametrize("field", ["Name", "Alias"])
async def test_the_name_a_tv_gives_itself_cannot_forge_a_log_line(h, caplog, field):
    """It is whatever the device that bonds in the window says: cleaned before anybody gets it."""
    caplog.set_level(logging.INFO)
    remote = await h.remote()
    forged = "TV\nERROR tcl_me_remo.daemon: lost the connection\r\n\x1b[2Jto the system D-Bus\t\x7f " + "x" * 300
    tv = h.bluez.add_device(TV, name=forged if field == "Name" else "")
    tv.props["Alias"] = forged  # bluetoothd's Alias is the remote name unless the user set another
    await h.pair(remote, tv)

    clean = ("TV ERROR tcl_me_remo.daemon: lost the connection [2Jto the system D-Bus " + "x" * 300)[:64]
    assert h.paired == [(TV, clean)]  # what the service stores in the settings and logs itself
    assert remote.state.tv_name == clean and remote._cfg.tv_name == clean
    assert f"paired with {clean} ({TV})" in caplog.messages
    assert not [message for message in caplog.messages if not message.isprintable()]


def test_clean_name_keeps_ordinary_names_as_they_are():
    for name in ("Living Room TV", "Smart TV Pro 65”", "テレビ", "Wohnzimmer – TCL", ""):
        assert remote_module._clean_name(name) == name
    assert remote_module._clean_name("  TCL \t 55C745 \n") == "TCL 55C745"
    assert remote_module._clean_name("\n\r\x00") == ""
    assert len(remote_module._clean_name("x" * 63 + " " + "y" * 10)) == 63  # cut, then no trailing blank


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


# --------------------------------------------------------------------------- bonds without a completed window


async def test_bond_from_an_unfinished_window_is_removed_again(h):
    """Otherwise BlueZ refuses that TV's next attempt (JustWorksRepairing = never) for good."""
    remote = await h.remote()
    tv = h.bluez.add_device(TV, rpa="4F:00:00:00:00:01")
    await remote.start_pairing(0.3)
    assert await tv.pair()  # bonded ... but the window ends before the TV uses the HID service
    await eventually(lambda: stored(h, "pending") == [TV], what="the bond to be put on record")
    await eventually(lambda: not remote.state.pairing, what="window timeout")
    assert "timed out" in remote.state.error and not remote.state.paired and h.paired == []
    assert ("device_removed", tv.path) in h.bluez.events and tv not in h.bluez.devices
    assert stored(h, "pending") == [] and stored(h, "stale") == []

    # The user tries again: the TV is treated like any new one.
    again = h.bluez.add_device(TV, rpa="4F:00:00:00:00:02")
    await h.pair(remote, again)
    assert h.paired == [(TV, "Living Room TV")] and remote.state.paired
    assert stored(h, "pending") == [] and again in h.bluez.devices


async def test_cancelling_after_the_tv_bonded_removes_the_bond_too(h):
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    await remote.start_pairing(30)
    assert await tv.pair()
    await eventually(lambda: stored(h, "pending") == [TV])
    await remote.cancel_pairing()
    assert tv not in h.bluez.devices and stored(h, "pending") == [] and not remote.state.paired
    await assert_nothing_left_of_pairing_mode(h, remote)


async def test_bond_that_cannot_be_removed_now_is_removed_later(h):
    """bluetoothd goes away with the window open: the bond is still there when it returns."""
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    await remote.start_pairing(30)
    assert await tv.pair()
    await eventually(lambda: stored(h, "pending") == [TV])
    await h.bluez.restart()
    await eventually(lambda: tv not in h.bluez.devices, what="removal of the unfinished bond")
    await eventually(lambda: stored(h, "stale") == [] and stored(h, "pending") == [])
    assert not remote.state.pairing and "pairing was interrupted" in remote.state.error


async def test_bond_of_a_window_the_service_did_not_survive_is_removed_at_the_next_start(h):
    h.state_path.write_text(json.dumps({"version": 1, "tv": "", "resubscribes": None, "stale": [], "pending": [TV]}))
    tv = h.bluez.add_device(TV, bonded=True)
    other = h.bluez.add_device("11:11:11:11:11:11", name="Gamepad", bonded=True)
    remote = await h.remote()
    await eventually(lambda: tv not in h.bluez.devices, what="removal of the orphaned bond")
    await eventually(lambda: stored(h, "stale") == [] and stored(h, "pending") == [])
    assert other in h.bluez.devices and not remote.state.paired  # nobody else's bond is touched


async def test_pairing_mode_removes_the_bond_of_a_tv_the_settings_no_longer_name(h):
    """Settings reset or edited by hand: forget() can no longer name the TV, BlueZ still holds its bond."""
    remote, tv = await h.paired_remote()
    await remote.disconnect()
    remote.apply_config(fake_bluez.quick_config())  # no TV any more
    assert not remote.state.paired and tv in h.bluez.devices
    gamepad = h.bluez.add_device("11:11:11:11:11:11", name="Gamepad", bonded=True)
    h.bluez.events.clear()

    await remote.start_pairing(30)
    names = h.bluez.event_names()
    assert names.index("device_removed") < names.index("agent_registered")
    assert tv not in h.bluez.devices and gamepad in h.bluez.devices
    again = h.bluez.add_device(TV, rpa="4F:00:00:00:00:03")
    assert await again.pair_like_android()
    await eventually(lambda: not remote.state.pairing and remote.state.paired, what="pairing to complete")
    assert h.paired[-1] == (TV, "Living Room TV")


# --------------------------------------------------------------------------- cancelling at awkward moments


@pytest.mark.parametrize(
    "member, argument",
    [("RegisterAgent", None), ("RequestDefaultAgent", None), ("Set", "Pairable"), ("RegisterAdvertisement", None)],
)
async def test_cancel_while_the_window_is_being_set_up(h, monkeypatch, member, argument):
    """The request lands between two steps of start_pairing; whatever came after must be undone."""
    remote = await h.remote()
    entered = slow_down(h, monkeypatch, member, 0.05, argument=argument)
    opening = asyncio.ensure_future(remote.start_pairing(30))
    await asyncio.wait_for(entered.wait(), 2.0)
    await remote.cancel_pairing()  # returns only when everything is put back
    assert opening.done() and opening.exception() is None
    await assert_nothing_left_of_pairing_mode(h, remote)
    # Nothing is stuck: pairing mode still opens, and other agents can register.
    await remote.start_pairing(30)
    assert remote.state.pairing and h.bluez.advert["Discoverable"] is True
    await remote.cancel_pairing()


@pytest.mark.parametrize(
    "member, argument", [("RequestDefaultAgent", None), ("Set", "Pairable"), ("RegisterAdvertisement", None)]
)
async def test_cancel_while_the_window_reasserts_its_conditions(h, monkeypatch, member, argument):
    remote = await h.remote()
    await remote.start_pairing(30)
    assert remote.state.pairing and h.bluez.advert is not None
    entered = slow_down(h, monkeypatch, member, 0.05, argument=argument)
    # Give the periodic upkeep something to do for each of the three steps.
    h.bluez.adapter.props["Pairable"] = False
    h.bluez.emit_properties(HCI0, ADAPTER, {"Pairable": False})
    await h.bluez.release_advertisement()
    await asyncio.wait_for(entered.wait(), 2.0)
    await remote.cancel_pairing()
    await assert_nothing_left_of_pairing_mode(h, remote)


async def test_cancel_while_the_old_bond_is_being_removed(h):
    """RemoveDevice of a connected TV takes bluetoothd two seconds; the user may change their mind."""
    remote, tv = await h.paired_remote()
    h.bluez.disconnect_delay = 0.3
    h.bluez.events.clear()
    opening = asyncio.ensure_future(remote.start_pairing(30))
    await asyncio.sleep(0.1)
    await remote.cancel_pairing()
    assert opening.done() and opening.exception() is None
    assert "agent_registered" not in h.bluez.event_names() and "advert_registered" not in h.bluez.event_names()
    await assert_nothing_left_of_pairing_mode(h, remote)


async def test_forget_calls_a_pairing_mode_that_is_being_opened_off(h):
    remote, tv = await h.paired_remote()
    h.bluez.disconnect_delay = 0.2
    opening = asyncio.ensure_future(remote.start_pairing(30))
    await asyncio.sleep(0.05)
    await remote.forget()
    assert opening.done() and opening.exception() is None
    assert remote.state.tv_address == "" and not remote.state.paired
    await assert_nothing_left_of_pairing_mode(h, remote)


async def test_cancel_lets_a_tv_that_just_paired_be_stored(h):
    """Tearing the bond down at that point would leave the TV paired and us not."""
    remote = await h.remote()
    tv = h.bluez.add_device(TV)
    adopting = asyncio.Event()

    async def slow_listener(address, name):
        adopting.set()
        await asyncio.sleep(0.1)
        h.paired.append((address, name))

    remote.on_paired = slow_listener
    await remote.start_pairing(30)
    assert await tv.pair_like_android()
    await asyncio.wait_for(adopting.wait(), 2.0)
    await remote.cancel_pairing()
    assert h.paired == [(TV, "Living Room TV")] and remote.state.paired and tv in h.bluez.devices
    assert stored(h, "pending") == []
    assert not remote.state.pairing and h.bluez.advert is None and h.bluez.default_agent is None


async def test_bluetoothd_restart_during_pairing_leaves_a_reason(h):
    remote = await h.remote()
    await remote.start_pairing(30)
    await h.bluez.restart()
    await eventually(lambda: remote.state.registered and not remote.state.pairing, what="recovery")
    # Registering again clears registration trouble, not the news that pairing did not happen.
    assert remote.state.error == "pairing was interrupted: Bluetooth went away"
    await remote.start_pairing(30)
    assert remote.state.error == ""
    await remote.cancel_pairing()


# --------------------------------------------------------------------------- classic visibility


async def test_pairing_warns_while_the_adapter_itself_is_discoverable(h, caplog):
    """bluetoothd then drops "BR/EDR not supported" from our Flags; a TV may pair over classic and fail."""
    remote = await h.remote()
    h.bluez.adapter.props["Discoverable"] = True  # e.g. a desktop Bluetooth settings page is open
    h.bluez.emit_properties(HCI0, ADAPTER, {"Discoverable": True})
    await asyncio.sleep(0.02)
    await remote.start_pairing(0.5)
    assert remote.state.pairing and "discoverable over classic Bluetooth" in remote.state.error
    assert "discoverable over classic Bluetooth" in caplog.text

    h.bluez.adapter.props["Discoverable"] = False  # the other screen was closed
    h.bluez.emit_properties(HCI0, ADAPTER, {"Discoverable": False})
    await eventually(lambda: remote.state.error == "", what="the warning to clear")
    h.bluez.adapter.props["Discoverable"] = True
    h.bluez.emit_properties(HCI0, ADAPTER, {"Discoverable": True})
    await eventually(lambda: "discoverable over classic Bluetooth" in remote.state.error)

    await eventually(lambda: not remote.state.pairing, what="window timeout")
    assert remote.state.error.startswith("pairing mode timed out before a TV paired (this machine is also")
    # The adapter's setting is somebody else's: we never write it.
    assert sets(h, "Discoverable") == [] and h.bluez.adapter.props["Discoverable"] is True

    # The warning is about an open window: cancelling one leaves nothing behind.
    await remote.start_pairing(30)
    assert "discoverable over classic Bluetooth" in remote.state.error
    await remote.cancel_pairing()
    assert not remote.state.pairing and remote.state.error == ""


async def test_a_request_refused_because_pairing_is_open_does_not_become_the_state_s_error(h):
    """Something else wants the TV while the window is open (a wake action, a key from a
    shell).  It is told why not - and that is all: the refusal must neither show up in the
    pairing dialog, nor hide the warning that matters there, nor outlive the window."""
    remote = await h.remote()
    await remote.start_pairing(30)
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(0.2)
    assert err.value.code == "not_paired" and "pairing mode is open" in err.value.message
    assert remote.state.pairing and remote.state.error == ""

    # With the warning about classic visibility up, it stays what the dialog shows.
    h.bluez.adapter.props["Discoverable"] = True
    h.bluez.emit_properties(HCI0, ADAPTER, {"Discoverable": True})
    await eventually(lambda: "discoverable over classic Bluetooth" in remote.state.error)
    with pytest.raises(TransportError):
        await remote.ensure_ready(0.2, wake=True)
    assert remote.state.error.startswith("this machine is also discoverable over classic Bluetooth")

    await remote.cancel_pairing()
    assert not remote.state.pairing and remote.state.error == ""
    # Outside a window the same call's problem is the state's error, as ever.
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(0.2)
    assert err.value.code == "not_paired" and remote.state.error == "no TV is paired yet"


# --------------------------------------------------------------------------- what bluetoothd keeps of a removed TV


async def test_forget_then_pair_the_same_tv_again_in_one_bluetoothd_lifetime(h):
    """bluetoothd keeps the removed TV's subscription state; its new CCC writes change nothing,
    so neither StopNotify nor StartNotify is ever called."""
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    subscribed_at = remote._subscribed_at
    await remote.forget()
    state = remote.state
    assert not state.paired and not state.connected
    assert not state.subscribed  # nothing to show for a TV that is gone ...
    assert remote._app.subscribed  # ... although bluetoothd never told us the subscription ended

    again = h.bluez.add_device(TV, rpa="5A:00:00:00:00:02")
    again.auto_connect = True
    await remote.start_pairing(5)
    assert await again.pair_like_android()
    await eventually(lambda: not remote.state.pairing and remote.state.paired, what="pairing without StartNotify")
    assert remote._subscribed_at == subscribed_at  # indeed: no StartNotify was seen
    assert await remote.ensure_ready(2.0) and remote.state.delivery == "bluez" and remote.state.subscribed
    await remote.send_key("OK", 0.01)
    await eventually(lambda: len(again.keys("bluez")) == 2)
    assert h.raw.packets == [] and remote.bounces == 0 and h.bluez.dropped_notifications == 0


async def test_pairing_again_without_forgetting_works_the_same(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    again = h.bluez.add_device(TV, rpa="5A:00:00:00:00:03")
    again.auto_connect = True
    await remote.start_pairing(5)
    assert tv not in h.bluez.devices
    assert await again.pair_like_android()
    await eventually(lambda: not remote.state.pairing and remote.state.paired, what="pairing to complete")
    assert await remote.ensure_ready(2.0) and remote.state.delivery == "bluez"
    await remote.send_key("OK", 0.01)
    await eventually(lambda: len(again.keys("bluez")) == 2)
    assert h.raw.packets == [] and remote.bounces == 0


async def test_subscription_of_a_removed_tv_ends_with_the_next_notification(h):
    """The fake follows send_notification_to_device(): stale state goes when it is next looked at."""
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    await remote.forget()
    kbd = h.bluez.app.find_chrc("c4_kbd")
    assert h.bluez.app.ccc[kbd] == {TV}  # lingering

    other = h.bluez.add_device("22:22:22:22:22:22", name="Bedroom TV")
    await h.pair(remote, other)
    assert h.bluez.app.ccc[kbd] == {TV, "22:22:22:22:22:22"}
    assert await remote.ensure_ready(2.0)
    await remote.send_key("ENTER", 0.01)
    await eventually(lambda: len(other.keys("bluez")) == 2)
    assert all(subscribers == {"22:22:22:22:22:22"} for subscribers in h.bluez.app.ccc.values())
    assert remote.state.subscribed and remote.state.ready  # the new TV's subscription is untouched

    # And when the last subscriber's state goes that way, StopNotify does arrive.
    await remote.forget()
    assert remote._app.subscribed
    remote._app.reports[1].notify(b"\x00\x00")  # any notification makes bluetoothd look
    await eventually(lambda: not remote._app.subscribed, what="StopNotify for the removed TV")


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
