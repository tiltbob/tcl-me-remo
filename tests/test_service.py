import asyncio
import json
import time

import pytest
from fakes import TV_ADDRESS, FakeRemote, FakeWifi

from tcl_me_remo import config as C
from tcl_me_remo import service as service_module
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
    def __init__(self, tmp_path, *, patch=None, remote=None, wifi=None, connectors=None, power_state_path=None):
        # Explicit values so these tests do not depend on the shipped defaults.
        base = {
            "input": {"method": "macro", "preset": "custom", "macro": "HOME DOWN*{port} OK", "delay_after_wake": 2.0},
            "display": {"wait_seconds": 0.0},
            "power": {"on_repeat": 2, "repeat_gap": 0.8},
            "wifi": {"input_mode": "keycode"},
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
            power_state_path=power_state_path,
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
    record = await h.service.handle_event("suspend", budget=25.0)
    assert asyncio.get_running_loop().time() - started < 10.0  # waiting for Bluetooth would take 25 s
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
    assert asyncio.get_running_loop().time() - started < 10.0  # the connect timeout is 30 s
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


@pytest.mark.parametrize("patch", [None, {"enabled": False}, {"events": {"suspend_tv_off": False}}])
async def test_down_events_end_the_link_at_once(tmp_path, patch):
    """Whatever the down action did or skipped, the link is ended by us and not by the kernel."""
    h = Harness(tmp_path, patch=patch)
    h.remote.state.connected = h.remote.state.ready = True
    await h.service.handle_event("suspend")
    assert h.remote.disconnects == 1 and h.remote.fast_disconnects == 1
    assert not h.remote.state.connected


async def test_the_fast_disconnect_still_runs_when_the_action_ran_out_of_budget(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch={"bluetooth": {"off_connect_timeout": 30.0}})
    record = await h.service.handle_event("shutdown", budget=0.1)
    assert not record["ok"] and "ran out of time" in record["detail"]
    assert h.remote.disconnects == 1 and h.remote.fast_disconnects == 1


async def test_manual_disconnect_is_not_the_fast_one(tmp_path):
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    await h.service.rpc("disconnect", {})
    assert h.remote.disconnects == 1 and h.remote.fast_disconnects == 0


async def test_final_disconnect_is_bounded_and_fits_into_logind_s_wait(tmp_path, monkeypatch):
    from tcl_me_remo import lifecycle
    from tcl_me_remo import service as service_module

    # The arithmetic: budget + disconnect must leave room before logind stops waiting, both
    # with the default budget and with the one derived from logind's setting.
    spare = 0.5
    assert service_module.DEFAULT_DOWN_BUDGET + service_module.DISCONNECT_SECONDS <= lifecycle.DEFAULT_MAX_DELAY - spare
    assert service_module.DISCONNECT_SECONDS <= lifecycle.BUDGET_MARGIN - spare

    # A transport that does not come back is not waited for beyond DISCONNECT_SECONDS.
    monkeypatch.setattr(service_module, "DISCONNECT_SECONDS", 0.05)
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    h.remote.disconnect_delay = 30.0
    started = asyncio.get_running_loop().time()
    record = await h.service.handle_event("suspend")
    assert asyncio.get_running_loop().time() - started < 5.0
    assert record["ok"] and h.remote.fast_disconnects == 1 and h.service.phase == Phase.DOWN


class GatedRemote(FakeRemote):
    """Its disconnect does not return until the test lets it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.disconnect_started = asyncio.Event()
        self.release = asyncio.Event()

    async def disconnect(self, *, fast=False):
        self.disconnect_started.set()
        await self.release.wait()
        await super().disconnect(fast=fast)


async def test_the_budget_is_for_the_attempt_and_the_outcome_is_fixed_before_the_link_is_closed(tmp_path, monkeypatch):
    """A "TV off" that went out late in its time must not be recorded as "ran out of time"
    because closing the link afterwards took the rest of it."""
    monkeypatch.setattr(service_module, "DISCONNECT_SECONDS", 30.0)
    h = Harness(tmp_path, remote=GatedRemote())
    h.remote.state.connected = h.remote.state.ready = True
    down = asyncio.create_task(h.service.handle_event("suspend", budget=2.5))
    await asyncio.wait_for(h.remote.disconnect_started.wait(), 10)
    last = h.service.status()["last_action"]  # recorded (and logged) already
    assert last is not None and last["ok"] and last["detail"] == "TV off (Bluetooth)"
    # ...but the event is not over, and the machine is not let go, until the link is closed.
    assert not down.done() and h.service.phase == Phase.GOING_DOWN
    await asyncio.sleep(3.0)  # the budget passes while the link is being closed
    assert not down.done()
    h.remote.release.set()
    record = await down
    assert record["ok"] and record["detail"] == "TV off (Bluetooth)"
    assert record == h.service.status()["last_action"] and h.service.phase == Phase.DOWN


async def test_running_out_of_time_after_the_key_went_out_is_not_a_failure(tmp_path, monkeypatch):
    # e.g. a TV that answered the call late: the key is out, the pause after it is cut short.
    monkeypatch.setattr(service_module, "FLUSH_SECONDS", 30.0)
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    h.service._sleep = asyncio.sleep
    record = await h.service.handle_event("suspend", budget=2.5)
    assert record["ok"] and record["detail"] == "TV off (Bluetooth)" and h.remote.key_names() == ["SLEEP"]
    assert "out of time (2.5s) right after the key went out" in record["steps"]
    assert h.remote.disconnects == 1


async def test_repeats_that_do_not_fit_before_the_machine_goes_down_are_not_attempted(tmp_path):
    h = Harness(tmp_path, patch={"power": {"off_repeat": 3, "repeat_gap": 6.0}})
    h.remote.state.connected = h.remote.state.ready = True
    record = await h.service.handle_event("suspend", budget=3.5)
    assert record["ok"] and record["detail"] == "TV off (Bluetooth)"
    assert h.remote.key_names() == ["SLEEP"]  # the key went out once: that is a success
    assert "Bluetooth: sent SLEEP x1 of 3 (no time for more)" in record["steps"]
    assert 6.0 not in h.sleeps  # the gap before a repeat that cannot be finished is not started

    # Repeats that fit are sent on the way down as well.
    h2 = Harness(tmp_path, patch={"power": {"off_repeat": 3, "repeat_gap": 0.2}})
    h2.remote.state.connected = h2.remote.state.ready = True
    record = await h2.service.handle_event("suspend", budget=3.5)
    assert record["ok"] and h2.remote.key_names() == ["SLEEP"] * 3
    assert "Bluetooth: sent SLEEP x3" in record["steps"]

    # A manual "TV off" has twenty seconds: room for these.
    h3 = Harness(tmp_path, patch={"power": {"off_repeat": 3, "repeat_gap": 6.0}})
    h3.remote.state.connected = h3.remote.state.ready = True
    record = await h3.service.tv_off()
    assert record["ok"] and h3.remote.key_names() == ["SLEEP"] * 3


# --------------------------------------------------------------------------- a second route tries again

# The link is down because the settings have it down, and the first attempt gets 0.3 s.
LINK_DOWN_BY_DESIGN = {"bluetooth": {"reconnect_on_drop": False, "off_connect_timeout": 0.3}}


async def test_a_second_route_tries_again_when_the_link_was_down_by_design(tmp_path):
    """logind's route failed to get the TV back in its three seconds; the sleep hook has six."""
    h = Harness(tmp_path, remote=FakeRemote(connect_after=0.6), patch=LINK_DOWN_BY_DESIGN)
    first = await h.service.handle_event("suspend", budget=3.5)
    assert not first["ok"] and "did not connect within 0.3s" in first["detail"] and h.remote.keys == []
    assert h.service.phase == Phase.DOWN and h.remote.disconnects == 1

    second = await h.service.handle_event("suspend", budget=6.0)
    assert second["ok"] and not second["skipped"] and second["detail"] == "TV off (Bluetooth)"
    assert h.remote.key_names() == ["SLEEP"]
    assert "second attempt: the first one could not reach the TV in its time" in second["steps"]
    # It waits for the TV for as long as its own time allows (less the key and the pause after it).
    assert h.remote.ensure_calls == [0.3, pytest.approx(6.0 - 0.08 - 0.4 - 0.1)]
    assert h.service.phase == Phase.DOWN and h.remote.disconnects == 2  # and tidies up again
    assert [r["ok"] for r in h.service.status()["history"]] == [False, True]

    third = await h.service.handle_event("suspend", budget=6.0)
    assert third["skipped"] and "duplicate" in third["detail"] and h.remote.key_names() == ["SLEEP"]


async def test_the_second_attempt_is_made_once(tmp_path):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch=LINK_DOWN_BY_DESIGN)
    assert not (await h.service.handle_event("shutdown", budget=3.5))["ok"]
    second = await h.service.handle_event("shutdown", budget=0.5)
    assert not second["ok"] and not second["skipped"]
    assert h.remote.ensure_calls == [0.3, 0.3]  # never less than the first attempt had
    third = await h.service.handle_event("shutdown", budget=0.5)
    assert third["skipped"] and "duplicate" in third["detail"] and len(h.remote.ensure_calls) == 2

    # The next cycle starts afresh.
    h.clock.sleep_for(600)
    h.remote.connect_after = 0.0
    assert (await h.service.handle_event("resume"))["ok"]
    await h.remote.disconnect()
    h.remote.connect_after = None
    assert not (await h.service.handle_event("suspend", budget=3.5))["ok"]
    assert not (await h.service.handle_event("suspend", budget=0.5))["skipped"]


