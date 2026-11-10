"""Readiness, key delivery and link handling within one bluetoothd lifetime."""

import asyncio
import errno
import logging
import time

import fake_bluez
import pytest
from fake_bluez import eventually, quick_config

from tcl_me_remo import keys
from tcl_me_remo.bluez import advert as advert_module
from tcl_me_remo.bluez import remote as remote_module
from tcl_me_remo.interfaces import TransportError

TV = "AA:BB:CC:DD:EE:FF"
KBD, CONSUMER, SYSTEM = 0x010A, 0x010E, 0x0112  # report value handles of the frozen table


@pytest.fixture
async def h(private_bus, tmp_path, monkeypatch):
    async with fake_bluez.harness(tmp_path, monkeypatch) as harness:
        yield harness

#: What a loaded machine may add to any step.  Upper bounds on elapsed time allow this much, and
#: each test that has one makes the wrong behaviour take clearly longer than that.
SLOW = 2.0


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
    assert 0.05 + 0.15 <= elapsed < 0.2 + SLOW  # connect delay + settle: the link alone is not readiness

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
        await eventually(lambda: len(tv.received) % 2 == 0 and tv.received)
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
    assert 0.3 <= time.monotonic() - started < 0.3 + SLOW
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


async def test_power_off_takes_the_advertisement_down_for_good(h):
    """Otherwise the kernel would put it back on air when the adapter is switched on again."""
    remote, tv = await parted(h)
    tv.auto_connect = False
    waiting = asyncio.ensure_future(remote.ensure_ready(5.0))
    await eventually(lambda: h.bluez.advert is not None)
    h.bluez.set_powered(False)
    with pytest.raises(TransportError) as err:
        await waiting
    assert err.value.code == "bluetooth_off"
    await eventually(lambda: h.bluez.advert is None, what="advertisement to be unregistered")
    assert not remote.state.advertising


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
    await remote.stop()

    remote, tv = await parted(h)
    tv.auto_connect = False
    slow = asyncio.ensure_future(remote.ensure_ready(0.1 + SLOW + 1.0))
    await eventually(lambda: h.bluez.advert is not None)
    started = time.monotonic()
    assert await remote.ensure_ready(0.1) is False  # waits its own 0.1 s for the first caller, no longer
    assert time.monotonic() - started < 0.1 + SLOW and not slow.done()
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


# --------------------------------------------------------------------------- nothing is slept through


async def test_tv_connecting_while_the_advertisement_is_being_registered_is_noticed(h):
    """The change arrives while ensure_ready awaits bluetoothd, i.e. before it starts waiting."""
    remote, tv = await parted(h)
    tv.connect_delay = 0.0  # an awake TV answers the first advertising event
    started = time.monotonic()
    assert await remote.ensure_ready(10.0) is True
    assert time.monotonic() - started < 0.04 + SLOW  # connect + settle (0.04 s), not the whole budget
    assert remote.state.ready and h.bluez.advert is None


async def test_tv_connecting_while_the_wake_beacon_is_being_registered_is_noticed(h, monkeypatch):
    remote, tv = await parted(h, wake_beacon="mediatek")
    tv.connect_delay = 0.01
    real = fake_bluez.FakeBluez._register_advertisement

    async def register(self, adapter, msg):
        if msg.body[0].endswith("beacon"):
            await asyncio.sleep(0.05)  # bluetoothd answers once the kernel has the second instance
        return await real(self, adapter, msg)

    monkeypatch.setattr(fake_bluez.FakeBluez, "_register_advertisement", register)
    started = time.monotonic()
    assert await remote.ensure_ready(10.0, wake=True) is True
    assert time.monotonic() - started < 0.1 + SLOW
    await eventually(lambda: h.bluez.advert is None and h.bluez.beacon is None)


