"""The service driving the real Wi-Fi assist against the fake TV.

The service's own decisions are tested with a fake assist in test_service.py; these tests
check that the two fit together: the control requests of the pairing dialogue, and that the
error codes the assist reports make the service retry (or not) as intended.
"""

import asyncio
import shutil

import pytest
from fake_tv import new_assist, pair, running_tv, seed_identity
from fakes import FakeRemote
from test_service import Harness

from tcl_me_remo.ipc import RpcError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool")

LINK = "content://android.media.tv/passthrough/com.example%2F.Input%2FHW{port}"


@pytest.fixture
async def tv(tmp_path):
    async with running_tv(tmp_path / "tv") as fake:
        yield fake


def wifi_patch(tv, **wifi):
    settings = {"enabled": True, "host": tv.host, "input_mode": "app_link", "link_preset": "custom", "app_link": LINK}
    return {"input": {"method": "wifi", "hdmi_port": 2}, "wifi": {**settings, **wifi}}


async def test_pairing_dialogue_over_the_control_requests(tv, tmp_path):
    state = tmp_path / "atv"
    seed_identity(state)
    assist = new_assist(tv, state)
    harness = Harness(tmp_path, wifi=assist)
    rpc = harness.service.rpc

    assert (await rpc("status", {}))["wifi"]["paired"] is False
    started = await rpc("wifi_pair_start", {"host": tv.host})
    assert started == {"paired": False, "pairing": True, "pairing_host": tv.host}
    with pytest.raises(RpcError) as raised:
        await rpc("wifi_pair_finish", {"code": "zzzzzz"})
    assert raised.value.code == "pairing_failed"
    assert (await rpc("status", {}))["wifi"]["pairing"] is True  # still waiting for the right code
    assert harness.service.cfg.wifi.enabled is False

    finished = await rpc("wifi_pair_finish", {"code": tv.code, "name": "Living Room TV"})
    assert finished == {"paired": True, "pairing": False, "pairing_host": ""}
    assert harness.service.cfg.wifi.enabled and harness.service.cfg.wifi.host == tv.host
    assert await rpc("wifi_test", {}) == {"reachable": True, "on": True, "name": "Fake Model"}

    assert (await rpc("wifi_unpair", {}))["paired"] is False
    with pytest.raises(RpcError) as raised:
        await rpc("wifi_test", {})
    assert raised.value.code == "wifi_not_paired"
    await tv.idle()


async def test_cancelled_pairing_dialogue(tv, tmp_path):
    state = tmp_path / "atv"
    seed_identity(state)
    harness = Harness(tmp_path, wifi=new_assist(tv, state))
    assert (await harness.service.rpc("wifi_pair_start", {"host": tv.host}))["pairing"] is True
    assert (await harness.service.rpc("wifi_pair_cancel", {}))["pairing"] is False
    await tv.idle()


async def test_input_switch_keeps_trying_until_the_tv_listens(tv, tmp_path):
    state = tmp_path / "atv"
    seed_identity(state)
    assist = new_assist(tv, state)
    await pair(assist, tv)
    await tv.idle()
    await tv.close_remote_port()  # the TV has just been woken: its network is not up yet
    harness = Harness(tmp_path, wifi=assist, patch=wifi_patch(tv))

    async def opening_later():
        await asyncio.sleep(0.15)
        await tv.open_remote_port()

    opener = asyncio.create_task(opening_later())
    waits = harness.sleeps

    async def real_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0.1)

    harness.service._sleep = real_sleep
    record = await harness.service.switch_input()
    await opener
    assert record["ok"], record
    assert tv.links == [LINK.replace("{port}", "2")]
    assert waits.count(1.0) >= 1  # at least one refused attempt came first
    await tv.idle()


@pytest.mark.parametrize("how", ["close", "reset"])
async def test_input_switch_is_sent_again_when_the_tv_hangs_up_on_the_first_attempt(tv, tmp_path, how):
    """A TV that says it is ready and drops the connection never saw the link.  The assist
    must not call that a success: the service then tries again, and the input does get switched."""
    state = tmp_path / "atv"
    seed_identity(state)
    assist = new_assist(tv, state)
    await pair(assist, tv)
    await tv.idle()
    tv.drop_at_start, tv.drop_delay = how, 0.02
    harness = Harness(tmp_path, wifi=assist, patch=wifi_patch(tv))
    waits = harness.sleeps

    async def tv_recovers(seconds):
        waits.append(seconds)
        tv.drop_at_start = ""

    harness.service._sleep = tv_recovers
    record = await harness.service.switch_input()
    assert record["ok"], record
    assert waits.count(1.0) == 1 and tv.remote_connections == 2
    assert tv.links == [LINK.replace("{port}", "2")]
    await tv.idle()


async def test_input_switch_does_not_retry_an_address_that_cannot_exist(tv, tmp_path):
    state = tmp_path / "atv"
    seed_identity(state)
    assist = new_assist(tv, state)
    await pair(assist, tv)
    patch = {**wifi_patch(tv, host="tv..lan"), "input": {"method": "wifi", "wifi_fallback_to_macro": False}}
    harness = Harness(tmp_path, wifi=assist, patch=patch)
    record = await harness.service.switch_input()
    assert not record["ok"] and "address is not valid" in record["detail"]
    assert 1.0 not in harness.sleeps  # no second attempt


async def test_input_switch_does_not_retry_when_the_tv_no_longer_accepts_us(tv, tmp_path):
    state = tmp_path / "atv"
    seed_identity(state)
    assist = new_assist(tv, state)
    await pair(assist, tv)
    tv.paired.clear()  # as after "clear storage" on the TV
    patch = {**wifi_patch(tv), "input": {"method": "wifi", "wifi_fallback_to_macro": False}}
    harness = Harness(tmp_path, wifi=assist, patch=patch)
    record = await harness.service.switch_input()
    assert not record["ok"] and "pair" in record["detail"]
    assert 1.0 not in harness.sleeps  # no second attempt
    assert tv.links == []


async def test_suspend_reaches_the_tv_over_wifi_when_bluetooth_is_down(tv, tmp_path):
    state = tmp_path / "atv"
    seed_identity(state)
    assist = new_assist(tv, state)
    await pair(assist, tv)
    patch = {"wifi": {"enabled": True, "host": tv.host}, "bluetooth": {"off_connect_timeout": 30.0}}
    harness = Harness(tmp_path, remote=FakeRemote(connect_after=None), wifi=assist, patch=patch)
    began = asyncio.get_running_loop().time()
    record = await harness.service.handle_event("suspend", budget=3.0)
    assert record["ok"] and record["detail"] == "TV off (Wi-Fi)", record
    assert asyncio.get_running_loop().time() - began < 1.5
    assert tv.keys == [(223, 3)]  # KEYCODE_SLEEP, one short press
    await tv.idle()