async def test_an_attempt_that_ran_out_of_time_before_any_key_is_tried_again_too(tmp_path):
    patch = {"bluetooth": {"idle_disconnect_seconds": 60.0, "off_connect_timeout": 30.0}}
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch=patch)
    first = await h.service.handle_event("suspend", budget=0.2)
    assert not first["ok"] and "ran out of time (0.2s)" in first["detail"]
    h.remote.connect_after = 0.0
    second = await h.service.handle_event("suspend", budget=6.0)
    assert second["ok"] and h.remote.key_names() == ["SLEEP"]


@pytest.mark.parametrize(
    "patch, fail_code, why",
    [
        # The shipped settings keep the link up: a link that is down means a TV that does not
        # answer, and waiting longer would only make every such suspend slower.
        ({"bluetooth": {"off_connect_timeout": 0.3}}, None, "the link is kept up by design"),
        # Waiting is pointless (Bluetooth is off, no TV is paired): not a matter of time.
        (LINK_DOWN_BY_DESIGN, "bluetooth_off", "not a reachability failure"),
        (LINK_DOWN_BY_DESIGN | {"enabled": False}, None, "skipped"),
        (LINK_DOWN_BY_DESIGN | {"events": {"suspend_tv_off": False}}, None, "skipped"),
    ],
)
async def test_no_second_attempt(tmp_path, patch, fail_code, why):
    h = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch=patch)
    h.remote.fail_code = fail_code
    await h.service.handle_event("suspend", budget=3.5)
    h.remote.fail_code, h.remote.connect_after = None, 0.0  # it would work now
    second = await h.service.handle_event("suspend", budget=6.0)
    assert second["skipped"] and "duplicate" in second["detail"], why
    assert h.remote.keys == [] and h.remote.disconnects == 1