async def test_tv_connecting_during_the_pause_after_a_release_is_noticed(h, monkeypatch):
    """After Release() we stay off air for a moment; the TV may have answered the last event."""
    monkeypatch.setattr(remote_module, "ADVERT_RETRY", 8.0)
    remote, tv = await parted(h)
    tv.auto_connect = False
    task = asyncio.ensure_future(remote.ensure_ready(10.0))
    await eventually(lambda: h.bluez.advert is not None)
    await h.bluez.release_advertisement()
    await asyncio.sleep(0.02)
    started = time.monotonic()
    await tv.connect()
    assert await task is True
    assert time.monotonic() - started < SLOW  # not the rest of the pause, let alone of the budget


async def test_disconnect_calls_off_an_ensure_ready_that_is_talking_to_bluetoothd(h):
    remote, tv = await parted(h)
    tv.auto_connect = False
    h.bluez.advert_register_delay = 0.1
    started = time.monotonic()
    waiting = asyncio.ensure_future(remote.ensure_ready(10.0))
    await asyncio.sleep(0.03)  # RegisterAdvertisement is in flight
    await remote.disconnect()
    assert await waiting is False
    assert time.monotonic() - started < 0.1 + SLOW
    assert h.bluez.advert is None and not remote.state.advertising


# --------------------------------------------------------------------------- the caller's budget


async def test_a_slow_bluetoothd_does_not_stretch_the_budget(h):
    """Near suspend the service has about three seconds; a busy bluetoothd must not add its own."""
    remote, tv = await parted(h)
    tv.auto_connect = False
    h.bluez.advert_register_delay = 3.0
    started = time.monotonic()
    assert await remote.ensure_ready(0.3) is False
    assert 0.3 <= time.monotonic() - started < 0.3 + remote_module.CLEANUP_GRACE + SLOW
    assert not remote.state.advertising and "too slow" in remote.state.error
    # The request that was under way is taken back: nothing goes on air behind our back.
    await asyncio.sleep(max(0.0, 3.3 - (time.monotonic() - started)))  # bluetoothd has answered by now
    await eventually(lambda: h.bluez.advert is None and not remote._tasks and remote._ensure_active == 0)
    await asyncio.sleep(0.2)
    assert h.bluez.advert is None and not remote._tasks and remote._ensure_active == 0


async def test_the_pause_after_a_refused_advertisement_counts_against_the_budget(h, monkeypatch):
    monkeypatch.setattr(remote_module, "ADVERT_RETRY", 8.0)
    remote, tv = await parted(h)
    tv.auto_connect = False
    h.bluez.max_adverts = 0
    with pytest.raises(TransportError):
        await remote.ensure_ready(1.0)  # refused: a pause of eight seconds begins
    h.bluez.max_adverts = 5
    started = time.monotonic()
    assert await remote.ensure_ready(0.2) is False
    assert 0.2 <= time.monotonic() - started < 0.2 + SLOW
    assert "advert_registered" not in h.bluez.event_names()


async def test_switching_the_adapter_on_counts_against_the_budget(h):
    remote, tv = await parted(h, power_on_adapter=True)
    h.bluez.set_powered(False)
    await eventually(lambda: not remote.state.powered)
    h.bluez.power_on_delay = 4.0  # bluetoothd answers Powered=true only after seconds
    started = time.monotonic()
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(0.2)
    assert err.value.code == "bluetooth_off" and "could not be turned on" in err.value.message
    assert time.monotonic() - started < 0.2 + SLOW


async def test_too_little_time_to_be_answered_does_not_go_on_air(h):
    remote, tv = await parted(h)
    tv.auto_connect = False
    assert await remote.ensure_ready(remote_module.MIN_ADVERT_BUDGET / 2) is False
    assert h.bluez.events == []


