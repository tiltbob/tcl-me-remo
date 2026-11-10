"""End to end: the real service process, driven by the real CLI, against fake system services.

Everything else in this suite tests the parts in-process, with fakes on one side or the other.
Here ``python -m tcl_me_remo daemon`` runs as it does under systemd (its own process, default
factories, production timing constants) and is driven the way a person or a systemd hook
drives it: ``python -m tcl_me_remo <command>``, logind announcements and signals.  Assertions
are about what the TV, bluetoothd and logind saw.  See e2e_rig.py for the machinery.

Two rules keep these tests honest on a slow machine:

* nothing is expected to happen *within* a short time: conditions are polled with a generous
  timeout, and "X did not happen" is shown by something that happens afterwards on the same
  channel (a later key, an unregistration), never by waiting a while and looking;
* where the order of two things matters and they reach the test by different routes, the
  evidence is taken from one place that saw both (see ``lock_released``).
"""

import asyncio
import shutil
import signal
import stat

import fake_bluez
import pytest
from e2e_rig import CONSUMER, DOWN, ENTER, HOME, KEYBOARD, OK, SLEEP, SYSTEM, WAKE, Reaper, Rig, eventually
from fake_logind import FakeLogind

TV_ADDRESS = "AA:BB:CC:DD:EE:FF"
TV_NAME = "Living Room TV"
NOT_SUBSCRIBED = "the TV is connected but will not receive keys until it is paired again"


@pytest.fixture
def reaper():
    reaper = Reaper()
    yield reaper
    # Whatever the test did, and however it ended: no process of ours is left behind.
    assert reaper.kill_leftovers() == [], "processes were still running after the test"


@pytest.fixture
async def rig(tmp_path, private_bus, reaper):
    async with fake_bluez.running() as bluez:
        # A long delay limit: how much of it the service may use is its "budget" for telling
        # the TV, and no test here is about running out of it.
        logind = FakeLogind(max_delay_usec=60_000_000)
        await logind.start()
        rig = Rig(tmp_path, reaper, bus_address=private_bus, bluez=bluez, logind=logind)
        try:
            async with rig.cleaned_up():
                yield rig
        finally:
            await logind.stop()


async def start_paired(rig, *, android=12):
    """Start the service and pair a TV with it through the CLI.  The link is up afterwards.

    The TV is awake: it connects whenever the service calls it.
    """
    await rig.start()
    tv = rig.bluez.add_device(TV_ADDRESS, name=TV_NAME, android=android)
    tv.auto_connect = True
    await rig.pair(tv)
    return tv


async def received(tv, expected):
    """The TV has received exactly these reports, in this order."""
    try:
        await eventually(lambda: len(tv.keys()) >= len(expected), "the reports to reach the TV")
    except AssertionError:
        pass  # the comparison says what is missing
    assert tv.keys() == expected


async def lock_released(rig, what):
    """Wait until the service lets go of a delay lock; returns its log as it was at that moment.

    The lock is a pipe and a key is a D-Bus message: which of the two the test sees first says
    little about which the service did first.  The service's own log does.  A line it wrote
    before it closed the lock is in the file by the time the fake logind sees the pipe close,
    however slow the machine is.
    """
    await eventually(lambda: rig.logind.held(what) == 0, f"the {what} lock to be released")
    return rig.log()


async def nothing_listens(host, port):
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10.0)
    except ConnectionRefusedError:
        return True
    except (OSError, TimeoutError):
        return False
    writer.close()
    return False


# --------------------------------------------------------------------------- start-up


