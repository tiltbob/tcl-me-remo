"""The rig behind test_e2e.py: the real service process and the real CLI, on fake system services.

What is real: ``python -m tcl_me_remo daemon`` as a subprocess with its default factories
(BleRemote, the logind client, AtvAssist, the control socket, sd_notify, signal handling) and
``python -m tcl_me_remo <command>`` as a second subprocess.  What is fake: bluetoothd and
logind, served by the test's own event loop on the private bus, and the TV, a device of the
fake bluetoothd.

Because the fakes live on the test's event loop, nothing here may block it: subprocesses are
asyncio subprocesses and every wait is a poll with a generous timeout.  The service process
runs with its production timing constants; only what the settings file can shorten is
shortened (see ``BASE_SETTINGS``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO, Callable

from tcl_me_remo import config

PY_MODULES = Path(__file__).resolve().parent.parent / "py_modules"

#: How long any single condition may take to come true.  Far more than is ever needed on an
#: idle machine: a loaded CI runner must not turn a slow step into a failure.
WAIT = 30.0
#: Upper bound for one CLI call (the CLI's own longest client timeout is far above this, but
#: nothing these tests ask for takes long).
CLI_TIMEOUT = 45.0

# Value handles of the three input reports in the frozen GATT table (hid.py, bluez/gatt.py).
KEYBOARD, CONSUMER, SYSTEM = 0x010A, 0x010E, 0x0112


def consumer_tap(usage: int) -> list[tuple[int, bytes]]:
    """Press and release of a Consumer page usage, as the TV receives them."""
    return [(CONSUMER, usage.to_bytes(2, "little") + b"\x00\x00"), (CONSUMER, bytes(4))]


WAKE = [(SYSTEM, b"\x04"), (SYSTEM, b"\x00")]  # Generic Desktop "System Wake Up", bit 2
ENTER = [(KEYBOARD, b"\x28\x00"), (KEYBOARD, b"\x00\x00")]
SLEEP = consumer_tap(0x0032)
HOME = consumer_tap(0x0223)
DOWN = consumer_tap(0x0043)
OK = consumer_tap(0x0041)

#: Settings every test starts from.  Bluetooth timings are as short as the settings allow;
#: waits the service makes on behalf of the TV are long, so that a slow machine changes how
#: long a test takes and never how it ends.
BASE_SETTINGS: dict[str, Any] = {
    "log_level": "DEBUG",  # the service log is the only evidence a failed CI run leaves behind
    "display": {"require_external": "no", "wait_seconds": 0},  # the test machine's /sys is not ours to read
    "input": {"method": "none", "delay_after_wake": 0},
    "power": {"repeat_gap": 0.1},
    "bluetooth": {
        # The process must never open a raw HCI socket: CI has no Bluetooth hardware, and on a
        # developer's machine that kernel socket would be the real controller.
        "raw_notify": "off",
        "settle_ms": 20,
        "key_press_ms": 10,
        "key_gap_ms": 20,
        "resubscribe_wait": 25.0,
        "connect_timeout": 30.0,
        "off_connect_timeout": 30.0,
    },
}


async def eventually(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    """Poll until ``predicate()`` is truthy and return its value; fail the test if it never is."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        await asyncio.sleep(0.02)


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        out[key] = _merge(out.get(key, {}), value) if isinstance(value, dict) else value
    return out