async def test_no_second_attempt_after_a_key_went_out_or_the_display_check_said_no(tmp_path):
    h = Harness(tmp_path, patch=LINK_DOWN_BY_DESIGN)
    h.remote.state.connected = h.remote.state.ready = True
    assert (await h.service.handle_event("suspend", budget=3.5))["ok"]
    assert (await h.service.handle_event("suspend", budget=6.0))["skipped"]
    assert h.remote.key_names() == ["SLEEP"]

    undocked = {"card0-eDP-1": "connected", "card0-DP-1": "disconnected"}
    h2 = Harness(
        tmp_path / "undocked", remote=FakeRemote(connect_after=None), patch=LINK_DOWN_BY_DESIGN, connectors=undocked
    )
    assert "no external display" in (await h2.service.handle_event("suspend", budget=3.5))["detail"]
    h2.remote.connect_after = 0.0
    assert "duplicate" in (await h2.service.handle_event("suspend", budget=6.0))["detail"]
    assert h2.remote.keys == []

    # A simulated event ("Test sleep") is no cycle of its own and leaves nothing to retry.
    h3 = Harness(tmp_path, remote=FakeRemote(connect_after=None), patch=LINK_DOWN_BY_DESIGN)
    assert not (await h3.service.handle_event("suspend", budget=3.5, simulate=True))["ok"]
    h3.remote.connect_after = 0.0
    assert (await h3.service.handle_event("suspend", budget=6.0))["ok"]  # the first real one
    assert h3.remote.ensure_calls == [0.3, 0.3]


async def test_a_duplicate_that_arrives_during_the_first_attempt_waits_and_then_tries_again(tmp_path):
    class Remote(FakeRemote):
        """The first call for the TV goes unanswered, for as long as the test says."""

        def __init__(self):
            super().__init__(connect_after=0.0)
            self.calling = asyncio.Event()
            self.give_up = asyncio.Event()

        async def ensure_ready(self, timeout, *, wake=False):
            if self.ensure_calls:
                return await super().ensure_ready(timeout, wake=wake)
            self.ensure_calls.append(timeout)
            self.calling.set()
            await self.give_up.wait()
            return False

    h = Harness(tmp_path, remote=Remote(), patch=LINK_DOWN_BY_DESIGN)
    first = asyncio.create_task(h.service.handle_event("suspend", budget=30.0))
    await asyncio.wait_for(h.remote.calling.wait(), 10)
    assert h.service.phase == Phase.GOING_DOWN
    second = asyncio.create_task(h.service.handle_event("suspend", budget=6.0))
    await asyncio.sleep(0.2)
    assert not first.done() and not second.done() and h.remote.ensure_calls == [0.3]  # it waits its turn
    h.remote.give_up.set()
    assert not (await first)["ok"]
    assert (await second)["ok"] and h.remote.key_names() == ["SLEEP"] and h.remote.disconnects == 2


# --------------------------------------------------------------------------- a down event nobody followed


async def test_a_down_event_the_machine_did_not_follow_stops_counting_after_a_while(tmp_path):
    """"tcl-me-remo event shutdown" typed by hand, a suspend that failed without a word:
    the next real suspend must not be dropped as a duplicate for ever."""
    stale = service_module.STALE_DOWN_SECONDS
    assert stale >= 60  # clearly longer than a real suspend or shutdown takes to begin
    h = Harness(tmp_path, remote=OrderedRemote())
    h.remote.state.connected = h.remote.state.ready = True
    assert not h.service.check_still_up(time.monotonic() + 10 * stale)  # running: nothing to do
    await h.service.handle_event("shutdown")
    assert h.service.phase == Phase.DOWN
    now = time.monotonic()
    assert not h.service.check_still_up(now + stale - 10) and h.service.phase == Phase.DOWN
    assert (await h.service.handle_event("suspend"))["skipped"]  # until then it is a duplicate

    assert h.service.check_still_up(now + stale + 10)
    assert h.service.phase == Phase.RUNNING and h.service.status()["phase"] == "running"
    assert h.remote.order == ["disconnect", "resume_link"]  # the link the down action closed
    assert not h.service.check_still_up(now + 10 * stale)
    h.remote.state.connected = h.remote.state.ready = True
    h.remote.keys.clear()
    record = await h.service.handle_event("suspend")
    assert not record["skipped"] and h.remote.key_names() == ["SLEEP"]


async def test_a_machine_that_slept_is_not_mistaken_for_one_that_never_went_down(tmp_path):
    h = Harness(tmp_path, patch={"power": {"on_repeat": 1}, "input": {"method": "none"}})
    await h.service.handle_event("suspend")
    h.clock.sleep_for(3600)  # it slept; the resume has not been reported yet
    assert not h.service.check_still_up(time.monotonic() + 10 * service_module.STALE_DOWN_SECONDS)
    assert h.service.phase == Phase.DOWN
    assert not (await h.service.handle_event("resume"))["skipped"]  # ...and is handled as a wake
    assert h.remote.key_names()[-1] == "WAKE"


# --------------------------------------------------------------------------- the link after boot and wake


async def test_every_real_boot_and_resume_resumes_the_link_once(tmp_path):
    h = Harness(tmp_path)
    await h.service.handle_event("boot")
    assert h.remote.resumes == 1
    await h.service.handle_event("boot")  # duplicate
    assert h.remote.resumes == 1
    await h.service.handle_event("resume")  # no suspend before it and the clocks agree: duplicate
    assert h.remote.resumes == 1

    await h.service.handle_event("suspend")
    h.clock.sleep_for(600)
    assert h.remote.resumes == 1  # going down resumes nothing
    await h.service.handle_event("resume")
    assert h.remote.resumes == 2
    await h.service.handle_event("resume")  # the sleep.target unit after logind
    assert h.remote.resumes == 2

    # A sleep nobody announced, noticed by the clocks alone, is a real resume too.
    h.clock.sleep_for(600)
    await h.service.handle_event("resume")
    assert h.remote.resumes == 3