async def test_stopping_the_advertisement_gives_up_waiting_but_not_the_request(h, monkeypatch):
    remote, tv = await parted(h)
    tv.auto_connect = False
    waiting = asyncio.ensure_future(remote.ensure_ready(0.15))
    await eventually(lambda: h.bluez.advert is not None)
    real = fake_bluez.FakeBluez._call

    async def slow(self, msg, after):
        if msg.member == "UnregisterAdvertisement":
            await asyncio.sleep(3.0)
        return await real(self, msg, after)

    monkeypatch.setattr(fake_bluez.FakeBluez, "_call", slow)
    started = time.monotonic()
    assert await waiting is False
    assert time.monotonic() - started < 0.15 + remote_module.CLEANUP_GRACE + SLOW
    assert not remote.state.advertising
    await eventually(lambda: h.bluez.advert is None, timeout=8.0, what="the unregistration to arrive")
    await eventually(lambda: not remote._tasks and remote._ensure_active == 0)


async def test_a_lock_in_hand_over_is_waited_for_against_the_deadline(h):
    """asyncio.Lock reads "free" between release() and the next waiter running."""
    remote, tv = await parted(h)
    tv.auto_connect = False
    result = {}

    async def third():
        started = time.monotonic()
        result["value"] = await remote.ensure_ready(0.1)
        result["elapsed"] = time.monotonic() - started

    real, calls = remote._run_ensure, []

    async def run_ensure(deadline, wake=False):
        calls.append(deadline)
        try:
            return await real(deadline, wake)
        finally:
            if len(calls) == 1:
                # Becomes runnable in the loop iteration in which A hands the lock to B.
                result["task"] = asyncio.ensure_future(third())

    remote._run_ensure = run_ensure
    first = asyncio.ensure_future(remote.ensure_ready(0.2))
    await asyncio.sleep(0.05)
    second = asyncio.ensure_future(remote.ensure_ready(1.0))  # queued on the lock
    assert await asyncio.gather(first, second) == [False, False]
    await result["task"]
    assert result["value"] is False and result["elapsed"] < 0.3  # its own 0.1 s, not B's whole second


async def test_cleanup_survives_a_second_cancellation(h):
    """gather() cancels a second time when its own awaiter is cancelled (the service's power race)."""
    remote, tv = await parted(h, wake_beacon="mediatek")
    tv.auto_connect = False
    task = asyncio.ensure_future(remote.ensure_ready(30, wake=True))
    await eventually(lambda: h.bluez.advert is not None and h.bluez.beacon is not None)
    task.cancel()
    await asyncio.sleep(0)  # the task is in its clean-up now
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await eventually(lambda: h.bluez.advert is None and h.bluez.beacon is None, what="both to leave the air")
    await eventually(lambda: not remote._tasks and remote._ensure_active == 0)
    assert not remote.state.advertising and not remote._op_lock.locked()
    tv.auto_connect = True
    assert await remote.ensure_ready(2.0)


async def test_background_policy_stays_out_while_ensure_ready_takes_over(h):
    """A BlueZ signal during the hand-over must not start a second caller with the slow advert."""
    remote, tv = await parted(h, reconnect_on_drop=True)
    tv.auto_connect = False
    assert not await remote.ensure_ready(0.1)  # un-parks: background calling starts
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["MinInterval"] == 150)
    h.bluez.events.clear()

    real = remote._cancel_task

    async def cancel_and_signal(name):
        # Any adapter or device property change (RSSI while Steam scans, a controller connecting).
        h.bluez.emit_properties(h.bluez.adapter.path, fake_bluez.ADAPTER, {"Alias": "renamed"})
        await asyncio.sleep(0.02)  # the signal is handled while the background task winds down
        await real(name)

    remote._cancel_task = cancel_and_signal
    waiting = asyncio.ensure_future(remote.ensure_ready(0.5))
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["MinInterval"] == 30)
    assert remote._reconnect_task is None or remote._reconnect_task.done()
    h.bluez.emit_properties(h.bluez.adapter.path, fake_bluez.ADAPTER, {"Alias": "again"})
    await asyncio.sleep(0.1)
    assert remote._reconnect_task is None or remote._reconnect_task.done()
    # One registration, the fast one, for as long as somebody waits.
    assert [props["MinInterval"] for name, props in h.bluez.events if name == "advert_registered"] == [30]
    assert await waiting is False
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["MinInterval"] == 150)