class Reaper:
    """Last line of defence: no process started by a test outlives it.

    The rig stops its processes itself.  This runs afterwards from a plain (not async) fixture
    finaliser, so it still works when the event loop is in no state to run the rig's own
    clean-up, and kills whatever is left.
    """

    def __init__(self) -> None:
        self._started: list[tuple[asyncio.subprocess.Process, bool]] = []

    def track(self, proc: asyncio.subprocess.Process, *, group: bool = False) -> None:
        self._started.append((proc, group))

    def kill_leftovers(self) -> list[int]:
        """Kill what is still running and return its PIDs (empty when the rig did its job)."""
        killed = []
        for proc, group in self._started:
            if proc.returncode is not None:
                continue
            try:
                pid, _status = os.waitpid(proc.pid, os.WNOHANG)
            except ChildProcessError:
                continue  # it had exited and the event loop collected it
            if pid:
                continue  # it had exited; collected just now
            # Still running, and still our child: its PID cannot have been given to anybody
            # else.  Killed by that PID, or through the process group we created for it.
            (os.killpg if group else os.kill)(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(proc.pid, 0)
            killed.append(proc.pid)
        return killed


class Rig:
    def __init__(self, tmp_path: Path, reaper: Reaper, *, bus_address: str = "", bluez: Any = None, logind: Any = None):
        self.bluez = bluez
        self.logind = logind
        self.state_dir = tmp_path / "state"
        self.runtime_dir = tmp_path / "run"
        self.config_path = self.state_dir / "config.json"
        self.socket_path = self.runtime_dir / "control.sock"
        self.log_path = tmp_path / "daemon.log"
        self._cwd = tmp_path
        self._reaper = reaper
        self._env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("TCL_ME_REMO_") and name != "NOTIFY_SOCKET"
        }
        self._env.update(
            PYTHONPATH=str(PY_MODULES),
            TCL_ME_REMO_CONFIG=str(self.config_path),
            TCL_ME_REMO_STATE_DIR=str(self.state_dir),
            TCL_ME_REMO_RUNTIME_DIR=str(self.runtime_dir),
            TCL_ME_REMO_SOCKET_GROUP="",  # the test user is not root and "wheel" may not exist
        )
        # Never the machine's own system bus: without a private bus the service finds none at
        # all, instead of registering a second remote with the real bluetoothd and logind.
        self._env["DBUS_SYSTEM_BUS_ADDRESS"] = bus_address or "unix:path=/nonexistent/tcl-me-remo-e2e"
        self._notify_path = tmp_path / "notify"
        self._notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._notify.bind(str(self._notify_path))
        self._notify.setblocking(False)
        self._notified: list[str] = []
        self.daemon: asyncio.subprocess.Process | None = None
        self._exit_expected = False
        self._starts = 0
        self._clients: set[asyncio.subprocess.Process] = set()
        self.write_settings({})

    # ------------------------------------------------------------------ settings file

    def write_settings(self, patch: dict[str, Any]) -> None:
        """Write the settings file: the defaults, ``BASE_SETTINGS`` and ``patch``, validated."""
        cfg = config.apply_patch(config.default_config(), _merge(BASE_SETTINGS, patch))
        config.save(cfg, self.config_path)

    def settings(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ the service process

    @property
    def notifications(self) -> list[str]:
        """Everything the service has told "systemd" (sd_notify) so far, in order."""
        while True:
            try:
                self._notified.append(self._notify.recv(4096).decode())
            except BlockingIOError:
                return self._notified

    def log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def log_problems(self) -> list[str]:
        """Lines of the service log that no test expects: errors and tracebacks."""
        return [line for line in self.log().splitlines() if line.startswith(("ERROR", "CRITICAL", "Traceback"))]

    def _open_log(self, heading: str) -> BinaryIO:
        """The file the service writes to; one file for all its starts in a test."""
        log = open(self.log_path, "ab")  # noqa: SIM115 - handed to the child process by the caller
        log.write(f"{heading}\n".encode())
        log.flush()
        return log

    @property
    def running(self) -> bool:
        return self.daemon is not None and self.daemon.returncode is None

    async def start(self, *, first_start_since_boot: bool = False) -> None:
        """Start the service and wait until it reports READY=1.

        Whether the service treats a first start as a boot depends on the uptime of the
        machine the tests run on.  Unless a test asks for a first start, the marker that says
        "already started in this boot" is put in place, as after a restart of the service.
        """
        assert not self.running, "the service is already running"
        marker = self.runtime_dir / "booted"
        if first_start_since_boot:
            marker.unlink(missing_ok=True)
        else:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            marker.touch()
        self._starts += 1
        self.notifications.clear()  # a new process, a new record
        with self._open_log(f"---- service start #{self._starts} ----") as log:
            self.daemon = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tcl_me_remo", "daemon",
                env={**self._env, "NOTIFY_SOCKET": str(self._notify_path)},
                cwd=self._cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # its own process group, so that clean-up reaches any child
            )  # fmt: skip
        self._reaper.track(self.daemon, group=True)
        self._exit_expected = False
        await eventually(self._ready_or_dead, "READY=1 from the service")
        if not self.running:
            self._exit_expected = True  # reported here, not a second time by close()
            raise AssertionError(f"the service exited with code {self.daemon.returncode} while starting")

    def _ready_or_dead(self) -> bool:
        return "READY=1" in self.notifications or not self.running

    async def stop(self, sig: int = signal.SIGTERM) -> int:
        """Send the service a signal, wait for it to exit and return its exit code."""
        assert self.daemon is not None and self.running, "the service is not running"
        self._exit_expected = True
        self.daemon.send_signal(sig)
        try:
            return await asyncio.wait_for(self.daemon.wait(), WAIT)
        except TimeoutError:
            raise AssertionError(f"the service did not exit within {WAIT:g}s of signal {sig}") from None

    async def close(self) -> None:
        """Stop everything this rig started.  Fails if the service died by itself."""
        clients, self._clients = list(self._clients), set()
        for proc in clients:
            if proc.returncode is None:
                proc.kill()
        for proc in clients:
            await proc.wait()
        daemon = self.daemon
        died = daemon is not None and daemon.returncode is not None and not self._exit_expected
        if daemon is not None and daemon.returncode is None:
            daemon.terminate()
            try:
                await asyncio.wait_for(daemon.wait(), 15.0)
            except TimeoutError:
                os.killpg(daemon.pid, signal.SIGKILL)
                await daemon.wait()
        self._notify.close()
        assert not died, f"the service exited by itself with code {daemon.returncode}"

    @contextlib.asynccontextmanager
    async def cleaned_up(self) -> AsyncIterator[Rig]:
        """``async with rig.cleaned_up():`` - what every test owes at its end, pass or fail.

        The processes are stopped and the service log is printed.  pytest shows captured
        output only for a failing test, where it is what the failure is diagnosed from.
        Problems the service only wrote to its log fail the test as well.
        """
        try:
            yield self
        finally:
            try:
                await self.close()
            finally:
                print(f"---- service log ({self.log_path}) ----\n{self.log()}")
                if self.bluez is not None:
                    print(f"---- the fake bluetoothd saw: {self.bluez.event_names()}")
        assert self.log_problems() == [], "the service logged errors"

    # ------------------------------------------------------------------ the CLI

    async def cli(self, *args: str, timeout: float = CLI_TIMEOUT) -> tuple[int, str, str]:
        """Run ``tcl-me-remo <args>`` as a process of its own: ``(exit code, stdout, stderr)``."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tcl_me_remo", *args,
            env=self._env,
            cwd=self._cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        self._reaper.track(proc)
        self._clients.add(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            raise AssertionError(f"'tcl-me-remo {' '.join(args)}' did not finish within {timeout:g}s") from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            self._clients.discard(proc)
        assert proc.returncode is not None
        return proc.returncode, out.decode(), err.decode()

    async def ok(self, *args: str) -> str:
        """Run a CLI command that must succeed; returns what it printed."""
        code, out, err = await self.cli(*args)
        assert code == 0, f"'tcl-me-remo {' '.join(args)}' exited with {code}: {err or out}"
        assert err == "", err
        return out

    async def status(self) -> dict[str, Any]:
        return json.loads(await self.ok("status", "--json"))

    async def status_when(self, predicate: Callable[[dict[str, Any]], Any], what: str) -> dict[str, Any]:
        """Ask for the status until ``predicate(status)`` holds; returns that status."""
        deadline = asyncio.get_running_loop().time() + WAIT
        while True:
            status = await self.status()
            if predicate(status):
                return status
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out after {WAIT:g}s waiting for {what}; last status: {status}")
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ the TV

    async def pair(self, tv: Any, *, window: int = 60) -> tuple[str, dict[str, Any]]:
        """Pair through the CLI while the TV does what an Android TV does.

        Returns what ``tcl-me-remo pair`` printed, and what bluetoothd looked like while
        pairing mode was open (see :meth:`pairing_mode`).
        """
        cli = asyncio.ensure_future(self.cli("pair", "--timeout", str(window), timeout=window + 20.0))
        try:
            while_open = await eventually(lambda: self.pairing_mode() or cli.done(), "pairing mode to open")
            assert not cli.done(), f"'tcl-me-remo pair' gave up at once: {cli.result()}"
            paired = await tv.pair_like_android()  # outside the assert: it must run under python -O too
            if not paired:
                raise AssertionError("the fake TV could not pair")
            code, out, err = await cli
        finally:
            cli.cancel()
            await asyncio.gather(cli, return_exceptions=True)
        assert code == 0, f"'tcl-me-remo pair' exited with {code}: {err or out}"
        return out, while_open

    def pairing_mode(self) -> dict[str, Any] | None:
        """``{"advert": ..., "adapter": ...}`` as they are right now if pairing mode is open, else None.

        The discoverable advertisement is the last thing the service sets up for pairing, so
        once it is on air the agent and the adapter's ``Pairable`` are in place as well.
        """
        advert = self.bluez.advert
        if advert is None or not advert.get("Discoverable") or self.bluez.default_agent is None:
            return None
        return {"advert": dict(advert), "adapter": dict(self.bluez.adapter.props)}
