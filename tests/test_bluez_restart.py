"""What happens around bluetoothd restarts: lost subscriptions, direct delivery, recovery."""

import asyncio
import dataclasses
import errno
import json
import logging
import time

import fake_bluez
import pytest
from fake_bluez import eventually, quick_config

from tcl_me_remo import config as config_mod
from tcl_me_remo.bluez import rawhci
from tcl_me_remo.interfaces import TransportError
from tcl_me_remo.service import Service

TV = "AA:BB:CC:DD:EE:FF"
KBD, CONSUMER, SYSTEM = 0x010A, 0x010E, 0x0112
HOME = bytes.fromhex("23020000")


@pytest.fixture
async def h(private_bus, tmp_path, monkeypatch):
    async with fake_bluez.harness(tmp_path, monkeypatch) as harness:
        yield harness

#: What a loaded machine may add to any step.  Upper bounds on elapsed time allow this much, and
#: each test that has one makes the wrong behaviour take clearly longer than that.
SLOW = 2.0


async def rebooted(h, *, android=12, **overrides):
    """A TV paired in an earlier bluetoothd life; bluetoothd has restarted since (a reboot)."""
    remote, tv = await h.paired_remote(android=android, **overrides)
    await h.bluez.restart()
    await eventually(lambda: remote.state.registered and remote.state.paired, what="re-registration")
    assert not remote.state.subscribed and not remote.state.connected and not tv.connected
    h.bluez.events.clear()
    return remote, tv


def hint(h):
    return json.loads(h.state_path.read_text())["resubscribes"]


def debt(h):
    """The boot in which raw delivery left the kernel's counters in need of a repair ("" if none)."""
    return json.loads(h.state_path.read_text())["raw_debt"]


# --------------------------------------------------------------------------- the restart itself


async def test_restart_drops_everything_and_registers_again(h, caplog):
    caplog.set_level(logging.INFO)
    remote, tv = await h.paired_remote(reconnect_on_drop=True)
    assert await remote.ensure_ready(2.0)
    tv.auto_connect = False
    old_path = tv.path

    await h.bluez.stop()
    await eventually(lambda: not remote.state.available, what="bluetoothd to be noticed gone")
    state = remote.state
    assert not state.registered and not state.connected and not state.ready and not state.subscribed
    assert not state.advertising and not state.paired and state.delivery == ""
    assert state.tv_address == TV  # what we remember is not what BlueZ currently tells us
    with pytest.raises(TransportError) as err:
        await remote.send_key("HOME", 0.01)
    assert err.value.code == "not_connected"

    h.bluez.events.clear()
    await h.bluez.start()
    await eventually(lambda: remote.state.registered and remote.state.paired, what="re-registration")
    assert tv.path == old_path  # (public address: same path)
    # GATT first; only then may anything go on air (here: the background call for the TV).
    await eventually(lambda: h.bluez.advert is not None)
    assert h.bluez.event_names()[:2] == ["gatt_registered", "advert_registered"]
    assert h.bluez.app.table()[0][0] == 0x0100 and remote.state.error == ""
    assert remote._app.report_value_handle(2) == (CONSUMER, "bluez")


async def test_ensure_ready_while_bluetoothd_is_down(h, monkeypatch):
    remote, tv = await h.paired_remote()
    await h.bluez.stop()
    await eventually(lambda: not remote.state.available)
    started = time.monotonic()
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(5.0)
    assert err.value.code == "no_adapter" and "bluetoothd" in err.value.message
    assert 0.2 <= time.monotonic() - started < 0.2 + SLOW  # waited the grace period, not the whole budget

    # If it comes back within the grace period the caller never notices.
    monkeypatch.setattr(fake_bluez.remote_module, "ADAPTER_GRACE", 3.0)
    remote._changed_at = time.monotonic()
    waiting = asyncio.ensure_future(remote.ensure_ready(5.0))
    await asyncio.sleep(0.1)
    await h.bluez.start()
    assert await waiting is True


async def test_bluetoothd_absent_at_start_then_appearing(h):
    await h.bluez.stop()
    remote = await h.remote()  # must not raise or hang
    state = remote.state
    assert not state.available and not state.registered and state.adapter == ""
    await h.bluez.start()
    await eventually(lambda: remote.state.registered, what="registration once bluetoothd is there")
    assert h.bluez.app.table()[0][0] == 0x0100


async def test_adapter_removed_and_added_back(h):
    remote, tv = await h.paired_remote(android=14)
    assert await remote.ensure_ready(2.0)
    await h.bluez.remove_adapter()
    await eventually(lambda: not remote.state.available, what="adapter loss")
    state = remote.state
    assert not state.registered and not state.connected and not state.ready and not state.subscribed
    assert state.error == "the Bluetooth adapter is gone"
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(0.5)
    assert err.value.code == "no_adapter"

    h.bluez.events.clear()
    await h.bluez.add_adapter()
    await eventually(lambda: remote.state.registered and remote.state.paired, what="re-registration")
    assert h.bluez.event_names() == ["gatt_registered"] and remote.state.error == ""
    # The adapter's GATT database was destroyed, so the subscription is gone like after a restart.
    assert await remote.ensure_ready(5.0) is True
    assert remote.state.delivery == "bluez" and remote.state.subscribed  # Android 14 subscribed again


