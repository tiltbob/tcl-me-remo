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
        {
            "input": {"method": "macro", "preset": "custom", "macro": "HOME DOWN*{port} OK", "delay_after_wake": 0.0},
            "display": {"require_external": "no"},
            "power": {"on_repeat": 2, "repeat_gap": 0.1},
        },
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
    code, out, _ = await running("input", "--via", "macro")
    assert code == 0 and "input switched (Bluetooth macro)" in out
    code, out, _ = await running("input", "--via", "wifi")
    assert code == 1 and "failed:" in out  # the Wi-Fi assist is not set up in this test

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


def test_status_says_whether_the_link_is_ready_and_how_keys_are_delivered():
    def bluetooth_line(**state):
        out = io.StringIO()
        link = {"available": True, "powered": True, "paired": True, "adapter": "hci0", "tv_name": "TV", **state}
        cli._print_status({"version": "x", "enabled": True, "bluetooth": link}, out)
        return next(line for line in out.getvalue().splitlines() if line.startswith("bluetooth:"))

    # The documents tell people to look for exactly these words.
    ready = bluetooth_line(connected=True, subscribed=False, ready=True, delivery="raw")
    assert "connected, ready (delivery: raw)" in ready
    ready = bluetooth_line(connected=True, subscribed=True, ready=True, delivery="bluez")
    assert "connected, ready (delivery: bluez)" in ready
    # After a boot or wake the link is up and the TV has subscribed, but nothing was sent yet.
    resting = bluetooth_line(connected=True, subscribed=True, ready=False)
    assert "no key sent on this link yet" in resting and "waiting" not in resting
    assert "waiting for the TV to subscribe" in bluetooth_line(connected=True, subscribed=False, ready=False)
    assert "calling the TV" in bluetooth_line(connected=False, advertising=True)


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


def test_uninstall_runs_the_installed_installer(monkeypatch):
    from tcl_me_remo import installer

    spawned, exit_code = [], 0

    def spawn(argv):
        spawned.append(argv)
        return exit_code

    monkeypatch.setattr(installer, "_spawn", spawn)
    monkeypatch.setattr(installer.shutil, "which", lambda name: f"/usr/bin/{name}")

    # as a normal user: through sudo, with a word about the password SteamOS does not have
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["uninstall", "--purge"], out=out, err=err) == 0
    assert spawned[-1][:2] == ["sudo", "--"] and spawned[-1][-2:] == ["uninstall", "--purge"]
    assert spawned[-1][2:] == installer.self_command("uninstall", "--purge")  # this copy's own installer
    assert "passwd" in out.getvalue() and "Decky" in out.getvalue()

    # as root: directly; settings are kept unless asked otherwise; a failure is passed on
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    exit_code = 1
    assert cli.run(["uninstall"], out=io.StringIO(), err=io.StringIO()) == 1
    assert spawned[-1] == installer.self_command("uninstall")

    # sudo gave up (no password set, or a wrong one): say what to do about it
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    err = io.StringIO()
    assert cli.run(["uninstall"], out=io.StringIO(), err=err) == 1 and "passwd" in err.getvalue()

    # no sudo: say so instead of failing obscurely
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    err = io.StringIO()
    assert cli.run(["uninstall"], out=io.StringIO(), err=err) == 1 and "run this as root" in err.getvalue()
    assert len(spawned) == 3


# --------------------------------------------------------------------------- odd input, clear answers


@pytest.mark.parametrize("budget", ["nan", "inf", "-inf", "1e999", "0", "-1", "121"])
async def test_a_budget_that_is_no_sensible_number_is_a_usage_error(running, budget):
    """Not a traceback (``settimeout(nan)``), and not left to the service: exit code 2."""
    for extra in ((), ("--simulate",), ("--no-wait",)):
        code, out, err = await running("event", "suspend", *extra, f"--budget={budget}")
        assert code == 2 and out == "" and "--budget" in err and "120" in err
    assert running.remote.keys == [] and running.service.phase.value == "running"


async def test_the_largest_budget_there_is_can_be_given(running):
    from tcl_me_remo import service

    assert cli.MAX_BUDGET == service.MAX_EVENT_BUDGET  # the command and the service agree on the range
    code, out, _ = await running("event", "suspend", "--simulate", "--budget", "120")
    assert code == 0 and "TV off" in out


async def test_wifi_pair_without_anyone_to_type_the_code_cancels_the_pairing(running):
    def nobody(prompt):
        raise EOFError  # what input() does without a terminal

    code, out, err = await running("wifi", "pair", "192.168.1.50", ask=nobody)
    assert code == 1 and "cancelled" in out and err == ""
    assert running.wifi.pairing_host == "" and not running.wifi.paired  # the TV is not left waiting

    def interrupted(prompt):
        raise KeyboardInterrupt

    code, out, _ = await running("wifi", "pair", "192.168.1.50", ask=interrupted)
    assert code == 1 and running.wifi.pairing_host == ""


