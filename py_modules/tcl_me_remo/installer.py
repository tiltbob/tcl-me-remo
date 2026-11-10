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

Root never writes inside a home directory: the ``tcl-me-remo`` link in ``~/.local/bin`` is
made by a child process that has taken the identity of the home's owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SERVICE = "tcl-me-remo.service"
SLEEP_HOOK = "tcl-me-remo-sleep.service"
UNITS = (SERVICE, SLEEP_HOOK)
MIN_PYTHON = (3, 11)
# Seconds the sleep hook gives "TV off".  Normally the service has dealt with the suspend
# through logind by the time the hook runs, and the hook returns at once; this is for the
# cases in which it is the first to report it, or the attempt may be made a second time
# (service.py, _second_attempt_due).  With the final disconnect it must stay well inside the
# hook's TimeoutStartSec=10, and the CLI itself waits 3 s longer than the budget.
HOOK_BUDGET = 6
# What "systemctl is-active" prints for a unit that runs or is on its way to running.
RUNNING_STATES = ("active", "activating", "reloading")

# The "name" in the Decky plugin's plugin.json.  Decky finds a plugin by it, not by its folder.
PLUGIN_NAME = "tcl-me-remo"
# Files of the installer's own in the state directory:
#   the note the plugin's uninstall hook leaves (main.py writes it: {"plugins_dir", "time"}),
REMOVED_MARKER = "plugin-removed.json"
#   and the links to the command that were made, so that uninstall removes exactly those.
LINKS_RECORD = "command-links.json"
# In the install directory: the boot in which code or units were replaced and the moment
# within it (CLOCK_MONOTONIC, microseconds), until the service has been restarted with them.
RESTART_PENDING = "RESTART-PENDING"
# Seconds between "the plugin was removed" and the check whether it is really gone (Decky
# removes a plugin and puts it back when it updates one), and how old the note must be
# before a check may act on it.  The first is also how long after its start the service
# waits before it looks for a note that a reboot left unanswered.
ORPHAN_CHECK_DELAY = 180
ORPHAN_MIN_AGE = 120
# Decky unpacks a plugin folder by folder: a plugins directory that changed within this many
# seconds may be in the middle of that, and what is missing from it proves nothing yet.
PLUGINS_SETTLE = 60
# How the check runs under systemd-run: as a unit of its own, gone when it has ended, and
# never there for good should it hang.  (main.py carries the same options.)
ORPHAN_CHECK_OPTIONS = (
    "--collect",
    "--quiet",
    "--property=RuntimeMaxSec=600",
    "--description=tcl-me-remo: remove the service if the plugin is gone",
)
# More of a plugin.json than this is not read.
MANIFEST_LIMIT = 64 * 1024

# Runs this module from a given py_modules directory.  -I: no user site-packages and no
# environment influence; it runs as root.  (main.py carries the same text.)
INSTALLER_SNIPPET = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from tcl_me_remo import installer; sys.exit(installer.main(sys.argv[2:]))"
)

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
# Well clear of systemd's default of 1024: the service must still be able to open sysfs and
# its settings file when something holds every control connection it allows, and more.
LimitNOFILE=4096

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