# --------------------------------------------------------------------------- (a) the TV re-subscribes


async def test_android_14_resubscribes_and_bluez_delivers(h):
    remote, tv = await rebooted(h, android=14, resubscribe_wait=6.0)
    tv.resubscribe_delay = 0.1
    started = time.monotonic()
    assert await remote.ensure_ready(10.0) is True
    assert time.monotonic() - started < 0.1 + SLOW  # did not sit out the whole resubscribe_wait
    assert remote.state.delivery == "bluez" and remote.state.subscribed
    assert remote.bounces == 0 and tv.connections == 2  # pairing link + this one

    await remote.send_key("HOME", 0.01)
    await eventually(lambda: len(tv.received) == 2)
    assert tv.keys("bluez") == [(CONSUMER, HOME), (CONSUMER, bytes(4))]
    assert h.raw.packets == [] and h.raw.opened == 0
    assert hint(h) is True


async def test_known_resubscriber_is_given_its_full_time(h):
    """A TV that re-subscribed before is never put on direct delivery early."""
    remote, tv = await rebooted(h, android=14, resubscribe_wait=0.6)
    assert await remote.ensure_ready(5.0) and hint(h) is True
    await h.bluez.restart()
    await eventually(lambda: remote.state.registered and remote.state.paired)
    tv.resubscribe_delay = 0.4  # slower than the short wait, faster than the configured one
    assert await remote.ensure_ready(5.0) is True
    assert remote.state.delivery == "bluez" and remote.bounces == 0


# --------------------------------------------------------------------------- (b) it never does


async def test_android_12_gets_one_bounce_then_direct_delivery(h, caplog):
    caplog.set_level(logging.INFO)
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) is True
    state = remote.state
    assert state.ready and state.delivery == "raw" and not state.subscribed and state.error == ""
    assert remote.bounces == 1
    assert tv.connections == 3  # pairing link, first link after the restart (bounced), this one
    assert "reconnecting it once before using direct delivery" in caplog.text
    assert tv.listening and tv.ignored == []  # nothing was sent on the link that would have ignored it

    await remote.send_key("HOME", 0.01)
    handle = tv.conn_handle
    assert h.raw.packets == [
        rawhci.att_notification_packet(handle, CONSUMER, HOME),
        rawhci.att_notification_packet(handle, CONSUMER, bytes(4)),
    ]
    assert h.raw.packets[0] == bytes([0x02, handle, 0x00, 0x0B, 0x00, 0x07, 0x00, 0x04, 0x00, 0x1B, 0x0E, 0x01]) + HOME
    assert tv.keys("raw") == [(CONSUMER, HOME), (CONSUMER, bytes(4))]
    # One path only: nothing was (pointlessly) offered to bluetoothd as well.
    assert tv.keys("bluez") == [] and h.bluez.dropped_notifications == 0
    assert hint(h) is False

    # Keys of the other pages use their own handles.
    await remote.send_key("WAKE", 0.01)
    await remote.send_key("ENTER", 0.01)
    assert tv.keys("raw")[2:] == [(SYSTEM, b"\x04"), (SYSTEM, b"\x00"), (KBD, b"\x28\x00"), (KBD, b"\x00\x00")]

    # The link closes: the kernel's packet accounting is repaired once.  This controller has LE
    # buffers of its own (the fake answers "8"), so the LE count is all there is to rebuild.
    assert h.raw.commands == [] and debt(h) == rawhci.boot_id()  # on record while the link is up
    await tv.drop()
    await eventually(lambda: not remote.state.connected)
    await eventually(lambda: debt(h) == "", what="the repair to be settled")
    assert h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND]
    assert h.raw.filters == [rawhci.command_complete_filter(rawhci.OPCODE_LE_READ_BUFFER_SIZE)]

    # Next use in the same bluetoothd lifetime: no second bounce, and hardly any waiting.
    # "Hardly any waiting" is checked on the wait the transport will use, not with a stopwatch
    # around the whole reconnect: the full resubscribe_wait here is 0.3 s, and a busy machine
    # needs longer than that just to get the fake TV connected again.
    assert remote._resubscribe_wait() < 0.3  # the remembered hint shortens it
    started = time.monotonic()
    assert await remote.ensure_ready(5.0) is True
    assert time.monotonic() - started < 0.5 + SLOW  # nowhere near the 5 s budget
    assert remote.bounces == 1 and remote.state.delivery == "raw" and tv.connections == 4
    await remote.send_key("HOME", 0.01)
    assert len(tv.keys("raw")) == 8 and tv.ignored == []


async def test_late_subscription_switches_back_to_bluez_without_duplicates(h):
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"
    await remote.send_key("HOME", 0.01)
    assert len(h.raw.packets) == 2

    await tv.subscribe()  # e.g. the TV got its Android 14 update, or was paired again
    await eventually(lambda: remote.state.delivery == "bluez")
    assert remote.state.ready and remote.state.subscribed
    await remote.send_key("OK", 0.01)
    await eventually(lambda: len(tv.received) == 4)
    assert tv.received[2:] == [(CONSUMER, bytes.fromhex("41000000"), "bluez"), (CONSUMER, bytes(4), "bluez")]
    assert len(h.raw.packets) == 2  # nothing more went out directly: each key arrives exactly once
    assert hint(h) is True

    # The injected packets are still settled when the link closes.
    await tv.drop()
    await eventually(lambda: h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND])


