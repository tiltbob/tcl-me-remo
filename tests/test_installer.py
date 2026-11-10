import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
        elif verb in ("restart", "start"):
            if self.fail_restart:
                return 1, "Job for tcl-me-remo.service failed."
            self.active.update(units)
        return 0, ""

    def count(self, verb):
        return sum(1 for call in self.calls if call[:2] == ["systemctl", verb])


def _layout(root):
    return Layout(
        install_dirs=(root / "opt" / "tcl-me-remo", root / "var" / "lib" / "tcl-me-remo" / "app"),
        unit_dir=root / "etc" / "systemd" / "system",
        state_dir=root / "var" / "lib" / "tcl-me-remo",
        link_dirs=(root / "usr-local-bin",),
    )


@pytest.fixture
def env(tmp_path):
    return _layout(tmp_path), FakeSystemd()


def _newer_release(tmp_path):
    """A "new release": the same code with one file changed."""
    newer = tmp_path / "newer"
    shutil.copytree(
        REPO / "py_modules" / "tcl_me_remo",
        newer / "py_modules" / "tcl_me_remo",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    changed = newer / "py_modules" / "tcl_me_remo" / "paths.py"
    changed.write_text(changed.read_text() + "\n# changed\n")
    return newer


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
    assert "event suspend --budget 6\n" in hook and "event resume --no-wait" in hook
    assert "TimeoutStartSec=10" in hook and "TimeoutStopSec=20" in service

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
    newer = _newer_release(tmp_path)
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
    restarts = systemd.count("restart")
    result = installer.install(layout, systemd, source=REPO)
    assert result["restarted"] and result["log"] == ["service started"]
    # "start", not "restart": if a start is already under way (boot), it is not interrupted
    assert systemd.count("start") == 1 and systemd.count("restart") == restarts


class StartingSystemd(FakeSystemd):
    """The unit has a start job running: "systemctl is-active" says "activating"."""

    starting = False

    def __call__(self, argv, timeout):
        if self.starting and argv[:2] == ["systemctl", "is-active"]:
            self.calls.append(list(argv))
            return 3, "activating\n"
        return super().__call__(argv, timeout)


def test_a_service_that_is_still_starting_is_left_alone(tmp_path):
    # At boot the plugin's install can run while the unit is "activating" (it reports ready
    # only after registering with BlueZ).  Nothing changed, so nothing may be restarted.
    layout, systemd = _layout(tmp_path), StartingSystemd()
    installer.install(layout, systemd, source=REPO)
    systemd.calls.clear()
    systemd.starting = True
    result = installer.install(layout, systemd, source=REPO)
    assert result["log"] == ["already up to date"] and not result["restarted"]
    assert systemd.count("restart") == 0 and systemd.count("start") == 0
    assert result["problems"] == ["the service is still starting"]


class CrashLoop(StartingSystemd):
    """A unit that fails at every start: "activating" to is-active, between attempts too."""

    sub_state, restarts = "auto-restart", 7

    def __call__(self, argv, timeout):
        if argv[:2] == ["systemctl", "show"] and "SubState" in argv:
            self.calls.append(list(argv))
            return 0, f"NRestarts={self.restarts}\nSubState={self.sub_state}\n"
        return super().__call__(argv, timeout)


def test_a_service_that_fails_at_every_start_is_not_called_starting(tmp_path):
    # With Restart=on-failure systemd never calls such a unit "failed": it is "activating"
    # while it waits for the next attempt.  Still nothing to restart, but the one place that
    # tells the user the service cannot start must not read "already up to date".
    layout, systemd = _layout(tmp_path), CrashLoop()
    installer.install(layout, systemd, source=REPO)
    systemd.calls.clear()
    systemd.starting = True
    result = installer.install(layout, systemd, source=REPO)
    assert systemd.count("restart") == 0 and systemd.count("start") == 0
    (trouble,) = result["problems"]
    assert "keeps failing" in trouble and "8 times" in trouble and "journalctl -b -u tcl-me-remo" in trouble
    assert result["log"] == ["already up to date", trouble] and result["active"] is False
    assert installer.info(layout, systemd, source=REPO)["problems"] == [trouble]
    # the first restart after a failure is under way: not "keeps failing" yet, but not just "starting"
    systemd.sub_state, systemd.restarts = "start", 1
    (trouble,) = installer.info(layout, systemd, source=REPO)["problems"]
    assert trouble.startswith("the service failed and systemd is starting it again")
    # an ordinary start
    systemd.restarts = 0
    assert installer.info(layout, systemd, source=REPO)["problems"] == ["the service is still starting"]
    assert installer.install(layout, systemd, source=REPO)["log"] == ["already up to date"]


def test_the_command_line_does_not_say_a_thing_twice(monkeypatch, capsys):
    result = {"log": ["already up to date", "it keeps failing"], "problems": ["it keeps failing", "not enabled"]}
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "install", lambda **_kwargs: result)
    assert installer.main(["install"]) == 0
    assert capsys.readouterr().out.splitlines() == ["already up to date", "it keeps failing", "warning: not enabled"]