async def test_one_refused_interval_does_not_slow_advertising_down_for_good(h, monkeypatch, caplog):
    remote, tv = await parted(h)
    tv.auto_connect = False
    h.bluez.unsupported_advert_properties = {"MinInterval"}  # one refusal, whatever the reason
    assert not await remote.ensure_ready(0.1)
    h.bluez.unsupported_advert_properties = set()

    def intervals():
        return [props.get("MinInterval") for name, props in h.bluez.events if name == "advert_registered"]

    assert intervals() == [None] and "refused the advertising interval settings" in caplog.text
    # For a while the plain advertisement is used without asking twice every time...
    h.bluez.events.clear()
    assert not await remote.ensure_ready(0.1)
    assert intervals() == [None]
    # ...then the full one gets another chance.
    monkeypatch.setattr(advert_module, "BASIC_RETRY_SECONDS", 0.0)
    remote._advertiser._basic_until = 0.0
    h.bluez.events.clear()
    assert not await remote.ensure_ready(0.1)
    assert intervals() == [30]

    # An error that is not about the properties does not trigger the fallback at all.
    h.bluez.events.clear()
    h.bluez.max_adverts = 0
    with pytest.raises(TransportError):
        await remote.ensure_ready(0.5)
    h.bluez.max_adverts = 5
    assert remote._advertiser._basic_until == 0.0


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


async def test_key_is_released_before_a_link_is_closed_on_request(h):
    """bluetoothd keeps the link for two more seconds: a key left down would auto-repeat."""
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    h.bluez.disconnect_delay = 0.4
    before = len(tv.received)
    key = asyncio.ensure_future(remote.send_key("DOWN", 0.2))
    await eventually(lambda: len(tv.received) == before + 1, what="the key press")
    closing = asyncio.ensure_future(remote.disconnect())
    # The release goes out at once, while the link is still up, not after the hold time.
    await eventually(lambda: len(tv.received) == before + 2, timeout=0.1, what="the key release")
    assert tv.connected
    assert tv.received[before:] == [(CONSUMER, bytes.fromhex("43000000"), "bluez"), (CONSUMER, bytes(4), "bluez")]
    with pytest.raises(TransportError) as err:
        await key
    assert err.value.code == "not_connected" and "closed while DOWN was held" in err.value.message
    await closing
    assert len(tv.received) == before + 2  # released exactly once
    assert not remote._send_lock.locked() and remote._held is None