async def test_subscription_arriving_during_the_bounce_wins(h):
    remote, tv = await rebooted(h, android=14, resubscribe_wait=0.1)
    tv.resubscribe_delay = 10  # so slow that we give up on it and bounce
    task = asyncio.ensure_future(remote.ensure_ready(5.0))
    await eventually(lambda: remote.bounces == 1)
    await eventually(lambda: tv.connected and tv.connections == 3)
    await tv.subscribe()  # Android 14 rediscovers on the new connection
    assert await task is True
    assert remote.state.delivery == "bluez" and h.raw.packets == []


async def test_service_restart_with_the_tv_still_connected(h):
    """Our process restarting loses the subscription just like a bluetoothd restart."""
    remote, tv = await h.paired_remote(android=12)
    assert await remote.ensure_ready(2.0)
    await remote.stop()
    assert tv.connected and h.bluez.app is None

    again = await h.remote(tv_address=TV)
    assert again.state.registered and again.state.connected and not again.state.subscribed
    assert h.bluez.app.table()[0][0] == 0x0100  # same handles as before
    assert await again.ensure_ready(5.0) is True
    assert again.state.delivery == "raw" and again.bounces == 1
    await again.send_key("HOME", 0.01)
    assert tv.keys("raw") == [(CONSUMER, HOME), (CONSUMER, bytes(4))] and tv.ignored == []


async def test_background_policy_prepares_direct_delivery_ahead_of_time(h):
    """With reconnect_on_drop the bounce does not wait for the first key (there may be no time then)."""
    remote, tv = await rebooted(h, android=12, reconnect_on_drop=True)
    # Nobody calls ensure_ready.  The TV is called, connects, stays silent, is bounced, comes back.
    await eventually(lambda: remote.bounces == 1 and tv.connections == 3 and remote.state.connected, what="bounce")
    await eventually(lambda: h.bluez.advert is None)
    assert not remote.state.ready and hint(h) is False
    await asyncio.sleep(0.15)
    assert remote.bounces == 1 and tv.connections == 3  # once is enough

    # The few seconds before suspend are now plenty: nothing is left to do but settle.
    started = time.monotonic()
    assert await remote.ensure_ready(3.0) is True
    assert time.monotonic() - started < SLOW and remote.state.delivery == "raw"
    assert remote.bounces == 1 and tv.connections == 3  # no new connection was needed
    await remote.send_key("SLEEP", 0.01)
    assert tv.keys("raw") == [(CONSUMER, bytes.fromhex("32000000")), (CONSUMER, bytes(4))] and tv.ignored == []


async def test_background_policy_leaves_a_subscribing_tv_alone(h):
    remote, tv = await rebooted(h, android=14, reconnect_on_drop=True)
    await eventually(lambda: remote.state.connected and remote.state.subscribed)
    await asyncio.sleep(0.5)  # longer than resubscribe_wait
    assert remote.bounces == 0 and tv.connections == 2
    assert await remote.ensure_ready(3.0) is True and remote.state.delivery == "bluez"


async def test_budget_shorter_than_the_waits_returns_false_without_bouncing(h):
    # A wait far longer than the budget, and a budget far longer than connecting takes, so that
    # neither a slow machine nor a fast one changes which of the two runs out.
    remote, tv = await rebooted(h, android=12, resubscribe_wait=4.0)
    assert await remote.ensure_ready(1.0) is False
    assert remote.bounces == 0 and tv.connected and not remote.state.ready
    assert remote.state.error == "the TV connected but was not ready for keys in time"
    assert h.bluez.advert is None
    # The next call picks up where this one left off instead of starting the wait again.
    assert await remote.ensure_ready(5.0) is True and remote.state.delivery == "raw"


async def test_partial_resubscription_keeps_each_report_on_the_path_that_reaches_the_tv(h):
    """The TV writes its three subscriptions one after another; keys may be due in between."""
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"

    await tv.subscribe(only="c4_kbd")  # the first of three writes has arrived
    await eventually(lambda: remote._app.reports[1].notifying)
    assert remote.state.ready and remote.state.delivery == "raw"  # direct delivery is still needed
    await remote.send_key("HOME", 0.01)   # consumer page: bluetoothd has no subscription for it yet
    await remote.send_key("ENTER", 0.01)  # keyboard page: bluetoothd forwards it now
    await eventually(lambda: len(tv.received) == 4)
    assert tv.received == [
        (CONSUMER, HOME, "raw"),
        (CONSUMER, bytes(4), "raw"),
        (KBD, b"\x28\x00", "bluez"),
        (KBD, b"\x00\x00", "bluez"),
    ]
    assert h.bluez.dropped_notifications == 0 and tv.ignored == []  # nothing lost, nothing twice

    await tv.subscribe()  # the other two
    await eventually(lambda: remote.state.delivery == "bluez")
    await remote.send_key("HOME", 0.01)
    await eventually(lambda: len(tv.received) == 6)
    assert tv.received[4:] == [(CONSUMER, HOME, "bluez"), (CONSUMER, bytes(4), "bluez")]
    assert len(h.raw.packets) == 2