async def test_simulated_up_events_do_not_resume_the_link_and_a_simulated_down_event_gives_it_back(tmp_path):
    h = Harness(tmp_path, remote=OrderedRemote())
    await h.service.handle_event("boot", simulate=True)
    await h.service.handle_event("resume", simulate=True)
    assert h.remote.resumes == 0
    # "Test sleep" closes the link like a real suspend, but the machine stays up and no wake
    # follows: left parked, the link would be down when the next real suspend needs it.
    await h.service.handle_event("suspend", simulate=True)
    assert h.remote.order == ["disconnect", "resume_link"] and h.service.phase == Phase.RUNNING

    # Simulated while the machine really is going down: the real event has the say.
    h.remote.order.clear()
    await h.service.handle_event("suspend")
    await h.service.handle_event("suspend", simulate=True)
    assert "resume_link" not in h.remote.order and h.service.phase == Phase.DOWN


@pytest.mark.parametrize(
    "patch, connectors, reason",
    [
        ({"enabled": False}, None, "turned off"),
        ({"events": {"resume_tv_on": False, "resume_switch_input": False}}, None, "nothing is set"),
        (None, {"card0-eDP-1": "connected", "card0-DP-1": "disconnected"}, "no external display"),
    ],
)
async def test_the_link_is_resumed_on_a_wake_in_which_nothing_is_sent(tmp_path, patch, connectors, reason):
    """Otherwise the link the suspend closed stays down until the next suspend needs it."""
    h = Harness(tmp_path, patch=patch, connectors=connectors)
    await h.service.handle_event("suspend")
    h.clock.sleep_for(60)
    h.remote.ensure_calls.clear()
    h.remote.keys.clear()
    record = await h.service.handle_event("resume")
    assert record["skipped"] and reason in record["detail"]
    assert h.remote.resumes == 1 and h.remote.ensure_calls == [] and h.remote.keys == []


async def test_the_link_is_resumed_before_anything_else_is_decided(tmp_path):
    order = []

    class Remote(FakeRemote):
        def resume_link(self):
            order.append("resume_link")
            super().resume_link()

        async def ensure_ready(self, timeout, *, wake=False):
            order.append("ensure_ready")
            return await super().ensure_ready(timeout, wake=wake)

    h = Harness(tmp_path, remote=Remote())
    await h.service.handle_event("boot")
    assert order[:2] == ["resume_link", "ensure_ready"]


async def test_a_failing_resume_link_does_not_cost_the_wake_action(tmp_path, caplog):
    class Remote(FakeRemote):
        def resume_link(self):
            raise RuntimeError("boom")

    h = Harness(tmp_path, remote=Remote())
    record = await h.service.handle_event("boot")
    assert record["ok"] and "WAKE" in h.remote.key_names()
    assert "could not resume the Bluetooth link" in caplog.text


async def test_a_shutdown_that_is_called_off_resumes_the_link(tmp_path):
    h = Harness(tmp_path)
    h.service.cancel_shutdown()  # nothing was going down: nothing to resume
    assert h.remote.resumes == 0
    await h.service.handle_event("shutdown")
    h.service.cancel_shutdown()
    assert h.remote.resumes == 1 and h.service.phase == Phase.RUNNING


