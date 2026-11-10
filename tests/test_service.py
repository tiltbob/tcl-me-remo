import asyncio
import json

import pytest
from fakes import TV_ADDRESS, FakeRemote, FakeWifi

from tcl_me_remo import config as C
from tcl_me_remo.ipc import RpcError
from tcl_me_remo.service import Phase, Service


class Clock:
    """Stands in for (CLOCK_BOOTTIME - CLOCK_MONOTONIC): grows only while 'suspended'."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return self.offset

    def sleep_for(self, seconds):
        self.offset += seconds


def make_sysfs(tmp_path, connectors):
    drm = tmp_path / "class" / "drm"
    drm.mkdir(parents=True, exist_ok=True)
    for name, status in connectors.items():
        d = drm / name
        d.mkdir(exist_ok=True)
        (d / "status").write_text(status + "\n")
        (d / "edid").write_bytes(b"")
    return tmp_path


class Harness:
    def __init__(self, tmp_path, *, patch=None, remote=None, wifi=None, connectors=None):
        base = {
            "input": {"macro": "HOME DOWN*{port} OK", "delay_after_wake": 2.0},
            "display": {"wait_seconds": 0.0},
        }
        cfg = C.apply_patch(C.default_config(), base)
        if patch:
            cfg = C.apply_patch(cfg, patch)
        self.remote = remote or FakeRemote()
        self.wifi = wifi
        self.clock = Clock()
        self.sleeps = []
        self.saved = []
        self.sys_root = make_sysfs(tmp_path, connectors if connectors is not None else {"card0-HDMI-A-1": "connected"})

        async def fake_sleep(seconds):
            self.sleeps.append(seconds)
            await asyncio.sleep(0)

        self.service = Service(
            cfg,
            remote=self.remote,
            wifi=self.wifi,
            save_config=self.saved.append,
            sys_root=self.sys_root,
            sleep=fake_sleep,
            sleep_offset_fn=self.clock,
            recent_logs=lambda n: [f"line {i}" for i in range(n)],
        )


# --------------------------------------------------------------------------- up events


async def test_boot_wakes_tv_then_switches_input(tmp_path):
    h = Harness(tmp_path, patch={"input": {"hdmi_port": 2}})
    record = await h.service.handle_event("boot")
    assert record["ok"] and not record["skipped"], record
    assert record["name"] == "tv_on" and record["trigger"] == "boot"
    assert h.remote.key_names() == ["WAKE", "WAKE", "HOME", "DOWN", "DOWN", "OK"]
    assert 2.0 in h.sleeps  # waited delay_after_wake between power and input
    assert h.sleeps.index(0.8) < h.sleeps.index(2.0)  # the repeat gap comes before the wait
    assert "TV on (Bluetooth)" in record["detail"] and "input switched (Bluetooth macro)" in record["detail"]
    assert any("playing macro" in step for step in record["steps"])
    assert h.remote.ensure_calls[0] == 30.0  # connect_timeout


async def test_boot_is_handled_once(tmp_path):
    h = Harness(tmp_path)
    await h.service.handle_event("boot")
    count = len(h.remote.keys)
    again = await h.service.handle_event("boot")
    assert again["skipped"] and "duplicate" in again["detail"]
    assert len(h.remote.keys) == count
    assert len(h.service.status()["history"]) == 1  # duplicates stay out of the history


async def test_resume_without_a_sleep_is_a_duplicate(tmp_path):
    h = Harness(tmp_path)
    record = await h.service.handle_event("resume")
    assert record["skipped"] and "duplicate" in record["detail"]
    assert h.remote.keys == []


async def test_resume_detected_by_clock_jump_even_if_suspend_was_never_announced(tmp_path):
    h = Harness(tmp_path)
    h.clock.sleep_for(300)
    record = await h.service.handle_event("resume")
    assert record["ok"] and not record["skipped"]
    assert h.remote.key_names()[:2] == ["WAKE", "WAKE"]
    # the second route reporting the same resume is ignored
    assert (await h.service.handle_event("resume"))["skipped"]


async def test_only_input_switch_when_power_on_is_disabled(tmp_path):
    h = Harness(tmp_path, patch={"events": {"boot_tv_on": False}})
    record = await h.service.handle_event("boot")
    assert record["ok"]
    assert h.remote.key_names() == ["HOME", "DOWN", "OK"]
    assert 2.0 not in h.sleeps  # no wake, so no wake delay


async def test_event_with_nothing_configured_is_skipped(tmp_path):
    h = Harness(tmp_path, patch={"events": {"boot_tv_on": False, "boot_switch_input": False}})
    record = await h.service.handle_event("boot")
    assert record["skipped"] and "nothing is set" in record["detail"]
    h2 = Harness(tmp_path, patch={"events": {"boot_tv_on": False}, "input": {"method": "none"}})
    assert (await h2.service.handle_event("boot"))["skipped"]


async def test_master_switch_disables_automatic_actions_only(tmp_path):
    h = Harness(tmp_path, patch={"enabled": False})
    assert (await h.service.handle_event("boot"))["skipped"]
    assert (await h.service.handle_event("suspend"))["skipped"]
    assert h.remote.keys == []
    assert (await h.service.tv_off())["ok"]  # manual control still works
    assert h.remote.key_names() == ["SLEEP"]


async def test_handheld_without_external_display_leaves_the_tv_alone(tmp_path):
    h = Harness(tmp_path, connectors={"card0-eDP-1": "connected", "card0-DP-1": "disconnected"})
    record = await h.service.handle_event("boot")
    assert record["ok"] and record["skipped"] and "no external display" in record["detail"]
    down = await h.service.handle_event("suspend")
    assert down["skipped"] and "no external display" in down["detail"]
    assert h.remote.keys == []
    assert h.remote.disconnects == 1  # still tidies the link before sleeping


async def test_power_failure_stops_before_input_switch(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch={"bluetooth": {"connect_timeout": 1.0}})
    record = await h.service.handle_event("boot")
    assert not record["ok"]
    assert "did not connect within 1s" in record["detail"]
    assert h.remote.keys == []
    assert len(h.remote.ensure_calls) == 1  # did not try again for the macro


async def test_unpaired_remote_reports_reason(tmp_path):
    remote = FakeRemote(paired=False)
    remote.fail_code = "not_paired"
    h = Harness(tmp_path, remote=remote)
    record = await h.service.handle_event("boot")
    assert not record["ok"] and "not_paired" in record["detail"]


# --------------------------------------------------------------------------- down events


async def test_suspend_turns_tv_off_and_resume_turns_it_back_on(tmp_path):
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    record = await h.service.handle_event("suspend")
    assert record["ok"] and record["name"] == "tv_off" and record["trigger"] == "suspend"
    assert h.remote.key_names() == ["SLEEP"]
    assert h.remote.ensure_calls == []  # link was already up: no reconnect needed
    assert h.remote.disconnects == 1
    assert h.service.phase == Phase.DOWN

    dup = await h.service.handle_event("suspend")  # e.g. the sleep.target unit after logind
    assert dup["skipped"] and "duplicate" in dup["detail"]
    assert h.remote.key_names() == ["SLEEP"]

    h.clock.sleep_for(3600)
    up = await h.service.handle_event("resume")
    assert up["ok"] and not up["skipped"]
    assert h.service.phase == Phase.RUNNING
    assert h.remote.key_names()[1:3] == ["WAKE", "WAKE"]


async def test_resume_after_aborted_suspend_still_turns_tv_on(tmp_path):
    # logind announces the sleep, we turn the TV off, then the suspend fails: no clock jump.
    h = Harness(tmp_path)
    await h.service.handle_event("suspend")
    up = await h.service.handle_event("resume")
    assert up["ok"] and not up["skipped"]
    assert "WAKE" in h.remote.key_names()


async def test_missed_resume_does_not_block_the_next_suspend(tmp_path):
    h = Harness(tmp_path)
    await h.service.handle_event("suspend")
    h.clock.sleep_for(600)  # slept and woke, but nobody told us
    h.remote.keys.clear()
    record = await h.service.handle_event("suspend")
    assert record["ok"] and not record["skipped"]
    assert h.remote.key_names() == ["SLEEP"]


async def test_suspend_reconnects_briefly_when_link_is_down(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=0.01))
    record = await h.service.handle_event("suspend")
    assert record["ok"]
    assert h.remote.ensure_calls == [3.0]  # off_connect_timeout, not the long connect_timeout
    assert h.remote.key_names() == ["SLEEP"]


async def test_suspend_falls_back_to_wifi(tmp_path):
    wifi = FakeWifi()
    h = Harness(
        tmp_path,
        remote=FakeRemote(connect_after=None),
        wifi=wifi,
        patch={"wifi": {"enabled": True, "host": "192.168.1.50"}, "bluetooth": {"off_connect_timeout": 0.05}},
    )
    record = await h.service.handle_event("suspend")
    assert record["ok"] and "Wi-Fi" in record["detail"]
    assert wifi.keys == [("192.168.1.50", 223)]  # KEYCODE_SLEEP


async def test_suspend_with_link_down_races_both_transports(tmp_path):
    # Wi-Fi answers at once; the machine must not wait for the Bluetooth attempt to time out.
    wifi = FakeWifi()
    remote = FakeRemote(connect_after=None)
    patch = {"wifi": {"enabled": True, "host": "tv.lan"}, "bluetooth": {"off_connect_timeout": 30.0}}
    h = Harness(tmp_path, remote=remote, wifi=wifi, patch=patch)
    started = asyncio.get_running_loop().time()
    record = await h.service.handle_event("suspend", budget=3.0)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert record["ok"] and record["detail"] == "TV off (Wi-Fi)"
    assert wifi.keys == [("tv.lan", 223)]

    # Bluetooth reconnects while Wi-Fi is unreachable: Bluetooth wins.
    wifi2 = FakeWifi()
    wifi2.fail_code = "wifi_unreachable"
    h2 = Harness(tmp_path, remote=FakeRemote(connect_after=0.05), wifi=wifi2, patch=patch)
    record = await h2.service.handle_event("suspend", budget=3.0)
    assert record["ok"] and record["detail"] == "TV off (Bluetooth)"
    assert h2.remote.key_names() == ["SLEEP"]
    assert any(step.startswith("Wi-Fi: ") for step in record["steps"])

    # Neither works: both reasons are reported.
    wifi3 = FakeWifi()
    wifi3.fail_code = "wifi_unreachable"
    patch3 = {"wifi": {"enabled": True, "host": "tv.lan"}, "bluetooth": {"off_connect_timeout": 0.05}}
    h3 = Harness(tmp_path, remote=FakeRemote(connect_after=None), wifi=wifi3, patch=patch3)
    record = await h3.service.handle_event("suspend", budget=3.0)
    assert not record["ok"]
    assert "Bluetooth: the TV did not connect" in record["detail"] and "Wi-Fi: wifi problem" in record["detail"]


async def test_wake_uses_wifi_only_after_bluetooth_failed(tmp_path):
    wifi = FakeWifi()
    patch = {
        "wifi": {"enabled": True, "host": "tv.lan"},
        "bluetooth": {"connect_timeout": 1.0},
        "input": {"method": "none"},
    }
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), wifi=wifi, patch=patch)
    record = await h.service.tv_on()
    assert record["ok"] and record["detail"] == "TV on (Wi-Fi)"
    assert wifi.keys == [("tv.lan", 224)]  # KEYCODE_WAKEUP
    assert record["steps"][0].startswith("Bluetooth: the TV did not connect")

    # with a working Bluetooth link Wi-Fi is not touched at all
    wifi_b = FakeWifi()
    hb = Harness(tmp_path, wifi=wifi_b, patch=patch)
    assert (await hb.service.tv_on())["detail"] == "TV on (Bluetooth)"
    assert wifi_b.keys == []


async def test_suspend_reports_failure_when_tv_is_unreachable(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch={"bluetooth": {"off_connect_timeout": 0.05}})
    record = await h.service.handle_event("suspend")
    assert not record["ok"] and "did not connect" in record["detail"]
    assert h.remote.disconnects == 1
    assert h.service.phase == Phase.DOWN


async def test_wifi_power_fallback_can_be_disabled(tmp_path):
    wifi = FakeWifi()
    h = Harness(
        tmp_path,
        remote=FakeRemote(connect_after=None),
        wifi=wifi,
        patch={
            "wifi": {"enabled": True, "host": "192.168.1.50", "power_fallback": False},
            "bluetooth": {"off_connect_timeout": 0.05},
        },
    )
    assert not (await h.service.handle_event("suspend"))["ok"]
    assert wifi.keys == []


async def test_down_event_respects_its_budget(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch={"bluetooth": {"off_connect_timeout": 30.0}})
    started = asyncio.get_running_loop().time()
    record = await h.service.handle_event("suspend", budget=0.2)
    assert asyncio.get_running_loop().time() - started < 1.5
    assert not record["ok"] and "ran out of time (0.2s)" in record["detail"]
    assert h.remote.disconnects == 1  # the tidy-up still ran


async def test_reboot_keeps_tv_on_by_default_and_shutdown_turns_it_off(tmp_path):
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    record = await h.service.handle_event("reboot")
    assert record["skipped"] and "reboot" in record["detail"]
    assert h.remote.keys == []
    assert h.remote.disconnects == 1

    h2 = Harness(tmp_path)
    h2.remote.state.connected = h2.remote.state.ready = True
    assert (await h2.service.handle_event("shutdown"))["ok"]
    assert h2.remote.key_names() == ["SLEEP"]


async def test_cancelled_shutdown_returns_to_running(tmp_path):
    h = Harness(tmp_path)
    await h.service.handle_event("shutdown")
    assert h.service.phase == Phase.DOWN
    h.service.cancel_shutdown()
    assert h.service.phase == Phase.RUNNING
    assert not (await h.service.handle_event("shutdown"))["skipped"]


async def test_suspend_preempts_a_running_wake_sequence(tmp_path):
    h = Harness(tmp_path)
    h.service._sleep = asyncio.sleep  # real time: the 2 s delay_after_wake keeps tv_on busy
    boot = asyncio.create_task(h.service.handle_event("boot"))
    await asyncio.sleep(0.05)
    assert h.service.status()["busy"] == "tv_on (boot)"
    h.service._sleep = lambda seconds: asyncio.sleep(0)
    down = await h.service.handle_event("suspend")
    boot_record = await boot
    assert not boot_record["ok"] and "interrupted" in boot_record["detail"]
    assert down["ok"]
    assert h.remote.key_names()[-1] == "SLEEP"
    assert "HOME" not in h.remote.key_names()  # the macro never started


async def test_resume_waits_for_the_down_action_instead_of_cancelling_it(tmp_path):
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    h.remote.key_delay = 0.1
    down = asyncio.create_task(h.service.handle_event("suspend"))
    await asyncio.sleep(0.02)
    up = await h.service.handle_event("resume")
    down_record = await down
    assert down_record["ok"] and up["ok"]
    assert h.remote.key_names()[0] == "SLEEP" and "WAKE" in h.remote.key_names()[1:]


async def test_simulated_events_do_not_touch_bookkeeping(tmp_path):
    h = Harness(tmp_path)
    record = await h.service.handle_event("suspend", simulate=True)
    assert record["ok"] and h.remote.key_names() == ["SLEEP"]
    assert h.service.phase == Phase.RUNNING
    record = await h.service.handle_event("resume", simulate=True)
    assert record["ok"] and not record["skipped"]
    record = await h.service.handle_event("boot", simulate=True)
    assert not record["skipped"]
    assert not (await h.service.handle_event("boot"))["skipped"]  # the real boot still counts


async def test_unknown_event(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(RpcError) as err:
        await h.service.handle_event("hibernate-ish")
    assert err.value.code == "bad_request"


# --------------------------------------------------------------------------- input switching


async def test_wifi_input_switch_sends_hdmi_keycode(tmp_path):
    wifi = FakeWifi()
    patch = {"input": {"method": "wifi", "hdmi_port": 3}, "wifi": {"enabled": True, "host": "tv.lan"}}
    h = Harness(tmp_path, wifi=wifi, patch=patch)
    record = await h.service.switch_input()
    assert record["ok"] and "Wi-Fi" in record["detail"]
    assert wifi.keys == [("tv.lan", 245)]  # KEYCODE_TV_INPUT_HDMI_3
    assert h.remote.keys == []


async def test_wifi_input_switch_retries_while_tv_network_wakes_up(tmp_path):
    wifi = FakeWifi()
    wifi.unreachable_times = 2
    patch = {"input": {"method": "wifi"}, "wifi": {"enabled": True, "host": "tv.lan"}}
    h = Harness(tmp_path, wifi=wifi, patch=patch)
    record = await h.service.switch_input()
    assert record["ok"]
    assert wifi.attempts == 3


async def test_wifi_input_switch_app_link(tmp_path):
    wifi = FakeWifi()
    patch = {
        "input": {"method": "wifi", "hdmi_port": 2},
        "wifi": {"enabled": True, "host": "tv.lan", "input_mode": "app_link", "app_link": "tv://input/HDMI{port}"},
    }
    h = Harness(tmp_path, wifi=wifi, patch=patch)
    assert (await h.service.switch_input())["ok"]
    assert wifi.links == [("tv.lan", "tv://input/HDMI2")]


async def test_wifi_failure_falls_back_to_macro(tmp_path):
    wifi = FakeWifi(paired=False)
    patch = {"input": {"method": "wifi"}, "wifi": {"enabled": True, "host": "tv.lan"}}
    h = Harness(tmp_path, wifi=wifi, patch=patch)
    record = await h.service.switch_input()
    assert record["ok"] and "Bluetooth macro" in record["detail"]
    assert any("falling back" in step for step in record["steps"])
    assert h.remote.key_names() == ["HOME", "DOWN", "OK"]


async def test_wifi_failure_without_fallback_fails(tmp_path):
    wifi = FakeWifi()
    wifi.fail_code = "wifi_error"
    patch = {
        "input": {"method": "wifi", "wifi_fallback_to_macro": False},
        "wifi": {"enabled": True, "host": "tv.lan"},
    }
    h = Harness(tmp_path, wifi=wifi, patch=patch)
    record = await h.service.switch_input()
    assert not record["ok"] and "wifi_error" in record["detail"]
    assert h.remote.keys == []
    assert wifi.attempts == 1  # a non-"unreachable" error is not retried


async def test_macro_method_without_macro_explains(tmp_path):
    h = Harness(tmp_path, patch={"input": {"macro": ""}})
    record = await h.service.switch_input()
    assert not record["ok"] and "no input macro" in record["detail"]


async def test_input_method_none(tmp_path):
    h = Harness(tmp_path, patch={"input": {"method": "none"}})
    record = await h.service.tv_on()
    assert record["ok"] and h.remote.key_names() == ["WAKE", "WAKE"]
    assert not (await h.service.switch_input())["ok"]


# --------------------------------------------------------------------------- manual + rpc


async def test_manual_requests_are_rejected_while_busy(tmp_path):
    h = Harness(tmp_path)
    h.service._sleep = asyncio.sleep
    first = asyncio.create_task(h.service.tv_on())
    await asyncio.sleep(0.05)
    with pytest.raises(RpcError) as err:
        await h.service.tv_off()
    assert err.value.code == "busy"
    with pytest.raises(RpcError) as err:
        await h.service.send_key("HOME")
    assert err.value.code == "busy"
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    # the caller went away, but the action itself keeps going and is recorded
    assert h.service.status()["busy"] == "tv_on (manual)"
    await asyncio.wait_for(h.service._action, 5)
    assert h.service.status()["last_action"]["ok"]


async def test_send_key_and_run_macro(tmp_path):
    h = Harness(tmp_path, patch={"input": {"hdmi_port": 3}})
    assert await h.service.send_key("home") == {"key": "HOME"}
    assert await h.service.send_key("OK", hold=1.5) == {"key": "OK"}
    assert h.remote.keys == [("HOME", 0.08), ("OK", 1.5)]
    assert h.service.status()["history"] == []  # single keys are not history entries
    for bad_key, bad_hold, code in (("NOPE", None, "bad_key"), ("OK", 0, "bad_request"), ("OK", 99, "bad_request")):
        with pytest.raises(RpcError) as err:
            await h.service.send_key(bad_key, bad_hold)
        assert err.value.code == code

    h.remote.keys.clear()
    record = await h.service.run_macro("UP*{port} OK")
    assert record["ok"] and h.remote.key_names() == ["UP", "UP", "UP", "OK"]
    assert not (await h.service.run_macro("NOT_A_KEY"))["ok"]
    assert not (await h.service.run_macro("  "))["ok"]


async def test_send_key_when_tv_never_connects(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch={"bluetooth": {"connect_timeout": 1.0}})
    with pytest.raises(RpcError) as err:
        await h.service.send_key("HOME")
    assert err.value.code == "tv_unreachable"


async def test_rpc_surface(tmp_path):
    wifi = FakeWifi(paired=False)
    h = Harness(tmp_path, wifi=wifi)
    rpc = h.service.rpc

    status = await rpc("status", {})
    json.dumps(status)
    assert status["bluetooth"]["tv_address"] == TV_ADDRESS
    assert status["input"]["macro"] == "HOME DOWN*1 OK"
    assert status["display"]["gate_ok"] is True
    assert status["wifi"] == {
        "available": True,
        "enabled": False,
        "host": "",
        "name": "",
        "paired": False,
        "pairing": False,
        "pairing_host": "",
    }

    info = await rpc("get_config", {})
    json.dumps(info)
    assert info["config"]["input"]["hdmi_port"] == 1
    assert {"name": "WAKE", "label": "Wake (discrete on)"}.items() <= info["keys"][0].items()

    result = await rpc("set_config", {"patch": {"input": {"hdmi_port": 4}}})
    assert result["config"]["input"]["hdmi_port"] == 4
    assert h.saved[-1].input.hdmi_port == 4
    assert h.remote.config == h.service.cfg.bluetooth
    with pytest.raises(RpcError) as err:
        await rpc("set_config", {"patch": {"input": {"hdmi_port": 9}}})
    assert err.value.code == "invalid_config" and "input.hdmi_port" in err.value.message
    with pytest.raises(RpcError):
        await rpc("set_config", {})

    assert (await rpc("check_macro", {"macro": "home down*{port}"}))["rendered"] == "HOME DOWN*4"
    assert (await rpc("check_macro", {"macro": "bogus"}))["ok"] is False
    assert (await rpc("check_macro", {"macro": ""}))["ok"] is True

    assert (await rpc("send_key", {"key": "HOME"})) == {"key": "HOME"}
    assert (await rpc("tv_off", {}))["ok"]
    assert (await rpc("tv_on", {"switch_input": False}))["ok"]
    assert (await rpc("event", {"name": "suspend", "simulate": True, "budget": 2}))["trigger"] == "suspend"
    assert (await rpc("logs", {"lines": 3}))["lines"] == ["line 0", "line 1", "line 2"]

    assert (await rpc("pair_start", {"timeout": 60}))["pairing"] is True
    assert h.remote.pairing_calls == [60.0]
    assert (await rpc("pair_cancel", {}))["pairing"] is False
    assert (await rpc("connect", {}))["ready"] is True
    assert (await rpc("disconnect", {}))["connected"] is False

    for method, params in (
        ("nope", {}),
        ("_adopt_config", {}),
        ("rpc", {}),
        ("send_key", {}),
        ("send_key", {"key": "OK", "hold": "long"}),
        ("event", {"name": "boot", "budget": -1}),
        ("event", {"name": "hibernate-ish", "wait": False}),
        ("event", {"name": "boot", "wait": "no"}),
        ("event", {"name": "boot", "simulate": "yes"}),
        ("pair_start", {"timeout": 5}),
        ("logs", {"lines": 0}),
        ("tv_on", {"switch_input": 1}),
    ):
        with pytest.raises(RpcError) as err:
            await rpc(method, params)
        assert err.value.code in ("unknown_method", "bad_request"), (method, err.value.code)


async def test_rpc_wifi_pairing_flow(tmp_path):
    wifi = FakeWifi(paired=False)
    h = Harness(tmp_path, wifi=wifi)
    rpc = h.service.rpc
    assert (await rpc("wifi_discover", {}))["devices"][0]["host"] == "192.168.1.50"
    with pytest.raises(RpcError):
        await rpc("wifi_pair_start", {"host": "not a host"})
    assert (await rpc("wifi_pair_start", {"host": "192.168.1.50"}))["pairing"] is True
    with pytest.raises(RpcError) as err:
        await rpc("wifi_pair_finish", {"code": "ZZZZZZ"})
    assert err.value.code == "pairing_failed"
    assert h.service.cfg.wifi.enabled is False
    assert (await rpc("wifi_pair_finish", {"code": "A1B2C3", "name": "Living Room TV"}))["paired"] is True
    assert h.service.cfg.wifi.enabled and h.service.cfg.wifi.host == "192.168.1.50"
    assert h.service.cfg.wifi.name == "Living Room TV"
    assert (await rpc("wifi_unpair", {}))["paired"] is False
    assert h.service.cfg.wifi.enabled is False

    no_wifi = Harness(tmp_path)
    with pytest.raises(RpcError) as err:
        await no_wifi.service.rpc("wifi_discover", {})
    assert err.value.code == "wifi_unavailable"


async def test_pairing_callback_and_forget(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(paired=False))
    await h.service.on_paired("11:22:33:44:55:66", "Bedroom TV")
    assert h.service.cfg.bluetooth.tv_address == "11:22:33:44:55:66"
    assert h.saved[-1].bluetooth.tv_name == "Bedroom TV"
    assert h.remote.config.tv_address == "11:22:33:44:55:66"
    await h.service.rpc("forget_tv", {})
    assert h.remote.forgotten == 1
    assert h.service.cfg.bluetooth.tv_address == ""


async def test_save_failure_is_reported_but_setting_takes_effect(tmp_path):
    h = Harness(tmp_path)

    def broken(_cfg):
        raise OSError("read-only file system")

    h.service._save_config = broken
    with pytest.raises(RpcError) as err:
        h.service.set_config({"input": {"hdmi_port": 2}})
    assert err.value.code == "save_failed"
    assert h.service.cfg.input.hdmi_port == 2
