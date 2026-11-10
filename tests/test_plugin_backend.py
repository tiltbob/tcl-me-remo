"""The Decky backend (main.py), exercised with a stand-in for Decky's `decky` module."""

import asyncio
import importlib
import json
import logging
import os
import stat
import sys
import time
import types
from pathlib import Path

import pytest
from fakes import FakeRemote

from tcl_me_remo import config as C
from tcl_me_remo import installer, ipc
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
    monkeypatch.setattr(module, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(module, "INSTALLED_MODULES", (str(tmp_path / "opt" / "py_modules"), str(tmp_path / "app")))
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


def _service_installed(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "opt" / "py_modules" / "tcl_me_remo").mkdir(parents=True)


async def test_uninstall_hook_leaves_a_note_and_schedules_a_check(backend, monkeypatch, tmp_path):
    commands = []

    async def fake_run(argv, timeout):
        commands.append(argv)
        assert timeout <= 3.0  # Decky kills the plugin 5 s after asking it to stop
        return 0, "", ""

    monkeypatch.setattr(backend, "_run", fake_run)
    _service_installed(tmp_path)
    plugin = backend.Plugin()
    await plugin._uninstall()
    # the note outlives a reboot, which the timer does not; the installed service finds it
    note = json.loads((tmp_path / "state" / installer.REMOVED_MARKER).read_text())
    assert note["plugins_dir"] == str(REPO.parent)  # where Decky keeps its plugins, not this one's folder
    assert abs(note["time"] - time.time()) < 5
    (argv,) = commands
    assert argv[:2] == ["systemd-run", f"--on-active={installer.ORPHAN_CHECK_DELAY}"]
    # the note decides, so that an install in the meantime calls the check off
    assert argv[-2:] == [str(tmp_path / "opt" / "py_modules"), "uninstall-if-orphaned"]
    # the same kind of unit as the one the service starts for this
    assert tuple(argv[2 : argv.index(sys.executable)]) == installer.ORPHAN_CHECK_OPTIONS
    # nothing was stopped or removed directly: an update must not interrupt the service
    assert not any("systemctl" in part for part in argv)


async def test_the_note_is_on_the_disk_before_it_has_its_name(backend, monkeypatch, tmp_path):
    # "Uninstall, then the machine goes off": a note that is renamed into place before its
    # contents are written out can come back empty after a power cut.
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("sync " + ("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"))
        return real_fsync(fd)

    def replace(src, dst):
        events.append("rename")
        return real_replace(src, dst)

    async def fake_run(argv, timeout):
        return 0, "", ""

    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    _service_installed(tmp_path)
    await backend.Plugin()._uninstall()
    assert events == ["sync file", "rename", "sync directory"]
    assert os.listdir(tmp_path / "state") == [installer.REMOVED_MARKER]


async def test_uninstall_hook_does_nothing_without_an_installed_service(backend, monkeypatch, tmp_path):
    async def fake_run(argv, timeout):
        raise AssertionError("nothing to schedule")

    monkeypatch.setattr(backend, "_run", fake_run)
    await backend.Plugin()._uninstall()
    assert not (tmp_path / "state").exists()  # and no state directory is made for the note
    # settings that were kept when the service was removed are not a service either
    (tmp_path / "state").mkdir()
    await backend.Plugin()._uninstall()
    assert os.listdir(tmp_path / "state") == []


async def test_uninstall_hook_survives_a_failing_systemd_run(backend, monkeypatch, tmp_path):
    async def fake_run(argv, timeout):
        return 1, "", "Failed to connect to bus"

    monkeypatch.setattr(backend, "_run", fake_run)
    _service_installed(tmp_path)
    await backend.Plugin()._uninstall()
    assert (tmp_path / "state" / installer.REMOVED_MARKER).exists()  # the service checks after its next start


async def test_the_check_is_scheduled_even_if_the_note_cannot_be_written(backend, monkeypatch, tmp_path, caplog):
    commands = []

    async def fake_run(argv, timeout):
        commands.append(argv)
        return 0, "", ""

    monkeypatch.setattr(backend, "_run", fake_run)
    _service_installed(tmp_path)
    # as a full or read-only /var would: the note's temporary file cannot be made
    (tmp_path / "state" / (installer.REMOVED_MARKER + ".tmp")).mkdir()
    with caplog.at_level(logging.WARNING, logger="decky-test"):
        await backend.Plugin()._uninstall()
    assert not (tmp_path / "state" / installer.REMOVED_MARKER).exists()
    assert "could not note the removal" in caplog.text
    # then the timer is all there is, and it is told where to look
    (argv,) = commands
    assert argv[-3:] == ["uninstall-if-orphaned", "--plugins-dir", str(REPO.parent)]
    # ... in words the installer understands
    arguments = argv[argv.index("uninstall-if-orphaned") :]
    seen = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "uninstall_if_orphaned", lambda where=None: seen.append(where) or {"log": []})
    assert installer.main(arguments) == 0 and seen == [REPO.parent]


def test_backend_and_installer_agree(backend):
    # main.py cannot import the installer (it runs in Decky's own Python), so it repeats these.
    sys.modules.pop("main", None)
    fresh = importlib.import_module("main")  # without the fixture's replacements
    assert fresh.STATE_DIR == str(installer.Layout().state_dir)
    assert fresh.REMOVED_MARKER == installer.REMOVED_MARKER
    assert fresh.ORPHAN_CHECK_DELAY == installer.ORPHAN_CHECK_DELAY
    assert fresh.ORPHAN_CHECK_OPTIONS == installer.ORPHAN_CHECK_OPTIONS
    assert fresh.INSTALLER_SNIPPET == installer.INSTALLER_SNIPPET
    assert fresh.SOCKET_PATH == "/run/tcl-me-remo/control.sock"
    assert fresh.INSTALLED_MODULES == tuple(str(path / "py_modules") for path in installer.Layout().install_dirs)


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