async def test_startup_registers_the_frozen_table_holds_the_locks_and_stays_off_the_air(rig):
    await rig.start()
    assert rig.notifications == ["READY=1"]
    assert stat.S_ISSOCK(rig.socket_path.stat().st_mode)

    status = await rig.status()
    assert status["phase"] == "running" and status["busy"] == "" and status["config_problems"] == []
    bluetooth = status["bluetooth"]
    assert bluetooth["available"] and bluetooth["adapter"] == "hci0" and bluetooth["powered"]
    assert bluetooth["registered"] and bluetooth["error"] == ""
    assert not (bluetooth["paired"] or bluetooth["connected"] or bluetooth["advertising"] or bluetooth["pairing"])
    assert "no TV paired (run: tcl-me-remo pair)" in await rig.ok("status")
    assert "starting" in await rig.ok("logs", "-n", "100")  # the log tail comes from the running service

    # The HID service sits at the handles every paired TV has stored.
    table = rig.bluez.app.table()
    assert table[0][:3] == (0x0100, "service", "1812")
    assert [handle for handle, _kind, _uuid, _path in table] == list(range(0x0100, 0x0115))
    values = [(path.rsplit("/", 1)[-1], handle) for handle, kind, uuid, path in table if kind == "value"]
    assert values[-3:] == [("c4_kbd", KEYBOARD), ("c5_consumer", CONSUMER), ("c6_system", SYSTEM)]

    # Both delay locks were taken before the service called itself ready, and it knows how
    # long logind will wait for it (the rig's logind says 60 s).
    assert rig.logind.held("sleep") == 1 and rig.logind.held("shutdown") == 1
    assert "logind waits up to 60s" in rig.log()

    assert await rig.stop(signal.SIGINT) == 0  # Ctrl-C, when somebody runs it by hand
    assert "STOPPING=1" in rig.notifications
    # Its whole life in bluetoothd's eyes: no advertisement and no agent without a reason,
    # and the adapter is as the user left it.
    await eventually(lambda: rig.bluez.app is None, "the HID service to be unregistered")
    names = rig.bluez.event_names()
    assert names[0] == "gatt_registered" and set(names) <= {"gatt_registered", "gatt_unregistered"}
    adapter = rig.bluez.adapter.props
    assert adapter["Discoverable"] is False and adapter["Pairable"] is False
    assert adapter["Alias"] == "steamdeck" and adapter["Name"] == "steamdeck"


async def test_the_service_starts_without_bluetoothd_and_logind_and_registers_when_bluetoothd_appears(
    tmp_path, private_bus, reaper
):
    # At boot nothing orders bluetoothd's readiness before ours, and bluetoothd may be restarted.
    rig = Rig(tmp_path, reaper, bus_address=private_bus)
    async with rig.cleaned_up():
        await rig.start()
        bluetooth = (await rig.status())["bluetooth"]
        assert not bluetooth["available"] and not bluetooth["registered"]
        assert "no Bluetooth adapter" in await rig.ok("status")
        code, out, err = await rig.cli("pair", "--timeout", "10")
        assert (code, out, err) == (1, "", "tcl-me-remo: the Bluetooth service (bluetoothd) is not running\n")
        assert "logind is not available" in rig.log()  # said once, and the service carries on

        async with fake_bluez.running() as bluez:
            await eventually(lambda: bluez.app is not None, "the HID service to be registered")
            assert bluez.app.table()[0][:3] == (0x0100, "service", "1812")
            status = await rig.status_when(lambda status: status["bluetooth"]["registered"], "the service to notice")
            assert status["bluetooth"]["available"] and status["bluetooth"]["adapter"] == "hci0"
            assert await rig.stop() == 0


# --------------------------------------------------------------------------- pairing