async def test_losing_the_subscription_while_ready_is_not_papered_over(h):
    """Without it bluetoothd drops every report; "ready" would send keys into the void."""
    remote, tv = await h.paired_remote(android=12)
    assert await remote.ensure_ready(2.0) and remote.state.delivery == "bluez"
    await tv.unsubscribe()  # the TV writes its subscriptions to "off"; the link stays up
    await eventually(lambda: not remote.state.subscribed)
    state = remote.state
    assert state.connected and not state.ready and state.delivery == ""
    with pytest.raises(TransportError) as err:
        await remote.send_key("HOME", 0.01)
    assert err.value.code == "not_connected" and h.bluez.dropped_notifications == 0

    # The next ensure_ready looks again and takes the path that still works.
    tv.listening = True
    assert await remote.ensure_ready(2.0) is True and remote.state.delivery == "raw"
    await remote.send_key("HOME", 0.01)
    assert tv.keys("raw") == [(CONSUMER, HOME), (CONSUMER, bytes(4))] and h.bluez.dropped_notifications == 0


async def test_losing_the_subscription_with_direct_delivery_off_is_an_honest_error(h):
    remote, tv = await h.paired_remote(android=12, raw_notify="off")
    assert await remote.ensure_ready(2.0)
    await tv.unsubscribe()
    await eventually(lambda: not remote.state.ready)
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(2.0)
    assert err.value.code == "not_subscribed" and h.raw.packets == []


# --------------------------------------------------------------------------- settling the kernel's accounts


async def test_shared_buffer_controller_gets_its_classic_credit_count_rebuilt(h):
    """A controller without LE buffers of its own: the lost credits are in the shared pool."""
    h.raw.le_buffers = 0
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"
    await remote.send_key("HOME", 0.01)
    assert h.raw.commands == []  # never while the link is up
    await tv.drop()
    await eventually(lambda: debt(h) == "", what="the repair to be settled")
    assert h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND, rawhci.READ_BUFFER_SIZE_COMMAND]
    # Settled means settled: the next disconnect of an unused link sends nothing.
    assert await remote.ensure_ready(5.0)
    await tv.drop()
    await eventually(lambda: not remote.state.connected and debt(h) == "")
    await asyncio.sleep(0.1)
    assert len(h.raw.commands) == 2


async def test_debt_left_by_a_stopped_service_is_settled_by_the_next_one(h):
    """stop() cannot repair (the link is still up); the state file carries the debt over."""
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"
    await remote.send_key("HOME", 0.01)
    await remote.stop()  # a service restart or plugin update
    assert tv.connected and h.raw.commands == [] and debt(h) == rawhci.boot_id()

    # (1) The link is still up when the service is back: the repair waits for it to close.
    again = await h.remote(tv_address=TV)
    assert again.state.connected
    await asyncio.sleep(0.15)
    assert h.raw.commands == [] and debt(h) == rawhci.boot_id()
    await tv.drop()
    await eventually(lambda: debt(h) == "", what="the inherited repair")
    assert h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND]
    await again.stop()

    # (2) The link went down while nobody was running: repaired right at the next start.
    data = json.loads(h.state_path.read_text())
    h.state_path.write_text(json.dumps({**data, "raw_debt": rawhci.boot_id()}))
    h.raw.commands.clear()
    later = await h.remote(tv_address=TV)
    await eventually(lambda: debt(h) == "", what="the repair at start-up")
    assert h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND]
    await later.stop()

    # (3) A debt from before a reboot belongs to a kernel that no longer exists.
    h.state_path.write_text(json.dumps({**data, "raw_debt": "00000000-0000-0000-0000-000000000000"}))
    h.raw.commands.clear()
    await h.remote(tv_address=TV)
    await asyncio.sleep(0.15)
    assert h.raw.commands == [] and debt(h) == ""


async def test_tv_coming_back_during_the_repair_keeps_the_new_link_on_record(h, monkeypatch):
    monkeypatch.setattr(rawhci, "REPLY_WAIT", 0.6)
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"
    await remote.send_key("HOME", 0.01)
    h.raw.le_buffers = None  # the controller's answer is outstanding: the repair takes its full wait
    await tv.drop()
    await eventually(lambda: not remote.state.connected)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"  # back before it is done
    assert not remote._settle_task.done()
    await asyncio.wait_for(remote._settle_task, 2.0)
    assert h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND]
    assert debt(h) == rawhci.boot_id()  # direct delivery is in use again: still on record
    await remote.send_key("HOME", 0.01)
    await tv.drop()
    await eventually(lambda: debt(h) == "", what="the second repair")
    assert h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND] * 2