class OrderedRemote(FakeRemote):
    """Records the order of the two calls that park and un-park the link."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.order = []

    async def disconnect(self, *, fast=False):
        self.order.append("disconnect")
        await super().disconnect(fast=fast)

    def resume_link(self):
        self.order.append("resume_link")
        super().resume_link()


async def down_event_in_flight(h, name="suspend"):
    """Start a real down event and return when its power key is being sent.

    The key waits at the remote's gate, so the event stays "in flight" for as long as the
    test needs, without a race against a timer: ``h.remote.key_gate.set()`` lets it finish.
    """
    h.remote.key_gate = asyncio.Event()
    h.remote.key_waiting.clear()
    task = asyncio.create_task(h.service.handle_event(name, budget=30.0))
    await asyncio.wait_for(h.remote.key_waiting.wait(), 10)
    assert h.service.phase == Phase.GOING_DOWN
    return task


@pytest.mark.parametrize("patch", [None, {"events": {"resume_tv_on": False, "resume_switch_input": False}}])
async def test_a_wake_that_overtakes_the_down_action_is_the_last_word(tmp_path, patch):
    """logind stops waiting after its delay, so a machine can sleep and wake with the down
    action unfinished.  When that action then ends, the machine is up: it must not be marked
    "down" (the next suspend would look like a duplicate and leave the TV on), and the link
    its last step has just parked must not stay parked."""
    h = Harness(tmp_path, remote=OrderedRemote(), patch=patch)
    h.remote.state.connected = h.remote.state.ready = True
    down = await down_event_in_flight(h)
    h.clock.sleep_for(600)
    up = asyncio.create_task(h.service.handle_event("resume"))
    await asyncio.sleep(0)  # the wake is taken note of, and its action (if any) waits its turn
    assert h.service.phase == Phase.RUNNING and not down.done()
    h.remote.key_gate.set()
    assert (await down)["ok"] and (await up)["ok"]
    assert h.service.phase == Phase.RUNNING and h.service.status()["phase"] == "running"
    # Un-parked when the wake was handled, parked by the down action's last step, un-parked again.
    assert h.remote.order == ["resume_link", "disconnect", "resume_link"]

    # The next suspend is a real one.
    h.remote.state.connected = h.remote.state.ready = True
    h.remote.keys.clear()
    record = await h.service.handle_event("suspend")
    assert not record["skipped"] and h.remote.key_names() == ["SLEEP"]
    assert h.service.phase == Phase.DOWN and h.remote.order[-1] == "disconnect"


async def test_a_shutdown_called_off_while_its_action_still_runs_leaves_the_link_resumed(tmp_path):
    h = Harness(tmp_path, remote=OrderedRemote())
    h.remote.state.connected = h.remote.state.ready = True
    down = await down_event_in_flight(h, "shutdown")
    h.service.cancel_shutdown()
    h.remote.key_gate.set()
    assert (await down)["ok"]
    assert h.service.phase == Phase.RUNNING
    assert h.remote.order == ["resume_link", "disconnect", "resume_link"]
    assert not (await h.service.handle_event("shutdown"))["skipped"]


async def test_an_overtaken_down_action_does_not_speak_for_the_newer_down_event(tmp_path):
    """A sleep nobody announced the end of, then the next suspend while the first action is
    still running: the machine *is* going down, and nothing is to be resumed."""
    h = Harness(tmp_path, remote=OrderedRemote())
    h.remote.state.connected = h.remote.state.ready = True
    first = await down_event_in_flight(h)
    h.clock.sleep_for(600)
    h.remote.key_gate.set()  # (the newer event cancels the first before it can use this)
    second = await h.service.handle_event("suspend")
    assert not second["skipped"]
    assert "interrupted" in (await first)["detail"]
    assert h.service.phase == Phase.DOWN and "resume_link" not in h.remote.order


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


async def test_an_action_pre_empted_before_its_first_step_still_answers_its_caller(tmp_path):
    # "TV on" from the panel and a suspend in the same instant: the caller of "TV on" is the
    # control connection's handler, and must get a record to send back, not a cancellation.
    h = Harness(tmp_path, patch={"input": {"method": "none"}})
    h.remote.state.connected = h.remote.state.ready = True
    manual = asyncio.ensure_future(h.service.tv_on())
    down = asyncio.ensure_future(h.service.handle_event("suspend"))
    manual_record, down_record = await asyncio.gather(manual, down)
    assert not manual_record["ok"] and "interrupted" in manual_record["detail"]
    assert down_record["ok"] and h.remote.key_names() == ["SLEEP"]
    assert [r["name"] for r in h.service.status()["history"]] == ["tv_on", "tv_off"]


async def test_resume_waits_for_the_down_action_instead_of_cancelling_it(tmp_path):
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    down = await down_event_in_flight(h)
    h.clock.sleep_for(600)  # logind stopped waiting: the machine slept and woke with the action unfinished
    up = asyncio.create_task(h.service.handle_event("resume"))
    await asyncio.sleep(0.2)
    assert not up.done() and not down.done() and h.remote.keys == []  # it waits; it does not cancel
    h.remote.key_gate.set()
    down_record, up_record = await down, await up
    assert down_record["ok"] and up_record["ok"]
    assert h.remote.key_names()[0] == "SLEEP" and "WAKE" in h.remote.key_names()[1:]
    assert h.service.phase == Phase.RUNNING


async def test_a_late_notification_of_the_previous_wake_does_not_undo_the_next_suspend(tmp_path):
    """Each wake is reported twice (logind, then the sleep hook, which needs a Python start-up).
    If the user suspends again in between, the second report arrives while the new down
    action runs.  The machine cannot have slept for *that* suspend yet: it is a duplicate, and
    must not queue a "TV on" behind the "TV off"."""
    h = Harness(tmp_path, patch={"power": {"on_repeat": 1}, "input": {"method": "none"}})
    h.remote.state.connected = h.remote.state.ready = True
    await h.service.handle_event("suspend")
    h.clock.sleep_for(3600)
    assert not (await h.service.handle_event("resume"))["skipped"]  # the wake, first report
    h.remote.keys.clear()
    down = await down_event_in_flight(h)  # suspended again at once
    late = asyncio.create_task(h.service.handle_event("resume"))  # the same wake, second report
    await asyncio.sleep(0)
    assert late.done(), "taken for a new wake: it is waiting to turn the TV on behind the TV off"
    assert late.result()["skipped"] and "duplicate" in late.result()["detail"]
    h.remote.key_gate.set()
    assert (await down)["ok"]
    assert h.remote.key_names() == ["SLEEP"] and h.service.phase == Phase.DOWN

    # The wake that really follows this suspend still counts, with or without a clock jump.
    up = await h.service.handle_event("resume")
    assert not up["skipped"] and h.remote.key_names() == ["SLEEP", "WAKE"]


async def test_queued_event_does_not_block_a_suspend(tmp_path):
    # A manual action is running, a wake event is waiting its turn behind it, and then the
    # machine suspends: the suspend must still get through at once.
    h = Harness(tmp_path, patch={"input": {"delay_after_wake": 20.0}})
    h.service._sleep = asyncio.sleep  # the manual tv_on now really waits 20 s before its macro
    manual = asyncio.create_task(h.service.tv_on())
    await asyncio.sleep(0.05)
    h.clock.sleep_for(60)
    queued = asyncio.create_task(h.service.handle_event("resume"))
    await asyncio.sleep(0.05)
    h.service._sleep = lambda seconds: asyncio.sleep(0)
    started = asyncio.get_running_loop().time()
    down = await h.service.handle_event("suspend", budget=15.0)
    assert asyncio.get_running_loop().time() - started < 8.0  # not behind the 20 s of the manual action
    assert down["ok"] and h.remote.key_names()[-1] == "SLEEP"
    assert not (await manual)["ok"]
    late = await asyncio.wait_for(queued, 5)
    assert late["skipped"] and "superseded" in late["detail"]
    assert h.remote.key_names()[-1] == "SLEEP"  # the queued wake did not undo the TV-off


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
        "wifi": {
            "enabled": True,
            "host": "tv.lan",
            "input_mode": "app_link",
            "link_preset": "custom",
            "app_link": "tv://input/HDMI{port}",
        },
    }
    h = Harness(tmp_path, wifi=wifi, patch=patch)
    assert (await h.service.switch_input())["ok"]
    assert wifi.links == [("tv.lan", "tv://input/HDMI2")]
    assert h.service.status()["input"]["wifi_link"] == "tv://input/HDMI2"

    # built-in TV tables: the ids are not a simple function of the port number
    h.service.set_config({"wifi": {"link_preset": "tcl-realtek"}, "input": {"hdmi_port": 4}})
    assert (await h.service.switch_input())["ok"]
    assert wifi.links[-1] == (
        "tv.lan",
        "content://android.media.tv/passthrough/com.tcl.tvinput%2F.passthroughinput.TvPassThroughService%2FHW1413745664",
    )
    h.service.set_config({"wifi": {"link_preset": "tcl-mediatek"}, "input": {"hdmi_port": 1}})
    assert (await h.service.switch_input())["ok"]
    assert wifi.links[-1][1].endswith("com.tcl.tvinput%2F.TvPassThroughService%2FHW15")

    # a custom preset with nothing entered is a configuration problem, not a crash
    h.service.set_config(
        {"wifi": {"link_preset": "custom", "app_link": ""}, "input": {"wifi_fallback_to_macro": False}}
    )
    record = await h.service.switch_input()
    assert not record["ok"] and "no app link" in record["detail"]


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


async def test_a_method_can_be_tried_before_it_is_turned_on(tmp_path):
    wifi = FakeWifi()
    patch = {"input": {"method": "none"}, "wifi": {"enabled": True, "host": "tv.lan"}}
    h = Harness(tmp_path, wifi=wifi, patch=patch)
    assert not (await h.service.switch_input())["ok"]  # switching is off...
    record = await h.service.switch_input("macro")  # ...but each method can be tried
    assert record["ok"] and record["detail"] == "input switched (Bluetooth macro)"
    assert h.remote.key_names() == ["HOME", "DOWN", "OK"]
    record = await h.service.rpc("switch_input", {"via": "wifi"})
    assert record["ok"] and record["detail"] == "input switched (Wi-Fi)"
    assert wifi.keys == [("tv.lan", 243)]

    # an explicit method never falls back to the other one: the user asked to see *this* one
    wifi.fail_code = "wifi_error"
    h.remote.keys.clear()
    record = await h.service.switch_input("wifi")
    assert not record["ok"] and h.remote.keys == []

    for bad in ("telepathy", 5):
        with pytest.raises(RpcError) as err:
            await h.service.rpc("switch_input", {"via": bad})
        assert err.value.code == "bad_request"


async def test_macro_method_without_macro_explains(tmp_path):
    h = Harness(tmp_path, patch={"input": {"macro": ""}})
    record = await h.service.switch_input()
    assert not record["ok"] and "no input macro" in record["detail"]


async def test_shipped_defaults_do_not_press_navigation_keys(tmp_path):
    # Out of the box only the discrete power keys are sent: an untested input macro could
    # wander around the TV's UI on every boot.
    remote = FakeRemote()
    service = Service(C.default_config(), remote=remote, sys_root=make_sysfs(tmp_path, {"card0-HDMI-A-1": "connected"}))
    record = await service.handle_event("boot")
    assert record["ok"] and record["detail"] == "TV on (Bluetooth)"
    assert remote.key_names() == ["WAKE"]  # exactly one press
    down = await service.handle_event("suspend")
    assert down["ok"] and remote.key_names() == ["WAKE", "SLEEP"]


async def test_wake_measures_are_requested_only_when_turning_the_tv_on(tmp_path):
    # The wake beacon can rouse a suspended TV.  That is wanted for "TV on" and for nothing
    # else: waking a TV to tell it to sleep, or to press a key, would leave it on.
    h = Harness(tmp_path)
    await h.service.tv_on(switch_input=False)
    assert h.remote.wake_flags == [True]

    for action in (h.service.tv_off, h.service.switch_input, lambda: h.service.send_key("HOME")):
        await h.remote.disconnect()
        h.remote.wake_flags.clear()
        await action()
        assert h.remote.wake_flags == [False], action

    await h.remote.disconnect()
    h.remote.wake_flags.clear()
    await h.service.handle_event("suspend")
    assert h.remote.wake_flags == [False]


@pytest.mark.parametrize("spec", ["POWER", "S:81", "s:0x81", "C:0030", "K:66", "C:019E"])
async def test_power_toggle_is_never_repeated(tmp_path, spec):
    """A toggle sent twice undoes itself.  What makes a key a toggle is its usage, not its name."""
    stored = C.apply_patch(C.default_config(), {"power": {"on_key": spec}}).power.on_key
    patch = {"power": {"on_key": spec, "on_repeat": 3, "off_key": spec, "off_repeat": 3}, "input": {"method": "none"}}
    h = Harness(tmp_path, patch=patch)
    assert (await h.service.tv_on())["ok"]
    assert h.remote.key_names() == [stored]
    assert (await h.service.tv_off())["ok"]
    assert h.remote.key_names() == [stored, stored]
    h.remote.keys.clear()
    await h.remote.disconnect()
    assert (await h.service.handle_event("suspend"))["ok"]
    assert h.remote.key_names() == [stored]


class LingeringWifi(FakeWifi):
    """Like the real assist: the key is on the wire at once, the call returns a little later."""

    def __init__(self, order, linger):
        super().__init__()
        self.order = order
        self.linger = linger

    async def send_key(self, host, keycode, *, timeout):
        await super().send_key(host, keycode, timeout=timeout)
        self.order.append(("wifi", keycode))
        await asyncio.sleep(self.linger)


class NotingRemote(FakeRemote):
    def __init__(self, order, **kwargs):
        super().__init__(**kwargs)
        self.order = order

    async def ensure_ready(self, timeout, *, wake=False):
        ready = await super().ensure_ready(timeout, wake=wake)
        self.order.append(("ble gave up" if not ready else "ble ready", None))
        return ready

    async def send_key(self, spec, hold):
        await super().send_key(spec, hold)
        self.order.append(("ble", spec))


async def test_a_toggle_as_off_key_is_never_raced_against_the_wifi_assist(tmp_path):
    """The assist's SLEEP followed by a Bluetooth toggle turns the TV off and back on."""
    wifi_on = {"wifi": {"enabled": True, "host": "tv.lan"}, "bluetooth": {"off_connect_timeout": 30.0}}

    # Control, discrete off key: both at once, and the TV getting SLEEP twice does no harm.
    order = []
    h = Harness(tmp_path, remote=NotingRemote(order, connect_after=0.3), wifi=LingeringWifi(order, 0.8), patch=wifi_on)
    assert (await h.service.handle_event("suspend", budget=25.0))["ok"]
    assert order[0] == ("wifi", 223)

    # A toggle: Bluetooth alone first.  The TV answers, the toggle goes out, Wi-Fi is not used.
    order = []
    patch = wifi_on | {"power": {"off_key": "S:81"}}
    h = Harness(tmp_path, remote=NotingRemote(order, connect_after=0.3), wifi=LingeringWifi(order, 0.8), patch=patch)
    record = await h.service.handle_event("suspend", budget=25.0)
    assert record["ok"] and record["detail"] == "TV off (Bluetooth)"
    assert order == [("ble ready", None), ("ble", "S:81")] and h.wifi.keys == []

    # The TV does not answer: only when Bluetooth has given up is the (discrete) SLEEP sent
    # over Wi-Fi, and Bluetooth gives up early enough to leave the assist its share.
    order = []
    h = Harness(tmp_path, remote=NotingRemote(order, connect_after=None), wifi=LingeringWifi(order, 0.0), patch=patch)
    record = await h.service.handle_event("suspend", budget=3.5)
    assert record["ok"] and record["detail"] == "TV off (Wi-Fi)"
    assert order == [("ble gave up", None), ("wifi", 223)] and h.remote.keys == []
    wait = h.remote.ensure_calls[0]
    assert wait <= 3.5 - service_module.FLUSH_SECONDS - service_module.WIFI_AFTER_BLE_SECONDS + 0.001


