"""Install, update and remove the system service.

Used by ``install.sh`` and by the Decky plugin backend, so it must run under any Python 3
with only the standard library (Decky brings its own interpreter).

Where things go, and why (SteamOS has a read-only root that is replaced on every OS update;
Bazzite is similar and adds SELinux):

    /opt/tcl-me-remo/                 the code.  /opt survives updates on both systems.
    /etc/systemd/system/*.service     the units.  SteamOS keeps unit files (and their enable
                                      links) across updates but wipes most of the rest of /etc.
    /var/lib/tcl-me-remo/             settings and state.  /var is carried over on update.

The units start ``/usr/bin/python3`` with the code as an argument rather than executing a
file of ours directly: under SELinux, systemd may only start programs with a system label.

The service is restarted only when its code or unit actually changed.  A restart makes the
TV's Bluetooth subscription start over, so an install that changes nothing must not cause one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SERVICE = "tcl-me-remo.service"
SLEEP_HOOK = "tcl-me-remo-sleep.service"
UNITS = (SERVICE, SLEEP_HOOK)
MIN_PYTHON = (3, 11)
HOOK_BUDGET = 3.5

Run = Callable[[list[str], float], tuple[int, str]]


class InstallError(RuntimeError):
    """The installation cannot proceed; the message says what to do about it."""


@dataclass(frozen=True)
class Layout:
    install_dirs: tuple[Path, ...] = (Path("/opt/tcl-me-remo"), Path("/var/lib/tcl-me-remo/app"))
    unit_dir: Path = Path("/etc/systemd/system")
    state_dir: Path = Path("/var/lib/tcl-me-remo")
    python: str = "/usr/bin/python3"
    # Places to put a "tcl-me-remo" command, best effort.  /usr/local is read-only on SteamOS.
    link_dirs: tuple[Path, ...] = field(default=(Path("/usr/local/bin"),))


def _clean_env() -> dict[str, str]:
    # Same reasoning as system.clean_env(); duplicated to keep this module import-free.
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    env.pop("LD_LIBRARY_PATH", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    return env


def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=_clean_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def source_root() -> Path:
    """The directory that contains ``py_modules/tcl_me_remo`` for the running copy."""
    return Path(__file__).resolve().parents[2]


def _package_files(package: Path) -> list[Path]:
    return sorted(
        path
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith((".pyc", ".pyo"))
    )


def tree_digest(package: Path) -> str:
    """Stable digest of a package directory's contents (names and bytes)."""
    digest = hashlib.sha256()
    for path in _package_files(package):
        digest.update(str(path.relative_to(package)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def bundled_version(source: Path) -> str:
    init = source / "py_modules" / "tcl_me_remo" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "unknown"


def bundled_stamp(source: Path) -> str:
    return f"{bundled_version(source)} {tree_digest(source / 'py_modules' / 'tcl_me_remo')}"


def installed_dir(layout: Layout) -> Path | None:
    for candidate in layout.install_dirs:
        if (candidate / "VERSION").is_file():
            return candidate
    return None


def installed_stamp(layout: Layout) -> str:
    target = installed_dir(layout)
    if target is None:
        return ""
    try:
        return (target / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


LAUNCHER = '''#!/usr/bin/python3 -I
"""tcl-me-remo launcher (written by the installer; do not edit)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "py_modules"))

from tcl_me_remo.cli import main  # noqa: E402

sys.exit(main())
'''


def render_units(layout: Layout, target: Path) -> dict[str, str]:
    launcher = f"{layout.python} -I {target}/bin/tcl-me-remo"
    env = (
        f"Environment=TCL_ME_REMO_CONFIG={layout.state_dir}/config.json\n"
        f"Environment=TCL_ME_REMO_STATE_DIR={layout.state_dir}\n"
    )
    service = f"""[Unit]
Description=tcl-me-remo - control a TV as a Bluetooth remote
Documentation=https://github.com/tiltbob/tcl-me-remo
# Ordered after Bluetooth so that at shutdown this service is stopped first, while the
# radio can still carry the "TV off" key.
After=bluetooth.service dbus.service systemd-logind.service
Wants=bluetooth.service

[Service]
Type=notify
NotifyAccess=main
ExecStart={launcher} daemon
{env}Restart=on-failure
RestartSec=3
TimeoutStartSec=45
TimeoutStopSec=20
KillMode=mixed

[Install]
WantedBy=multi-user.target
"""
    hook = f"""[Unit]
Description=tcl-me-remo - tell the service about sleep and wake
Documentation=https://github.com/tiltbob/tcl-me-remo
# Second route for suspend/resume next to the service's own logind lock: systemd runs this
# before any kind of sleep and stops it again after wake-up.  The service ignores whichever
# notification arrives second.
Before=sleep.target
StopWhenUnneeded=yes

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=-{launcher} event suspend --budget {HOOK_BUDGET:g}
ExecStop=-{launcher} event resume --no-wait
TimeoutStartSec=10
TimeoutStopSec=10

[Install]
WantedBy=sleep.target
"""
    return {SERVICE: service, SLEEP_HOOK: hook}


def _write_if_changed(path: Path, content: str, mode: int) -> bool:
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def _own_by_root(tree: Path) -> None:
    """Make the tree root-owned and not writable by anyone else (it runs as root)."""
    for path in [tree, *tree.rglob("*")]:
        if path.is_symlink():
            continue
        if os.geteuid() == 0:
            os.chown(path, 0, 0)
        executable = path.is_dir() or (path.parent.name == "bin" and path.parent.parent == tree)
        os.chmod(path, 0o755 if executable else 0o644)


def _pick_target(layout: Layout) -> Path:
    current = installed_dir(layout)
    if current is not None:
        return current
    problems = []
    for candidate in layout.install_dirs:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            probe = candidate.parent / f".{candidate.name}.probe"
            probe.write_text("")
            probe.unlink()
            return candidate
        except OSError as exc:
            problems.append(f"{candidate}: {exc}")
    raise InstallError("no writable place for the service files (" + "; ".join(problems) + ")")


def _copy_code(source: Path, target: Path, stamp: str) -> None:
    staging = target.with_name(target.name + ".new")
    retired = target.with_name(target.name + ".old")
    for leftover in (staging, retired):
        shutil.rmtree(leftover, ignore_errors=True)
    package = source / "py_modules" / "tcl_me_remo"
    shutil.copytree(
        package,
        staging / "py_modules" / "tcl_me_remo",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (staging / "bin").mkdir()
    (staging / "bin" / "tcl-me-remo").write_text(LAUNCHER, encoding="utf-8")
    for extra in ("LICENSE", "README.md"):
        if (source / extra).is_file():
            shutil.copy2(source / extra, staging / extra)
    (staging / "VERSION").write_text(stamp + "\n", encoding="utf-8")
    _own_by_root(staging)
    if target.exists():
        os.replace(target, retired)
    os.replace(staging, target)
    shutil.rmtree(retired, ignore_errors=True)


def _check_python(layout: Layout, run: Run) -> None:
    code, out = run([layout.python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], 10.0)
    if code != 0:
        raise InstallError(f"{layout.python} is missing or does not run; the service needs Python 3.11 or newer")
    try:
        version = tuple(int(part) for part in out.strip().split("."))
    except ValueError:
        raise InstallError(f"could not read the version of {layout.python} ({out.strip()!r})") from None
    if version < MIN_PYTHON:
        needed = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        raise InstallError(f"{layout.python} is Python {out.strip()}; the service needs Python {needed} or newer")


def _link_command(layout: Layout, target: Path, user_home: Path | None, log: list[str]) -> None:
    launcher = target / "bin" / "tcl-me-remo"
    dirs = list(layout.link_dirs)
    if user_home is not None:
        dirs.append(user_home / ".local" / "bin")
    for directory in dirs:
        link = directory / "tcl-me-remo"
        try:
            if link.is_symlink() and os.readlink(link) == str(launcher):
                continue
            directory.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(launcher)
            if user_home is not None and directory.is_relative_to(user_home) and os.geteuid() == 0:
                owner = user_home.stat()
                for path in (directory, link):
                    os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
            log.append(f"command available as {link}")
        except OSError as exc:
            log.append(f"could not create {link}: {exc}")


def info(layout: Layout | None = None, run: Run = run_command, *, source: Path | None = None) -> dict[str, Any]:
    """Describe what is installed.  Never raises."""
    layout = layout or Layout()
    source = source or source_root()
    stamp = installed_stamp(layout)
    try:
        bundled = bundled_stamp(source)
    except OSError:
        bundled = ""
    problems: list[str] = []
    target = installed_dir(layout)
    units_present = all((layout.unit_dir / unit).is_file() for unit in UNITS)
    active = run(["systemctl", "is-active", SERVICE], 5.0)[1].strip()
    enabled = run(["systemctl", "is-enabled", SERVICE], 5.0)[1].strip()
    hook_enabled = run(["systemctl", "is-enabled", SLEEP_HOOK], 5.0)[1].strip()
    installed = target is not None and units_present
    if target is not None and not units_present:
        problems.append("the service files are in place but the systemd units are missing (an OS update?)")
    if installed and active != "active":
        problems.append(f"the service is {active or 'not running'}")
    if installed and enabled != "enabled":
        problems.append("the service is not enabled, so it will not start at boot")
    if installed and hook_enabled != "enabled":
        problems.append("the sleep hook is not enabled")
    return {
        "installed": installed,
        "active": active == "active",
        "enabled": enabled == "enabled",
        "installed_version": stamp.split(" ")[0] if stamp else "",
        "bundled_version": bundled.split(" ")[0] if bundled else "",
        "needs_update": bool(bundled) and stamp != bundled,
        "install_dir": str(target) if target else "",
        "problems": problems,
    }


def install(
    layout: Layout | None = None,
    run: Run = run_command,
    *,
    source: Path | None = None,
    user_home: Path | None = None,
    start: bool = True,
) -> dict[str, Any]:
    """Install or update.  Idempotent; restarts the service only if something changed."""
    layout = layout or Layout()
    source = source or source_root()
    log: list[str] = []
    if not (source / "py_modules" / "tcl_me_remo" / "__init__.py").is_file():
        raise InstallError(f"{source} does not contain the tcl-me-remo code")
    _check_python(layout, run)

    target = _pick_target(layout)
    stamp = bundled_stamp(source)
    code_changed = installed_stamp(layout) != stamp or not (target / "bin" / "tcl-me-remo").is_file()
    if code_changed:
        try:
            _copy_code(source, target, stamp)
        except OSError as exc:
            raise InstallError(f"could not write to {target}: {exc}") from None
        log.append(f"installed version {stamp.split(' ')[0]} to {target}")
        if shutil.which("restorecon"):
            run(["restorecon", "-R", str(target)], 30.0)

    try:
        layout.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(layout.state_dir, 0o755)
    except OSError as exc:
        raise InstallError(f"could not create {layout.state_dir}: {exc}") from None

    units_changed = False
    try:
        for name, content in render_units(layout, target).items():
            if _write_if_changed(layout.unit_dir / name, content, 0o644):
                units_changed = True
                log.append(f"wrote {layout.unit_dir / name}")
    except OSError as exc:
        raise InstallError(f"could not write the systemd units: {exc}") from None
    if units_changed:
        run(["systemctl", "daemon-reload"], 30.0)

    for unit in UNITS:
        if run(["systemctl", "is-enabled", unit], 5.0)[1].strip() != "enabled":
            code, out = run(["systemctl", "enable", unit], 30.0)
            if code != 0:
                raise InstallError(f"could not enable {unit}: {out.strip()}")
            log.append(f"enabled {unit}")

    restarted = False
    if start:
        active = run(["systemctl", "is-active", SERVICE], 5.0)[1].strip() == "active"
        if code_changed or units_changed or not active:
            code, out = run(["systemctl", "restart", SERVICE], 60.0)
            if code != 0:
                raise InstallError(
                    f"the service did not start: {out.strip() or 'see journalctl -u tcl-me-remo'}"
                )
            restarted = True
            log.append("service restarted" if active else "service started")

    _link_command(layout, target, user_home, log)
    if not log:
        log.append("already up to date")
    state = info(layout, run, source=source)
    return {"changed": code_changed or units_changed, "restarted": restarted, "log": log, **state}


def uninstall(layout: Layout | None = None, run: Run = run_command, *, purge: bool = False) -> dict[str, Any]:
    """Remove the service.  Settings and the TV pairing are kept unless ``purge`` is set."""
    layout = layout or Layout()
    log: list[str] = []
    for unit in reversed(UNITS):
        if (layout.unit_dir / unit).exists():
            run(["systemctl", "disable", "--now", unit], 60.0)
            try:
                (layout.unit_dir / unit).unlink()
            except OSError as exc:
                log.append(f"could not remove {layout.unit_dir / unit}: {exc}")
            else:
                log.append(f"removed {unit}")
    run(["systemctl", "daemon-reload"], 30.0)
    for directory in layout.link_dirs:
        link = directory / "tcl-me-remo"
        if link.is_symlink():
            try:
                link.unlink()
            except OSError:
                pass
    for candidate in layout.install_dirs:
        for suffix in ("", ".new", ".old"):
            path = candidate.with_name(candidate.name + suffix)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                log.append(f"removed {path}")
    if purge and layout.state_dir.exists():
        shutil.rmtree(layout.state_dir, ignore_errors=True)
        log.append(f"removed settings and state in {layout.state_dir}")
    elif layout.state_dir.exists():
        log.append(f"kept settings in {layout.state_dir} (use --purge to remove them)")
    if not log:
        log.append("nothing was installed")
    return {"log": log}


def uninstall_if_orphaned(plugin_dir: Path, layout: Layout | None = None, run: Run = run_command) -> dict[str, Any]:
    """Remove the service if the Decky plugin that installed it is gone.

    Decky calls a plugin's uninstall hook for updates as well as real removals, so the hook
    cannot remove the service itself.  It schedules this check for a little later instead:
    after an update the plugin directory exists again and nothing happens.
    """
    if (plugin_dir / "plugin.json").is_file():
        return {"log": [f"{plugin_dir} is still installed; leaving the service alone"]}
    return uninstall(layout, run)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="tcl-me-remo-install", description="Install or remove the tcl-me-remo service."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install", help="install or update the service and start it")
    p.add_argument("--user-home", type=Path, help="also link the command into this user's ~/.local/bin")
    p = sub.add_parser("uninstall", help="stop and remove the service")
    p.add_argument("--purge", action="store_true", help="also delete settings and state")
    sub.add_parser("status", help="show what is installed")
    p = sub.add_parser("uninstall-if-orphaned", help=argparse.SUPPRESS)
    p.add_argument("plugin_dir", type=Path)
    args = parser.parse_args(argv)

    if args.command != "status" and os.geteuid() != 0:
        print("this needs root: run it with sudo", file=sys.stderr)
        return 1
    try:
        if args.command == "install":
            result = install(user_home=args.user_home)
        elif args.command == "uninstall":
            result = uninstall(purge=args.purge)
        elif args.command == "uninstall-if-orphaned":
            result = uninstall_if_orphaned(args.plugin_dir)
        else:
            result = info()
            state = "installed" if result["installed"] else "not installed"
            running = "running" if result["active"] else "not running"
            print(f"tcl-me-remo {result['installed_version'] or result['bundled_version']}: {state}, {running}")
            result = {"log": result["problems"]}
    except InstallError as exc:
        print(f"tcl-me-remo: {exc}", file=sys.stderr)
        return 1
    for line in result.get("log", []):
        print(line)
    for problem in result.get("problems", []):
        print(f"warning: {problem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