async def test_bluetoothd_restart_does_not_write_the_debt_off(h):
    """The kernel's link and its counters outlive bluetoothd; only a repair clears the debt."""
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"
    await remote.send_key("HOME", 0.01)
    await h.bluez.restart()
    await eventually(lambda: remote.state.registered and remote.state.paired and not remote.state.connected)
    await eventually(lambda: debt(h) == "", what="the repair once the TV is known to be gone")
    assert h.raw.commands == [rawhci.LE_READ_BUFFER_SIZE_COMMAND]


# --------------------------------------------------------------------------- (c) direct delivery off / failing


async def test_raw_notify_off_reports_not_subscribed(h):
    remote, tv = await rebooted(h, android=12, raw_notify="off")
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(5.0)
    assert err.value.code == "not_subscribed"
    assert "will not receive keys until it is paired again" in err.value.message
    assert "Wi-Fi assist" in err.value.message
    state = remote.state
    assert state.connected and not state.ready and state.error == err.value.message
    assert remote.bounces == 0 and h.raw.opened == 0 and h.bluez.advert is None
    assert hint(h) is False
    with pytest.raises(TransportError):
        await remote.send_key("HOME", 0.01)

    # Android 14 is unaffected by the setting.
    await tv.subscribe()
    assert await remote.ensure_ready(5.0) is True and remote.state.delivery == "bluez"


# --------------------------------------------------------------------------- (c2) what "auto" makes of the kernel


@pytest.mark.parametrize(
    "kernel, named",
    [
        (fake_bluez.OLD_KERNEL, "Linux 6.11.11-valve19-1-neptune-611"),
        ("6.16.12-200.fc42.x86_64", "Linux 6.16.12-200.fc42.x86_64"),
        ("garbage", "its version, 'garbage', could not be read"),  # unknown counts as old
    ],
)
async def test_auto_does_not_use_direct_delivery_on_a_kernel_it_would_disturb(h, kernel, named):
    remote, tv = await rebooted(h, android=12, raw_notify="auto", kernel=kernel)
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(5.0)
    message = err.value.message
    assert err.value.code == "not_subscribed"
    # The user is told exactly what is going on and what the two ways out are.
    assert "not used automatically on this kernel" in message and named in message and "6.17" in message
    assert "(bluetooth.raw_notify) to 'on'" in message and "pair the TV again" in message
    state = remote.state
    assert state.connected and not state.ready and state.error == message
    # Like "off": the link is left alone (a second link would be of no use), nothing is opened.
    assert remote.bounces == 0 and tv.connections == 2 and h.raw.opened == 0 and h.raw.packets == []
    assert h.bluez.advert is None
    # What was learnt about the TV is kept: it is a fact about the TV, not about the kernel.
    assert hint(h) is False
    with pytest.raises(TransportError):
        await remote.send_key("HOME", 0.01)

    # "on" is the user's word that they want it anyway; it takes effect without a restart.
    remote.apply_config(quick_config(tv_address=TV, raw_notify="on"))
    assert await remote.ensure_ready(5.0) is True
    assert remote.state.delivery == "raw" and remote.state.error == "" and remote.bounces == 1
    await remote.send_key("HOME", 0.01)
    assert tv.keys("raw") == [(CONSUMER, HOME), (CONSUMER, bytes(4))]
    assert debt(h) == rawhci.boot_id()  # this kernel needs the repair

    # ... and back: "auto" withdraws it at once, in the middle of a ready link.
    remote.apply_config(quick_config(tv_address=TV, raw_notify="auto"))
    assert not remote.state.ready and remote.state.delivery == ""
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(5.0)
    assert err.value.code == "not_subscribed" and remote.bounces == 1


@pytest.mark.parametrize("kernel", [fake_bluez.NEW_KERNEL, "6.17.0", "7.2.7-valve3-1-neptune"])
async def test_auto_uses_direct_delivery_where_it_leaves_no_trace(h, kernel):
    remote, tv = await rebooted(h, android=12, raw_notify="auto", kernel=kernel)
    assert await remote.ensure_ready(5.0) is True
    assert remote.state.delivery == "raw" and remote.bounces == 1 and hint(h) is False
    await remote.send_key("HOME", 0.01)
    assert tv.keys("raw") == [(CONSUMER, HOME), (CONSUMER, bytes(4))] and tv.ignored == []
    # Nothing to repair on such a kernel: no record on disk, no command after the link closed.
    assert debt(h) == ""
    await tv.drop()
    await eventually(lambda: not remote.state.connected)
    await asyncio.sleep(0.1)
    assert h.raw.commands == [] and debt(h) == ""


async def test_on_uses_direct_delivery_on_a_new_kernel_too_without_a_repair(h):
    remote, tv = await rebooted(h, android=12, raw_notify="on", kernel=fake_bluez.NEW_KERNEL)
    assert await remote.ensure_ready(5.0) is True and remote.state.delivery == "raw"
    await remote.send_key("HOME", 0.01)
    await tv.drop()
    await eventually(lambda: not remote.state.connected)
    await asyncio.sleep(0.1)
    assert h.raw.commands == [] and debt(h) == ""