async def test_stop_then_start_takes_a_fresh_look_at_bluez(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    await remote.stop()
    assert not remote.state.available and not remote.state.connected and not remote.state.paired
    await tv.drop()  # nobody is watching
    await asyncio.sleep(0.05)
    await remote.start()
    state = remote.state
    assert state.available and state.registered and state.paired and not state.connected
    tv.auto_connect = True
    assert await remote.ensure_ready(5.0)
    assert remote.state.connected and tv.connected


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


# --------------------------------------------------------------------------- ending the link at once (before sleep)

#: Management "Disconnect" (0x0014) on hci0 for AA:BB:CC:DD:EE:FF, a public address.
DISCONNECT_TV = bytes.fromhex("1400 0000 0700 ffeeddccbbaa 01")


async def test_fast_disconnect_ends_the_link_through_the_kernel(h):
    """Before suspend there is no time for bluetoothd's two seconds, and the reason code matters."""
    h.bluez.disconnect_delay = 30.0  # bluetoothd's way would take far too long to go unnoticed
    remote, tv = await h.paired_remote(reconnect_on_drop=True)
    assert await remote.ensure_ready(2.0)
    h.bluez.events.clear()
    started = time.monotonic()
    await remote.disconnect(fast=True)
    assert time.monotonic() - started < remote_module.FAST_DISCONNECT_WAIT + SLOW
    assert h.mgmt.commands == [DISCONNECT_TV] and h.bluez.disconnect_calls == 0
    await eventually(lambda: not tv.connected and not remote.state.connected)
    state = remote.state
    assert not state.ready and state.delivery == "" and state.error == ""
    assert h.mgmt.opened == 1 and h.mgmt.closed == 1  # the control socket was given back

    # Parked exactly as after a normal disconnect: no calling until there is something to send.
    await asyncio.sleep(0.1)
    assert "advert_registered" not in h.bluez.event_names() and not remote.state.advertising
    await remote.disconnect(fast=True)  # no link: nothing to ask anybody for
    assert h.mgmt.commands == [DISCONNECT_TV] and h.bluez.disconnect_calls == 0
    assert await remote.ensure_ready(5.0) is True and tv.connections == 2


async def test_manual_disconnect_keeps_going_through_bluetoothd(h):
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    await remote.disconnect()
    assert h.mgmt.commands == [] and h.mgmt.opened == 0 and h.bluez.disconnect_calls == 1
    assert not tv.connected and not remote.state.connected


async def test_fast_disconnect_names_the_tv_by_the_kind_of_address_it_has(h):
    """The kernel looks a link up by address and address type; a static random identity is type 2."""
    h.bluez.disconnect_delay = 30.0
    remote, tv = await h.paired_remote(identity_type="random")
    assert await remote.ensure_ready(2.0)
    await remote.disconnect(fast=True)
    assert h.mgmt.commands == [bytes.fromhex("1400 0000 0700 ffeeddccbbaa 02")]
    await eventually(lambda: not tv.connected and not remote.state.connected)
    assert h.bluez.disconnect_calls == 0


async def test_fast_disconnect_follows_the_kernel_when_it_lists_the_link_under_a_private_address(h):
    h.bluez.disconnect_delay = 30.0
    remote, tv = await h.paired_remote(rpa="4A:11:22:33:44:55")
    assert await remote.ensure_ready(2.0)
    assert remote.state.tv_address == TV
    tv.kernel_keeps_rpa = True  # bluetoothd says AA:BB:... (public); the kernel never resolved the address
    await remote.disconnect(fast=True)
    assert h.mgmt.commands == [bytes.fromhex("1400 0000 0700 55443322114a 02")]
    await eventually(lambda: not tv.connected and not remote.state.connected)
    assert h.bluez.disconnect_calls == 0


async def test_fast_disconnect_lets_the_last_report_out_first(h, monkeypatch):
    """A link ended under a key-up that is still on its way would leave the TV awake."""
    monkeypatch.setattr(remote_module, "REPORT_FLUSH", 0.2)
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    before = len(tv.received)
    seen = []
    find = h.mgmt.find

    def watched(*args):
        seen.append((time.monotonic(), len(tv.received)))
        return find(*args)

    h.mgmt.find = watched
    key = asyncio.ensure_future(remote.send_key("SLEEP", 5.0))
    await eventually(lambda: len(tv.received) == before + 1, what="the key press")
    started = time.monotonic()
    await remote.disconnect(fast=True)
    # The key was released at once, and the kernel was asked only after the pause for it.
    assert tv.received[before:] == [(CONSUMER, bytes.fromhex("32000000"), "bluez"), (CONSUMER, bytes(4), "bluez")]
    assert len(seen) == 1 and seen[0][1] == before + 2 and seen[0][0] - started >= 0.2
    with pytest.raises(TransportError) as err:
        await key
    assert "closed while SLEEP was held" in err.value.message
    await eventually(lambda: not tv.connected and not remote.state.connected)


async def test_fast_disconnect_is_not_taken_back_when_the_caller_stops_waiting(h):
    """The service gives it half a second; whatever happens in that time, the kernel is asked."""
    h.bluez.disconnect_delay = 30.0
    h.mgmt.disconnect_delay = 0.3
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    await remote.send_key("SLEEP", 0.01)  # so that the pause for the last report is still running
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(remote.disconnect(fast=True), 0.01)
    assert h.mgmt.commands == [DISCONNECT_TV] and h.bluez.disconnect_calls == 0
    assert h.mgmt.opened == 1 and h.mgmt.closed == 1  # nothing is left open behind a caller that left
    # Not usable any more even though the link is still up for a moment...
    assert tv.connected and not remote.state.ready
    with pytest.raises(TransportError):
        await remote.send_key("HOME", 0.01)
    # ...and when it goes down that is not mistaken for the TV dropping us.
    await eventually(lambda: not tv.connected and not remote.state.connected)
    assert h.bluez.advert is None


async def test_fast_disconnect_does_not_wait_for_a_tv_that_stopped_answering(h):
    """The controller reports the link down only when the TV acknowledged or timed out."""
    h.bluez.disconnect_delay = 30.0
    h.mgmt.completes = False
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    started = time.monotonic()
    await remote.disconnect(fast=True)
    elapsed = time.monotonic() - started
    assert remote_module.FAST_DISCONNECT_WAIT <= elapsed < remote_module.FAST_DISCONNECT_WAIT + SLOW
    # The kernel has the request: that is all there is to do, bluetoothd is not asked as well.
    assert h.mgmt.commands == [DISCONNECT_TV] and h.bluez.disconnect_calls == 0 and h.mgmt.closed == 1
    assert tv.connected and remote.state.connected and not remote.state.ready
    await tv.drop()
    await eventually(lambda: not remote.state.connected)


@pytest.mark.parametrize(
    "fault, why",
    [
        ("no sockets", "no Bluetooth socket support"),
        ("not root", "must run as root"),
        ("busy", "another disconnect is under way"),
        ("unknown link", "no such link"),
        ("no address type", "what kind of address"),
        ("two candidates", "none is listed as the TV"),
    ],
)
async def test_fast_disconnect_falls_back_to_bluetoothd(h, fault, why, caplog):
    caplog.set_level(logging.INFO)
    remote, tv = await h.paired_remote(rpa="4A:11:22:33:44:55" if fault == "two candidates" else "")
    assert await remote.ensure_ready(2.0)
    if fault == "no sockets":
        h.mgmt.open_error = OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
    elif fault == "not root":
        h.mgmt.refuse_status = 0x14
    elif fault == "busy":
        h.mgmt.refuse_status = 0x0A
    elif fault == "unknown link":
        tv.identity_type = "random"  # the kernel tracks the link by another address type than BlueZ reports
    elif fault == "no address type":
        tv._set(AddressType="")
        await asyncio.sleep(0.05)
    else:
        # Somebody else's device is connected to us as well and the kernel lists neither link
        # under the TV's address: nothing says which one is the TV, so nothing is guessed.
        tv.kernel_keeps_rpa = True
        other = h.bluez.add_device("5B:00:00:00:00:01", name="Phone")
        await other.connect()
    await remote.disconnect(fast=True)
    assert h.bluez.disconnect_calls == 1 and not tv.connected and not remote.state.connected
    assert why in caplog.text and "asking BlueZ, which takes two seconds" in caplog.text
    assert h.mgmt.opened == h.mgmt.closed
    if fault == "two candidates":
        assert h.mgmt.commands == [] and other.connected


async def test_fast_disconnect_falls_back_when_the_kernel_refuses_a_moment_later(h, caplog):
    """Kernels from 6.11 on accept the command and look for the link afterwards."""
    caplog.set_level(logging.INFO)
    remote, tv = await h.paired_remote()
    assert await remote.ensure_ready(2.0)
    h.mgmt.refuse_status, h.mgmt.refuse_delay = 0x0E, 0.05  # mgmt_errno_status(-ENOTCONN)
    await remote.disconnect(fast=True)
    assert h.mgmt.commands == [DISCONNECT_TV] and h.bluez.disconnect_calls == 1
    assert not tv.connected and not remote.state.connected and h.mgmt.opened == h.mgmt.closed == 1
    assert "the kernel did not end the TV link (status 0x0E)" in caplog.text


async def test_fast_disconnect_calls_off_a_connection_attempt_like_the_normal_one(h):
    remote, tv = await parted(h)
    tv.auto_connect = False
    waiting = asyncio.ensure_future(remote.ensure_ready(30))
    await eventually(lambda: h.bluez.advert is not None)
    await remote.disconnect(fast=True)
    assert await asyncio.wait_for(waiting, SLOW) is False
    assert h.bluez.advert is None and h.mgmt.commands == [] and h.mgmt.opened == 0


async def test_fast_disconnect_with_direct_delivery_settles_the_kernel_afterwards(h):
    """The link that carried injected packets ends the fast way too; the repair follows as usual."""
    remote, tv = await h.paired_remote(raw_notify="always")
    assert await remote.ensure_ready(2.0) and remote.state.delivery == "raw"
    await remote.send_key("SLEEP", 0.01)
    await remote.disconnect(fast=True)
    await eventually(lambda: not tv.connected and not remote.state.connected)
    await eventually(lambda: h.raw.commands != [], what="the repair after the link closed")
    assert h.mgmt.commands == [DISCONNECT_TV] and h.bluez.disconnect_calls == 0


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


async def test_reconnect_on_drop_keeps_calling_the_tv_in_the_background(h):
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
    assert await remote.ensure_ready(2.0)  # the link is already there: only the settle time is left

    # A link we close ourselves stays closed: no calling until there is something to send.
    h.bluez.events.clear()
    await remote.disconnect()
    await asyncio.sleep(0.1)
    assert "advert_registered" not in h.bluez.event_names() and not remote.state.advertising

    # A waiting ensure_ready calls with the fast interval...
    waiting = asyncio.ensure_future(remote.ensure_ready(0.2))
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["MinInterval"] == 30)
    assert await waiting is False
    # ...and when it gives up the background calling carries on.
    await eventually(lambda: h.bluez.advert is not None and h.bluez.advert["MinInterval"] == 150)
    await tv.connect()
    await eventually(lambda: h.bluez.advert is None and remote.state.connected)

    # Switching the option off at runtime stops the calling.
    await tv.drop()
    await eventually(lambda: h.bluez.advert is not None)
    remote.apply_config(quick_config(tv_address=TV, reconnect_on_drop=False))
    await eventually(lambda: h.bluez.advert is None, what="background calling to stop")