async def test_pairing_through_the_cli_stores_the_tv(rig):
    await rig.start()
    tv = rig.bluez.add_device(TV_ADDRESS, name=TV_NAME)
    out, while_open = await rig.pair(tv)
    assert "Pairing mode is open." in out and f"Paired with {TV_NAME}." in out

    # While pairing mode was open the advertisement was discoverable and the adapter was not:
    # that would turn on classic Bluetooth inquiry and make the TV pair the wrong way.
    assert while_open["advert"]["Type"] == "peripheral" and while_open["advert"]["LocalName"] == "SteamOS Remote"
    assert while_open["adapter"]["Discoverable"] is False and while_open["adapter"]["Pairable"] is True
    assert while_open["adapter"]["Alias"] == "steamdeck"

    stored = rig.settings()["bluetooth"]
    assert stored["tv_address"] == TV_ADDRESS and stored["tv_name"] == TV_NAME
    bluetooth = (await rig.status())["bluetooth"]
    assert bluetooth["paired"] and bluetooth["connected"] and bluetooth["subscribed"] and not bluetooth["pairing"]
    assert bluetooth["tv_address"] == TV_ADDRESS and bluetooth["tv_name"] == TV_NAME
    assert f"tv: {TV_NAME}" in await rig.ok("status")

    # Pairing mode is over: nothing of it is left in bluetoothd.
    await eventually(lambda: rig.bluez.default_agent is None, "the pairing agent to be unregistered")
    assert rig.bluez.advert is None and tv.connected
    adapter = rig.bluez.adapter.props
    assert adapter["Pairable"] is False and adapter["Discoverable"] is False and adapter["Alias"] == "steamdeck"
    assert rig.bluez.event_names().count("gatt_registered") == 1


async def test_status_does_not_say_it_waits_for_a_subscription_the_tv_has_made(rig):
    await start_paired(rig)
    bluetooth = (await rig.status())["bluetooth"]
    assert bluetooth["connected"] and bluetooth["subscribed"]
    assert "waiting for the TV to subscribe" not in await rig.ok("status")


async def test_forget_removes_the_bond_and_the_stored_address(rig):
    tv = await start_paired(rig)
    assert "forgotten" in await rig.ok("forget")
    assert rig.settings()["bluetooth"]["tv_address"] == ""
    assert "device_removed" in rig.bluez.event_names() and not tv.connected
    bluetooth = (await rig.status())["bluetooth"]
    assert not bluetooth["paired"] and not bluetooth["connected"] and bluetooth["tv_address"] == ""
    code, _out, err = await rig.cli("key", "HOME")
    assert code == 1 and err == "tcl-me-remo: no TV is paired yet\n"


# --------------------------------------------------------------------------- keys


async def test_off_on_keys_and_macros_reach_the_tv(rig):
    tv = await start_paired(rig)

    assert "ok: TV off (Bluetooth)" in await rig.ok("off")
    await received(tv, SLEEP)  # the discrete off key: pressed, then released
    assert "ok: TV on (Bluetooth)" in await rig.ok("on", "--no-input")
    await received(tv, SLEEP + WAKE)
    assert await rig.ok("key", "HOME") == "sent HOME\n"
    await received(tv, SLEEP + WAKE + HOME)
    assert "ok: macro played" in await rig.ok("macro", "DOWN*2", "OK")
    await received(tv, SLEEP + WAKE + HOME + DOWN + DOWN + OK)

    bluetooth = (await rig.status())["bluetooth"]
    assert bluetooth["ready"] and bluetooth["delivery"] == "bluez"


async def test_an_unknown_key_is_refused_with_a_clear_message(rig):
    await rig.start()
    code, out, err = await rig.cli("key", "NOPE")
    assert code == 1 and out == ""
    assert err.startswith("tcl-me-remo: unknown key 'NOPE' (use a name such as HOME")
    assert "Traceback" not in err
    assert (await rig.status())["phase"] == "running"  # the service took no harm


async def test_disconnect_closes_the_link_until_it_is_asked_for_again(rig):
    tv = await start_paired(rig)
    assert await rig.ok("disconnect") == "disconnected\n"
    await eventually(lambda: not tv.connected, "the link to be dropped")
    assert rig.bluez.advert is None  # closed on purpose: the TV is not called back
    assert await rig.ok("connect") == "connected\n"
    assert tv.connected
    assert await rig.ok("key", "HOME") == "sent HOME\n"
    await received(tv, HOME)