async def test_background_policy_does_not_bounce_a_link_direct_delivery_will_not_use(h):
    """auto on an old kernel: the TV is called and left connected; reconnecting it would gain nothing."""
    remote, tv = await rebooted(h, android=12, raw_notify="auto", reconnect_on_drop=True)
    await eventually(lambda: remote.state.connected and h.bluez.advert is None, what="the TV to connect")
    await asyncio.sleep(0.5)  # longer than resubscribe_wait and the settle time
    assert remote.bounces == 0 and tv.connections == 2 and h.raw.opened == 0

    # Before suspend the answer is immediate and honest, which leaves the Wi-Fi assist its time.
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(3.0)
    assert err.value.code == "not_subscribed" and remote.bounces == 0

    # Allowing it starts the preparation right away, without anybody asking for the TV.
    remote.apply_config(quick_config(tv_address=TV, raw_notify="on", reconnect_on_drop=True))
    await eventually(lambda: remote.bounces == 1 and tv.connections == 3 and remote.state.connected, what="bounce")
    assert await remote.ensure_ready(3.0) is True and remote.state.delivery == "raw"


async def test_a_tv_known_to_stay_silent_is_reported_sooner_when_direct_delivery_is_not_used(h):
    remote, tv = await rebooted(h, android=12, raw_notify="auto", resubscribe_wait=4.0)
    assert remote._resubscribe_wait() == 4.0
    with pytest.raises(TransportError):
        await remote.ensure_ready(10.0)
    assert hint(h) is False

    # Another reboot: the full wait would only keep the Wi-Fi assist from having its turn.
    await remote.stop()
    await h.bluez.restart()
    fresh = await h.remote(tv_address=TV, raw_notify="auto", resubscribe_wait=4.0)
    assert fresh._resubscribe_wait() < 0.3
    started = time.monotonic()
    with pytest.raises(TransportError) as err:
        await fresh.ensure_ready(10.0)
    assert err.value.code == "not_subscribed" and time.monotonic() - started < 4.0 - 0.5

    # A TV that subscribes after all (it got its Android 14 update) is believed again.
    await tv.subscribe()
    assert await fresh.ensure_ready(5.0) is True and fresh.state.delivery == "bluez" and hint(h) is True


async def test_always_is_unchanged_by_the_kernel(h):
    remote, tv = await h.paired_remote(android=12, raw_notify="always", kernel=fake_bluez.OLD_KERNEL)
    assert await remote.ensure_ready(5.0) is True and remote.state.delivery == "raw"


@pytest.mark.parametrize(
    "error, text",
    [
        (PermissionError(errno.EPERM, "Operation not permitted"), "must run as root"),
        (OSError(errno.EAFNOSUPPORT, "Address family not supported"), "no Bluetooth socket support"),
    ],
)
async def test_direct_delivery_unavailable_degrades_to_not_subscribed(h, error, text):
    remote, tv = await rebooted(h, android=12)
    h.raw.open_error = error
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(5.0)
    assert err.value.code == "not_subscribed" and text in err.value.message
    assert "paired again" in err.value.message
    assert not remote.state.ready and remote.state.connected


async def test_direct_delivery_write_failure(h):
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"

    # The press cannot be sent: an error, nothing stuck.
    h.raw.send_error = OSError(errno.ENETDOWN, "Network is down")
    with pytest.raises(TransportError) as err:
        await remote.send_key("HOME", 0.01)
    assert err.value.code == "bluetooth_error" and "switched off" in err.value.message
    assert not remote.state.ready and tv.connected and h.raw.packets == []

    # The release cannot be sent: the key would stay down on the TV, so the link is dropped.
    h.raw.send_error = None
    assert await remote.ensure_ready(5.0)
    sending = asyncio.ensure_future(remote.send_key("HOME", 0.2))
    await eventually(lambda: len(h.raw.packets) == 1)
    h.raw.send_error = OSError(errno.EIO, "Input/output error")
    with pytest.raises(TransportError):
        await sending
    await eventually(lambda: not tv.connected, what="link drop after a failed release")


async def test_raw_notify_always_bypasses_bluez_even_when_subscribed(h):
    remote, tv = await h.paired_remote(android=12, raw_notify="always")
    assert remote.state.subscribed
    assert await remote.ensure_ready(5.0) is True
    # No bounce either: a TV that paired after the service was registered got no Service Changed.
    assert remote.state.delivery == "raw" and remote.bounces == 0
    await remote.send_key("HOME", 0.01)
    assert tv.keys("raw") == [(CONSUMER, HOME), (CONSUMER, bytes(4))] and tv.keys("bluez") == []
    await tv.subscribe()  # a StartNotify does not change a diagnostic override
    await asyncio.sleep(0.05)
    assert remote.state.delivery == "raw"


# --------------------------------------------------------------------------- (d) the remembered hint


async def test_hint_shortens_the_wait_on_later_boots(h):
    remote, tv = await rebooted(h, android=12, resubscribe_wait=0.6)
    started = time.monotonic()
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"
    first = time.monotonic() - started
    assert first >= 0.6 and hint(h) is False

    # Another reboot, and a fresh service process that only has the state file.
    await remote.stop()
    await h.bluez.restart()
    fresh = await h.remote(tv_address=TV, resubscribe_wait=0.6)
    started = time.monotonic()
    assert await fresh.ensure_ready(5.0) and fresh.state.delivery == "raw"
    second = time.monotonic() - started
    assert second < 0.45 < first and fresh.bounces == 1