def _holds(path: Path, content: str) -> bool:
    try:
        return path.read_text(encoding="utf-8") == content
    except OSError:
        return False


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _properties(text: str) -> dict[str, str]:
    """What "systemctl show -p ..." printed, as a dictionary."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _mark_restart_pending(tree: Path) -> None:
    """Note that the running service is older than what is being installed.

    The note names the current boot, because a reboot starts the service with the new files
    just as a restart does; a note from an earlier boot means nothing.  It also names the
    moment, on the clock systemd uses for a service's start time: a service that was started
    after it (the panel's "Restart service", systemd after a crash) runs the new files too.
    """
    moment = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
    (tree / RESTART_PENDING).write_text(f"{_boot_id()} {moment}\n", encoding="ascii")


def _restart_pending(target: Path | None, run: Run) -> bool:
    """True if code or units were replaced in this boot and the service was not restarted since.

    Asks systemd only when there is a note from this boot, which is rare.  Whatever cannot
    be found out counts as "not restarted": one restart too many is the smaller mistake.
    """
    if target is None:
        return False
    try:
        boot, _, moment = (target / RESTART_PENDING).read_text(encoding="ascii").strip().partition(" ")
    except (OSError, ValueError):
        return False
    if boot != _boot_id():
        return False
    shown = _properties(
        run(["systemctl", "show", "-p", "NeedDaemonReload", "-p", "ExecMainStartTimestampMonotonic", SERVICE], 5.0)[1]
    )
    if shown.get("NeedDaemonReload") != "no":
        return True  # systemd has not even read the unit that was written (or cannot be asked)
    try:
        return int(shown["ExecMainStartTimestampMonotonic"]) <= int(moment)
    except (KeyError, ValueError):
        return True


def _mark_again(target: Path) -> None:
    """Move the note's moment forward to now; if that fails, the earlier one stands."""
    try:
        _mark_restart_pending(target)
    except OSError:
        pass


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
    _mark_restart_pending(staging)
    _own_by_root(staging)
    if target.exists():
        os.replace(target, retired)
    os.replace(staging, target)
    _mark_again(target)  # only a service started from here on has the new files
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


# Makes or removes ~/.local/bin/tcl-me-remo.  Run in a child process with the identity of the
# home's owner, see _user_link().  Arguments: "create" or "remove", the home directory, the
# launcher to link to, then every launcher path that counts as ours.
_USER_LINK_SCRIPT = r"""
import os
import sys

action, home, launcher = sys.argv[1:4]
ours = sys.argv[4:]
os.umask(0o022)
directory = os.path.join(home, ".local", "bin")
link = os.path.join(directory, "tcl-me-remo")
try:
    current = os.readlink(link) if os.path.islink(link) else None
    if action == "remove":
        if current in ours:
            os.unlink(link)
            print("removed")
        else:
            print("kept")
    elif current == launcher:
        print("present")
    elif current not in ours and os.path.lexists(link):
        print("foreign")  # a file or a link of the user's own: not ours to replace
    else:
        if current is not None:
            os.unlink(link)
        os.makedirs(directory, exist_ok=True)
        os.symlink(launcher, link)
        print("created")
except OSError as exc:
    print("error", exc.strerror or exc)
"""


def _launchers(layout: Layout) -> list[str]:
    """Every path our launcher can have; a link to one of these was made by an install."""
    return [str(candidate / "bin" / "tcl-me-remo") for candidate in layout.install_dirs]


def _user_link(action: str, home: Path, launcher: str, ours: list[str]) -> tuple[str, str]:
    """Create or remove ``~/.local/bin/tcl-me-remo`` as the owner of ``home``.

    Returns ``(outcome, detail)``; the outcome is "created", "present", "foreign", "removed",
    "kept" or "error".

    Everything below a home directory belongs to its owner, including where each part of a
    path leads.  Root creating, deleting or chowning something "in ~/.local/bin" can be
    pointed anywhere, so root does not do it: a child process gives up root first and then
    can do exactly what the user could do anyway.  Nothing needs a chown afterwards either.
    """
    try:
        owner = os.stat(home)
    except OSError as exc:
        return "error", f"{home}: {exc.strerror or exc}"
    identity: dict[str, Any] = {}
    if os.geteuid() == 0 and owner.st_uid != 0:
        identity = {"user": owner.st_uid, "group": owner.st_gid, "extra_groups": []}
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-I", "-c", _USER_LINK_SCRIPT, action, str(home), launcher, *ours],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20.0,
            env=_clean_env(),
            cwd="/",
            check=False,
            **identity,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return "error", str(exc)
    lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    outcome, _, detail = (lines[-1] if lines else "").partition(" ")
    if proc.returncode != 0 or outcome not in ("created", "present", "foreign", "removed", "kept", "error"):
        return "error", lines[-1] if lines else f"exit code {proc.returncode}"
    return outcome, detail


def _recorded_links(layout: Layout) -> list[dict[str, str]]:
    """The links earlier installs made: ``{"link": path, "home": home or ""}`` each."""
    try:
        data = json.loads((layout.state_dir / LINKS_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and isinstance(item.get("link"), str) and isinstance(item.get("home"), str):
            entries.append({"link": item["link"], "home": item["home"]})
    return entries


def _link_command(
    layout: Layout, target: Path, user_home: Path | None, log: list[str], notes: list[str]
) -> None:
    """Put a ``tcl-me-remo`` command on the PATH, and remember where.

    ``log`` gets what was done; ``notes`` what could not be done and the user should know.
    """
    launcher = str(target / "bin" / "tcl-me-remo")
    ours = _launchers(layout)
    in_place: list[dict[str, str]] = []
    for directory in layout.link_dirs:
        link = directory / "tcl-me-remo"
        try:
            current = os.readlink(link) if link.is_symlink() else None
            if current != launcher:
                if current not in ours and (link.is_symlink() or link.exists()):
                    continue  # somebody else's
                directory.mkdir(parents=True, exist_ok=True)
                if current is not None:
                    link.unlink()
                link.symlink_to(launcher)
                log.append(f"command available as {link}")
            in_place.append({"link": str(link), "home": ""})
        except OSError:
            # These directories are a convenience and often cannot be written: /usr/local is
            # part of the read-only system image on SteamOS.  Not worth a line in the result.
            continue
    if user_home is not None:
        link = user_home / ".local" / "bin" / "tcl-me-remo"
        outcome, detail = _user_link("create", user_home, launcher, ours)
        if outcome == "created":
            log.append(f"command available as {link}")
        if outcome in ("created", "present"):
            in_place.append({"link": str(link), "home": str(user_home)})
        elif outcome == "foreign":
            notes.append(f"left {link} alone: it was not put there by tcl-me-remo")
        else:
            notes.append(f"could not link the command into {link.parent}: {detail}")
    if not in_place:
        notes.append(f"the command is {launcher} (it could not be linked into a directory on the PATH)")
    recorded = _recorded_links(layout)
    merged = recorded + [entry for entry in in_place if entry not in recorded]
    if merged != recorded:
        try:
            _write_if_changed(layout.state_dir / LINKS_RECORD, json.dumps(merged, indent=1) + "\n", 0o644)
        except OSError as exc:
            notes.append(f"could not write {layout.state_dir / LINKS_RECORD}: {exc}")


def _remove_links(layout: Layout, log: list[str]) -> None:
    """Remove the links installs made, as far as they still point at our launcher."""
    ours = _launchers(layout)
    entries = _recorded_links(layout)
    entries += [{"link": str(directory / "tcl-me-remo"), "home": ""} for directory in layout.link_dirs]
    done: set[str] = set()
    for entry in entries:
        if entry["link"] in done:
            continue
        done.add(entry["link"])
        if entry["home"]:
            removed = _user_link("remove", Path(entry["home"]), ours[0], ours)[0] == "removed"
        else:
            link = Path(entry["link"])
            try:
                removed = link.is_symlink() and os.readlink(link) in ours
                if removed:
                    link.unlink()
            except OSError:
                removed = False
        if removed:
            log.append(f"removed {entry['link']}")
    try:
        (layout.state_dir / LINKS_RECORD).unlink()
    except OSError:
        pass


def _start_trouble(run: Run) -> str:
    """For a unit that "is-active" calls "activating": what is wrong if it is not simply starting.

    systemd calls a service "activating" while it starts, and also while it waits to start a
    service again that has failed (Restart=on-failure).  One that fails at every start is
    therefore "activating" nearly all the time, and never "failed".
    """
    shown = _properties(run(["systemctl", "show", "-p", "SubState", "-p", "NRestarts", SERVICE], 5.0)[1])
    try:
        restarts = int(shown.get("NRestarts", "0"))
    except ValueError:
        restarts = 0
    # NRestarts counts the restarts that were begun; in "auto-restart" one more is due.
    failures = restarts + shown.get("SubState", "").startswith("auto-restart")
    if failures >= 2:
        return (
            f"the service keeps failing (systemd has had to start it again {failures} times);"
            " to see why: journalctl -b -u tcl-me-remo"
        )
    if failures:
        return "the service failed and systemd is starting it again; if this goes on, see: journalctl -b -u tcl-me-remo"
    return ""


def info(layout: Layout | None = None, run: Run = run_command, *, source: Path | None = None) -> dict[str, Any]:
    """Describe what is installed.  Never raises."""
    return _describe(layout or Layout(), run, source)[0]


def _describe(layout: Layout, run: Run, source: Path | None) -> tuple[dict[str, Any], str]:
    """What ``info()`` returns, and separately the trouble the service has starting, if any."""
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
    restart_pending = installed and _restart_pending(target, run)
    trouble = _start_trouble(run) if installed and active == "activating" else ""
    if target is not None and not units_present:
        problems.append("the service files are in place but the systemd units are missing (an OS update?)")
    if installed and active == "activating":
        problems.append(trouble or "the service is still starting")
    elif installed and active != "active":
        problems.append(f"the service is {active or 'not running'}")
    elif restart_pending:
        problems.append(
            f"version {stamp.split(' ')[0]} was installed, but the service was not restarted and still runs"
            " what was there before; install again (Repair) to finish the update"
        )
    if installed and enabled != "enabled":
        problems.append("the service is not enabled, so it will not start at boot")
    if installed and hook_enabled != "enabled":
        problems.append("the sleep hook is not enabled")
    state = {
        "installed": installed,
        "active": active == "active",
        "enabled": enabled == "enabled",
        "installed_version": stamp.split(" ")[0] if stamp else "",
        "bundled_version": bundled.split(" ")[0] if bundled else "",
        "needs_update": bool(bundled) and stamp != bundled,
        # The installed files are newer than the running service (an install was cut short).
        "restart_pending": restart_pending,
        "install_dir": str(target) if target else "",
        "problems": problems,
    }
    return state, trouble


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
    notes: list[str] = []
    if not (source / "py_modules" / "tcl_me_remo" / "__init__.py").is_file():
        raise InstallError(f"{source} does not contain the tcl-me-remo code")
    # Whoever installs wants the service: a note that the Decky plugin was removed is void.
    _clear_removed_marker(layout)
    _check_python(layout, run)

    target = _pick_target(layout)
    # An earlier install may have replaced code or units and ended before its restart.
    unfinished = _restart_pending(target, run)
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
            if _holds(layout.unit_dir / name, content):
                continue
            # Noted before the write: should this install end before the restart below, the
            # next one still knows that the service runs with the old unit.
            _mark_restart_pending(target)
            _write_if_changed(layout.unit_dir / name, content, 0o644)
            units_changed = True
            log.append(f"wrote {layout.unit_dir / name}")
    except OSError as exc:
        raise InstallError(f"could not write the systemd units: {exc}") from None
    if units_changed or unfinished:  # the earlier install may have ended before its reload, too
        run(["systemctl", "daemon-reload"], 30.0)
        _mark_again(target)  # a service started before systemd read the units does not count

    for unit in UNITS:
        if run(["systemctl", "is-enabled", unit], 5.0)[1].strip() != "enabled":
            code, out = run(["systemctl", "enable", unit], 30.0)
            if code != 0:
                raise InstallError(f"could not enable {unit}: {out.strip()}")
            log.append(f"enabled {unit}")

    restarted = False
    if start:
        state = run(["systemctl", "is-active", SERVICE], 5.0)[1].strip()
        running = state in RUNNING_STATES
        if code_changed or units_changed or unfinished:
            verb = "restart"
        elif not running:
            # Not "restart": at boot the unit may be starting this very moment (a queued
            # start job, or "activating"), and "start" then waits for that instead of
            # killing a service that is just registering with BlueZ.
            verb = "start"
        else:
            verb = ""
        if verb:
            code, out = run(["systemctl", verb, SERVICE], 60.0)
            if code != 0:
                raise InstallError(
                    f"the service did not start: {out.strip() or 'see journalctl -u tcl-me-remo'}"
                )
            if verb == "restart" and running and not (code_changed or units_changed):
                restarted = True
                log.append("service restarted to finish an earlier update")
            elif verb == "restart" or state in ("inactive", "failed", "deactivating"):
                restarted = True
                log.append("service restarted" if running else "service started")
            # else: the state could not be read and "start" was the safe thing to say to a
            # service that most likely runs; nothing to report.
        try:
            (target / RESTART_PENDING).unlink()
        except OSError:
            pass

    _link_command(layout, target, user_home, log, notes)
    if not log:
        log.append("already up to date")
    state, trouble = _describe(layout, run, source)
    if trouble:
        # Nothing here restarts a service that is being started, but "already up to date"
        # must not be all that Repair has to say about one that fails every time.
        notes.append(trouble)
    return {"changed": code_changed or units_changed, "restarted": restarted, "log": log + notes, **state}


_MAC = re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}")


def _tv_addresses(state_dir: Path) -> list[str]:
    """Addresses of every TV the service has, or may have left, a Bluetooth bond with.

    From the settings (the paired TV) and the Bluetooth state file (the TV last used, and
    bonds that were to be removed later).  BlueZ keeps the bonds themselves.
    """

    def read(name: str) -> dict[str, Any]:
        try:
            data = json.loads((state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    bluetooth = read("config.json").get("bluetooth")
    store = read("bluetooth.json")
    candidates = [bluetooth.get("tv_address") if isinstance(bluetooth, dict) else None, store.get("tv")]
    for name in ("stale", "pending"):
        if isinstance(store.get(name), list):
            candidates.extend(store[name])
    found: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str) and _MAC.fullmatch(candidate.upper()) and candidate.upper() not in found:
            found.append(candidate.upper())
    return found


def _clear_removed_marker(layout: Layout) -> None:
    try:
        (layout.state_dir / REMOVED_MARKER).unlink()
    except OSError:
        pass


def uninstall(layout: Layout | None = None, run: Run = run_command, *, purge: bool = False) -> dict[str, Any]:
    """Remove the service.  Settings and the TV pairing are kept unless ``purge`` is set."""
    layout = layout or Layout()
    log: list[str] = []
    target = installed_dir(layout)
    # Read before anything else: these files are the only record of which bonds are ours,
    # and both the service's "forget" and the purge erase them.
    bonds = _tv_addresses(layout.state_dir) if purge else []
    forgotten = False
    if bonds and target is not None:
        # The pairing goes with the settings.  It consists of a bond that BlueZ keeps, and
        # with the settings gone nothing would know which bond that is, while BlueZ refuses
        # to pair again with a TV it still holds one for.  The running service removes it
        # best (it also stops calling the TV), so ask it first, through its own command.
        code, _out = run([layout.python, "-I", str(target / "bin" / "tcl-me-remo"), "forget"], 10.0)
        if code == 0:
            forgotten = True
            log.append("the service removed its Bluetooth pairing with the TV")
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
    if purge:
        # Then every bond named before or now, directly: the service may not have answered,
        # may have been stopped half-way, or could not reach BlueZ and put the bond on its
        # list for later.  For a bond that is gone already this says nothing.
        for address in dict.fromkeys(bonds + _tv_addresses(layout.state_dir)):
            code, out = run(["bluetoothctl", "--timeout", "5", "remove", address], 10.0)
            if code == 0:
                log.append(f"removed the Bluetooth pairing with {address}")
            elif "not available" in out:  # "Device ... not available": BlueZ has no such bond
                continue
            elif forgotten:
                # The service has reported the pairing removed; this was only to make sure.
                log.append(
                    f"could not check that no Bluetooth pairing with {address} is left;"
                    f" if pairing that TV fails later, run: bluetoothctl remove {address}"
                )
            else:
                log.append(
                    f"could not remove the Bluetooth pairing with {address};"
                    f" before pairing that TV again, run: bluetoothctl remove {address}"
                )
    _remove_links(layout, log)
    for candidate in layout.install_dirs:
        for suffix in ("", ".new", ".old"):
            path = candidate.with_name(candidate.name + suffix)
            if path.is_symlink():
                path.unlink(missing_ok=True)  # somebody's link to another place: the link goes, no more
            elif path.exists():
                shutil.rmtree(path, ignore_errors=True)
            else:
                continue
            log.append(f"could not remove {path}" if os.path.lexists(path) else f"removed {path}")
    _clear_removed_marker(layout)
    if purge and layout.state_dir.exists():
        shutil.rmtree(layout.state_dir, ignore_errors=True)
        log.append(f"removed settings and state in {layout.state_dir}")
    elif layout.state_dir.exists():
        log.append(f"kept settings in {layout.state_dir} (use --purge to remove them)")
    if not log:
        log.append("nothing was installed")
    return {"log": log}


def _manifest_text(path: Path) -> str | None:
    """The text of a plugin.json; None if there is no such file.  Raises OSError if it cannot be read.

    The plugins folder belongs to the user and this runs as root, unattended: something
    that is not a regular file (a FIFO would block for good) is not opened for reading, and
    no more than a manifest's worth is read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = b""
        while len(data) < MANIFEST_LIMIT:
            chunk = os.read(fd, MANIFEST_LIMIT - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    return data.decode("utf-8", "replace")


def _changed_recently(path: Path, now: float) -> bool:
    try:
        return 0 <= now - os.stat(path).st_mtime < PLUGINS_SETTLE
    except OSError:
        return False


def _look_for_plugin(plugins_dir: Path, now: float) -> tuple[bool | None, str]:
    """``plugin_installed()``, and for "cannot tell" the reason in words."""
    unreadable = f"{plugins_dir} is not there or cannot be read"
    try:
        if not plugins_dir.is_absolute() or not plugins_dir.parent.is_dir() or not plugins_dir.is_dir():
            return None, unreadable
        settled = not _changed_recently(plugins_dir, now)
        folders = sorted(plugins_dir.iterdir())
    except OSError:
        return None, unreadable
    for folder in folders:
        try:
            text = _manifest_text(folder / "plugin.json")
        except OSError:
            return None, f"{folder / 'plugin.json'} cannot be read"
        if text is None:
            if folder.is_dir() and _changed_recently(folder, now):
                settled = False  # possibly ours, being unpacked: the manifest is not there yet
            continue
        try:
            manifest = json.loads(text)
            name = manifest.get("name") if isinstance(manifest, dict) else None
        except ValueError:
            # Possibly ours, caught half-written during an update.
            name = PLUGIN_NAME if PLUGIN_NAME in text else None
        if name == PLUGIN_NAME:
            return True, ""
    if not settled:
        return None, f"{plugins_dir} was changed moments ago; Decky may be installing a plugin"
    return False, ""


def plugin_installed(plugins_dir: Path, *, now: float | None = None) -> bool | None:
    """Whether Decky has the plugin in ``plugins_dir``: True, False, or None for "cannot tell".

    Decky knows a plugin by the name in its plugin.json and unpacks an update into whatever
    folder the zip names, so every folder is looked at, not one fixed path.  A plugins
    directory that is not there (the home is not mounted yet, Decky was moved or removed)
    proves nothing, and neither does a plugin.json that cannot be read, nor the absence of
    the plugin from a directory that was changed a moment ago.
    """
    return _look_for_plugin(plugins_dir, time.time() if now is None else now)[0]


def _removed_marker(layout: Layout) -> tuple[bool, dict[str, Any] | None]:
    """Whether the plugin left a note that it was removed, and what the note says.

    ``(True, None)`` is a note that cannot be used: unreadable, or without the absolute path
    of a plugins directory.
    """
    path = layout.state_dir / REMOVED_MARKER
    if not path.is_file():
        return False, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, None
    where = data.get("plugins_dir") if isinstance(data, dict) else None
    if not isinstance(where, str) or not os.path.isabs(where):
        return True, None
    return True, data


def uninstall_if_orphaned(
    plugins_dir: Path | None = None,
    layout: Layout | None = None,
    run: Run = run_command,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Remove the service if the Decky plugin that installed it is gone.

    Decky calls a plugin's uninstall hook for updates as well as real removals, so the hook
    cannot remove the service itself.  It leaves a note in the state directory and this check
    runs a little later: from a timer the hook sets, and, because a reboot discards that
    timer, again some minutes after each start of the service for as long as the note is
    there.  Any install removes the note.

    Without ``plugins_dir`` the note decides where to look, and without a note nothing
    happens.  (The hook passes the directory only when it could not write the note.)  The
    service is removed only when the plugins directory is there and holds no plugin of ours;
    when that cannot be told, the note stays for the next check.
    """
    layout = layout or Layout()
    now = time.time() if now is None else now
    if plugins_dir is None:
        noted, marker = _removed_marker(layout)
        if not noted:
            return {"orphaned": False, "log": ["no removed plugin is on record; leaving the service alone"]}
        if marker is None:
            # Kept, it would have the service start this check after every start, for nothing.
            _clear_removed_marker(layout)
            return {
                "orphaned": None,
                "log": [
                    f"the note that the plugin was removed ({layout.state_dir / REMOVED_MARKER}) cannot be"
                    " read; discarding it and leaving the service alone (if the plugin is gone, remove the"
                    " service with: tcl-me-remo uninstall)"
                ],
            }
        written = marker.get("time")
        age = now - (written if isinstance(written, (int, float)) else 0)
        if 0 <= age < ORPHAN_MIN_AGE:
            # Decky may be in the middle of an update: the old folder is gone, the new one
            # not there yet.  The timer set together with this note comes back later.
            return {"orphaned": None, "log": ["the plugin was removed moments ago; a later check decides"]}
        plugins_dir = Path(marker["plugins_dir"])
    present, why = _look_for_plugin(plugins_dir, now)
    if present is None:
        return {
            "orphaned": None,
            "log": [f"cannot tell whether the plugin is still installed ({why}); leaving the service alone"],
        }
    if present:
        _clear_removed_marker(layout)
        return {"orphaned": False, "log": [f"the plugin is installed in {plugins_dir}; leaving the service alone"]}
    result = uninstall(layout, run)
    return {"orphaned": True, "log": [f"the plugin is gone from {plugins_dir}; removing its service", *result["log"]]}


def self_command(*args: str) -> list[str]:
    """The command line that runs this copy of the installer with the given arguments."""
    modules = Path(__file__).resolve().parents[1]
    return [sys.executable, "-I", "-c", INSTALLER_SNIPPET, str(modules), *args]


def orphan_check_command(state_dir: Path) -> list[str] | None:
    """What the running service starts when the plugin left a note that it was removed.

    None if there is no note.  The check may end in removing the service, so it must not be
    a child of the service (stopping the unit would kill it half-way): systemd-run starts it
    as a unit of its own.
    """
    if not (state_dir / REMOVED_MARKER).is_file():
        return None
    return ["systemd-run", *ORPHAN_CHECK_OPTIONS, *self_command("uninstall-if-orphaned")]


def _spawn(argv: list[str]) -> int:
    """Run a command on the caller's terminal (sudo may have to ask for a password)."""
    try:
        return subprocess.call(argv, env=_clean_env())  # noqa: S603 - fixed argv, no shell
    except OSError as exc:
        print(f"tcl-me-remo: could not run {argv[0]}: {exc}", file=sys.stderr)
        return 127


def uninstall_from_command_line(purge: bool, out: Any, err: Any) -> int:
    """``tcl-me-remo uninstall``: remove the service with the installed copy of this installer.

    For people who have neither the Decky plugin (any more) nor a checkout with install.sh.
    Returns a process exit code.
    """
    argv = self_command("uninstall", *(["--purge"] if purge else []))
    if os.geteuid() != 0:
        if shutil.which("sudo") is None:
            print("tcl-me-remo: removing a system service needs root, and there is no sudo; run this as root", file=err)
            return 1
        print("Root is needed to remove a system service; asking sudo.", file=out)
        print(
            "(If it asks for a password and you never set one, which is how SteamOS starts out:"
            " press Ctrl+C, set one with 'passwd', and run this again.)",
            file=out,
        )
        argv = ["sudo", "--", *argv]
    print(
        "If the Decky plugin is still installed, uninstall it in Decky as well:"
        " it installs the service again whenever it loads.",
        file=out,
    )
    if hasattr(out, "flush"):
        out.flush()
    if _spawn(argv) == 0:
        return 0
    if argv[0] == "sudo":
        print(
            "tcl-me-remo: that did not complete. If sudo refused the password, or you have none yet,"
            " set one with 'passwd' and run this again.",
            file=err,
        )
    return 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="tcl-me-remo-install", description="Install or remove the tcl-me-remo service."
    )
    parser.add_argument("--json", action="store_true", help="print the result as one JSON object")
    parser.add_argument("--source", type=Path, help="directory that holds py_modules/ (default: this copy)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install", help="install or update the service and start it")
    p.add_argument("--user-home", type=Path, help="also link the command into this user's ~/.local/bin")
    p = sub.add_parser("uninstall", help="stop and remove the service")
    p.add_argument("--purge", action="store_true", help="also delete settings and state")
    sub.add_parser("status", help="show what is installed")
    p = sub.add_parser("uninstall-if-orphaned", help=argparse.SUPPRESS)
    p.add_argument("--plugins-dir", type=Path, help="where to look when there is no note from the plugin")
    args = parser.parse_args(argv)

    def emit(result: dict[str, Any], code: int) -> int:
        if args.json:
            print(json.dumps(result))
        return code

    if args.command != "status" and os.geteuid() != 0:
        message = "this needs root: run it with sudo"
        print(message, file=sys.stderr)
        return emit({"error": message}, 1)
    try:
        if args.command == "install":
            result = install(source=args.source, user_home=args.user_home)
        elif args.command == "uninstall":
            result = uninstall(purge=args.purge)
        elif args.command == "uninstall-if-orphaned":
            result = uninstall_if_orphaned(args.plugins_dir)
        else:
            result = info(source=args.source)
            if not args.json:
                state = "installed" if result["installed"] else "not installed"
                running = "running" if result["active"] else "not running"
                if result["restart_pending"]:
                    running += " (an older version: the update was not finished)"
                print(f"tcl-me-remo {result['installed_version'] or result['bundled_version']}: {state}, {running}")
    except InstallError as exc:
        print(f"tcl-me-remo: {exc}", file=sys.stderr)
        return emit({"error": str(exc)}, 1)
    if not args.json:
        for line in result.get("log", []):
            print(line)
        for problem in result.get("problems", []):
            if problem not in result.get("log", []):
                print(f"warning: {problem}")
    return emit(result, 0)


if __name__ == "__main__":
    sys.exit(main())
