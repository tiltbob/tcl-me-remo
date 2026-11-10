"""The Decky backend (main.py), exercised with a stand-in for Decky's `decky` module."""

import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path

import pytest
from fakes import FakeRemote

from tcl_me_remo import config as C
from tcl_me_remo import ipc
from tcl_me_remo.service import Service

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(tmp_path, monkeypatch):
    fake_decky = types.ModuleType("decky")
    fake_decky.DECKY_PLUGIN_DIR = str(REPO)
    fake_decky.DECKY_USER_HOME = str(tmp_path / "home" / "deck")
    fake_decky.logger = logging.getLogger("decky-test")
    monkeypatch.setitem(sys.modules, "decky", fake_decky)
    monkeypatch.syspath_prepend(str(REPO))
    sys.modules.pop("main", None)
    module = importlib.import_module("main")
    monkeypatch.setattr(module, "SYSTEM_PYTHON", sys.executable)
    monkeypatch.setattr(module, "SOCKET_PATH", str(tmp_path / "control.sock"))
    yield module
    sys.modules.pop("main", None)


@pytest.fixture
async def service(tmp_path, backend):
    cfg = C.apply_patch(C.default_config(), {"display": {"require_external": "no"}})
    remote = FakeRemote()
    svc = Service(cfg, remote=remote, sys_root=tmp_path)
    server = ipc.IpcServer(backend.SOCKET_PATH, svc.rpc, group=None)
    await server.start()
    yield svc, remote
    await server.stop()


async def test_rpc_bridge_round_trip(backend, service):
    _svc, remote = service
    plugin = backend.Plugin()
    reply = await plugin.rpc("status")
    assert reply["ok"] is True and reply["result"]["bluetooth"]["tv_name"] == "Living Room TV"
    reply = await plugin.rpc("send_key", {"key": "HOME"})
    assert reply == {"ok": True, "result": {"key": "HOME"}}
    assert remote.key_names() == ["HOME"]
    # service-side errors keep their code and message
    reply = await plugin.rpc("send_key", {"key": "BANANA"})
    assert reply["ok"] is False and reply["error"]["code"] == "bad_key"
    reply = await plugin.rpc("set_config", {"patch": {"input": {"hdmi_port": 9}}})
    assert reply["error"]["code"] == "invalid_config" and "input.hdmi_port" in reply["error"]["message"]


async def test_rpc_never_raises(backend):
    plugin = backend.Plugin()
    reply = await plugin.rpc("status")  # no service is listening
    assert reply["ok"] is False and reply["error"]["code"] == "unavailable"
    assert (await plugin.rpc(42))["error"]["code"] == "bad_request"
    assert (await plugin.rpc("status", "nope"))["error"]["code"] == "bad_request"


async def test_rpc_timeout(backend, tmp_path, monkeypatch):
    async def stuck(reader, writer):
        await asyncio.sleep(5)

    server = await asyncio.start_unix_server(stuck, path=backend.SOCKET_PATH)
    monkeypatch.setattr(backend, "DEFAULT_TIMEOUT", 0.2)
    try:
        reply = await backend.Plugin().rpc("status")
        assert reply["error"]["code"] == "timeout"
    finally:
        server.close()


async def test_service_info_runs_the_bundled_installer_with_the_system_python(backend):
    plugin = backend.Plugin()  # the panel may call before _main has run
    state = await plugin.service_info()
    assert state["installed"] is False and state["bundled_version"]
    assert set(state) >= {"installed", "active", "enabled", "installed_version", "needs_update", "problems"}


async def test_install_failure_is_reported_to_the_panel(backend, monkeypatch):
    calls = []

    async def fake_installer(*args, timeout=120.0):
        calls.append(args)
        if args[0] == "install":
            return {"error": "the service needs Python 3.11 or newer"}
        return {"installed": False, "active": False, "enabled": False, "problems": []}

    monkeypatch.setattr(backend, "_installer", fake_installer)
    plugin = backend.Plugin()
    await plugin._main()
    await plugin._startup
    assert calls[0][:2] == ("install", "--user-home")
    reply = await plugin.install_service()
    assert reply["ok"] is False and "Python 3.11" in reply["error"]["message"]
    state = await plugin.service_info()
    assert state["problems"][0].startswith("install failed:")


async def test_installing_is_reported_and_concurrent_requests_share_one_install(backend, monkeypatch):
    started, release, runs = asyncio.Event(), asyncio.Event(), []

    async def slow_installer(*args, timeout=120.0):
        if args[0] == "install":
            runs.append(args)
            started.set()
            await release.wait()
            return {"log": ["installed"], "installed": True}
        return {"installed": False, "active": False, "enabled": False, "problems": []}

    monkeypatch.setattr(backend, "_installer", slow_installer)
    plugin = backend.Plugin()
    assert (await plugin.service_info())["installing"] is False
    await plugin._main()
    await started.wait()
    assert (await plugin.service_info())["installing"] is True
    # the user presses Repair while the start-up install is still running: no second install
    pressed = asyncio.ensure_future(plugin.install_service())
    await asyncio.sleep(0.05)
    release.set()
    reply = await pressed
    assert reply == {"ok": True, "result": {"log": ["installed"], "installed": True}}
    assert len(runs) == 1
    assert (await plugin.service_info())["installing"] is False
    # afterwards a new press runs a fresh install
    assert (await plugin.install_service())["ok"] is True
    assert len(runs) == 2


async def test_uninstall_hook_only_schedules_a_check(backend, monkeypatch):
    commands = []

    async def fake_run(argv, timeout):
        commands.append(argv)
        assert timeout <= 3.0  # Decky kills the plugin 5 s after asking it to stop
        return 0, "", ""

    monkeypatch.setattr(backend, "_run", fake_run)
    plugin = backend.Plugin()
    await plugin._uninstall()
    (argv,) = commands
    assert argv[0] == "systemd-run" and "--on-active=180" in argv
    assert argv[-2:] == ["uninstall-if-orphaned", str(REPO)]
    assert "/opt/tcl-me-remo/py_modules" in argv
    # nothing was stopped or removed directly: an update must not interrupt the service
    assert not any("systemctl" in part for part in argv)


async def test_remove_and_restart(backend, monkeypatch):
    seen = []

    async def fake_installer(*args, timeout=120.0):
        seen.append(args)
        return {"log": ["removed tcl-me-remo.service"]}

    async def fake_run(argv, timeout):
        seen.append(tuple(argv))
        return 1, "", "Unit tcl-me-remo.service not found."

    monkeypatch.setattr(backend, "_installer", fake_installer)
    monkeypatch.setattr(backend, "_run", fake_run)
    plugin = backend.Plugin()
    await plugin._main()
    await plugin._startup
    assert (await plugin.remove_service(True))["ok"] is True
    assert ("uninstall", "--purge") in seen
    reply = await plugin.restart_service()
    assert reply["ok"] is False and "not found" in reply["error"]["message"]


def test_clean_env_drops_the_loaders_library_path(backend, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxyz")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in backend._clean_env()