async def test_a_second_command_is_refused_while_one_is_running(rig):
    tv = await start_paired(rig)
    tv.auto_connect = False  # out of reach for now, so that "on" has to wait for it
    await tv.drop()
    await eventually(lambda: rig.bluez.advert is not None, "the service to call the TV")
    turning_on = asyncio.ensure_future(rig.cli("on", "--no-input"))
    try:
        await rig.status_when(lambda status: status["busy"] == "tv_on (manual)", "'on' to be under way")
        for command in (["off"], ["key", "HOME"]):
            code, out, err = await rig.cli(*command)
            assert (code, out, err) == (1, "", "tcl-me-remo: busy with 'tv_on' (manual); try again shortly\n")
        await tv.connect()
        code, out, _err = await turning_on
    finally:
        turning_on.cancel()
        await asyncio.gather(turning_on, return_exceptions=True)
    assert code == 0 and "ok: TV on (Bluetooth)" in out
    await received(tv, WAKE)  # and nothing of what was refused


# --------------------------------------------------------------------------- suspend and resume


async def test_suspend_sends_sleep_before_the_lock_is_released_and_resume_wakes_the_tv(rig):
    tv = await start_paired(rig)
    assert rig.logind.held("sleep") == 1

    rig.logind.prepare_for_sleep(True)
    log = await lock_released(rig, "sleep")  # only now may the machine sleep
    assert "Bluetooth: sent SLEEP" in log and "tv_off (suspend): TV off (Bluetooth)" in log
    # The TV's side of the same order: the key, then the link going down.  Both reach the fake
    # bluetoothd from the service over one bus connection, so in the order they were sent.
    await eventually(lambda: not tv.connected, "the link to be dropped")
    assert tv.keys() == SLEEP
    status = await rig.status()
    assert status["phase"] == "down" and status["last_action"]["trigger"] == "suspend" and status["last_action"]["ok"]
    assert rig.logind.held("shutdown") == 1  # that lock is for another day

    rig.logind.prepare_for_sleep(False)
    await eventually(lambda: rig.logind.held("sleep") == 1, "the sleep lock to be taken again")
    await received(tv, SLEEP + WAKE)  # the TV answered the call and was woken
    assert tv.connected
    status = await rig.status_when(lambda status: not status["busy"], "the resume to be handled")
    assert status["phase"] == "running"
    assert status["last_action"]["trigger"] == "resume" and status["last_action"]["ok"]


async def test_suspend_while_the_tv_is_away_holds_the_lock_until_the_tv_has_been_told(rig):
    tv = await start_paired(rig)
    tv.auto_connect = False  # out of reach for now: our calls go unanswered
    await tv.drop()
    await eventually(lambda: rig.bluez.advert is not None, "the service to call the TV")

    rig.logind.prepare_for_sleep(True)
    await rig.status_when(lambda status: status["busy"] == "tv_off (suspend)", "the suspend to be handled")
    await eventually(lambda: rig.bluez.advert is not None, "the service to keep calling the TV")
    # The service can do nothing but wait for the TV, and that is what it does: the machine
    # is kept awake and nothing has been sent.
    assert rig.logind.held("sleep") == 1 and tv.keys() == []

    await tv.connect()
    log = await lock_released(rig, "sleep")
    assert "Bluetooth: sent SLEEP" in log
    await received(tv, SLEEP)
    await eventually(lambda: not tv.connected, "the link to be dropped")


async def test_suspend_with_tv_off_disabled_sends_nothing_and_still_releases_the_lock(rig):
    tv = await start_paired(rig)
    assert await rig.ok("config", "events.suspend_tv_off", "false") == "events.suspend_tv_off = false\n"

    rig.logind.prepare_for_sleep(True)
    log = await lock_released(rig, "sleep")
    assert "sent SLEEP" not in log
    status = await rig.status()
    assert status["phase"] == "down"
    assert status["last_action"]["trigger"] == "suspend" and status["last_action"]["skipped"]
    assert status["last_action"]["detail"] == "turning the TV off on suspend is disabled"

    rig.logind.prepare_for_sleep(False)
    await received(tv, WAKE)  # the first thing the TV hears since it was paired