async def test_state_file_damage_and_absence_are_tolerated(h):
    h.state_path.write_text("{not json")
    remote, tv = await rebooted(h, android=12)
    assert await remote.ensure_ready(5.0) and remote.state.delivery == "raw"
    assert json.loads(h.state_path.read_text()) == {
        "version": 1,
        "tv": TV,
        "resubscribes": False,
        "stale": [],
        "pending": [],
        "raw_debt": rawhci.boot_id(),  # direct delivery is attached: the kernel will need a repair
    }

    h.state_path.write_text('["a list"]')
    other = fake_bluez.BleRemote(await h.bus(), quick_config(), state_path=None, raw_link=h.raw.link())
    assert other._store.resubscribes(TV) is None  # no file at all is fine too


def test_state_store_keeps_unfinished_bonds_and_the_raw_debt(tmp_path):
    from tcl_me_remo.bluez.store import StateStore

    path = tmp_path / "state.json"
    store = StateStore(path)
    store.add_pending("aa:bb:cc:dd:ee:ff")
    store.add_pending("AA:BB:CC:DD:EE:FF")  # once
    store.add_stale("11:22:33:44:55:66")
    store.set_raw_debt("boot-1")
    assert StateStore(path).pending == [TV] and StateStore(path).raw_debt("boot-1")
    assert not StateStore(path).raw_debt("boot-2")  # another boot, another kernel

    again = StateStore(path)
    again.pending_to_stale()
    assert again.pending == [] and again.stale == ["11:22:33:44:55:66", TV]
    again.clear_raw_debt()
    reread = StateStore(path)
    assert reread.pending == [] and reread.stale == ["11:22:33:44:55:66", TV] and not reread.raw_debt("boot-1")

    # Without a boot id (no /proc) a debt is still a debt.
    reread.set_raw_debt("")
    assert StateStore(path).raw_debt("")
    # Junk in the new fields is ignored like junk anywhere else.
    path.write_text(json.dumps({"version": 1, "pending": "nope", "raw_debt": 7, "stale": [1, "aa:bb:cc:dd:ee:ff"]}))
    junk = StateStore(path)
    assert junk.pending == [] and not junk.raw_debt("7") and junk.stale == [TV]


# --------------------------------------------------------------------------- wake beacon


async def test_wake_beacon_is_broadcast_only_when_waking(h):
    remote, tv = await h.paired_remote(wake_beacon="mediatek")
    await remote.disconnect()
    tv.auto_connect = False
    h.bluez.events.clear()

    # Reaching the TV for anything else (turning it off, a key) never wakes it.
    assert await remote.ensure_ready(0.1) is False
    assert [props["Type"] for name, props in h.bluez.events if name == "advert_registered"] == ["peripheral"]

    waiting = asyncio.ensure_future(remote.ensure_ready(1.0, wake=True))
    await eventually(lambda: h.bluez.beacon is not None and h.bluez.advert is not None)
    assert h.bluez.beacon == {
        "Type": "broadcast",
        # MediaTek's default pattern: a zero, the TV's address (low byte first), four zeros, "CRKTM".
        "ManufacturerData": {0x0046: bytes.fromhex("00" "ffeeddccbbaa" "00000000") + b"CRKTM"},
    }
    await tv.connect()
    assert await waiting is True
    assert h.bluez.beacon is None and h.bluez.advert is None  # both off air once the TV is there

    await remote.disconnect()
    remote.apply_config(quick_config(tv_address=TV, wake_beacon="custom", wake_beacon_data="1d00aabbcc"))
    assert await remote.ensure_ready(0.1, wake=True) is False
    beacons = [props for name, props in h.bluez.events if name == "advert_registered" and props["Type"] == "broadcast"]
    assert beacons[-1]["ManufacturerData"] == {0x001D: bytes.fromhex("aabbcc")}
    assert h.bluez.beacon is None  # timed out: off air


async def test_wake_beacon_failure_does_not_get_in_the_way(h, caplog):
    remote, tv = await h.paired_remote(wake_beacon="mediatek")
    await remote.disconnect()
    h.bluez.max_adverts = 1  # room for the connectable advertisement only
    assert await remote.ensure_ready(2.0, wake=True) is True
    assert "could not broadcast the wake beacon" in caplog.text


# --------------------------------------------------------------------------- stop


