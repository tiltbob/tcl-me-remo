import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tcl_me_remo import installer
from tcl_me_remo.installer import SERVICE, SLEEP_HOOK, InstallError, Layout

REPO = Path(__file__).resolve().parents[1]


class FakeSystemd:
    """Enough of systemctl for the installer: enabled/active state per unit."""

    def __init__(self, python_version="3.13"):
        self.enabled = set()
        self.active = set()
        self.calls = []
        self.python_version = python_version
        self.fail_restart = False

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[0] != "systemctl":
            if "-c" in argv:  # the interpreter version probe
                return (0, self.python_version + "\n") if self.python_version else (-1, "not found")
            return 0, ""
        verb, units = argv[1], [a for a in argv[2:] if not a.startswith("-")]
        if verb == "is-active":
            return (0, "active\n") if units[0] in self.active else (3, "inactive\n")
        if verb == "is-enabled":
            return (0, "enabled\n") if units[0] in self.enabled else (1, "disabled\n")
        if verb == "enable":
            self.enabled.update(units)
        elif verb == "disable":
            self.enabled.difference_update(units)
            self.active.difference_update(units)
        elif verb == "restart":
            if self.fail_restart:
                return 1, "Job for tcl-me-remo.service failed."
            self.active.update(units)
        return 0, ""

    def count(self, verb):
        return sum(1 for call in self.calls if call[:2] == ["systemctl", verb])


@pytest.fixture
def env(tmp_path):
    layout = Layout(
        install_dirs=(tmp_path / "opt" / "tcl-me-remo", tmp_path / "var" / "lib" / "tcl-me-remo" / "app"),
        unit_dir=tmp_path / "etc" / "systemd" / "system",
        state_dir=tmp_path / "var" / "lib" / "tcl-me-remo",
        link_dirs=(tmp_path / "usr-local-bin",),
    )
    return layout, FakeSystemd()


def test_fresh_install_lays_everything_out(env, tmp_path):
    layout, systemd = env
    home = tmp_path / "home" / "deck"
    home.mkdir(parents=True)
    result = installer.install(layout, systemd, source=REPO, user_home=home)

    target = layout.install_dirs[0]
    assert (target / "py_modules" / "tcl_me_remo" / "service.py").is_file()
    assert (target / "py_modules" / "tcl_me_remo" / "_vendor" / "dbus_fast" / "__init__.py").is_file()
    assert not list(target.rglob("__pycache__"))
    assert os.access(target / "bin" / "tcl-me-remo", os.X_OK)
    assert (target / "VERSION").read_text().split()[0] == installer.bundled_version(REPO)
    assert not target.with_name("tcl-me-remo.new").exists()

    service = (layout.unit_dir / SERVICE).read_text()
    assert f"ExecStart=/usr/bin/python3 -I {target}/bin/tcl-me-remo daemon" in service
    assert "After=bluetooth.service" in service and "Type=notify" in service
    assert f"Environment=TCL_ME_REMO_CONFIG={layout.state_dir}/config.json" in service
    hook = (layout.unit_dir / SLEEP_HOOK).read_text()
    assert "Before=sleep.target" in hook and "WantedBy=sleep.target" in hook
    assert "event suspend --budget 3.5" in hook and "event resume --no-wait" in hook

    assert systemd.enabled == {SERVICE, SLEEP_HOOK}
    assert systemd.active == {SERVICE}
    assert result["changed"] and result["restarted"]
    assert result["installed"] and result["active"] and result["enabled"] and not result["needs_update"]
    assert result["problems"] == []
    assert os.readlink(home / ".local" / "bin" / "tcl-me-remo") == str(target / "bin" / "tcl-me-remo")
    assert (layout.link_dirs[0] / "tcl-me-remo").is_symlink()