async def test_a_suspend_announced_by_logind_and_by_the_sleep_hook_is_acted_on_once(rig):
    # tcl-me-remo-sleep.service reports suspend and resume through the CLI, next to logind.
    tv = await start_paired(rig)
    rig.logind.prepare_for_sleep(True)
    await lock_released(rig, "sleep")
    assert await rig.ok("event", "suspend", "--budget", "30") == "skipped: duplicate suspend event ignored\n"

    # On the way up the hook happens to be first, and logind's announcement is the duplicate.
    assert "ok: TV on (Bluetooth)" in await rig.ok("event", "resume")
    rig.logind.prepare_for_sleep(False)
    await eventually(lambda: rig.logind.held("sleep") == 1, "the sleep lock to be taken again")
    assert await rig.ok("key", "HOME") == "sent HOME\n"
    await received(tv, SLEEP + WAKE + HOME)  # one SLEEP and one WAKE, then the key
    history = (await rig.status())["history"]
    assert [(record["name"], record["trigger"]) for record in history] == [("tv_off", "suspend"), ("tv_on", "resume")]


# --------------------------------------------------------------------------- power-off and reboot


async def test_power_off_through_logind_turns_the_tv_off(rig):
    tv = await start_paired(rig)
    rig.logind.prepare_for_shutdown(True, "power-off")
    log = await lock_released(rig, "shutdown")
    assert "Bluetooth: sent SLEEP" in log and "tv_off (shutdown): TV off (Bluetooth)" in log
    await received(tv, SLEEP)
    status = await rig.status()
    assert status["phase"] == "down" and status["last_action"]["trigger"] == "shutdown" and status["last_action"]["ok"]


async def test_reboot_through_logind_leaves_the_tv_on(rig):
    tv = await start_paired(rig)
    rig.logind.prepare_for_shutdown(True, "reboot")
    log = await lock_released(rig, "shutdown")  # a reboot is not held up either
    assert "sent SLEEP" not in log
    last = (await rig.status())["last_action"]
    assert last["trigger"] == "reboot" and last["skipped"]
    assert last["detail"] == "turning the TV off on reboot is disabled"

    # The reboot is called off.  The next key is the first one the TV hears: no SLEEP before it.
    rig.logind.prepare_for_shutdown(False)
    await rig.status_when(lambda status: status["phase"] == "running", "the service to carry on")
    await eventually(lambda: rig.logind.held("shutdown") == 1, "the shutdown lock to be taken again")
    assert await rig.ok("key", "HOME") == "sent HOME\n"
    await received(tv, HOME)


# --------------------------------------------------------------------------- stopping


async def test_sigterm_is_a_clean_stop_and_not_a_shutdown(rig):
    tv = await start_paired(rig)
    assert await rig.stop() == 0
    notes = rig.notifications
    assert notes[0] == "READY=1" and "STOPPING=1" in notes[1:]
    assert not rig.socket_path.exists()
    # Unregistering is the last thing the service asks of bluetoothd.  Anything it had sent
    # to the TV would have arrived before: stopping the service is not turning the machine off.
    # (Whether the fake recorded the unregistration or only saw the service leave the bus depends
    # on which of the two answered first; the registration being gone is what matters.)
    await eventually(lambda: rig.bluez.app is None, "the HID service to be unregistered")
    assert tv.keys() == []
    assert rig.logind.held("sleep") == 0 and rig.logind.held("shutdown") == 0
    assert "service stop requested (the system is not shutting down)" in rig.log()