async def test_stop_is_clean(h):
    remote, tv = await h.paired_remote(reconnect_on_drop=True)
    bus = remote._bus
    tv.auto_connect = False
    await tv.drop()
    await eventually(lambda: h.bluez.advert is not None)
    await remote.start_pairing(30)
    waiting = asyncio.ensure_future(remote.ensure_ready(30))
    await asyncio.sleep(0.05)

    await remote.stop()
    assert h.bluez.app is None and h.bluez.advert is None and h.bluez.beacon is None
    assert h.bluez.default_agent is None and h.bluez.adapter.props["Pairable"] is False
    with pytest.raises(TransportError):
        await asyncio.wait_for(waiting, 1.0)
    state = remote.state
    assert not state.registered and not state.ready and not state.advertising and not state.pairing
    assert not remote._tasks
    await remote.stop()  # idempotent
    with pytest.raises(TransportError) as err:
        await remote.ensure_ready(1.0)
    assert err.value.code == "bluetooth_error"

    # Nothing of ours is left on the bus connection: a new remote can use it.
    again = fake_bluez.BleRemote(bus, quick_config(), raw_link=h.raw.link())
    await again.start()
    assert again.state.registered and h.bluez.app.table()[0][0] == 0x0100
    await again.stop()


async def test_stop_never_raises_when_bluetoothd_is_gone(h):
    remote, tv = await h.paired_remote()
    await remote.start_pairing(30)
    await h.bluez.stop()
    await remote.stop()
    await remote.stop()


@pytest.mark.parametrize("kernel, injects", [(fake_bluez.OLD_KERNEL, False), (fake_bluez.NEW_KERNEL, True)])
async def test_shipped_defaults_inject_nothing_on_a_kernel_they_would_disturb(h, tmp_path, kernel, injects):
    """The other tests choose ``raw_notify`` themselves (quick_config says "on").  This one takes
    the settings a new installation has, cut down only in their waits, through a reboot."""
    shipped = config_mod.default_config()
    shipped.bluetooth = dataclasses.replace(shipped.bluetooth, settle_ms=40, resubscribe_wait=0.3, key_press_ms=10)
    remote = await h.remote(shipped.bluetooth, kernel=kernel)
    tv = h.bluez.add_device(android=12)
    tv.auto_connect = True
    await h.pair(remote, tv)
    shipped.bluetooth = dataclasses.replace(shipped.bluetooth, tv_address=TV, tv_name="Living Room TV")
    service = Service(shipped, remote=remote, sys_root=tmp_path)
    assert remote._cfg.raw_notify == "auto" == config_mod.BluetoothConfig().raw_notify
    assert remote._cfg.reconnect_on_drop is True

    await h.bluez.restart()  # the reboot: bluetoothd has lost the TV's subscription
    await eventually(lambda: remote.state.registered and remote.state.connected, what="the TV to be called back")
    if injects:
        # Background calling gets the one-off reconnect out of the way where direct delivery will be used.
        await eventually(lambda: remote.bounces == 1 and tv.connections == 3, what="the reconnect ahead of time")
    else:
        await asyncio.sleep(0.6)  # resubscribe_wait and more: nothing is done about the silent TV
        assert remote.bounces == 0 and tv.connections == 2

    record = await service.tv_on(switch_input=False)
    if injects:
        assert record["ok"], record
        assert tv.keys("raw") == [(SYSTEM, b"\x04"), (SYSTEM, b"\x00")] and remote.state.delivery == "raw"
        assert debt(h) == ""  # nothing to repair there
    else:
        assert not record["ok"]
        assert "not used automatically on this kernel (Linux 6.11.11-valve19-1-neptune-611" in record["detail"]
        assert "'on'" in record["detail"] and "pair the TV again" in record["detail"]
        assert service.status()["bluetooth"]["error"] in record["detail"]
        assert h.raw.opened == 0 and h.raw.packets == [] and h.raw.commands == [] and tv.keys("raw") == []
        assert remote.bounces == 0 and tv.connections == 2 and tv.connected
    await service.rpc("disconnect", {})


# --------------------------------------------------------------------------- with the service core


async def test_service_drives_the_real_transport(h, tmp_path):
    """The contract the rest of the product relies on, end to end against the fake bluetoothd."""
    remote, tv = await rebooted(h, android=12)
    cfg = config_mod.default_config()
    cfg.bluetooth = quick_config(tv_address=TV, tv_name="Living Room TV")
    service = Service(cfg, remote=remote, sys_root=tmp_path)
    remote.on_paired = service.on_paired

    record = await service.tv_on(switch_input=False)
    assert record["ok"], record
    assert tv.keys("raw") == [(SYSTEM, b"\x04"), (SYSTEM, b"\x00")]  # WAKE, direct delivery after the "reboot"
    assert service.status()["bluetooth"]["delivery"] == "raw"

    assert (await service.send_key("HOME"))["key"] == "HOME"
    record = await service.tv_off()
    assert record["ok"], record
    assert tv.keys("raw")[-2:] == [(CONSUMER, bytes.fromhex("32000000")), (CONSUMER, bytes(4))]

    # Pairing through the service stores the TV in its configuration.
    await service.rpc("pair_start", {"timeout": 30})
    new_tv = h.bluez.add_device("C0:FF:EE:00:00:01", name="Bedroom TV")
    assert await new_tv.pair_like_android()
    await eventually(lambda: service.cfg.bluetooth.tv_address == "C0:FF:EE:00:00:01")
    await eventually(lambda: not remote.state.pairing)
    assert service.cfg.bluetooth.tv_name == "Bedroom TV" and remote.state.paired

    await service.rpc("forget_tv", {})
    assert service.cfg.bluetooth.tv_address == "" and not remote.state.paired
