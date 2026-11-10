"""Readiness, key delivery and link handling within one bluetoothd lifetime."""

import asyncio
import logging
import time

import fake_bluez
import pytest
from fake_bluez import eventually, quick_config

from tcl_me_remo import keys
from tcl_me_remo.interfaces import TransportError

TV = "AA:BB:CC:DD:EE:FF"
KBD, CONSUMER, SYSTEM = 0x010A, 0x010E, 0x0112  # report value handles of the frozen table


@pytest.fixture
async def h(private_bus, tmp_path, monkeypatch):
    async with fake_bluez.harness(tmp_path, monkeypatch) as harness:
        yield harness


async def parted(h, **overrides):
    """A paired TV whose link has been closed again: the usual state between uses."""
    remote, tv = await h.paired_remote(**overrides)
    await remote.disconnect()
    assert not tv.connected and not remote.state.connected
    h.bluez.events.clear()
    return remote, tv


# --------------------------------------------------------------------------- ensure_ready


async def test_happy_path_advertises_until_the_tv_connects_then_settles(h):
    remote, tv = await parted(h, settle_ms=150)
    tv.connect_delay = 0.05
    started = time.monotonic()
    assert await remote.ensure_ready(5.0) is True
    elapsed = time.monotonic() - started
    assert 0.05 + 0.15 <= elapsed < 1.0  # connect delay + settle: the link alone is not readiness

    state = remote.state
    assert state.connected and state.ready and state.delivery == "bluez" and state.error == ""
    assert not state.advertising and h.bluez.advert is None  # off air as soon as the TV is there
    # One advertisement, for the bonded TV only: connectable, not discoverable, fast.
    adverts = [props for name, props in h.bluez.events if name == "advert_registered"]
    assert len(adverts) == 1
    assert adverts[0]["Discoverable"] is False and adverts[0]["Type"] == "peripheral"
    assert (adverts[0]["MinInterval"], adverts[0]["MaxInterval"]) == (30, 60)
    assert h.bluez.event_names() == ["advert_registered", "advert_unregistered"]
    assert h.bluez.property_sets[-1][2] != "Discoverable"

    # Already ready: answers at once and does not advertise again.
    assert await remote.ensure_ready(0) is True
    assert h.bluez.event_names() == ["advert_registered", "advert_unregistered"]


async def test_reconnect_in_the_same_bluetoothd_lifetime_needs_no_new_subscription(h):
    remote, tv = await parted(h)
    subscribed_at = remote._subscribed_at
    for _ in range(2):
        assert await remote.ensure_ready(5.0)
        await remote.send_key("HOME", 0.01)
        await tv.drop()  # e.g. suspend
        await eventually(lambda: not remote.state.connected)
        assert not remote.state.ready and remote.state.delivery == ""
        assert remote.state.subscribed  # bluetoothd kept the CCC of the bonded TV
    assert remote._subscribed_at == subscribed_at  # no StartNotify was needed or seen
    assert tv.keys("bluez") == [(CONSUMER, bytes.fromhex("23020000")), (CONSUMER, bytes(4))] * 2
    assert h.raw.packets == [] and remote.bounces == 0


async def test_timeout_returns_false_and_stops_advertising(h):
    remote, tv = await parted(h)
    tv.auto_connect = False  # the TV is off / out of range
    started = time.monotonic()
    assert await remote.ensure_ready(0.3) is False
    assert 0.3 <= time.monotonic() - started < 1.0
    assert h.bluez.event_names() == ["advert_registered", "advert_unregistered"]
    assert h.bluez.advert is None and not remote.state.advertising
    assert remote.state.error == "the TV did not connect over Bluetooth"
    # A zero budget does not even go on air.
    assert await remote.ensure_ready(0) is False
    assert h.bluez.event_names().count("advert_registered") == 1


async def test_not_paired(h):
    remote = await h.remote()
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(1.0)
    assert err.value.code == "not_paired" and err.value.message == "no TV is paired yet"

    remote.apply_config(quick_config(tv_address=TV, tv_name="Gone"))
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(1.0)
    assert err.value.code == "not_paired" and "no longer knows the TV AA:BB:CC:DD:EE:FF" in err.value.message
    assert remote.state.error == err.value.message
    assert h.bluez.advert is None and "advert_registered" not in h.bluez.event_names()

    # A device with that address that is not bonded does not count.
    stranger = h.bluez.add_device(TV)
    await stranger.connect()
    await asyncio.sleep(0.05)
    assert not remote.state.paired and not remote.state.connected