async def test_stopping_while_a_key_is_down_releases_it(rig):
    # A key left down would repeat on the TV for as long as the link stays up, and bluetoothd
    # keeps the link when the service goes.
    tv = await start_paired(rig)
    holding = asyncio.ensure_future(rig.cli("key", "HOME", "--hold", "10"))
    try:
        await eventually(lambda: tv.keys() == HOME[:1], "the key to go down")
        assert await rig.stop() == 0
        code, _out, err = await holding
    finally:
        holding.cancel()
        await asyncio.gather(holding, return_exceptions=True)
    assert code == 3 and "the service closed the connection without answering" in err
    await eventually(lambda: rig.bluez.app is None, "the HID service to be unregistered")
    assert tv.keys() == HOME


async def test_sigterm_while_pairing_mode_is_open_puts_everything_back(rig):
    await rig.start()
    waiting = asyncio.ensure_future(rig.cli("pair", "--timeout", "60"))
    try:
        await eventually(rig.pairing_mode, "pairing mode to open")
        assert await rig.stop() == 0
        code, _out, err = await waiting
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)

    await eventually(lambda: rig.bluez.app is None, "the HID service to be unregistered")
    names = rig.bluez.event_names()
    assert "gatt_registered" in names  # what is asserted below is the state, not which call got there first
    assert rig.bluez.advert is None and rig.bluez.default_agent is None
    adapter = rig.bluez.adapter.props
    assert adapter["Pairable"] is False and adapter["Discoverable"] is False
    # The CLI that was waiting for the TV notices that the service has gone.
    assert code == 3 and "Is the service running?" in err and "Traceback" not in err


async def test_a_killed_service_leaves_a_stale_socket_that_does_not_stop_the_next_start(rig):
    tv = await start_paired(rig)
    assert await rig.stop(signal.SIGKILL) == -signal.SIGKILL
    assert stat.S_ISSOCK(rig.socket_path.stat().st_mode)  # nobody tidied up
    code, _out, err = await rig.cli("status")
    assert code == 3 and "Is the service running?" in err
    # bluetoothd drops what a vanished client had registered; the bond is bluetoothd's and stays.
    await eventually(lambda: rig.bluez.app is None, "bluetoothd to notice that the service is gone")

    await rig.start()
    bluetooth = (await rig.status())["bluetooth"]
    assert bluetooth["registered"] and bluetooth["paired"] and bluetooth["tv_address"] == TV_ADDRESS
    assert bluetooth["connected"] and tv.connected  # the link never went down
    assert rig.bluez.app.table()[0][:3] == (0x0100, "service", "1812")  # and the table is where it was
    assert rig.bluez.event_names().count("agent_registered") == 1  # paired once


# --------------------------------------------------------------------------- settings


async def test_a_new_device_name_is_used_without_registering_the_hid_service_again(rig):
    await rig.start()
    assert await rig.ok("config", "bluetooth.device_name", "Den Remote") == 'bluetooth.device_name = "Den Remote"\n'
    assert rig.settings()["bluetooth"]["device_name"] == "Den Remote"
    assert await rig.ok("config", "bluetooth.device_name") == '"Den Remote"\n'

    tv = rig.bluez.add_device(TV_ADDRESS, name=TV_NAME)
    _out, while_open = await rig.pair(tv)
    assert while_open["advert"]["LocalName"] == "Den Remote"
    # Registering again would make bluetoothd forget every subscription: it happened once, at start-up.
    assert rig.bluez.event_names().count("gatt_registered") == 1
    assert rig.settings()["bluetooth"]["device_name"] == "Den Remote"  # pairing saved the TV next to it


async def test_an_invalid_setting_is_refused_and_the_file_is_left_alone(rig):
    await rig.start()
    before = rig.config_path.read_bytes()
    for name, value, message in (
        ("bluetooth.settle_ms", "99999", "bluetooth.settle_ms: must be at most 10000"),
        ("input.hdmi_port", "two", "input.hdmi_port: must be a whole number, not 'two'"),
        ("bluetooth.no_such_setting", "1", "bluetooth.no_such_setting: unknown setting"),
    ):
        code, out, err = await rig.cli("config", name, value)
        assert (code, out, err) == (1, "", f"tcl-me-remo: {message}\n")
    assert rig.config_path.read_bytes() == before
    assert await rig.ok("config", "bluetooth.settle_ms") == "20\n"  # and the service still uses the old value


