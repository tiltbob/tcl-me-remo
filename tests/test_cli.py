import asyncio
import io
import json

import pytest
from fakes import FakeRemote, FakeWifi

from tcl_me_remo import cli, ipc
from tcl_me_remo import config as C
from tcl_me_remo.service import Service


@pytest.fixture
async def running(tmp_path):
    """A real Service behind a real control socket, driven through the CLI's sync client."""
    cfg = C.apply_patch(
        C.default_config(),
        {"input": {"macro": "HOME DOWN*{port} OK", "delay_after_wake": 0.0}, "display": {"require_external": "no"}},
    )
    remote, wifi = FakeRemote(), FakeWifi(paired=False)
    service = Service(cfg, remote=remote, wifi=wifi, sys_root=tmp_path, recent_logs=lambda n: ["a", "b", "c"][-n:])
    server = ipc.IpcServer(tmp_path / "control.sock", service.rpc, group=None)
    await server.start()

    async def run_cli(*argv, ask=lambda prompt: ""):
        out, err = io.StringIO(), io.StringIO()

        def call(method, params=None, *, timeout=30.0):
            return ipc.call_sync(server.path, method, params, timeout=timeout)

        code = await asyncio.to_thread(lambda: cli.run(list(argv), call=call, out=out, err=err, ask=ask))
        return code, out.getvalue(), err.getvalue()

    run_cli.service, run_cli.remote, run_cli.wifi, run_cli.server = service, remote, wifi, server
    yield run_cli
    await server.stop()


async def test_on_off_input_key_macro(running):
    code, out, _ = await running("on")
    assert code == 0 and "ok: TV on (Bluetooth), input switched (Bluetooth macro)" in out
    assert running.remote.key_names() == ["WAKE", "WAKE", "HOME", "DOWN", "OK"]

    running.remote.keys.clear()
    code, out, _ = await running("on", "--no-input")
    assert code == 0 and running.remote.key_names() == ["WAKE", "WAKE"]

    code, out, _ = await running("off")
    assert code == 0 and "TV off" in out

    code, out, _ = await running("input")
    assert code == 0 and "input switched" in out

    running.remote.keys.clear()
    code, out, _ = await running("key", "home")
    assert (code, out.strip()) == (0, "sent HOME")
    code, out, _ = await running("key", "OK", "--hold", "1.5")
    assert running.remote.keys[-1] == ("OK", 1.5)
    code, _, err = await running("key", "BANANA")
    assert code == 1 and "unknown key" in err

    running.remote.keys.clear()
    code, out, _ = await running("macro", "UP*2", "OK")
    assert code == 0 and running.remote.key_names() == ["UP", "UP", "OK"]
    code, out, _ = await running("macro", "BOGUS")
    assert code == 1 and "failed:" in out


async def test_status_plain_and_json(running):
    code, out, _ = await running("status")
    assert code == 0
    assert "automatic actions: on" in out
    assert "tv: Living Room TV" in out
    assert "idle (connects when needed)" in out
    assert "macro: HOME DOWN*1 OK" in out
    code, out, _ = await running("status", "--json")
    assert json.loads(out)["bluetooth"]["adapter"] == "hci0"


async def test_config_show_get_set(running):
    code, out, _ = await running("config")
    assert code == 0 and "input.hdmi_port = 1" in out and 'power.on_key = "WAKE"' in out
    code, out, _ = await running("config", "input.hdmi_port")
    assert out.strip() == "1"
    code, out, _ = await running("config", "input")
    assert "input.method = \"macro\"" in out
    code, out, _ = await running("config", "input.hdmi_port", "3")
    assert code == 0 and out.strip() == "input.hdmi_port = 3"
    assert running.service.cfg.input.hdmi_port == 3
    code, out, _ = await running("config", "events.reboot_tv_off", "true")
    assert running.service.cfg.events.reboot_tv_off is True
    # text settings are taken verbatim even when they look like numbers
    code, out, _ = await running("config", "bluetooth.device_name", "1234")
    assert code == 0 and running.service.cfg.bluetooth.device_name == "1234"
    code, _, err = await running("config", "input.hdmi_port", "9")
    assert code == 1 and "input.hdmi_port" in err
    code, _, err = await running("config", "no.such.thing")
    assert code == 1 and "unknown setting" in err


async def test_event_and_logs(running):
    code, out, _ = await running("event", "suspend", "--simulate", "--budget", "3")
    assert code == 0 and "TV off" in out
    code, out, _ = await running("event", "resume")
    assert code == 0 and "skipped: duplicate" in out
    code, out, _ = await running("logs", "-n", "2")
    assert out.split() == ["b", "c"]

    running.remote.keys.clear()
    code, out, _ = await running("event", "suspend", "--simulate", "--no-wait")
    assert (code, out.strip()) == (0, "queued: suspend")
    for _ in range(100):
        if running.remote.key_names() == ["SLEEP"]:
            break
        await asyncio.sleep(0.02)
    assert running.remote.key_names() == ["SLEEP"]  # the action ran although nobody waited for it


async def test_wifi_commands(running):
    code, out, _ = await running("wifi", "discover")
    assert code == 0 and "192.168.1.50\tLiving Room TV" in out
    code, out, err = await running("wifi", "pair", "192.168.1.50", ask=lambda prompt: " A1B2C3 \n")
    assert code == 0, err
    assert running.service.cfg.wifi.enabled and running.service.cfg.wifi.host == "192.168.1.50"
    code, out, _ = await running("wifi", "unpair")
    assert code == 0 and running.wifi.paired is False
    code, _, err = await running("wifi", "pair", "192.168.1.50", "--code", "WRONG1")
    assert code == 1 and "wrong code" in err
    assert running.wifi.pairing_host == ""  # a failed attempt is cancelled, not left dangling
    code, out, _ = await running("wifi")
    assert code == 2


async def test_forget_connect_disconnect(running):
    code, out, _ = await running("connect")
    assert code == 0 and out.strip() == "connected"
    code, out, _ = await running("disconnect")
    assert code == 0
    code, out, _ = await running("forget")
    assert code == 0 and running.remote.forgotten == 1


def test_keys_listing_needs_no_service():
    out = io.StringIO()
    assert cli.run(["keys"], out=out) == 0
    assert "WAKE" in out.getvalue() and "KEYCODE_SLEEP" in out.getvalue()


def test_no_command_prints_help():
    out = io.StringIO()
    assert cli.run([], out=out) == 2
    assert "usage: tcl-me-remo" in out.getvalue()


def test_service_unavailable_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("TCL_ME_REMO_SOCKET", str(tmp_path / "nope.sock"))
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["status"], out=out, err=err) == 3
    assert "Is the service running?" in err.getvalue()
