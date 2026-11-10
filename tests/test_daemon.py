import asyncio
import json

import pytest
from fake_logind import FakeLogind, eventually
from fakes import FakeRemote

from tcl_me_remo import config as C
from tcl_me_remo import ipc
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
        self.daemon = None
        self.task = None
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
        async def make_remote(_bus, _cfg):
            return self.remote

        async def shutdown_kind():
            return self.kind

        self.daemon = Daemon(
            make_remote=make_remote,
            make_wifi=lambda _cfg: self.wifi,
            uptime=lambda: self.uptime,
            shutdown_kind=shutdown_kind,
            watchdog_interval=0.05,
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
    rig.uptime = 5000.0
    await rig.start()
    kill_private_bus()
    code = await asyncio.wait_for(rig.task, 10)
    rig.task = None
    assert code == 1
    assert rig.remote.keys == []  # no TV action is attempted on the way out


async def test_no_system_bus_is_a_clean_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp_path}/no-such-bus")
    monkeypatch.setenv("TCL_ME_REMO_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("TCL_ME_REMO_RUNTIME_DIR", str(tmp_path / "run"))
    assert await Daemon().run() == 1