# --------------------------------------------------------------------------- after a restart


async def test_keys_arrive_after_a_restart_when_the_tv_subscribes_again(rig):
    tv = await start_paired(rig, android=14)
    await rig.ok("key", "HOME")
    await received(tv, HOME)

    assert await rig.stop() == 0
    await rig.start()
    # The new process registered the HID service anew, which bluetoothd tells the TV about;
    # an Android 14 TV then writes its three subscriptions again.  All three are waited for
    # here: what happens to a key that is due in between is the subject of the next test.
    await eventually(lambda: tv.listening, "the TV to subscribe again")
    await rig.ok("key", "HOME")
    await received(tv, HOME + HOME)

    bluetooth = (await rig.status())["bluetooth"]
    assert bluetooth["paired"] and bluetooth["ready"] and bluetooth["delivery"] == "bluez"
    assert rig.bluez.event_names().count("agent_registered") == 1  # it was not paired again


async def test_a_key_is_not_reported_as_sent_before_the_tv_subscribed_to_its_report(rig):
    tv = await start_paired(rig, android=14)
    assert await rig.stop() == 0
    tv.resubscribe_delay = 3600.0  # this test writes the TV's subscriptions itself, one at a time
    await rig.start()
    await tv.subscribe(only="c4_kbd")  # the first of three: the keyboard report.  HOME is in the second.

    # Waiting for the missing subscription would be right, and so would refusing.  The TV gets
    # round to the other two once the command has returned, or after a long while if it waits.
    sending = asyncio.ensure_future(rig.cli("key", "HOME"))
    try:
        await asyncio.wait([sending], timeout=20.0)
        await tv.subscribe()
        code, _out, _err = await sending
    finally:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
    if code == 0:
        # ENTER is in the report that was subscribed all along: once it has arrived, anything
        # sent before it has arrived too.
        await rig.ok("key", "ENTER")
        await eventually(lambda: tv.keys()[-2:] == ENTER, "ENTER to reach the TV")
        assert tv.keys() == HOME + ENTER, "'key HOME' answered 'sent HOME' for a key the TV never got"


async def test_a_tv_that_does_not_subscribe_again_is_reported_honestly_and_pairing_again_helps(rig):
    # Nothing will come from this TV after the restart, so the wait for it can be short.
    rig.write_settings({"bluetooth": {"resubscribe_wait": 0.5}})
    tv = await start_paired(rig, android=12)
    await rig.ok("key", "HOME")
    await received(tv, HOME)

    assert await rig.stop() == 0
    await rig.start()
    # Android 11-13 keep using the subscription they remember; bluetoothd has forgotten it and
    # drops every report.  Direct delivery is off in these tests, so the key cannot be sent,
    # and the service must say so instead of sending it into the void.
    code, out, err = await rig.cli("key", "HOME")
    assert code == 1 and out == ""
    assert err.startswith(f"tcl-me-remo: {NOT_SUBSCRIBED}") and "Pair the TV again" in err
    code, out, err = await rig.cli("off")
    assert code == 1 and err == ""
    assert f"failed: Bluetooth: {NOT_SUBSCRIBED}" in out

    bluetooth = (await rig.status())["bluetooth"]
    assert bluetooth["connected"] and not bluetooth["ready"] and not bluetooth["subscribed"]
    assert bluetooth["error"].startswith(NOT_SUBSCRIBED)
    assert f"problem: {NOT_SUBSCRIBED}" in await rig.ok("status")

    # What the message tells the user to do works.  (The old pairing was removed on the TV as well.)
    again = rig.bluez.add_device(TV_ADDRESS, name=TV_NAME, android=12)
    await rig.pair(again)
    assert await rig.ok("key", "HOME") == "sent HOME\n"
    await received(again, HOME)
    assert tv.keys() == HOME  # nothing reached the TV in between


