"""The mirror of bluetoothd's object tree."""

import asyncio

import fake_bluez
import pytest
from fake_bluez import eventually

from tcl_me_remo import dbus
from tcl_me_remo.bluez import adapter as adapter_module
from tcl_me_remo.bluez.adapter import ADAPTER, DEVICE, BluezWatcher
from tcl_me_remo.dbus import Message, Variant

BLUETOOTHD = ":1.42"
HCI0 = "/org/bluez/hci0"
DEV = f"{HCI0}/dev_AA_BB_CC_DD_EE_FF"


def signal(path, interface, member, signature, body, sender=BLUETOOTHD):
    message = Message.new_signal(path, interface, member, signature, body)
    message.sender = sender
    return message


def added(path, interfaces, **kwargs):
    wire = {
        name: {key: Variant(sig, value) for key, (sig, value) in props.items()} for name, props in interfaces.items()
    }
    return signal("/", dbus.OBJECT_MANAGER, "InterfacesAdded", "oa{sa{sv}}", [path, wire], **kwargs)


def removed(path, names, **kwargs):
    return signal("/", dbus.OBJECT_MANAGER, "InterfacesRemoved", "oas", [path, names], **kwargs)


def changed(path, interface, props, invalidated=(), **kwargs):
    wire = {key: Variant(sig, value) for key, (sig, value) in props.items()}
    body = [interface, wire, list(invalidated)]
    return signal(path, dbus.PROPERTIES, "PropertiesChanged", "sa{sv}as", body, **kwargs)


def make_watcher():
    events = []
    watcher = BluezWatcher(None, on_change=lambda: events.append("change"), on_owner_change=events.append)
    watcher.owner, watcher.synced = BLUETOOTHD, True
    return watcher, events


def test_signals_are_folded_into_the_mirror():
    watcher, events = make_watcher()
    watcher._handle_signal(added(DEV, {DEVICE: {"Address": ("s", "AA:BB:CC:DD:EE:FF"), "Connected": ("b", False)}}))
    assert watcher.objects == {DEV: {DEVICE: {"Address": "AA:BB:CC:DD:EE:FF", "Connected": False}}}
    watcher._handle_signal(changed(DEV, DEVICE, {"Connected": ("b", True), "RSSI": ("n", -40)}))
    watcher._handle_signal(changed(DEV, DEVICE, {}, invalidated=["RSSI"]))
    assert watcher.interface(DEV, DEVICE) == {"Address": "AA:BB:CC:DD:EE:FF", "Connected": True}
    assert watcher.devices(HCI0) == {DEV: watcher.interface(DEV, DEVICE)} and watcher.devices("/org/bluez/hci1") == {}
    watcher._handle_signal(removed(DEV, [DEVICE]))
    assert watcher.objects == {} and events == ["change"] * 4

    # Things that change nothing do not wake anybody.
    watcher._handle_signal(changed(DEV, DEVICE, {"Connected": ("b", True)}))  # unknown object
    watcher._handle_signal(removed(DEV, [DEVICE]))
    assert len(events) == 4


def test_only_the_current_owner_of_org_bluez_is_believed():
    watcher, events = make_watcher()
    spoof = added(DEV, {DEVICE: {"Paired": ("b", True)}}, sender=":1.99")
    watcher._handle_signal(spoof)
    assert watcher.objects == {} and events == []

    # NameOwnerChanged must come from the bus itself.
    lost = ["org.bluez", BLUETOOTHD, ""]
    fake = signal(dbus.DBUS_PATH, dbus.DBUS_NAME, "NameOwnerChanged", "sss", lost, sender=":1.99")
    watcher._handle_signal(fake)
    assert watcher.owner == BLUETOOTHD

    # Somebody else's name changing hands is none of our business.
    other = signal(
        dbus.DBUS_PATH, dbus.DBUS_NAME, "NameOwnerChanged", "sss", ["org.example", ":1.7", ""], sender=dbus.DBUS_NAME
    )
    watcher._handle_signal(other)
    assert watcher.owner == BLUETOOTHD and events == []


def test_malformed_signals_are_survived():
    watcher, events = make_watcher()
    watcher._handle_signal(signal("/", dbus.OBJECT_MANAGER, "InterfacesAdded", "s", ["nonsense"]))
    watcher._handle_signal(signal(DEV, dbus.PROPERTIES, "PropertiesChanged", "s", ["nonsense"]))
    watcher._handle_signal(signal("/", dbus.OBJECT_MANAGER, "InterfacesAdded", "ou", [DEV, 5]))
    assert watcher.objects == {} and events == []


