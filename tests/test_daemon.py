import asyncio
import contextlib
import gc
import json
import logging
import time

import pytest
from fake_logind import FakeLogind, eventually
from fakes import FakeRemote

from tcl_me_remo import config as C
from tcl_me_remo import daemon as daemon_module
from tcl_me_remo import dbus, installer, ipc
from tcl_me_remo import service as service_module
from tcl_me_remo.daemon import Daemon
from tcl_me_remo.service import Phase


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.config_path = tmp_path / "etc" / "config.json"
        self.socket_path = tmp_path / "run" / "control.sock"
        monkeypatch.setenv("TCL_ME_REMO_CONFIG", str(self.config_path))
        monkeypatch.setenv("TCL_ME_REMO_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("TCL_ME_REMO_RUNTIME_DIR", str(tmp_path / "run"))
        monkeypatch.setenv("TCL_ME_REMO_SOCKET_GROUP", "")
        monkeypatch.delenv("TCL_ME_REMO_SOCKET", raising=False)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        self.remote = FakeRemote()
        self.wifi = None
        self.uptime = 30.0
        self.kind = None
        self.kind_asked = 0  # how often the service asked whether the system is going down
        self.kind_asked_at = []  # and when (time.monotonic())
        self.daemon = None
        self.task = None
        self.options = {}  # further Daemon(...) arguments
        cfg = C.apply_patch(
            C.default_config(),
            {
                "input": {"method": "none"},
                "display": {"require_external": "no"},
                "power": {"on_repeat": 1},
            },
        )
        C.save(cfg, self.config_path)

    async def start(self):
        async def make_remote(bus, _cfg):
            self.bus = bus  # the service's own connection to the (private) system bus
            return self.remote

        async def shutdown_kind():
            self.kind_asked += 1
            self.kind_asked_at.append(time.monotonic())
            if isinstance(self.kind, Exception):  # systemd gave no answer
                raise self.kind
            return self.kind

        self.daemon = Daemon(
            make_remote=make_remote,
            make_wifi=lambda _cfg: self.wifi,
            uptime=lambda: self.uptime,
            shutdown_kind=shutdown_kind,
            watchdog_interval=0.05,
            **self.options,
        )
        self.task = asyncio.create_task(self.daemon.run())
        await asyncio.wait_for(self.daemon.ready.wait(), 5)
        return self.daemon

    async def stop(self):
        if self.task is None:
            return None
        self.daemon.request_stop()
        code = await asyncio.wait_for(self.task, 10)
        self.task = None
        return code

    async def call(self, method, params=None):
        return await ipc.call(self.socket_path, method, params)


@pytest.fixture
async def rig(tmp_path, monkeypatch, private_bus):
    fake = FakeLogind()
    await fake.start()
    rig = Rig(tmp_path, monkeypatch)
    rig.logind = fake
    yield rig
    await rig.stop()
    await fake.stop()


async def test_boot_event_fires_once_per_boot(rig):
    await rig.start()
    await eventually(lambda: rig.remote.key_names() == ["WAKE"])
    status = await rig.call("status")
    assert status["last_action"]["trigger"] == "boot"
    assert await rig.stop() == 0
    assert not rig.socket_path.exists()

    # a restart of the service is not a boot
    rig.remote.keys.clear()
    await rig.start()
    await asyncio.sleep(0.2)
    assert rig.remote.keys == []


async def test_late_first_start_is_not_a_boot(rig):
    rig.uptime = 5000.0  # e.g. the plugin was just installed
    await rig.start()
    await asyncio.sleep(0.2)
    assert rig.remote.keys == []
    assert (await rig.call("status"))["last_action"] is None


async def test_suspend_and_resume_through_logind(rig):
    rig.uptime = 5000.0
    await rig.start()
    assert rig.logind.held("sleep") == 1
    rig.logind.prepare_for_sleep(True)
    await eventually(lambda: rig.remote.key_names() == ["SLEEP"])
    await eventually(lambda: rig.logind.held("sleep") == 0)  # only now may the machine sleep
    assert rig.daemon.service.phase == Phase.DOWN

    rig.logind.prepare_for_sleep(False)
    await eventually(lambda: rig.remote.key_names() == ["SLEEP", "WAKE"])
    assert rig.logind.held("sleep") == 1
    assert rig.daemon.service.phase == Phase.RUNNING


async def test_poweroff_and_reboot_through_logind(rig):
    rig.uptime = 5000.0
    await rig.start()
    rig.logind.prepare_for_shutdown(True, "reboot")
    await eventually(lambda: rig.logind.held("shutdown") == 0)
    assert rig.remote.keys == []  # reboot keeps the TV on by default

    rig.logind.prepare_for_shutdown(False)
    await eventually(lambda: rig.daemon.service.phase == Phase.RUNNING)
    await eventually(lambda: rig.logind.held("shutdown") == 1)
    rig.logind.prepare_for_shutdown(True, "power-off")
    await eventually(lambda: rig.remote.key_names() == ["SLEEP"])


async def test_stop_during_system_shutdown_turns_tv_off(rig):
    rig.uptime = 5000.0
    await rig.start()
    # logind without metadata cannot say reboot or power-off: nothing is sent yet
    rig.logind.prepare_for_shutdown(True)
    await eventually(lambda: rig.logind.held("shutdown") == 0)
    assert rig.remote.keys == []
    # ...then systemd stops the unit and the job queue shows a power-off
    rig.kind = "shutdown"
    assert await rig.stop() == 0
    assert rig.remote.key_names() == ["SLEEP"]


async def test_plain_service_stop_leaves_tv_alone(rig):
    rig.uptime = 5000.0
    await rig.start()
    assert await rig.stop() == 0
    assert rig.remote.keys == []


async def test_shutdown_seen_by_both_routes_acts_once(rig):
    rig.uptime = 5000.0
    await rig.start()
    rig.logind.prepare_for_shutdown(True, "power-off")
    await eventually(lambda: rig.remote.key_names() == ["SLEEP"])
    rig.kind = "shutdown"
    await rig.stop()
    assert rig.remote.key_names() == ["SLEEP"]


async def test_watchdog_notices_a_resume_nobody_announced(rig):
    rig.uptime = 5000.0
    daemon = await rig.start()
    offset = {"value": daemon.service._sleep_offset()}
    daemon.service._sleep_offset = lambda: offset["value"]
    daemon.service._offset_seen = offset["value"]
    await asyncio.sleep(0.15)
    assert rig.remote.keys == []
    offset["value"] += 120.0  # the machine slept for two minutes
    await eventually(lambda: rig.remote.key_names() == ["WAKE"])
    await asyncio.sleep(0.2)
    assert rig.remote.key_names() == ["WAKE"]  # handled once, not on every tick


async def test_settings_are_persisted_and_pairing_callback_is_wired(rig):
    rig.uptime = 5000.0
    daemon = await rig.start()
    await rig.call("set_config", {"patch": {"input": {"hdmi_port": 3}}})
    assert json.loads(rig.config_path.read_text())["input"]["hdmi_port"] == 3
    await rig.remote.on_paired("11:22:33:44:55:66", "Den TV")
    assert daemon.service.cfg.bluetooth.tv_address == "11:22:33:44:55:66"
    assert json.loads(rig.config_path.read_text())["bluetooth"]["tv_name"] == "Den TV"
    logs = (await rig.call("logs", {"lines": 50}))["lines"]
    assert any("starting" in line for line in logs)


async def test_stopping_closes_the_wifi_assist(rig):
    class Assist:
        closed = 0

        def status(self):
            return {"available": True, "paired": False, "pairing": True}

        async def close(self):
            self.closed += 1

    rig.wifi = Assist()
    await rig.start()
    assert (await rig.call("status"))["wifi"]["pairing"] is True
    assert await rig.stop() == 0
    assert rig.wifi.closed == 1  # a pairing still waiting for its code does not outlive the service


async def test_an_assist_that_fails_to_close_does_not_keep_the_service_from_stopping(rig):
    class Assist:
        def status(self):
            return {"available": True, "paired": False, "pairing": False}

        async def close(self):
            raise RuntimeError("socket already gone")

    rig.wifi = Assist()
    await rig.start()
    assert await rig.stop() == 0
    assert not rig.socket_path.exists()  # the rest of the shutdown still ran


async def test_damaged_config_still_starts(rig):
    rig.uptime = 5000.0
    rig.config_path.write_text("{ definitely not json")
    await rig.start()
    status = await rig.call("status")
    assert status["config_problems"] and "not valid JSON" in status["config_problems"][0]


async def test_losing_the_system_bus_ends_the_service_with_an_error(rig, kill_private_bus):
    # systemd restarts on failure; a process that silently lost BlueZ and logind is useless.
    stop_errors = []

    class Remote(FakeRemote):
        async def stop(self):
            # Like the real transport: it takes its objects off the bus on the way out,
            # best effort, whatever state the bus is in.
            try:
                await dbus.call(rig.bus, "org.bluez", "/org/bluez/hci0", "org.bluez.GattManager1", "X", timeout=30)
            except Exception as exc:  # noqa: BLE001
                stop_errors.append(exc)

    class Records(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    rig.remote = Remote()
    rig.uptime = 5000.0
    await rig.start()
    seen = Records()
    logging.getLogger().addHandler(seen)  # after the start: the service sets logging up afresh
    try:
        kill_private_bus()
        code = await asyncio.wait_for(rig.task, 10)
        rig.task = None
        assert code == 1
        assert rig.remote.keys == []  # no TV action is attempted on the way out

        # The exit is quiet: one line that says what happened.  A call on the dead bus fails
        # at once with an OSError, and leaves nothing behind for the garbage collector to
        # report as "Future exception was never retrieved" with a traceback.
        assert len(stop_errors) == 1 and isinstance(stop_errors[0], OSError)
        gc.collect()
        await asyncio.sleep(0)
        loud = [record.getMessage() for record in seen.records if record.levelno >= logging.WARNING]
        assert len(loud) == 1 and loud[0].startswith("lost the connection to the system D-Bus (the bus closed it)")
    finally:
        logging.getLogger().removeHandler(seen)
        # The fake logind of this test lost its bus as well; take note of that here, or the
        # library's "disconnected" future is reported when the test run ends.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(rig.logind.bus.wait_for_disconnect(), 5)


async def test_calls_on_a_lost_bus_fail_at_once_and_leave_no_failure_behind(private_bus, kill_private_bus):
    """The D-Bus library keeps the failure of a write in a future nobody looks at; it used to
    come back as "Future exception was never retrieved", with a traceback, when the service
    exited after losing the bus."""
    loop = asyncio.get_running_loop()

    def unseen_failures():
        # What asyncio will report when the garbage collector gets to them: futures of this
        # loop that hold an exception nobody has asked for.
        gc.collect()
        return [
            future
            for future in gc.get_objects()
            if isinstance(future, asyncio.Future)
            and future.get_loop() is loop
            and future.done()
            and not future.cancelled()
            and future._log_traceback
        ]

    bus = await dbus.connect_system_bus()
    name_of = (bus, dbus.DBUS_NAME, dbus.DBUS_PATH, dbus.DBUS_NAME, "GetNameOwner", "s", [dbus.DBUS_NAME])
    assert await dbus.call(*name_of) == [dbus.DBUS_NAME]

    kill_private_bus()
    # Our side has not noticed yet: this write is what finds out.
    with pytest.raises(OSError):
        await dbus.call(*name_of, timeout=30)
    with pytest.raises(Exception):  # noqa: B017 - the library says how the connection ended, in its own words
        await asyncio.wait_for(bus.wait_for_disconnect(), 10)
    # And once it is known to be gone (what the clean-up on the way out runs into).
    with pytest.raises(OSError):
        await dbus.call(*name_of, timeout=30)
    await asyncio.sleep(0)
    assert unseen_failures() == []


async def test_a_suspend_right_behind_a_resume_is_handled_after_it(rig):
    """The machine wakes and is sent back to sleep at once.  Reporting the resume takes a
    round trip to logind first; the suspend must not overtake it, or "TV on" is sent when
    the "TV off" is through and the machine asleep."""
    rig.uptime = 5000.0
    await rig.start()
    service = rig.daemon.service
    offset = {"value": service._sleep_offset()}
    service._sleep_offset = lambda: offset["value"]
    service._offset_seen = offset["value"]
    rig.remote.state.connected = rig.remote.state.ready = True
    rig.logind.prepare_for_sleep(True)
    await eventually(lambda: rig.logind.held("sleep") == 0)
    assert rig.remote.key_names() == ["SLEEP"]

    offset["value"] += 100.0  # it slept
    rig.logind.prepare_for_sleep(False)
    rig.logind.prepare_for_sleep(True)
    await eventually(lambda: rig.remote.key_names().count("SLEEP") == 2)
    await eventually(lambda: service.phase == Phase.DOWN and rig.logind.held("sleep") == 0)
    await asyncio.sleep(0.5)  # nothing follows
    assert rig.remote.key_names()[-1] == "SLEEP" and service.phase == Phase.DOWN
    assert (await rig.call("status"))["busy"] == ""


@pytest.mark.parametrize("by_design", [True, False])
async def test_the_stop_signal_tries_tv_off_again_when_the_link_was_down_by_design(rig, by_design):
    """logind's route had three seconds for a TV that had to be called first.  When systemd
    then stops the service there is more time, but it is only worth taking if the link was
    down because the settings have it down."""
    cfg, _ = C.load(rig.config_path)
    patch = {"bluetooth": {"off_connect_timeout": 0.3, "reconnect_on_drop": not by_design}}
    C.save(C.apply_patch(cfg, patch), rig.config_path)
    rig.remote = FakeRemote(connect_after=None)
    rig.uptime = 5000.0
    await rig.start()
    rig.logind.prepare_for_shutdown(True, "power-off")
    await eventually(lambda: rig.logind.held("shutdown") == 0)
    record = (await rig.call("status"))["last_action"]
    assert record["trigger"] == "shutdown" and not record["ok"] and rig.remote.keys == []

    rig.remote.connect_after = 0.0  # the TV would answer now
    rig.kind = "shutdown"
    assert await rig.stop() == 0
    if by_design:
        assert rig.remote.key_names() == ["SLEEP"]
        assert rig.remote.ensure_calls == [0.3, pytest.approx(daemon_module.STOP_BUDGET - 0.08 - 0.4 - 0.1)]
    else:
        assert rig.remote.keys == [] and rig.remote.ensure_calls == [0.3]


async def test_a_down_event_the_machine_did_not_follow_is_forgotten_after_a_while(rig, monkeypatch):
    """"tcl-me-remo event suspend" typed by hand is a real event to the service.  The next
    genuine suspend must not be taken for its duplicate for ever."""
    monkeypatch.setattr(service_module, "STALE_DOWN_SECONDS", 0.5)
    rig.uptime = 5000.0
    await rig.start()
    rig.remote.state.connected = rig.remote.state.ready = True
    assert (await rig.call("event", {"name": "suspend"}))["ok"]
    assert rig.remote.key_names() == ["SLEEP"]
    await eventually(lambda: rig.daemon.service.phase == Phase.RUNNING)  # the watchdog saw to it
    assert rig.remote.resumes >= 1  # and the link the down action closed may come back
    rig.logind.prepare_for_sleep(True)
    await eventually(lambda: rig.remote.key_names() == ["SLEEP", "SLEEP"])


@pytest.mark.parametrize("off_key", ["SLEEP", "POWER"])
async def test_a_shutdown_that_is_merely_slow_is_not_taken_for_one_that_did_not_happen(rig, monkeypatch, off_key):
    """Still running two minutes after logind announced the power-off: a stop job ahead of
    ours hangs.  The rule above used to call the down event off; the stop signal then sent
    the off key a second time, and an off key that is a toggle turned the TV back on while
    the machine powered off."""
    monkeypatch.setattr(service_module, "STALE_DOWN_SECONDS", 0.5)
    cfg, _ = C.load(rig.config_path)
    C.save(C.apply_patch(cfg, {"power": {"off_key": off_key}}), rig.config_path)
    rig.uptime = 5000.0
    await rig.start()
    service = rig.daemon.service
    rig.remote.state.connected = rig.remote.state.ready = True
    rig.kind = "shutdown"  # what systemd answers from the moment the shutdown has begun
    rig.logind.prepare_for_shutdown(True, "power-off")
    await eventually(lambda: rig.remote.key_names() == [off_key] and service.phase == Phase.DOWN, timeout=10)
    resumes = rig.remote.resumes

    # The watchdog comes to the point where it would give the down event up, asks, and
    # leaves it alone.  And again one such period later: not at its next round, which would
    # start two systemctl processes every two seconds for as long as the stop job hangs.
    await eventually(lambda: rig.kind_asked >= 3, timeout=20)
    assert service.phase == Phase.DOWN and rig.remote.resumes == resumes
    assert rig.remote.key_names() == [off_key]
    asked_at = list(rig.kind_asked_at)
    gaps = [later - earlier for earlier, later in zip(asked_at, asked_at[1:], strict=False)]
    assert min(gaps) >= service_module.STALE_DOWN_SECONDS, gaps  # a round of the watchdog is 0.05 s here

    assert await rig.stop() == 0  # the stop signal: the second report of the same shutdown
    assert rig.remote.key_names() == [off_key]


async def test_silence_from_systemd_is_not_taken_for_a_machine_that_stayed_up(rig, monkeypatch):
    """The same slow shutdown, with a systemd that does not answer when the watchdog asks
    (systemctl ran into its time-out).  Silence used to count as "not shutting down": the
    down event was called off, and the stop signal sent the toggle a second time."""
    monkeypatch.setattr(service_module, "STALE_DOWN_SECONDS", 0.5)
    cfg, _ = C.load(rig.config_path)
    C.save(C.apply_patch(cfg, {"power": {"off_key": "POWER"}}), rig.config_path)
    rig.uptime = 5000.0
    await rig.start()
    service = rig.daemon.service
    rig.remote.state.connected = rig.remote.state.ready = True
    rig.kind = daemon_module.NoAnswer("systemctl is-system-running gave no answer")
    rig.logind.prepare_for_shutdown(True, "power-off")
    await eventually(lambda: rig.remote.key_names() == ["POWER"] and service.phase == Phase.DOWN, timeout=10)
    resumes = rig.remote.resumes

    await eventually(lambda: rig.kind_asked >= 3 or service.phase != Phase.DOWN, timeout=20)
    assert service.phase == Phase.DOWN and rig.remote.resumes == resumes  # left alone, and asked again
    asked_at = list(rig.kind_asked_at)  # a full period later each time, not at the watchdog's next round
    gaps = [later - earlier for earlier, later in zip(asked_at, asked_at[1:], strict=False)]
    assert min(gaps) >= service_module.STALE_DOWN_SECONDS, gaps

    rig.kind = "shutdown"  # by the time of the stop signal systemd answers
    assert await rig.stop() == 0
    assert rig.remote.key_names() == ["POWER"]


async def test_a_down_event_stands_for_as_long_as_systemd_is_silent_and_no_longer(rig, monkeypatch):
    """The price of the rule above, on a machine that did stay up: while systemd says nothing
    the handled suspend keeps counting.  The first answer that the system is not shutting
    down ends that, as before."""
    monkeypatch.setattr(service_module, "STALE_DOWN_SECONDS", 0.5)
    rig.uptime = 5000.0
    await rig.start()
    service = rig.daemon.service
    rig.remote.state.connected = rig.remote.state.ready = True
    rig.kind = daemon_module.NoAnswer("systemctl is-system-running gave no answer")
    assert (await rig.call("event", {"name": "suspend"}))["ok"]  # by hand; no suspend follows
    await eventually(lambda: rig.kind_asked >= 2 or service.phase != Phase.DOWN, timeout=10)
    assert service.phase == Phase.DOWN and rig.remote.resumes == 0

    rig.kind = None  # systemd answers: not shutting down
    await eventually(lambda: service.phase == Phase.RUNNING, timeout=10)
    assert rig.remote.resumes == 1
    rig.logind.prepare_for_sleep(True)
    await eventually(lambda: rig.remote.key_names() == ["SLEEP", "SLEEP"], timeout=10)


async def test_a_stop_without_an_answer_from_systemd_is_a_plain_stop(rig):
    # At stop time silence is still "the service is merely being stopped": nothing is sent.
    rig.uptime = 5000.0
    await rig.start()
    rig.remote.state.connected = rig.remote.state.ready = True
    rig.kind = daemon_module.NoAnswer("systemctl is-system-running gave no answer")
    assert await rig.stop() == 0
    assert rig.kind_asked == 1 and rig.remote.keys == []


async def test_a_systemctl_that_gave_no_answer_is_told_from_one_that_said_no():
    """``system.shutdown_kind`` returns None for both.  The service's own question does not."""
    import inspect

    def systemctl(state, jobs=(0, "")):
        async def run(argv, timeout):
            return state if "is-system-running" in argv else jobs

        return run

    ask = daemon_module.shutdown_kind
    assert inspect.signature(Daemon).parameters["shutdown_kind"].default is ask
    for silence in ((-1, ""), (-1, "\n")):  # could not be run, or given up on after its time-out
        with pytest.raises(daemon_module.NoAnswer):
            await ask(systemctl(silence))
    assert await ask(systemctl((0, "running\n"))) is None
    assert await ask(systemctl((1, "degraded\n"))) is None
    assert await ask(systemctl((1, "stopping\n"), (0, "7 reboot.target start waiting\n"))) == "reboot"
    assert await ask(systemctl((1, "stopping\n"), (0, "7 poweroff.target start waiting\n"))) == "shutdown"
    # Only the first question decides whether there is an answer: "stopping" without the job
    # list is a shutdown of an unknown kind, as it was.
    assert await ask(systemctl((1, "stopping\n"), (-1, ""))) == "shutdown"


async def test_systemd_is_not_asked_about_a_down_event_that_is_not_old(rig):
    rig.uptime = 5000.0
    await rig.start()
    rig.remote.state.connected = rig.remote.state.ready = True
    rig.logind.prepare_for_sleep(True)
    await eventually(lambda: rig.remote.key_names() == ["SLEEP"], timeout=10)
    await eventually(lambda: rig.daemon.service.phase == Phase.DOWN, timeout=10)
    await asyncio.sleep(0.5)  # ten rounds of the watchdog
    assert rig.kind_asked == 0 and rig.daemon.service.phase == Phase.DOWN


@pytest.mark.parametrize("by_design", [True, False])
async def test_the_stop_signal_as_the_only_route_uses_its_time_when_the_link_is_down_by_design(rig, by_design):
    """A logind that cannot tell a reboot from a power-off leaves the shutdown to the stop
    signal.  That is then the only attempt, and with the link down because the settings have
    it down it waits for the TV for as long as its 8 s allow, not for ``off_connect_timeout``."""
    cfg, _ = C.load(rig.config_path)
    patch = {"bluetooth": {"off_connect_timeout": 0.3, "reconnect_on_drop": not by_design}}
    C.save(C.apply_patch(cfg, patch), rig.config_path)
    rig.remote = FakeRemote(connect_after=1.0)  # the TV answers, but not within off_connect_timeout
    rig.uptime = 5000.0
    await rig.start()
    rig.logind.prepare_for_shutdown(True)  # no metadata: the kind is not known yet
    await eventually(lambda: rig.logind.held("shutdown") == 0, timeout=10)
    assert rig.remote.keys == [] and rig.remote.ensure_calls == []

    rig.kind = "shutdown"
    assert await rig.stop() == 0
    if by_design:
        assert rig.remote.key_names() == ["SLEEP"]
        assert rig.remote.ensure_calls == [pytest.approx(daemon_module.STOP_BUDGET - 0.08 - 0.4 - 0.1)]
    else:
        assert rig.remote.keys == [] and rig.remote.ensure_calls == [0.3]


async def test_a_removed_plugin_is_looked_into_some_time_after_start(rig):
    # Decky's uninstall hook leaves a note and a timer; a reboot discards the timer.  The
    # service then has its installer decide (outside the service, which that may remove).
    rig.uptime = 5000.0
    commands = []

    async def run_command(argv, timeout):
        commands.append(argv)
        return 0, ""

    rig.options = {"run_command": run_command, "orphan_check_delay": 0.05}
    state = rig.tmp_path / "state"
    state.mkdir()
    await rig.start()
    await asyncio.sleep(0.25)
    assert commands == []  # no note: nothing is started, ever
    assert await rig.stop() == 0

    (state / installer.REMOVED_MARKER).write_text(json.dumps({"plugins_dir": "/home/deck/homebrew/plugins", "time": 0}))
    await rig.start()
    await eventually(lambda: len(commands) == 1)
    assert commands[0][0] == "systemd-run" and commands[0][-1] == "uninstall-if-orphaned"
    assert commands[0] == installer.orphan_check_command(state)
    await asyncio.sleep(0.25)
    assert len(commands) == 1  # once per start
    assert rig.remote.keys == []  # and the TV is not involved


async def test_no_system_bus_is_a_clean_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp_path}/no-such-bus")
    monkeypatch.setenv("TCL_ME_REMO_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("TCL_ME_REMO_RUNTIME_DIR", str(tmp_path / "run"))
    assert await Daemon().run() == 1


# --------------------------------------------------------------------------- a damaged settings file


async def test_a_settings_file_with_a_slip_in_it_is_kept_and_the_service_runs(rig):
    """One comma too many used to cost every setting as soon as any setting was changed."""
    rig.uptime = 5000.0
    good = rig.config_path.read_text()
    slip = good.replace('"enabled": true,', '"enabled": true,,', 1)
    assert slip != good
    rig.config_path.write_text(slip)
    kept = rig.config_path.with_name("config.json.broken")

    await rig.start()
    status = await rig.call("status")
    assert len(status["config_problems"]) == 1 and str(kept) in status["config_problems"][0]
    assert "not valid JSON" in status["config_problems"][0]
    assert kept.read_text() == slip and not rig.config_path.exists()  # out of the way before anything is saved

    # The first setting that is changed goes to a new file.  The user's file is untouched,
    # and the status keeps saying where it is.
    await rig.call("set_config", {"patch": {"events": {"reboot_tv_off": True}}})
    await rig.remote.on_paired("11:22:33:44:55:66", "Den TV")
    assert json.loads(rig.config_path.read_text())["events"]["reboot_tv_off"] is True
    assert kept.read_text() == slip
    assert (await rig.call("status"))["config_problems"] == status["config_problems"]

    # Until the service is started again: the settings file is in order by then.
    assert await rig.stop() == 0
    await rig.start()
    assert (await rig.call("status"))["config_problems"] == []
    assert kept.read_text() == slip


async def test_a_restart_with_nothing_changed_still_says_where_the_settings_went(rig):
    """The service starts at boot, so the notice used to live for one boot: after the next
    one it ran on defaults, with the TV unpaired, and status, doctor and the panel were silent."""
    rig.uptime = 5000.0
    slip = rig.config_path.read_text().replace('"enabled": true,', '"enabled": true,,', 1)
    rig.config_path.write_text(slip)
    kept = rig.config_path.with_name("config.json.broken")

    await rig.start()
    first = (await rig.call("status"))["config_problems"]
    assert len(first) == 1 and "not valid JSON" in first[0]
    assert await rig.stop() == 0

    for _ in range(2):  # a reboot, and another one
        await rig.start()
        problems = (await rig.call("status"))["config_problems"]
        assert len(problems) == 1 and str(kept) in problems[0] and "default settings" in problems[0]
        assert kept.read_text() == slip and not rig.config_path.exists()
        assert await rig.stop() == 0

    # It stays for the run in which the settings are made anew ...
    await rig.start()
    await rig.call("set_config", {"patch": {"events": {"reboot_tv_off": True}}})
    assert (await rig.call("status"))["config_problems"] == problems
    assert await rig.stop() == 0
    # ... and then there is a settings file again, and nothing to say.
    await rig.start()
    assert (await rig.call("status"))["config_problems"] == []
    assert kept.read_text() == slip
    assert await rig.stop() == 0


@pytest.mark.parametrize(
    "content, set_aside",
    [
        (b'{"input": {"hdmi_port": NaN}}', False),
        (b'{"input": {"hdmi_port": 1e999}, "display": {"wait_seconds": 1' + b"0" * 400 + b"}}", False),
        ('{"input": {"macro": "DOWN*²"}}'.encode(), False),
        ('{"bluetooth": {"tv_name": "Caf\xe9"}}'.encode("latin-1"), True),
        ('{"enabled": true}'.encode("utf-16"), True),
        (b"[" * 100_000, True),
    ],
    ids=["nan", "infinity-and-a-huge-number", "a-digit-int-does-not-take", "latin-1", "utf-16", "nested-too-deeply"],
)
async def test_the_service_starts_whatever_the_settings_file_holds(rig, content, set_aside):
    """Each of these used to end the process at start, again at every restart, with no
    control socket through which the setting could have been put right."""
    rig.uptime = 5000.0
    rig.config_path.write_bytes(content)
    await rig.start()
    status = await rig.call("status")
    assert status["config_problems"] and status["input"]["hdmi_port"] == 1
    assert rig.config_path.with_name("config.json.broken").exists() is set_aside
    await rig.call("set_config", {"patch": {"input": {"hdmi_port": 2}}})  # and it can be put right
    assert json.loads(rig.config_path.read_text())["input"]["hdmi_port"] == 2
    assert await rig.stop() == 0


async def test_a_loader_that_raises_does_not_stop_the_service(rig, monkeypatch):
    def broken(path):
        raise RuntimeError("a check that tripped over a value")

    monkeypatch.setattr(C, "load_for_service", broken)
    original = rig.config_path.read_text()
    rig.uptime = 5000.0
    await rig.start()
    problems = (await rig.call("status"))["config_problems"]
    assert len(problems) == 1 and "could not be loaded" in problems[0] and "RuntimeError" in problems[0]
    # Running on defaults, so the file is moved out of the way of the next save, and said to be.
    kept = rig.config_path.with_name("config.json.broken")
    assert kept.read_text() == original and str(kept) in problems[0]
    await rig.call("set_config", {"patch": {"input": {"hdmi_port": 2}}})
    assert (await rig.call("status"))["config_problems"] == problems
    assert await rig.stop() == 0


# --------------------------------------------------------------------------- one service at a time


async def test_a_second_daemon_does_not_start_while_the_first_one_answers(rig):
    """``sudo tcl-me-remo daemon`` by hand, with the service running."""
    rig.uptime = 5000.0
    await rig.start()
    touched = []

    async def make_remote(bus, _cfg):
        touched.append("bluetooth")
        return FakeRemote()

    second = Daemon(make_remote=make_remote, make_wifi=lambda _cfg: None, uptime=lambda: 5000.0)
    before = rig.config_path.read_text()
    assert await asyncio.wait_for(second.run(), 20) == 1
    assert touched == []  # it stopped before it registered a second remote with BlueZ
    assert rig.config_path.read_text() == before
    # The first one is still reachable, during and after.
    assert rig.socket_path.exists() and (await rig.call("status"))["phase"] == "running"
    assert await rig.stop() == 0
    assert not rig.socket_path.exists()