def test_a_state_that_could_not_be_read_does_not_cause_a_restart(tmp_path):
    class Unanswered(StartingSystemd):
        def __call__(self, argv, timeout):
            if self.starting and argv[:2] == ["systemctl", "is-active"]:
                self.calls.append(list(argv))
                return -1, "timed out after 5 seconds"
            return super().__call__(argv, timeout)

    layout, systemd = _layout(tmp_path), Unanswered()
    installer.install(layout, systemd, source=REPO)
    systemd.calls.clear()
    systemd.starting = True
    result = installer.install(layout, systemd, source=REPO)
    # "start" does nothing to a unit that runs; "restart" would have cost the TV's subscription
    assert systemd.count("restart") == 0 and systemd.count("start") == 1
    assert result["log"] == ["already up to date"] and not result["restarted"]  # nothing was started


class EnableFails(FakeSystemd):
    """systemctl enable fails while ``broken`` is set (as when systemd does not answer)."""

    broken = False

    def __call__(self, argv, timeout):
        if self.broken and argv[:2] == ["systemctl", "is-enabled"]:
            self.calls.append(list(argv))
            return 1, "disabled\n"
        if self.broken and argv[:2] == ["systemctl", "enable"]:
            self.calls.append(list(argv))
            return 1, "Failed to enable unit: Connection timed out"
        return super().__call__(argv, timeout)


def _interrupted_update(tmp_path, systemd=None):
    """Version 1 runs; the update to version 2 replaces the code and fails before the restart."""
    layout, systemd = _layout(tmp_path), systemd or EnableFails()
    installer.install(layout, systemd, source=REPO)
    newer = _newer_release(tmp_path)
    systemd.broken = True
    with pytest.raises(InstallError, match="could not enable"):
        installer.install(layout, systemd, source=newer)
    systemd.broken = False
    systemd.calls.clear()
    return layout, systemd, newer


def test_an_interrupted_update_is_finished_by_the_next_install(tmp_path):
    layout, systemd, newer = _interrupted_update(tmp_path)
    # the new code is on disk, the old service still runs: say so instead of "all is well"
    state = installer.info(layout, systemd, source=newer)
    assert state["active"] and not state["needs_update"] and state["restart_pending"] is True
    assert "was not restarted" in state["problems"][0]

    result = installer.install(layout, systemd, source=newer)
    assert result["restarted"] and not result["changed"]
    assert result["log"] == ["service restarted to finish an earlier update"]
    assert systemd.count("restart") == 1
    assert result["restart_pending"] is False and result["problems"] == []
    # and that was it: the next install has nothing to do
    assert installer.install(layout, systemd, source=newer)["log"] == ["already up to date"]
    assert systemd.count("restart") == 1


def test_a_reboot_also_finishes_an_interrupted_update(tmp_path, monkeypatch):
    # After a reboot the service runs the new code anyway; restarting it again would only
    # cost the TV's subscription.
    layout, systemd, newer = _interrupted_update(tmp_path)
    monkeypatch.setattr(installer, "_boot_id", lambda: "another-boot")
    assert installer.info(layout, systemd, source=newer)["restart_pending"] is False
    result = installer.install(layout, systemd, source=newer)
    assert result["log"] == ["already up to date"] and systemd.count("restart") == 0


class Clocked(EnableFails):
    """Also answers "systemctl show": when the service's process was started (on the clock
    systemd uses) and whether the unit file changed since systemd read it."""

    started, need_reload = 0, "no"

    def __call__(self, argv, timeout):
        if argv[:2] == ["systemctl", "show"] and "ExecMainStartTimestampMonotonic" in argv:
            self.calls.append(list(argv))
            return 0, f"ExecMainStartTimestampMonotonic={self.started}\nNeedDaemonReload={self.need_reload}\n"
        if argv[:2] == ["systemctl", "daemon-reload"]:
            self.need_reload = "no"
        if argv[:2] == ["systemctl", "restart"] or (argv[:2] == ["systemctl", "start"] and SERVICE not in self.active):
            self.started = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
        return super().__call__(argv, timeout)


def test_a_restart_by_other_means_finishes_an_interrupted_update(tmp_path):
    layout, systemd, newer = _interrupted_update(tmp_path, Clocked())
    assert installer.info(layout, systemd, source=newer)["restart_pending"] is True
    # the panel's "Restart service", or systemd after a crash: the new code runs now
    systemd(["systemctl", "restart", SERVICE], 60.0)
    restarts = systemd.count("restart")
    state = installer.info(layout, systemd, source=newer)
    assert state["restart_pending"] is False and state["problems"] == []
    result = installer.install(layout, systemd, source=newer)
    assert result["log"] == ["already up to date"] and not result["restarted"]
    assert systemd.count("restart") == restarts  # a second restart would only cost the TV's subscription
    assert not (layout.install_dirs[0] / installer.RESTART_PENDING).exists()