async def test_resume_link_lets_background_calling_start_again(h):
    """After a wake in which nothing is sent to the TV the link would otherwise stay parked."""
    remote, tv = await h.paired_remote(reconnect_on_drop=True)
    assert await remote.ensure_ready(2.0)
    await remote.disconnect(fast=True)  # what the service does before the machine sleeps
    await eventually(lambda: not tv.connected and not remote.state.connected)
    h.bluez.events.clear()
    await asyncio.sleep(0.1)
    assert "advert_registered" not in h.bluez.event_names()  # parked

    remote.resume_link()  # synchronous: the service calls it on its way into every boot and resume
    await eventually(lambda: "advert_registered" in h.bluez.event_names(), what="background calling after the wake")
    registered = [props for name, props in h.bluez.events if name == "advert_registered"]
    assert (registered[0]["MinInterval"], registered[0]["MaxInterval"]) == (150, 250)  # the relaxed one
    await eventually(lambda: remote.state.connected and h.bluez.advert is None, what="the TV to call back")
    assert tv.connections == 2 and not remote.state.ready
    remote.resume_link()  # nothing parked: nothing happens
    await asyncio.sleep(0.05)
    assert tv.connections == 2 and h.bluez.advert is None
    assert await remote.ensure_ready(3.0) is True  # the next suspend finds the link up