# --------------------------------------------------------------------------- a toggle as on key

TOGGLE = {"power": {"on_key": "POWER", "off_key": "POWER"}, "input": {"method": "none"}}


async def wake(h, seconds=600):
    """A real resume: the machine slept and says so."""
    h.clock.sleep_for(seconds)
    return await h.service.handle_event("resume")


async def test_an_on_toggle_is_sent_automatically_only_after_our_own_off(tmp_path):
    """A toggle turns a TV that is on off.  The only time the TV is known to be off is right
    after this service turned it off."""
    h = Harness(tmp_path, patch=TOGGLE)
    h.remote.state.connected = h.remote.state.ready = True

    # Nothing is known about the TV yet: it is taken to be on, and left alone.
    boot = await h.service.handle_event("boot")
    assert boot["ok"] and boot["skipped"] and "is a toggle" in boot["detail"] and "by hand" in boot["detail"]
    assert h.remote.keys == [] and h.service.status()["last_action"] == boot  # the user can read why

    # We turn it off at suspend; the wake after that turns it back on.
    assert (await h.service.handle_event("suspend"))["ok"]
    up = await wake(h)
    assert up["ok"] and not up["skipped"] and h.remote.key_names() == ["POWER", "POWER"]

    # A suspend that leaves the TV on (turned off in the settings): the wake must not toggle.
    h.service.set_config({"events": {"suspend_tv_off": False}})
    assert (await h.service.handle_event("suspend"))["skipped"]
    up = await wake(h)
    assert up["skipped"] and "is a toggle" in up["detail"] and h.remote.key_names() == ["POWER", "POWER"]

    # A simulated wake ("Test wake") shows what a real one would do.
    assert (await h.service.handle_event("resume", simulate=True))["skipped"]
    assert h.remote.key_names() == ["POWER", "POWER"]