def test_a_restart_before_systemd_read_the_new_unit_does_not_finish_the_update(tmp_path):
    layout, systemd, newer = _interrupted_update(tmp_path, Clocked())
    systemd(["systemctl", "restart", SERVICE], 60.0)
    systemd.need_reload = "yes"  # what systemd says of a unit file that is newer than what it has loaded
    assert installer.info(layout, systemd, source=newer)["restart_pending"] is True
    systemd.calls.clear()
    assert installer.install(layout, systemd, source=newer)["log"] == ["service restarted to finish an earlier update"]
    verbs = [call[1] for call in systemd.calls if call[0] == "systemctl"]
    assert verbs.index("daemon-reload") < verbs.index("restart")
    assert installer.install(layout, systemd, source=newer)["log"] == ["already up to date"]


def test_a_restart_just_before_the_new_files_took_effect_does_not_count(tmp_path, monkeypatch):
    # The note is written with the staged code and before a unit is replaced.  A service
    # that happens to be restarted between that and the swap (or systemd's reload) still
    # runs the old files, although it was started after the note's first moment.
    class Unlucky(Clocked):
        def __call__(self, argv, timeout):
            if self.broken and argv[:2] == ["systemctl", "daemon-reload"]:
                self.started = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000  # restarted with the old unit
            return super().__call__(argv, timeout)

    layout, systemd = _layout(tmp_path), Unlucky()
    installer.install(layout, systemd, source=REPO)
    (layout.unit_dir / SERVICE).write_text("[Service]\nExecStart=/bin/false\n")
    systemd.broken = True
    with pytest.raises(InstallError, match="could not enable"):
        installer.install(layout, systemd, source=REPO)
    assert installer.info(layout, systemd, source=REPO)["restart_pending"] is True
    systemd.broken = False
    assert installer.install(layout, systemd, source=REPO)["restarted"]

    real_replace = os.replace

    def replace(src, dst):
        if str(dst).endswith(".old"):  # the new code is staged, the old about to be moved away
            systemd(["systemctl", "restart", SERVICE], 60.0)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    systemd.broken = True
    with pytest.raises(InstallError, match="could not enable"):
        installer.install(layout, systemd, source=_newer_release(tmp_path))
    assert installer.info(layout, systemd, source=REPO)["restart_pending"] is True


def test_an_install_that_ended_before_the_reload_is_finished_with_one(tmp_path):
    class Killed(FakeSystemd):
        die = False

        def __call__(self, argv, timeout):
            if self.die and argv[:2] == ["systemctl", "daemon-reload"]:
                raise KeyboardInterrupt  # the installer is killed right after writing the units
            return super().__call__(argv, timeout)

    layout, systemd = _layout(tmp_path), Killed()
    installer.install(layout, systemd, source=REPO)
    (layout.unit_dir / SERVICE).write_text("[Service]\nExecStart=/bin/false\n")
    systemd.die = True
    with pytest.raises(KeyboardInterrupt):
        installer.install(layout, systemd, source=REPO)
    systemd.die = False
    systemd.calls.clear()
    assert installer.install(layout, systemd, source=REPO)["restarted"]
    verbs = [call[1] for call in systemd.calls if call[0] == "systemctl"]
    # systemd must read the new unit before it restarts the service from it
    assert verbs.index("daemon-reload") < verbs.index("restart")


def test_a_unit_change_that_was_not_followed_by_a_restart_is_finished_too(tmp_path):
    layout, systemd = _layout(tmp_path), EnableFails()
    installer.install(layout, systemd, source=REPO)
    (layout.unit_dir / SERVICE).write_text("[Service]\nExecStart=/bin/false\n")  # e.g. an older unit
    systemd.broken = True
    with pytest.raises(InstallError):
        installer.install(layout, systemd, source=REPO)
    systemd.broken = False
    restarts = systemd.count("restart")
    assert installer.install(layout, systemd, source=REPO)["restarted"]
    assert systemd.count("restart") == restarts + 1


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


def test_uninstall_says_removed_only_of_what_is_gone(env, tmp_path):
    # An install directory that is a link to another place: the link is removed, and
    # "removed" is not said of something that is still there.
    layout, systemd = env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "VERSION").write_text("0.0.1 x\n")
    layout.install_dirs[0].parent.mkdir(parents=True)
    layout.install_dirs[0].symlink_to(elsewhere)
    log = installer.uninstall(layout, systemd)["log"]
    assert f"removed {layout.install_dirs[0]}" in log
    assert not os.path.lexists(layout.install_dirs[0]) and (elsewhere / "VERSION").exists()


class Tools(FakeSystemd):
    """Adds the service's own "forget" command and bluetoothctl."""

    def __init__(self, forget=0, bluetoothctl=(0, "Device has been removed\n")):
        super().__init__()
        self.forget, self.bluetoothctl = forget, bluetoothctl

    def __call__(self, argv, timeout):
        if argv[-1] == "forget":
            self.calls.append(list(argv))
            return self.forget, ""
        if argv[0] == "bluetoothctl":
            self.calls.append(list(argv))
            return self.bluetoothctl
        return super().__call__(argv, timeout)

    def words(self):
        return [" ".join(call[-2:] if call[-1] == "forget" else call) for call in self.calls]