async def test_tv_is_matched_by_address_property_case_insensitively(h):
    tv = h.bluez.add_device(TV, bonded=True)
    remote = await h.remote(tv_address="aa:bb:cc:dd:ee:ff")
    assert remote.state.paired
    await tv.connect()
    await eventually(lambda: remote.state.connected)


async def test_bluetooth_off(h):
    remote, tv = await parted(h)
    h.bluez.set_powered(False)
    await eventually(lambda: not remote.state.powered)
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(1.0)
    assert err.value.code == "bluetooth_off"
    assert h.bluez.adapter.props["Powered"] is False  # not ours to switch on unless told to

    remote.apply_config(quick_config(tv_address=TV, power_on_adapter=True))
    assert await remote.ensure_ready(5.0) is True
    assert h.bluez.adapter.props["Powered"] is True and remote.state.powered


async def test_bluez_errors_surface_as_transport_errors(h):
    remote, tv = await parted(h)
    h.bluez.max_adverts = 0  # RegisterAdvertisement -> NotPermitted
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(1.0)
    assert err.value.code == "bluetooth_error" and "NotPermitted" in err.value.message
    assert remote.state.error == err.value.message
    h.bluez.max_adverts = 5
    assert await remote.ensure_ready(5.0)
    assert remote.state.error == ""


async def test_concurrent_callers_share_one_attempt(h):
    remote, tv = await parted(h)
    tv.connect_delay = 0.1
    results = await asyncio.gather(remote.ensure_ready(5.0), remote.ensure_ready(5.0), remote.ensure_ready(5.0))
    assert results == [True, True, True]
    assert h.bluez.event_names().count("advert_registered") == 1


async def test_concurrent_callers_share_a_failure_and_respect_their_own_deadline(h):
    remote = await h.remote()
    results = await asyncio.gather(remote.ensure_ready(1.0), remote.ensure_ready(1.0), return_exceptions=True)
    assert [type(r) for r in results] == [TransportError, TransportError]
    assert {r.code for r in results} == {"not_paired"}

    remote, tv = await parted(h)
    tv.auto_connect = False
    slow = asyncio.ensure_future(remote.ensure_ready(0.6))
    await eventually(lambda: h.bluez.advert is not None)
    started = time.monotonic()
    assert await remote.ensure_ready(0.1) is False  # waits its own 0.1 s for the first caller, no longer
    assert time.monotonic() - started < 0.4
    assert await slow is False