def test_adapters_are_listed_only_when_usable_and_in_index_order():
    watcher, _ = make_watcher()
    full = {ADAPTER: {}, adapter_module.GATT_MANAGER: {}, adapter_module.ADVERTISING_MANAGER: {}}
    watcher.objects = {
        "/org/bluez/hci10": dict(full),
        "/org/bluez/hci2": dict(full),
        "/org/bluez/hci0": {ADAPTER: {}},  # e.g. a classic-only controller: no LE advertising manager
        "/org/bluez": {adapter_module.AGENT_MANAGER: {}},
    }
    assert watcher.adapters() == ["/org/bluez/hci2", "/org/bluez/hci10"]


async def test_signals_racing_with_the_snapshot_are_replayed_on_top_of_it(monkeypatch):
    """Whether bluetoothd emitted them before or after building the reply, the end state is right."""
    watcher, events = make_watcher()
    watcher.synced = False

    async def get_managed_objects(bus, destination, path="/", *, timeout):
        # While the call is in flight: the TV connects, a scan result comes and goes.
        watcher._handle_signal(changed(DEV, DEVICE, {"Connected": ("b", True)}))
        watcher._handle_signal(added(f"{HCI0}/dev_11", {DEVICE: {"Paired": ("b", False)}}))
        watcher._handle_signal(removed(f"{HCI0}/dev_11", [DEVICE]))
        assert watcher.objects == {} and events == []  # held back, not applied to the stale mirror
        await asyncio.sleep(0)
        # The reply was built before the TV connected.
        return {HCI0: {ADAPTER: {"Powered": True}}, DEV: {DEVICE: {"Paired": True, "Connected": False}}}

    monkeypatch.setattr(adapter_module.dbus, "get_managed_objects", get_managed_objects)
    assert await watcher._sync_once() is True
    assert watcher.synced and events == ["change"]
    assert watcher.objects == {HCI0: {ADAPTER: {"Powered": True}}, DEV: {DEVICE: {"Paired": True, "Connected": True}}}


async def test_snapshot_from_a_bluetoothd_that_restarted_meanwhile_is_discarded(monkeypatch):
    watcher, events = make_watcher()
    watcher.synced = False

    async def get_managed_objects(bus, destination, path="/", *, timeout):
        watcher.owner = ":1.43"
        return {HCI0: {ADAPTER: {"Powered": True}}}

    monkeypatch.setattr(adapter_module.dbus, "get_managed_objects", get_managed_objects)
    assert await watcher._sync_once() is False
    assert watcher.objects == {} and not watcher.synced


# --------------------------------------------------------------------------- against the fake daemon


@pytest.fixture
async def bluez(private_bus):
    async with fake_bluez.running() as fake:
        yield fake


async def test_watcher_follows_the_daemon_across_a_restart(bluez):
    bus = await dbus.connect_system_bus()
    owners, changes = [], []
    watcher = BluezWatcher(bus, on_change=lambda: changes.append(1), on_owner_change=owners.append)
    try:
        tv = bluez.add_device(bonded=True)
        await watcher.start()
        assert watcher.synced and watcher.owner == bluez.bus.unique_name and owners == [watcher.owner]
        assert watcher.adapters() == [HCI0]
        assert watcher.interface(HCI0, ADAPTER)["Address"] == "11:22:33:44:55:00"
        assert watcher.devices(HCI0)[tv.path]["Bonded"] is True

        await tv.connect()
        await eventually(lambda: watcher.devices(HCI0)[tv.path]["Connected"])

        first = watcher.owner
        await bluez.stop()
        await eventually(lambda: watcher.owner is None)
        assert watcher.objects == {} and not watcher.synced and watcher.adapters() == []

        await bluez.start()
        await eventually(lambda: watcher.synced)
        assert watcher.owner not in (None, first) and owners == [first, None, watcher.owner]
        assert watcher.devices(HCI0)[tv.path]["Connected"] is False

        await watcher.stop()
        count = len(changes)
        await tv.connect()
        await asyncio.sleep(0.05)
        assert len(changes) == count  # stopped: no more callbacks
        # ...and nothing is believed any longer: what was mirrored is out of date from here on.
        assert watcher.owner is None and not watcher.synced and watcher.objects == {}

        # A later start takes a fresh snapshot instead of trusting the old one.
        await watcher.start()
        assert watcher.synced and watcher.devices(HCI0)[tv.path]["Connected"] is True
        assert owners[-1] == watcher.owner == bluez.bus.unique_name
    finally:
        await watcher.stop()
        bus.disconnect()


async def test_watcher_starts_without_a_daemon(private_bus):
    bus = await dbus.connect_system_bus()
    watcher = BluezWatcher(bus, on_change=lambda: None, on_owner_change=lambda owner: None)
    try:
        await watcher.start()
        assert watcher.owner is None and not watcher.synced and watcher.objects == {}
        async with fake_bluez.running():
            await eventually(lambda: watcher.synced, what="sync once bluetoothd appears")
            assert watcher.adapters() == [HCI0]
        await eventually(lambda: watcher.owner is None)
    finally:
        await watcher.stop()
        bus.disconnect()