def _paired(layout, tv="AA:BB:CC:DD:EE:FF", stale=()):
    (layout.state_dir / "config.json").write_text(json.dumps({"bluetooth": {"tv_address": tv}}))
    (layout.state_dir / "bluetooth.json").write_text(json.dumps({"tv": tv.lower(), "stale": list(stale)}))


def test_purge_has_the_running_service_forget_the_tv(tmp_path):
    # The bond lives in BlueZ.  Deleting the settings without it leaves a bond nobody knows
    # about, and BlueZ then refuses to pair with the same TV again.
    layout, tools = _layout(tmp_path), Tools(bluetoothctl=(1, "Device AA:BB:CC:DD:EE:FF not available\n"))
    installer.install(layout, tools, source=REPO)
    _paired(layout)
    launcher = layout.install_dirs[0] / "bin" / "tcl-me-remo"
    tools.calls.clear()
    log = installer.uninstall(layout, tools, purge=True)["log"]
    assert tools.calls[0] == ["/usr/bin/python3", "-I", str(launcher), "forget"]  # while the service still runs
    assert tools.calls[1][:3] == ["systemctl", "disable", "--now"]
    assert log[0] == "the service removed its Bluetooth pairing with the TV"
    assert [line for line in log if "Bluetooth" in line] == [log[0]]
    assert not layout.state_dir.exists()


def test_purge_removes_a_bond_the_service_forgot_only_on_paper(tmp_path):
    # The service answered (or was stopped half-way) and the files no longer name the TV,
    # but BlueZ still has the bond: the addresses read before asking are tried all the same.
    class Forgetful(Tools):
        def __call__(self, argv, timeout):
            if argv[-1] == "forget":
                (layout.state_dir / "config.json").write_text("{}")
                (layout.state_dir / "bluetooth.json").write_text("{}")
            return super().__call__(argv, timeout)

    layout, tools = _layout(tmp_path), Forgetful()
    installer.install(layout, tools, source=REPO)
    _paired(layout)
    log = installer.uninstall(layout, tools, purge=True)["log"]
    assert "bluetoothctl --timeout 5 remove AA:BB:CC:DD:EE:FF" in tools.words()
    assert "removed the Bluetooth pairing with AA:BB:CC:DD:EE:FF" in log


def test_purge_removes_the_bond_itself_when_the_service_cannot(tmp_path):
    layout, tools = _layout(tmp_path), Tools(forget=3)  # 3: the service does not answer
    installer.install(layout, tools, source=REPO)
    _paired(layout, stale=["11:22:33:44:55:66", "not an address"])
    tools.calls.clear()
    log = installer.uninstall(layout, tools, purge=True)["log"]
    words = tools.words()
    assert "bluetoothctl --timeout 5 remove AA:BB:CC:DD:EE:FF" in words
    assert "bluetoothctl --timeout 5 remove 11:22:33:44:55:66" in words
    assert len([word for word in words if word.startswith("bluetoothctl")]) == 2
    assert words.index("systemctl disable --now tcl-me-remo.service") < words.index(
        "bluetoothctl --timeout 5 remove AA:BB:CC:DD:EE:FF"
    )
    assert "removed the Bluetooth pairing with AA:BB:CC:DD:EE:FF" in log
    assert not any("the service removed" in line for line in log)


def test_purge_says_what_to_do_when_the_bond_could_not_be_removed(tmp_path):
    layout, tools = _layout(tmp_path), Tools(forget=3, bluetoothctl=(-1, "timed out"))
    installer.install(layout, tools, source=REPO)
    _paired(layout)
    log = installer.uninstall(layout, tools, purge=True)["log"]
    assert any(line.endswith("run: bluetoothctl remove AA:BB:CC:DD:EE:FF") for line in log)
    # a bond BlueZ does not have is not worth a line
    layout, tools = _layout(tmp_path / "second"), Tools(forget=3, bluetoothctl=(1, "Device AA:BB not available\n"))
    installer.install(layout, tools, source=REPO)
    _paired(layout)
    assert not any("Bluetooth" in line for line in installer.uninstall(layout, tools, purge=True)["log"])


def test_purge_does_not_contradict_the_service(tmp_path):
    # The service reported the pairing removed, and the pass that makes sure could not be
    # made (no bluetoothctl, or bluetoothd gone): that is not "could not remove".
    layout, tools = _layout(tmp_path), Tools(forget=0, bluetoothctl=(-1, "[Errno 2] No such file: 'bluetoothctl'"))
    installer.install(layout, tools, source=REPO)
    _paired(layout)
    log = [line for line in installer.uninstall(layout, tools, purge=True)["log"] if "Bluetooth" in line]
    assert log[0] == "the service removed its Bluetooth pairing with the TV"
    assert len(log) == 2
    assert log[1].startswith("could not check that no Bluetooth pairing with AA:BB:CC:DD:EE:FF is left")
    assert log[1].endswith("run: bluetoothctl remove AA:BB:CC:DD:EE:FF")
    assert not any("could not remove" in line for line in log)


def test_plain_uninstall_leaves_the_pairing_alone(tmp_path):
    layout, tools = _layout(tmp_path), Tools()
    installer.install(layout, tools, source=REPO)
    _paired(layout)
    tools.calls.clear()
    installer.uninstall(layout, tools)
    assert not any("forget" in call or call[0] == "bluetoothctl" for call in tools.calls)
    assert (layout.state_dir / "bluetooth.json").exists()