async def test_config_lists_what_each_setting_may_be(running):
    code, out, _ = await running("config")
    assert code == 0
    lines = {line.split(" = ")[0]: line for line in out.splitlines()}
    assert lines["input.hdmi_port"].startswith("input.hdmi_port = 1 ") and lines["input.hdmi_port"].endswith("1 to 4")
    assert lines["input.method"].endswith("# one of: macro, wifi, none")
    assert lines["bluetooth.raw_notify"].endswith("# one of: auto, on, off, always")
    assert lines["enabled"].endswith("# true or false")
    assert lines["display.wait_seconds"].endswith("# number, 0 to 60")
    assert lines["bluetooth.key_gap_ms"].endswith("# whole number, 20 to 5000")
    assert lines["bluetooth.device_name"].endswith("# text, up to 26 bytes")
    assert lines["display.edid_match"].endswith("# text, up to 64 characters")
    assert "1024 characters" in lines["input.macro"] and "tcl-me-remo keys" in lines["power.on_key"]
    assert "hci0" in lines["bluetooth.adapter"]
    assert lines["input.preset"].endswith("# one of: custom, tcl-row, tcl-row-home, tcl-row-first, tcl-column")
    assert "one of: custom, tcl-mediatek" in lines["wifi.link_preset"]
    assert all(" # " in line for line in lines.values())  # every setting says something
    assert max(map(len, out.splitlines())) < 130  # and it still reads as a list
    # the same for one section; a single setting stays a bare value, for scripts
    code, out, _ = await running("config", "input")
    assert "input.hdmi_port = 1" in out and "1 to 4" in out and "events." not in out
    code, out, _ = await running("config", "input.hdmi_port")
    assert out == "1\n"


async def test_config_without_a_schema_still_lists_the_settings():
    def call(method, params=None, *, timeout=30.0):
        return {"config": {"enabled": True, "input": {"hdmi_port": 2}}}  # an older service

    out = io.StringIO()
    assert cli.run(["config"], call=call, out=out, err=io.StringIO()) == 0
    assert out.getvalue() == "enabled = true\ninput.hdmi_port = 2\n"


async def test_junk_in_a_request_does_not_stop_status_and_logs_from_working(running):
    """It used to: the bogus key went into the history at full length, and every status
    reply after that was larger than a client reads."""
    for _ in range(3):
        code, out, _ = await running("macro", "X" * 1000)
        assert code == 1 and out.startswith("failed: ") and len(out) < 200
    code, out, err = await running("macro", "X" * 200_000)
    assert code == 1 and "limit" in err and len(err) < 200
    code, _, err = await running("key", "K" * 200_000)
    assert code == 1 and len(err) < 200
    code, out, _ = await running("status")
    assert code == 0 and "last:" in out and len(out) < 2000
    code, out, _ = await running("status", "--json")
    assert code == 0 and len(json.loads(out)["history"]) == 3  # what was refused unread left no record
    code, _, _ = await running("logs")
    assert code == 0


async def test_too_many_clients_is_an_error_with_a_message(tmp_path, running):
    running.server._max_clients = 0  # as if every place were taken
    code, out, err = await running("status")
    assert code == 1 and "too many clients" in err and "Traceback" not in err


def test_not_being_allowed_gets_its_own_hint(tmp_path, monkeypatch):
    def call(method, params=None, *, timeout=30.0):
        raise ipc.ServiceUnavailable("not allowed to use the tcl-me-remo service: Permission denied", denied=True)

    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["status"], call=call, out=out, err=err) == 3
    assert "sudo" in err.getvalue() and "wheel" in err.getvalue()
    assert "Is the service running" not in err.getvalue()  # it is; that advice would send people the wrong way


def test_a_service_that_does_not_take_the_connection_is_exit_code_3(tmp_path, monkeypatch):
    import socket

    path = tmp_path / "stuck.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(0)
    waiting = []
    try:
        for _ in range(64):  # fill the queue of a listener that never accepts
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.setblocking(False)
            try:
                client.connect(str(path))
            except BlockingIOError:
                client.close()
                break
            waiting.append(client)
        monkeypatch.setenv("TCL_ME_REMO_SOCKET", str(path))
        out, err = io.StringIO(), io.StringIO()
        # What the sleep hook runs.  It used to end in a traceback.
        assert cli.run(["event", "suspend", "--budget", "0.5"], out=out, err=err) in (1, 3)
        assert "tcl-me-remo:" in err.getvalue() and "Traceback" not in err.getvalue()
    finally:
        for sock in (listener, *waiting):
            sock.close()


def test_an_answer_of_the_wrong_shape_is_a_message_not_a_traceback():
    for argv, reply in ((["connect"], None), (["status"], []), (["logs"], {}), (["key", "OK"], "ok"), (["config"], 3)):
        err = io.StringIO()
        code = cli.run(argv, call=lambda *a, reply=reply, **k: reply, out=io.StringIO(), err=err)
        assert code == 1 and "unexpected answer" in err.getvalue(), argv