async def test_resume_link_does_nothing_without_reconnect_on_drop(h):
    remote, tv = await parted(h)  # reconnect_on_drop is off
    remote.resume_link()
    await asyncio.sleep(0.1)
    assert h.bluez.event_names() == [] and not remote.state.advertising and not tv.connected
    # Switching the option on later picks the link up: the wake has been noted.
    remote.apply_config(quick_config(tv_address=TV, reconnect_on_drop=True))
    await eventually(lambda: remote.state.connected, what="background calling once it is wanted")


async def test_resume_link_leaves_a_link_alone_that_is_meant_to_be_down_when_idle(h):
    """With an idle time-out the user wants the radio free; only a key starts that time-out."""
    remote, tv = await h.paired_remote(reconnect_on_drop=True, idle_disconnect_seconds=30)
    assert await remote.ensure_ready(2.0)
    await remote.disconnect(fast=True)
    await eventually(lambda: not remote.state.connected)
    h.bluez.events.clear()
    remote.resume_link()
    await asyncio.sleep(0.1)
    assert h.bluez.event_names() == [] and not tv.connected
    assert await remote.ensure_ready(3.0) is True  # there is something to send: now it is called


async def test_resume_link_never_raises(h, monkeypatch, caplog):
    remote = await h.remote(reconnect_on_drop=True, start=False)
    remote.resume_link()  # not started
    await remote.start()
    remote.resume_link()  # no TV bonded
    await asyncio.sleep(0.05)
    assert h.bluez.advert is None

    tv = h.bluez.add_device()
    tv.auto_connect = True
    await h.pair(remote, tv)
    await remote.disconnect()
    with monkeypatch.context() as patch:
        patch.setattr(remote, "_call_in_background", lambda: 1 / 0)
        remote.resume_link()
    assert "could not resume background calling" in caplog.text
    await remote.stop()
    remote.resume_link()  # stopped