# ---------------------------------------------------------------------- the command link


def test_the_users_own_file_is_not_replaced(env, tmp_path):
    layout, systemd = env
    mine = tmp_path / "home" / "deck" / ".local" / "bin" / "tcl-me-remo"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\n# my own wrapper\n")
    result = installer.install(layout, systemd, source=REPO, user_home=tmp_path / "home" / "deck")
    assert mine.read_text().endswith("my own wrapper\n") and not mine.is_symlink()
    assert result["log"][-1] == f"left {mine} alone: it was not put there by tcl-me-remo"
    # a link of the user's own to somewhere else is not ours either
    mine.unlink()
    mine.symlink_to("/usr/bin/true")
    installer.install(layout, systemd, source=REPO, user_home=tmp_path / "home" / "deck")
    assert os.readlink(mine) == "/usr/bin/true"
    installer.uninstall(layout, systemd)
    assert os.readlink(mine) == "/usr/bin/true"


def test_a_link_left_by_the_other_install_directory_is_replaced(env, tmp_path):
    layout, systemd = env
    home = tmp_path / "home" / "deck"
    link = home / ".local" / "bin" / "tcl-me-remo"
    link.parent.mkdir(parents=True)
    link.symlink_to(layout.install_dirs[1] / "bin" / "tcl-me-remo")  # the fallback place, earlier
    installer.install(layout, systemd, source=REPO, user_home=home)
    assert os.readlink(link) == str(layout.install_dirs[0] / "bin" / "tcl-me-remo")


def test_uninstall_removes_the_links_it_made(env, tmp_path):
    layout, systemd = env
    home = tmp_path / "home" / "deck"
    home.mkdir(parents=True)
    installer.install(layout, systemd, source=REPO, user_home=home)
    link = home / ".local" / "bin" / "tcl-me-remo"
    assert link.is_symlink()
    recorded = json.loads((layout.state_dir / installer.LINKS_RECORD).read_text())
    assert {"link": str(link), "home": str(home)} in recorded
    log = installer.uninstall(layout, systemd)["log"]  # no home is passed: the record names it
    assert not link.is_symlink() and f"removed {link}" in log
    assert not (layout.link_dirs[0] / "tcl-me-remo").is_symlink()
    assert not (layout.state_dir / installer.LINKS_RECORD).exists()


def test_uninstall_leaves_links_that_no_longer_point_at_the_command(env, tmp_path):
    # Ours when they were made and on record, then replaced by their owners: no longer ours.
    layout, systemd = env
    home = tmp_path / "home" / "deck"
    home.mkdir(parents=True)
    installer.install(layout, systemd, source=REPO, user_home=home)
    links = [home / ".local" / "bin" / "tcl-me-remo", layout.link_dirs[0] / "tcl-me-remo"]
    for link in links:
        assert link.is_symlink()
        link.unlink()
        link.symlink_to("/usr/bin/true")
    log = installer.uninstall(layout, systemd)["log"]
    assert [os.readlink(link) for link in links] == ["/usr/bin/true", "/usr/bin/true"]
    assert not any(str(link) in line for link in links for line in log)


def test_a_read_only_link_directory_is_not_reported(env, tmp_path, monkeypatch):
    # /usr/local/bin is part of the read-only image on SteamOS.  That must not make every
    # install result look like a failure, nor keep "already up to date" from being said.
    layout, systemd = env
    home = tmp_path / "home" / "deck"
    home.mkdir(parents=True)
    real_symlink = os.symlink

    def symlink(src, dst, *args, **kwargs):
        if str(dst).startswith(str(layout.link_dirs[0])):
            raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(src), None, str(dst))
        return real_symlink(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "symlink", symlink)
    first = installer.install(layout, systemd, source=REPO, user_home=home)
    assert not any("could not" in line or "Read-only" in line for line in first["log"])
    assert f"command available as {home / '.local' / 'bin' / 'tcl-me-remo'}" in first["log"]
    assert installer.install(layout, systemd, source=REPO, user_home=home)["log"] == ["already up to date"]
    # with no place for a link at all, the result says where the command is
    lonely = installer.install(layout, systemd, source=REPO)
    assert lonely["log"][0] == "already up to date" and "/bin/tcl-me-remo (it could not be linked" in lonely["log"][1]


USER = 54321  # any identity that is not root's; it needs no account


@pytest.fixture
def user_world():
    """A fake root that another user can walk into, with a home that user owns.  Needs root."""
    if os.geteuid() != 0:
        pytest.skip("needs root: the installer gives up root for the link in the user's home")
    probe = subprocess.run([sys.executable, "-I", "-c", "pass"], user=USER, group=USER, extra_groups=[], check=False)
    if probe.returncode != 0:
        pytest.skip("this Python cannot be started by another user")
    root = Path(tempfile.mkdtemp(prefix="tcl-me-remo-test-"))  # pytest's tmp_path is private to root
    try:
        os.chmod(root, 0o755)
        home = root / "home" / "deck"
        home.mkdir(parents=True)
        os.chown(home, USER, USER)
        yield _layout(root), home
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _owner(path):
    return os.lstat(path).st_uid