def test_installed_launcher_really_runs(env):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    launcher = layout.install_dirs[0] / "bin" / "tcl-me-remo"
    out = subprocess.run([sys.executable, "-I", str(launcher), "--version"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.startswith("tcl-me-remo ")
    keys = subprocess.run([sys.executable, "-I", str(launcher), "keys"], capture_output=True, text=True, timeout=30)
    assert "WAKE" in keys.stdout


def test_reinstall_without_changes_does_not_restart(env):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    restarts = systemd.count("restart")
    result = installer.install(layout, systemd, source=REPO)
    assert result["log"] == ["already up to date"]
    assert not result["changed"] and not result["restarted"]
    assert systemd.count("restart") == restarts  # a needless restart would drop the TV's subscription
    assert systemd.count("daemon-reload") == 1


def test_update_replaces_code_and_restarts(env, tmp_path):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    # a "new release": same layout, one file changed
    newer = tmp_path / "newer"
    (newer / "py_modules").mkdir(parents=True)
    import shutil

    shutil.copytree(REPO / "py_modules" / "tcl_me_remo", newer / "py_modules" / "tcl_me_remo")
    marker = newer / "py_modules" / "tcl_me_remo" / "paths.py"
    marker.write_text(marker.read_text() + "\n# changed\n")
    assert installer.info(layout, systemd, source=newer)["needs_update"] is True

    restarts = systemd.count("restart")
    result = installer.install(layout, systemd, source=newer)
    assert result["changed"] and result["restarted"]
    assert systemd.count("restart") == restarts + 1
    assert "# changed" in (layout.install_dirs[0] / "py_modules" / "tcl_me_remo" / "paths.py").read_text()
    assert not layout.install_dirs[0].with_name("tcl-me-remo.old").exists()


def test_heals_after_an_os_update_wiped_the_units(env):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    (layout.unit_dir / SERVICE).unlink()
    systemd.enabled.clear()
    systemd.active.clear()
    state = installer.info(layout, systemd, source=REPO)
    assert state["installed"] is False and "units are missing" in state["problems"][0]
    result = installer.install(layout, systemd, source=REPO)
    assert result["installed"] and result["active"] and systemd.enabled == {SERVICE, SLEEP_HOOK}


def test_stopped_service_is_started_again(env):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    systemd.active.clear()
    result = installer.install(layout, systemd, source=REPO)
    assert result["restarted"] and result["log"] == ["service started"]


def test_falls_back_when_opt_is_not_writable(env, tmp_path):
    layout, systemd = env
    (tmp_path / "opt").write_text("not a directory")
    result = installer.install(layout, systemd, source=REPO)
    assert result["install_dir"] == str(layout.install_dirs[1])
    assert str(layout.install_dirs[1]) in (layout.unit_dir / SERVICE).read_text()


def test_refuses_old_or_missing_python(env):
    layout, _ = env
    with pytest.raises(InstallError, match="needs Python 3.11 or newer"):
        installer.install(layout, FakeSystemd(python_version="3.9"), source=REPO)
    with pytest.raises(InstallError, match="missing or does not run"):
        installer.install(layout, FakeSystemd(python_version=""), source=REPO)
    assert not layout.install_dirs[0].exists()  # nothing was touched


def test_start_failure_is_explained(env):
    layout, systemd = env
    systemd.fail_restart = True
    with pytest.raises(InstallError, match="did not start"):
        installer.install(layout, systemd, source=REPO)


def test_bad_source(env, tmp_path):
    layout, systemd = env
    with pytest.raises(InstallError, match="does not contain"):
        installer.install(layout, systemd, source=tmp_path)


def test_uninstall_keeps_settings_unless_purged(env):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    (layout.state_dir / "config.json").write_text("{}")
    result = installer.uninstall(layout, systemd)
    assert not layout.install_dirs[0].exists()
    assert not (layout.unit_dir / SERVICE).exists() and not (layout.unit_dir / SLEEP_HOOK).exists()
    assert systemd.enabled == set() and systemd.active == set()
    assert not (layout.link_dirs[0] / "tcl-me-remo").is_symlink()
    assert (layout.state_dir / "config.json").exists()
    assert any("kept settings" in line for line in result["log"])
    assert installer.info(layout, systemd, source=REPO)["installed"] is False

    assert installer.uninstall(layout, systemd, purge=True)["log"][-1].startswith("removed settings")
    assert not layout.state_dir.exists()
    assert installer.uninstall(layout, systemd)["log"] == ["nothing was installed"]


def test_orphan_check_distinguishes_update_from_removal(env, tmp_path):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    plugin_dir = tmp_path / "homebrew" / "plugins" / "tcl-me-remo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text("{}")
    # plugin was updated: its directory is back, the service stays
    installer.uninstall_if_orphaned(plugin_dir, layout, systemd)
    assert layout.install_dirs[0].exists() and SERVICE in systemd.active
    # plugin was removed for good
    (plugin_dir / "plugin.json").unlink()
    installer.uninstall_if_orphaned(plugin_dir, layout, systemd)
    assert not layout.install_dirs[0].exists() and systemd.active == set()


def test_tree_digest_ignores_bytecode(tmp_path):
    package = tmp_path / "pkg"
    (package / "__pycache__").mkdir(parents=True)
    (package / "a.py").write_text("x = 1\n")
    first = installer.tree_digest(package)
    (package / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\x00")
    (package / "b.pyc").write_bytes(b"\x00")
    assert installer.tree_digest(package) == first
    (package / "a.py").write_text("x = 2\n")
    assert installer.tree_digest(package) != first


def test_cli_requires_root_for_changes(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert installer.main(["install"]) == 1
    assert "sudo" in capsys.readouterr().err
    assert installer.main(["--json", "uninstall"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "this needs root: run it with sudo"}


def test_cli_status_as_json(capsys):
    assert installer.main(["--json", "--source", str(REPO), "status"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["bundled_version"] == installer.bundled_version(REPO)
    assert state["installed"] is False  # nothing is installed in the test environment
    assert installer.main(["--source", str(REPO), "status"]) == 0
    assert "not installed" in capsys.readouterr().out
