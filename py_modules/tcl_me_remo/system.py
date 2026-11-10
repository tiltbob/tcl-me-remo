"""Small helpers for talking to the init system.  Standard library only."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# systemd targets whose pending start job tells a reboot from a power-off.
_REBOOT_TARGETS = ("reboot.target", "kexec.target", "soft-reboot.target")
_POWEROFF_TARGETS = ("poweroff.target", "halt.target")

Runner = Callable[[list[str], float], Awaitable[tuple[int, str]]]


def clean_env() -> dict[str, str]:
    """Environment for running system tools.

    Processes started from a PyInstaller-frozen host (Decky Loader) inherit an
    LD_LIBRARY_PATH that points at the host's bundled libraries, which breaks system
    binaries.  Drop it (restoring the original value if the host saved one).
    """
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    env.pop("LD_LIBRARY_PATH", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    return env


async def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run a command, returning ``(exit_code, stdout)``.  Never raises; -1 means it did not run."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=clean_env(),
        )
    except OSError as exc:
        log.debug("could not run %s: %s", argv[0], exc)
        return -1, ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.debug("%s timed out after %gs", argv[0], timeout)
        return -1, ""
    return proc.returncode if proc.returncode is not None else -1, stdout.decode("utf-8", "replace")


async def shutdown_kind(run: Runner = run_command) -> str | None:
    """Tell whether the machine is going down right now, and how.

    Returns ``"shutdown"`` (power-off/halt), ``"reboot"``, or ``None`` when the system is
    not shutting down (the service is merely being stopped or restarted).
    """
    _, state = await run(["systemctl", "is-system-running"], 1.5)
    if state.strip() != "stopping":
        return None
    code, jobs = await run(["systemctl", "list-jobs", "--no-legend", "--plain", "--no-pager"], 1.5)
    if code == 0:
        units = {parts[1] for parts in (line.split() for line in jobs.splitlines()) if len(parts) >= 3}
        if units.intersection(_REBOOT_TARGETS):
            return "reboot"
        if units.intersection(_POWEROFF_TARGETS):
            return "shutdown"
    # Going down for an unknown reason: treat it as a power-off, the safer guess for a TV.
    return "shutdown"


def first_start_since_boot(runtime_dir: Path) -> bool:
    """True exactly once per boot.

    ``runtime_dir`` lives on a tmpfs (/run), so a marker file there disappears on reboot but
    survives restarts of the service.
    """
    marker = Path(runtime_dir) / "booted"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError as exc:
        log.warning("cannot write %s (%s); treating this start as a boot", marker, exc)
        return True
    os.close(fd)
    return True


def sd_notify(message: str) -> bool:
    """Send a notification to systemd (``READY=1``, ``STATUS=...``).  No-op outside systemd."""
    target = os.environ.get("NOTIFY_SOCKET")
    if not target:
        return False
    address: str | bytes = target
    if target.startswith("@"):
        address = b"\0" + target[1:].encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), address)
        return True
    except OSError as exc:
        log.debug("sd_notify failed: %s", exc)
        return False