async def test_the_direction_of_the_last_power_key_survives_a_reboot(tmp_path):
    state = tmp_path / "state" / "power.json"

    def started(**kwargs):  # the service starting in a fresh process
        h = Harness(tmp_path, patch=TOGGLE, power_state_path=state, **kwargs)
        h.remote.state.connected = h.remote.state.ready = True
        return h

    h = started()
    assert (await h.service.handle_event("shutdown"))["ok"] and h.remote.key_names() == ["POWER"]
    assert json.loads(state.read_text())["last"] == "off"

    # Power-off, then boot: the TV is off because we turned it off.
    h = started()
    assert (await h.service.handle_event("boot"))["ok"] and h.remote.key_names() == ["POWER"]
    assert json.loads(state.read_text())["last"] == "on"

    # Reboot (the TV stays on by default), then boot: the toggle would turn it off.
    assert (await h.service.handle_event("reboot"))["skipped"]
    h = started()
    boot = await h.service.handle_event("boot")
    assert boot["skipped"] and "is a toggle" in boot["detail"] and h.remote.keys == []

    # A file that cannot be read or written costs nothing but the memory.
    state.write_text("{ not json")
    h = started()
    assert (await h.service.handle_event("boot"))["skipped"]
    state.unlink()
    state.parent.rmdir()
    state.parent.write_text("in the way")
    assert (await h.service.tv_off())["ok"] and h.remote.key_names() == ["POWER"]
    assert not (await wake(h))["skipped"]  # remembered for as long as the service runs