def test_the_link_in_the_home_is_made_as_its_owner(user_world):
    layout, home = user_world
    result = installer.install(layout, FakeSystemd(), source=REPO, user_home=home)
    link = home / ".local" / "bin" / "tcl-me-remo"
    assert os.readlink(link) == str(layout.install_dirs[0] / "bin" / "tcl-me-remo")
    assert f"command available as {link}" in result["log"]
    # nothing in the home belongs to root afterwards, ~/.local included
    assert [_owner(path) for path in (home / ".local", link.parent, link)] == [USER, USER, USER]
    assert installer.uninstall(layout, FakeSystemd())["log"].count(f"removed {link}") == 1
    assert not link.is_symlink()


def test_the_link_is_made_without_roots_groups(user_world):
    # Giving up root means its supplementary groups as well: with them the child could still
    # write where the user cannot.  Here ~/.local can be written by one of root's groups only.
    layout, home = user_world
    group = USER + 1
    local = home / ".local"
    local.mkdir()
    os.chown(local, 0, group)
    os.chmod(local, 0o770)
    before = os.getgroups()
    os.setgroups([*before, group])
    try:
        result = installer.install(layout, FakeSystemd(), source=REPO, user_home=home)
    finally:
        os.setgroups(before)
    assert not (local / "bin").exists()
    assert result["log"][-1].startswith(f"could not link the command into {local / 'bin'}: ")


def test_root_does_not_follow_a_path_the_user_controls(user_world):
    # ~/.local pointing into the install directory: root once deleted the launcher there,
    # put a link to itself in its place and gave the directory to the user.
    layout, home = user_world
    systemd = FakeSystemd()
    installer.install(layout, systemd, source=REPO)
    target = layout.install_dirs[0]
    os.symlink(target, home / ".local")
    os.lchown(home / ".local", USER, USER)
    result = installer.install(layout, systemd, source=REPO, user_home=home)
    launcher = target / "bin" / "tcl-me-remo"
    assert launcher.is_file() and not launcher.is_symlink()
    assert _owner(target / "bin") == 0 and _owner(launcher) == 0
    # what the user's own identity finds at that path is a file that is not its to replace
    seen = home / ".local" / "bin" / "tcl-me-remo"
    assert result["log"] == ["already up to date", f"left {seen} alone: it was not put there by tcl-me-remo"]
    assert str(seen) not in (layout.state_dir / installer.LINKS_RECORD).read_text()  # so uninstall leaves it too


# ---------------------------------------------------------------------- the plugin was removed


def _settled(*paths):
    """Give the directories the age of something Decky is not working on at the moment."""
    for path in paths:
        os.utime(path, (time.time() - 600, time.time() - 600))


def _plugins(tmp_path, *folders):
    plugins = tmp_path / "home" / "deck" / "homebrew" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    for folder, name in folders:
        (plugins / folder).mkdir()
        (plugins / folder / "plugin.json").write_text(json.dumps({"name": name}))
        _settled(plugins / folder)
    _settled(plugins)
    return plugins


def _note_removal(layout, plugins, age=installer.ORPHAN_CHECK_DELAY):
    note = {"plugins_dir": str(plugins), "time": int(time.time() - age)}
    (layout.state_dir / installer.REMOVED_MARKER).write_text(json.dumps(note))


def _installed(layout, systemd):
    return layout.install_dirs[0].exists() and SERVICE in systemd.active


def test_orphan_check_removes_the_service_when_the_plugin_is_gone(env, tmp_path):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    _note_removal(layout, _plugins(tmp_path, ("some-other-plugin", "Other")))
    result = installer.uninstall_if_orphaned(None, layout, systemd)
    assert result["orphaned"] is True and not _installed(layout, systemd)
    assert not (layout.state_dir / installer.REMOVED_MARKER).exists()
    assert (layout.state_dir).exists()  # settings and the pairing are kept


def test_orphan_check_finds_the_plugin_under_any_folder_name(env, tmp_path):
    # Decky removed "tcl-me-remo-main" (a GitHub source zip) and unpacked the release zip
    # into "tcl-me-remo": an update, although the folder the note came from is gone.
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    plugins = _plugins(tmp_path, ("some-other-plugin", "Other"), ("tcl-me-remo", installer.PLUGIN_NAME))
    _note_removal(layout, plugins)
    result = installer.uninstall_if_orphaned(None, layout, systemd)
    assert result["orphaned"] is False and _installed(layout, systemd)
    assert not (layout.state_dir / installer.REMOVED_MARKER).exists()  # answered
    # a plugin.json caught half-written still counts
    (plugins / "tcl-me-remo" / "plugin.json").write_text('{"name": "tcl-me-remo", "auth')
    assert installer.plugin_installed(plugins) is True