async def test_reconnect_on_drop_starts_calling_a_bonded_tv_at_start_up(h):
    tv = h.bluez.add_device(TV, bonded=True)
    remote = await h.remote(tv_address=TV, reconnect_on_drop=True)
    await eventually(lambda: h.bluez.advert is not None, what="background advertisement")
    # The HID service is registered first, always.
    assert h.bluez.event_names()[:2] == ["gatt_registered", "advert_registered"]
    assert h.bluez.advert["Discoverable"] is False
    await tv.connect()
    await eventually(lambda: h.bluez.advert is None and remote.state.connected)

    # No bonded TV, nothing to call.
    await remote.forget()
    await asyncio.sleep(0.1)
    assert h.bluez.advert is None


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


@pytest.fixture
async def no_adapters(private_bus, tmp_path, monkeypatch):
    async with fake_bluez.harness(tmp_path, monkeypatch, adapters=()) as harness:
        yield harness


async def test_no_adapter_at_all_then_one_is_plugged_in(no_adapters):
    h = no_adapters
    remote = await h.remote(tv_address=TV)
    state = remote.state
    assert not state.available and state.adapter == "" and not state.powered and not state.registered
    started = time.monotonic()
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(5.0)
    assert err.value.code == "no_adapter" and err.value.message == "no Bluetooth adapter was found"
    assert time.monotonic() - started < 5.0 - SLOW  # not the whole budget
    with pytest.raises(TransportError) as err:
        await remote.start_pairing(30)
    assert err.value.code == "no_adapter"
    await remote.disconnect()  # nothing to do, nothing raised
    await remote.forget()      # cannot ask BlueZ: remembered for later, local state cleared
    assert remote.state.tv_address == ""