async def test_cancellation_stops_advertising_and_leaves_nothing_running(h):
    remote, tv = await parted(h)
    tv.auto_connect = False
    task = asyncio.ensure_future(remote.ensure_ready(30))
    await eventually(lambda: h.bluez.advert is not None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.bluez.advert is None and not remote.state.advertising
    assert not remote._tasks and remote._ensure_active == 0 and not remote._op_lock.locked()

    # The way the service core bounds it near suspend:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(remote.ensure_ready(30), 0.15)
    assert h.bluez.advert is None

    tv.auto_connect = True
    assert await remote.ensure_ready(5.0)


async def test_advertisement_released_by_bluetoothd_goes_back_on_air(h):
    remote, tv = await parted(h)
    tv.auto_connect = False
    task = asyncio.ensure_future(remote.ensure_ready(5.0))
    await eventually(lambda: h.bluez.advert is not None)
    await h.bluez.release_advertisement()
    assert h.bluez.advert is None
    await eventually(lambda: h.bluez.advert is not None, what="re-registration after Release()")
    await tv.connect()
    assert await task is True


async def test_new_device_name_is_used_for_the_next_advertisement(h):
    remote, tv = await parted(h)
    tv.auto_connect = False
    remote.apply_config(quick_config(tv_address=TV, device_name="Den Remote"))
    assert await remote.ensure_ready(0.1) is False
    assert [props["LocalName"] for name, props in h.bluez.events if name == "advert_registered"] == ["Den Remote"]


# --------------------------------------------------------------------------- send_key


async def test_keys_of_each_page_go_to_their_report_characteristic(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    for spec in ("WAKE", "HOME", "ENTER", "k:3c", "SLEEP"):
        await remote.send_key(spec, 0.01)
    await eventually(lambda: len(tv.received) == 10)
    assert tv.received == [
        (SYSTEM, b"\x04", "bluez"), (SYSTEM, b"\x00", "bluez"),                              # System Wake Up
        (CONSUMER, bytes.fromhex("23020000"), "bluez"), (CONSUMER, bytes(4), "bluez"),      # AC Home
        (KBD, b"\x28\x00", "bluez"), (KBD, b"\x00\x00", "bluez"),                            # Enter
        (KBD, b"\x3c\x00", "bluez"), (KBD, b"\x00\x00", "bluez"),                            # F3 by raw usage
        (CONSUMER, bytes.fromhex("32000000"), "bluez"), (CONSUMER, bytes(4), "bluez"),      # Sleep
    ]  # fmt: skip
    assert h.bluez.dropped_notifications == 0 and h.raw.packets == []
    with pytest.raises(keys.UnknownKey):
        await remote.send_key("NOPE", 0.01)


async def test_hold_time_and_serialisation(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    stamps = []
    original = remote._deliver

    def deliver(report_id, payload):
        stamps.append((time.monotonic(), report_id, payload))
        original(report_id, payload)

    remote._deliver = deliver
    await asyncio.gather(remote.send_key("UP", 0.1), remote.send_key("ENTER", 0.05))
    assert [(report_id, payload) for _, report_id, payload in stamps] == [
        (2, bytes.fromhex("42000000")),
        (2, bytes(4)),
        (1, b"\x28\x00"),
        (1, b"\x00\x00"),
    ]  # the second key waits for the first one's release: never interleaved
    assert stamps[1][0] - stamps[0][0] >= 0.1 and stamps[3][0] - stamps[2][0] >= 0.05


async def test_send_key_needs_a_ready_link(h):
    remote, tv = await parted(h)
    with pytest.raises(TransportError) as err:
        await remote.send_key("HOME", 0.01)
    assert err.value.code == "not_connected"
    # Connected is not enough: readiness is only declared by ensure_ready (settle time).
    await tv.connect()
    await eventually(lambda: remote.state.connected)
    with pytest.raises(TransportError) as err:
        await remote.send_key("HOME", 0.01)
    assert err.value.code == "not_connected" and tv.received == []


async def test_link_dropping_mid_key_is_reported(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    sending = asyncio.ensure_future(remote.send_key("HOME", 0.3))
    await eventually(lambda: len(tv.received) == 1)
    await tv.drop()
    with pytest.raises(TransportError) as err:
        await sending
    assert err.value.code == "not_connected" and "dropped while HOME was held" in err.value.message
    assert len(tv.received) == 1  # no release into the void


async def test_cancelled_key_press_is_still_released(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    sending = asyncio.ensure_future(remote.send_key("VOL_UP", 5.0))
    await eventually(lambda: len(tv.received) == 1)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    await eventually(lambda: len(tv.received) == 2)
    assert tv.keys() == [(CONSUMER, bytes.fromhex("e9000000")), (CONSUMER, bytes(4))]
    assert remote.state.ready and not remote._send_lock.locked()


# --------------------------------------------------------------------------- link handling


async def test_disconnect(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    await remote.disconnect()
    state = remote.state
    assert not tv.connected and not state.connected and not state.ready and state.delivery == ""
    assert h.bluez.advert is None
    await remote.disconnect()  # not connected: nothing to do, nothing raised

    # It also calls off a connection attempt in progress and takes the advertisement down.
    tv.auto_connect = False
    waiting = asyncio.ensure_future(remote.ensure_ready(30))
    await eventually(lambda: h.bluez.advert is not None)
    await remote.disconnect()
    assert await asyncio.wait_for(waiting, 1.0) is False
    assert h.bluez.advert is None


async def test_disconnect_survives_being_cut_short(h):
    """The service gives disconnect() 0.5 s; real bluetoothd needs 2 s to drop the link."""
    h.bluez.disconnect_delay = 0.3
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(remote.disconnect(), 0.05)
    # Not usable any more even though the link is still up for a moment...
    assert tv.connected and not remote.state.ready
    with pytest.raises(TransportError):
        await remote.send_key("HOME", 0.01)
    # ...and when it goes down that is not mistaken for the TV dropping us.
    await eventually(lambda: not tv.connected and not remote.state.connected)
    assert h.bluez.advert is None


async def test_ensure_ready_waits_out_a_link_that_is_being_closed(h):
    h.bluez.disconnect_delay = 0.2
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    closing = asyncio.ensure_future(remote.disconnect())
    await asyncio.sleep(0.05)
    assert tv.connected
    assert await remote.ensure_ready(5.0) is True  # not on the dying link: on the next one
    await closing
    assert tv.connections == 2 and remote.state.ready


async def test_unexpected_drop_without_reconnect_on_drop_stays_quiet(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    h.bluez.events.clear()
    await tv.drop()
    await eventually(lambda: not remote.state.connected)
    await asyncio.sleep(0.1)
    assert h.bluez.events == [] and not remote.state.advertising  # a TV in standby is not poked


async def test_reconnect_on_drop_advertises_in_the_background(h, caplog):
    caplog.set_level(logging.INFO)
    remote, tv = await h.paired_remote(reconnect_on_drop=True)
    assert await remote.ensure_ready(2.0)
    tv.auto_connect = False
    h.bluez.events.clear()

    await tv.drop()
    await eventually(lambda: h.bluez.advert is not None, what="background advertisement")
    assert h.bluez.advert["Discoverable"] is False
    assert (h.bluez.advert["MinInterval"], h.bluez.advert["MaxInterval"]) == (150, 250)  # relaxed
    assert remote.state.advertising and not remote.state.ready

    await tv.connect()
    await eventually(lambda: h.bluez.advert is None, what="advertisement to stop once connected")
    assert remote.state.connected and not remote.state.ready
    assert await remote.ensure_ready(2.0)

    # Our own disconnect is not a drop.
    h.bluez.events.clear()
    await remote.disconnect()
    await asyncio.sleep(0.1)
    assert "advert_registered" not in h.bluez.event_names()

    # A waiting ensure_ready takes over from the background advertisement (fast interval).
    tv.auto_connect = True
    assert await remote.ensure_ready(2.0)
    tv.auto_connect = False
    await tv.drop()
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["MinInterval"] == 150)
    waiting = asyncio.ensure_future(remote.ensure_ready(0.2))
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["MinInterval"] == 30)
    assert await waiting is False
    assert h.bluez.advert is None  # timed out: off air, also in the background


async def test_idle_disconnect(h):
    remote, tv = await h.paired_remote(idle_disconnect_seconds=0.25, reconnect_on_drop=True)
    assert await remote.ensure_ready(2.0)
    await asyncio.sleep(0.15)
    await remote.send_key("HOME", 0.01)  # activity restarts the clock
    await asyncio.sleep(0.15)
    assert tv.connected and remote.state.ready
    await eventually(lambda: not tv.connected, timeout=1.0, what="idle disconnect")
    await eventually(lambda: not remote.state.connected)
    await asyncio.sleep(0.1)
    assert h.bluez.advert is None  # dropped on purpose: no background reconnect either
    assert await remote.ensure_ready(2.0)  # and the next use simply reconnects

    # Switching the option off at runtime keeps the link.
    remote.apply_config(quick_config(tv_address=TV, idle_disconnect_seconds=0))
    await asyncio.sleep(0.4)
    assert tv.connected and remote.state.ready


# --------------------------------------------------------------------------- adapters


@pytest.fixture
async def two_adapters(private_bus, tmp_path, monkeypatch):
    async with fake_bluez.harness(tmp_path, monkeypatch, adapters=("hci0", "hci1")) as harness:
        yield harness


async def test_adapter_selection(two_adapters):
    h = two_adapters
    first = await h.remote()
    assert first.state.adapter == "hci0"
    await first.stop()

    named = await h.remote(adapter="hci1")
    assert named.state.adapter == "hci1" and named.state.adapter_address == "11:22:33:44:55:01"
    assert list(h.bluez.adapters["/org/bluez/hci1"].apps) and not h.bluez.adapters["/org/bluez/hci0"].apps
    await named.stop()

    # "auto" follows the bond: the TV is bonded with hci1's address, not hci0's.
    h.bluez.add_device(TV, adapter="hci1", bonded=True)
    await asyncio.sleep(0.05)
    auto = await h.remote(tv_address=TV)
    assert auto.state.adapter == "hci1" and auto.state.paired
    await auto.stop()

    missing = await h.remote(adapter="hci7")
    assert not missing.state.available and not missing.state.registered
    with pytest.raises(TransportError) as err:
        await missing.ensure_ready(0.5)
    assert err.value.code == "no_adapter" and "hci7" in err.value.message