def test_orphan_check_does_nothing_when_it_cannot_tell(env, tmp_path):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    for missing in (tmp_path / "home" / "deck" / "homebrew" / "plugins", tmp_path / "not-mounted" / "x" / "plugins"):
        _note_removal(layout, missing)  # the home is not mounted yet, or Decky was moved
        result = installer.uninstall_if_orphaned(None, layout, systemd)
        assert result["orphaned"] is None and _installed(layout, systemd)
        assert "cannot tell" in result["log"][0]
        assert (layout.state_dir / installer.REMOVED_MARKER).exists()  # asked again after the next start
    assert installer.plugin_installed(Path("relative/plugins")) is None


def test_orphan_check_waits_while_decky_may_be_updating_the_plugin(env, tmp_path):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    _note_removal(layout, _plugins(tmp_path), age=5)  # the old folder is gone, the new one not there yet
    result = installer.uninstall_if_orphaned(None, layout, systemd)
    assert result["orphaned"] is None and _installed(layout, systemd)
    assert (layout.state_dir / installer.REMOVED_MARKER).exists()
    # the timer set with the note comes back when it is old enough
    later = time.time() + installer.ORPHAN_CHECK_DELAY
    assert installer.uninstall_if_orphaned(None, layout, systemd, now=later)["orphaned"] is True
    assert installer.ORPHAN_MIN_AGE < installer.ORPHAN_CHECK_DELAY


def test_orphan_check_decides_on_a_note_from_the_future(env, tmp_path):
    # The clock was ahead when the plugin was removed.  Waiting for the note to "age" could
    # take for ever; the timer that runs the check has waited long enough by itself.
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    _note_removal(layout, _plugins(tmp_path), age=-3600)
    assert installer.uninstall_if_orphaned(None, layout, systemd)["orphaned"] is True
    assert not _installed(layout, systemd)


@pytest.mark.parametrize("text", ["", "{not json", "[1, 2]", '{"time": 5}', '{"plugins_dir": "plugins"}'])
def test_a_note_that_cannot_be_used_is_discarded(env, text):
    # Nothing can be decided from it, and left in place it would have the service start
    # the check after each of its starts, for good.
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    (layout.state_dir / installer.REMOVED_MARKER).write_text(text)
    assert installer.orphan_check_command(layout.state_dir) is not None
    result = installer.uninstall_if_orphaned(None, layout, systemd)
    assert result["orphaned"] is None and _installed(layout, systemd)
    assert "cannot be read" in result["log"][0] and "tcl-me-remo uninstall" in result["log"][0]
    assert not (layout.state_dir / installer.REMOVED_MARKER).exists()
    assert installer.orphan_check_command(layout.state_dir) is None


def test_orphan_check_can_be_told_where_to_look(env, tmp_path, monkeypatch):
    # The plugin's uninstall hook passes the directory when it could not write the note.
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    plugins = _plugins(tmp_path, ("tcl-me-remo", installer.PLUGIN_NAME))
    assert installer.uninstall_if_orphaned(plugins, layout, systemd)["orphaned"] is False
    shutil.rmtree(plugins / "tcl-me-remo")
    _settled(plugins)
    assert installer.uninstall_if_orphaned(plugins, layout, systemd)["orphaned"] is True
    assert not _installed(layout, systemd)
    # and that is what the command line hands on
    seen = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "uninstall_if_orphaned", lambda where=None: seen.append(where) or {"log": []})
    assert installer.main(["uninstall-if-orphaned", "--plugins-dir", str(plugins)]) == 0
    assert installer.main(["uninstall-if-orphaned"]) == 0
    assert seen == [plugins, None]


def test_a_plugin_that_decky_is_unpacking_is_not_taken_for_gone(env, tmp_path):
    # A reinstall caught half-way: the folder is there, its plugin.json not yet.
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    plugins = _plugins(tmp_path, ("some-other-plugin", "Other"))
    (plugins / "tcl-me-remo").mkdir()
    _settled(plugins)  # even if only the new folder shows it
    _note_removal(layout, plugins, age=500)
    result = installer.uninstall_if_orphaned(None, layout, systemd)
    assert result["orphaned"] is None and _installed(layout, systemd)
    assert "changed moments ago" in result["log"][0]
    assert (layout.state_dir / installer.REMOVED_MARKER).exists()
    # the same for a plugins directory something was just removed from or added to
    shutil.rmtree(plugins / "tcl-me-remo")
    assert installer.plugin_installed(plugins) is None
    # a folder without a plugin.json that has been lying there is no plugin
    assert installer.plugin_installed(plugins, now=time.time() + installer.PLUGINS_SETTLE) is False
    assert installer.uninstall_if_orphaned(None, layout, systemd, now=time.time() + 600)["orphaned"] is True


