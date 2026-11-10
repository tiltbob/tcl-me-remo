import asyncio

import pytest
from fake_logind import FakeLogind, eventually

from tcl_me_remo import dbus
from tcl_me_remo.dbus import Message, MessageType
from tcl_me_remo.lifecycle import LOGIND_PATH, MANAGER, Logind, shutdown_kind_from_type


@pytest.fixture
async def setup(private_bus):
    fake = FakeLogind()
    await fake.start()
    bus = await dbus.connect_system_bus()
    events = []
    gate = asyncio.Event()
    gate.set()

    async def on_sleep(start):
        events.append(("sleep", start, fake.held("sleep")))
        await gate.wait()

    async def on_shutdown(start, kind):
        events.append(("shutdown", start, kind, fake.held("shutdown")))
        await gate.wait()

    logind = Logind(bus, on_sleep=on_sleep, on_shutdown=on_shutdown)
    yield fake, logind, events, gate, bus
    await logind.stop()
    bus.disconnect()
    await fake.stop()


def test_shutdown_kind_from_type():
    assert shutdown_kind_from_type("power-off") == "shutdown"
    assert shutdown_kind_from_type("poweroff") == "shutdown"
    assert shutdown_kind_from_type("halt") == "shutdown"
    assert shutdown_kind_from_type("reboot") == "reboot"
    assert shutdown_kind_from_type("kexec") == "reboot"
    assert shutdown_kind_from_type("soft-reboot") == "reboot"
    assert shutdown_kind_from_type("") == "unknown"
    assert shutdown_kind_from_type("factory-reset") == "unknown"


async def test_takes_delay_locks_and_reads_budget(setup):
    fake, logind, _events, _gate, _bus = setup
    assert await logind.start() is True
    assert logind.available
    assert fake.held("sleep") == 1 and fake.held("shutdown") == 1
    assert {(lock["what"], lock["mode"], lock["who"]) for lock in fake.manager.locks} == {
        ("sleep", "delay", "tcl-me-remo"),
        ("shutdown", "delay", "tcl-me-remo"),
    }
    assert logind.max_delay == 5.0
    assert logind.down_budget == 3.5


async def test_sleep_lock_is_held_during_the_handler_and_released_after(setup):
    fake, logind, events, gate, _bus = setup
    await logind.start()
    gate.clear()
    fake.prepare_for_sleep(True)
    await eventually(lambda: events)
    assert events == [("sleep", True, 1)]  # lock still held while our handler runs
    assert fake.held("sleep") == 1
    gate.set()
    await eventually(lambda: fake.held("sleep") == 0)  # released: logind may now suspend

    fake.prepare_for_sleep(False)
    await eventually(lambda: len(events) == 2)
    assert events[1] == ("sleep", False, 1)  # the lock was taken back before the resume handler ran
    assert fake.held("shutdown") == 1


async def test_sleep_signals_are_delivered_in_the_order_they_arrived(setup):
    """A machine that wakes and is sent back to sleep at once: reporting the resume starts
    with a round trip to logind, and the suspend that follows must not overtake it."""
    fake, logind, events, _gate, _bus = setup
    await logind.start()
    fake.prepare_for_sleep(True)
    await eventually(lambda: fake.held("sleep") == 0)
    fake.prepare_for_sleep(False)
    fake.prepare_for_sleep(True)
    await eventually(lambda: len(events) == 3)
    assert [event[:2] for event in events] == [("sleep", True), ("sleep", False), ("sleep", True)]
    assert events[2][2] == 1  # and the second suspend is delayed for us like the first
    await eventually(lambda: fake.held("sleep") == 0)


async def test_a_resume_reported_during_the_suspend_handler_waits_and_leaves_the_lock_held(setup):
    """logind stops waiting after its delay; the machine can sleep and wake with our handler
    still running.  The resume is delivered when the handler is through, and the lock that
    handler releases at its end is taken again: the next suspend has to wait for us too."""
    fake, logind, events, gate, _bus = setup
    await logind.start()
    gate.clear()
    fake.prepare_for_sleep(True)
    await eventually(lambda: len(events) == 1)
    fake.prepare_for_sleep(False)
    await asyncio.sleep(0.3)
    assert len(events) == 1
    gate.set()
    await eventually(lambda: len(events) == 2)
    assert events[1][:2] == ("sleep", False)
    await asyncio.sleep(0.5)  # longer than the fake logind keeps its own end of a lock open
    assert fake.held("sleep") == 1


async def test_shutdown_with_and_without_metadata(setup):
    fake, logind, events, _gate, _bus = setup
    await logind.start()
    fake.prepare_for_shutdown(True, "reboot")
    await eventually(lambda: events)
    assert events == [("shutdown", True, "reboot", 1)]
    await eventually(lambda: fake.held("shutdown") == 0)

    # called off: lock comes back, handler is told
    fake.prepare_for_shutdown(False)
    await eventually(lambda: len(events) == 2)
    assert events[1][:3] == ("shutdown", False, "")
    await eventually(lambda: fake.held("shutdown") == 1)

    # older systemd: no metadata signal at all
    fake.prepare_for_shutdown(True)
    await eventually(lambda: len(events) == 3)
    assert events[2][:3] == ("shutdown", True, "unknown")

    fake.prepare_for_shutdown(False)
    await eventually(lambda: len(events) == 4)
    fake.prepare_for_shutdown(True, "power-off")
    await eventually(lambda: len(events) == 5)
    assert events[4][:3] == ("shutdown", True, "shutdown")


async def test_handler_failure_still_releases_the_lock(private_bus):
    fake = FakeLogind()
    await fake.start()
    bus = await dbus.connect_system_bus()

    async def boom(start):
        raise RuntimeError("handler bug")

    async def nothing(start, kind):
        return None

    logind = Logind(bus, on_sleep=boom, on_shutdown=nothing)
    await logind.start()
    fake.prepare_for_sleep(True)
    await eventually(lambda: fake.held("sleep") == 0)
    await logind.stop()
    bus.disconnect()
    await fake.stop()


async def test_spoofed_signals_from_other_processes_are_ignored(setup):
    fake, logind, events, _gate, bus = setup
    await logind.start()
    rogue = await dbus.connect_system_bus()
    # A local process can address a signal straight at our connection, bypassing match rules.
    await rogue.send(
        Message(
            destination=bus.unique_name,
            path=LOGIND_PATH,
            interface=MANAGER,
            member="PrepareForSleep",
            signature="b",
            body=[True],
            message_type=MessageType.SIGNAL,
        )
    )
    await asyncio.sleep(0.2)
    assert events == []
    assert fake.held("sleep") == 1
    fake.prepare_for_sleep(True)  # the real one still works
    await eventually(lambda: events)
    rogue.disconnect()


async def test_missing_logind_is_tolerated(private_bus):
    bus = await dbus.connect_system_bus()

    async def never(*_args):
        raise AssertionError("no events expected")

    logind = Logind(bus, on_sleep=never, on_shutdown=never)
    assert await logind.start() is False
    assert not logind.available
    assert logind.down_budget == 3.5  # sensible default budget for the fallback paths
    await logind.stop()
    bus.disconnect()


async def test_stop_releases_everything(setup):
    fake, logind, _events, _gate, _bus = setup
    await logind.start()
    await logind.stop()
    await eventually(lambda: fake.held("sleep") == 0 and fake.held("shutdown") == 0)