async def test_manual_power_always_sends_a_toggle_and_is_remembered(tmp_path):
    h = Harness(tmp_path, patch=TOGGLE)
    h.remote.state.connected = h.remote.state.ready = True
    assert (await h.service.tv_on())["ok"] and h.remote.key_names() == ["POWER"]
    assert (await h.service.tv_on())["ok"] and h.remote.key_names() == ["POWER", "POWER"]  # by hand: always
    assert (await wake(h))["skipped"]  # the last key sent was an "on"
    assert (await h.service.tv_off())["ok"] and h.remote.key_names() == ["POWER"] * 3
    assert not (await wake(h))["skipped"] and h.remote.key_names() == ["POWER"] * 4


@pytest.mark.parametrize("how", ["key", "macro", "wake key"])
async def test_a_power_key_pressed_by_hand_after_our_off_is_taken_into_account(tmp_path, how):
    h = Harness(tmp_path, patch=TOGGLE)
    h.remote.state.connected = h.remote.state.ready = True
    assert (await h.service.tv_off())["ok"]
    if how == "key":
        await h.service.send_key("S:81")  # the user toggled it back on with the on-screen remote
    elif how == "macro":
        assert (await h.service.run_macro("POWER 1s HOME"))["ok"]
    else:
        await h.service.send_key("WAKE")
    sent = len(h.remote.keys)
    up = await wake(h)
    assert up["skipped"] and "is a toggle" in up["detail"] and len(h.remote.keys) == sent

    # ...while a discrete "off" by hand is as good as ours.
    await h.service.send_key("SLEEP")
    assert not (await wake(h))["skipped"] and h.remote.key_names()[-1] == "POWER"


async def test_a_held_back_toggle_does_not_hold_back_the_input_switch(tmp_path):
    h = Harness(tmp_path, patch={"power": {"on_key": "C:0030"}})
    record = await h.service.handle_event("boot")
    assert record["ok"] and not record["skipped"] and record["detail"] == "input switched (Bluetooth macro)"
    assert h.remote.key_names() == ["HOME", "DOWN", "OK"]
    assert any(step.startswith("TV on: not sent: the on key (C:0030) is a toggle") for step in record["steps"])
    assert 2.0 not in h.sleeps  # no wake, so no wait for the TV to start


async def test_discrete_power_keys_are_sent_whatever_was_sent_before(tmp_path):
    h = Harness(tmp_path, patch={"power": {"on_repeat": 1}, "input": {"method": "none"}})
    assert not (await h.service.handle_event("boot"))["skipped"]
    assert not (await wake(h))["skipped"]  # twice in a row, no "off" in between: harmless with WAKE
    assert h.remote.key_names() == ["WAKE", "WAKE"]


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


async def test_a_key_held_by_hand_does_not_keep_the_tv_on_when_the_machine_suspends(tmp_path):
    """The transport sends one key at a time.  A key held for ten seconds would keep SLEEP
    waiting until the machine is asleep."""
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    h.remote.honour_hold = True
    held = asyncio.create_task(h.service.rpc("send_key", {"key": "OK", "hold": 10}))
    queued = asyncio.create_task(h.service.send_key("VOL_UP", 10.0))  # waits for the first
    await asyncio.sleep(0.05)
    record = await h.service.handle_event("suspend", budget=3.5)
    assert record["ok"] and record["detail"] == "TV off (Bluetooth)", record
    assert h.remote.key_names() == ["SLEEP"]  # neither held key was seen through
    assert "a key that was being held by hand was cut short" in record["steps"]

    # The caller of the key is answered, with an error it can show (its task is not cancelled).
    with pytest.raises(RpcError) as err:
        await held
    assert err.value.code == "busy" and "cut short" in err.value.message
    with pytest.raises(RpcError) as err:
        await queued
    assert err.value.code == "busy"

    # Afterwards keys work as before.
    await wake(h)
    assert await h.service.send_key("HOME") == {"key": "HOME"}


async def test_a_caller_that_goes_away_takes_its_held_key_with_it(tmp_path):
    h = Harness(tmp_path)
    h.remote.state.connected = h.remote.state.ready = True
    h.remote.honour_hold = True
    held = asyncio.create_task(h.service.send_key("OK", 10.0))
    await asyncio.sleep(0.05)
    held.cancel()
    with pytest.raises(asyncio.CancelledError):
        await held
    assert h.service._key_task is None and h.remote.keys == []
    h.remote.honour_hold = False
    assert await h.service.send_key("HOME") == {"key": "HOME"}  # the key lock is free again


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
    assert [p["name"] for p in info["presets"]][:2] == ["tcl-row", "tcl-row-home"]
    assert info["link_presets"][0]["links"]["2"].endswith("%2FHW16")

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
    assert (await rpc("check_macro", {"macro": "down*{port}", "port": 2}))["rendered"] == "DOWN*2"
    with pytest.raises(RpcError):
        await rpc("check_macro", {"macro": "OK", "port": 7})
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
    assert await rpc("wifi_test", {}) == {"reachable": True, "on": True, "name": "Living Room TV"}
    assert (await rpc("wifi_unpair", {}))["paired"] is False
    assert h.service.cfg.wifi.enabled is False
    with pytest.raises(RpcError) as err:
        await rpc("wifi_test", {})
    assert err.value.code == "wifi_not_paired"
    h.service.set_config({"wifi": {"host": ""}})
    with pytest.raises(RpcError) as err:
        await rpc("wifi_test", {})
    assert err.value.code == "wifi_not_configured"

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