def test_the_search_for_the_plugin_cannot_be_held_up(tmp_path):
    # The plugins folder is the user's; the check runs as root with nobody watching.
    plugins = _plugins(tmp_path, ("some-other-plugin", "Other"))
    for name in ("pipe", "folder", "big"):
        (plugins / name).mkdir()
    os.mkfifo(plugins / "pipe" / "plugin.json")  # opening it for reading waits for a writer
    (plugins / "folder" / "plugin.json").mkdir()
    (plugins / "big" / "plugin.json").write_text(" " * installer.MANIFEST_LIMIT + '{"name": "tcl-me-remo"}')
    _settled(plugins, plugins / "pipe", plugins / "folder", plugins / "big")
    answer = []
    thread = threading.Thread(target=lambda: answer.append(installer.plugin_installed(plugins)), daemon=True)
    thread.start()
    thread.join(10)
    if not answer:  # let the thread go before failing
        os.close(os.open(plugins / "pipe" / "plugin.json", os.O_WRONLY | os.O_NONBLOCK))
    assert answer == [False]  # none of these is a plugin's manifest, and no more than a manifest is read


def test_an_install_cancels_the_orphan_check(env, tmp_path):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    _note_removal(layout, _plugins(tmp_path))
    # the plugin loads again after its update, or the user runs install.sh: the service is wanted
    installer.install(layout, systemd, source=REPO)
    assert not (layout.state_dir / installer.REMOVED_MARKER).exists()
    result = installer.uninstall_if_orphaned(None, layout, systemd)
    assert result["orphaned"] is False and _installed(layout, systemd)


def test_orphan_check_command_for_the_service(env):
    layout, systemd = env
    installer.install(layout, systemd, source=REPO)
    assert installer.orphan_check_command(layout.state_dir) is None  # no note, nothing to start
    _note_removal(layout, Path("/home/deck/homebrew/plugins"))
    argv = installer.orphan_check_command(layout.state_dir)
    assert argv[0] == "systemd-run"  # not a child of the service, which the check may stop
    assert not any(part.startswith("--on-") for part in argv)
    assert "--property=RuntimeMaxSec=600" in argv  # and not a unit that can stay for good
    modules = str(REPO / "py_modules")
    assert argv[-6:] == [sys.executable, "-I", "-c", installer.INSTALLER_SNIPPET, modules, "uninstall-if-orphaned"]
    # the snippet runs the installer from the directory it is given
    out = subprocess.run([*argv[-6:-1], "--json", "status"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "bundled_version" in json.loads(out.stdout)


def test_the_time_budgets_fit_into_the_time_outs_of_the_units(tmp_path):
    """These numbers live in five files: the unit texts and HOOK_BUDGET (installer.py), the
    command's own wait (cli.py), the attempt and the tidy-up after it (service.py), the stop
    budget (daemon.py) and the questions to systemd (system.py, asked through daemon.py).
    Here they are held against each other, each read from where it is used."""
    import asyncio
    import io
    import re

    from tcl_me_remo import cli, daemon
    from tcl_me_remo import service as service_module

    target = tmp_path / "opt" / "tcl-me-remo"
    units = installer.render_units(_layout(tmp_path), target)

    def seconds(unit, setting):
        (value,) = re.findall(rf"^{setting}=(\d+(?:\.\d+)?)$", units[unit], re.M)
        return float(value)

    spare = 0.5  # for a reply to travel, a process to start and end

    # The sleep hook: "tcl-me-remo event suspend --budget N", which systemd gives TimeoutStartSec.
    (command,) = re.findall(r"^ExecStart=-?(.+)$", units[SLEEP_HOOK], re.M)
    argv = command.split()
    argv = argv[argv.index(f"{target}/bin/tcl-me-remo") + 1 :]
    calls = []

    def call(method, params=None, *, timeout=30.0):
        calls.append((method, params, timeout))
        return {"ok": True, "skipped": True, "detail": "duplicate suspend event ignored", "steps": []}

    assert cli.run(argv, call=call, out=io.StringIO(), err=io.StringIO()) == 0
    ((method, params, command_waits),) = calls
    assert method == "event" and params["name"] == "suspend" and params.get("wait", True) is True
    assert params["budget"] == installer.HOOK_BUDGET
    # The service answers once the attempt and the tidy-up after it are over; the command
    # waits longer than that, and systemd longer than the command.
    service_answers = params["budget"] + service_module.DISCONNECT_SECONDS
    assert service_answers + spare <= command_waits <= seconds(SLEEP_HOOK, "TimeoutStartSec") - spare
    # A route with time of its own has more than the first one, or it has nothing to add.
    assert params["budget"] > service_module.DEFAULT_DOWN_BUDGET

    # The stop signal: ask systemd why, "TV off", tidy up, and then the rest of the way out
    # (sockets, the Bluetooth objects), all of it inside the main unit's TimeoutStopSec.
    asked = []

    async def run(argv, timeout):
        asked.append(timeout)
        return 0, "stopping\n" if "is-system-running" in argv else ""

    assert asyncio.run(daemon.shutdown_kind(run)) == "shutdown" and len(asked) == 2  # the service's question
    rest_of_the_way_out = 5.0
    stop_path = sum(asked) + daemon.STOP_BUDGET + service_module.DISCONNECT_SECONDS
    assert stop_path + rest_of_the_way_out <= seconds(SERVICE, "TimeoutStopSec")
    assert daemon.STOP_BUDGET > service_module.DEFAULT_DOWN_BUDGET


def test_plugin_name_matches_the_manifest():
    assert json.loads((REPO / "plugin.json").read_text())["name"] == installer.PLUGIN_NAME


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