# --------------------------------------------------------------------------- boot


async def test_a_boot_wakes_the_tv_and_is_handled_once(rig):
    # A first start within ten minutes of power-on is a boot and is acted on at once; a later
    # one is not (the service was just installed).  Which of the two this is depends on the
    # machine running the tests, so the boot is also announced the way the systemd hooks
    # announce events.  Either way it must be handled exactly once.
    #
    # The WAKE key is sent twice, five seconds apart: after a boot an Android 14 TV subscribes
    # again, and the first key may leave before its last subscription is in place.
    rig.write_settings(
        {
            "bluetooth": {"tv_address": TV_ADDRESS, "tv_name": TV_NAME},
            "power": {"on_repeat": 2, "repeat_gap": 5.0},
        }
    )
    tv = rig.bluez.add_device(TV_ADDRESS, name=TV_NAME, android=14, bonded=True)  # paired before this boot
    tv.auto_connect = True
    await rig.start(first_start_since_boot=True)

    out = await rig.ok("event", "boot")
    assert "ok: TV on (Bluetooth)" in out or "skipped: duplicate boot event ignored" in out
    status = await rig.status_when(lambda status: status["last_action"] and not status["busy"], "the boot to be over")
    boots = [record for record in status["history"] if record["trigger"] == "boot"]
    assert len(boots) == 1
    assert boots[0]["name"] == "tv_on" and boots[0]["ok"] and not boots[0]["skipped"]
    assert "Bluetooth: sent WAKE x2" in boots[0]["steps"]

    await eventually(lambda: tv.keys()[-2:] == WAKE, "WAKE to reach the TV")
    keys = tv.keys()
    assert set(keys) <= set(WAKE)  # never SLEEP, never anything else
    assert 1 <= keys.count(WAKE[0]) <= 2

    assert "skipped: duplicate boot event ignored" in await rig.ok("event", "boot")
    assert tv.keys() == keys
    assert (await rig.status())["history"] == status["history"]


# --------------------------------------------------------------------------- Wi-Fi assist


async def test_wifi_assist_is_available_and_fails_cleanly_when_no_tv_answers(rig):
    await rig.start()
    wifi = (await rig.status())["wifi"]
    assert wifi["available"] is True  # the default factory built the assist
    assert not (wifi["enabled"] or wifi["paired"] or wifi["pairing"])

    # The process uses the protocol's real ports, so there is no fake TV to talk to: this is
    # about the wiring and about failing well.  Should something answer on this machine, the
    # command still ends (the CLI and the assist bound every step) and still fails: the code
    # is made up.
    quiet = await nothing_listens("127.0.0.1", 6467)
    code, out, err = await rig.cli("wifi", "pair", "127.0.0.1", "--code", "123456")
    assert code == 1 and out == ""
    assert err.startswith("tcl-me-remo: ") and err.count("\n") == 1 and "Traceback" not in err
    if quiet and shutil.which("openssl"):
        assert "the TV at 127.0.0.1 refused the connection; check that it is on and is a Google TV" in err

    status = await rig.status()
    assert status["phase"] == "running"
    assert not (status["wifi"]["enabled"] or status["wifi"]["paired"] or status["wifi"]["pairing"])
    assert rig.settings()["wifi"]["enabled"] is False and rig.settings()["wifi"]["host"] == ""


# --------------------------------------------------------------------------- no service


async def test_the_cli_without_a_service_says_so_without_a_traceback(tmp_path, reaper):
    rig = Rig(tmp_path, reaper)
    async with rig.cleaned_up():
        for command in (["status"], ["off"], ["key", "HOME"], ["event", "suspend", "--budget", "3"]):
            code, out, err = await rig.cli(*command)
            assert code == 3 and out == ""
            assert err.startswith("tcl-me-remo: cannot reach the tcl-me-remo service at ")
            assert "Is the service running?" in err and "Traceback" not in err
        assert "WAKE" in await rig.ok("keys")  # what needs no service still works
