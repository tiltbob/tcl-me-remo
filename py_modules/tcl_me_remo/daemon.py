"""The background service: wires the transports, the service core and the event sources.

Three independent routes deliver lifecycle events to ``Service`` (which de-duplicates them):

1. logind delay inhibitors (``lifecycle.Logind``) - the normal route for suspend, resume and
   shutdown; they hold the machine until the TV has been told.
2. This process being stopped while the system is going down (SIGTERM from systemd).  The
   unit is ordered after bluetooth.service, so it is stopped *before* Bluetooth goes away.
   This covers shutdowns that bypass logind and systemd versions that do not tell a reboot
   from a power-off in the logind signal.
3. A clock-jump watchdog: CLOCK_BOOTTIME runs during suspend, CLOCK_MONOTONIC does not, so a
   growing difference proves a resume even if nothing announced the suspend.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__, config, installer, logsetup, paths, system
from .interfaces import Remote, WifiAssist
from .ipc import IpcServer, socket_answers
from .lifecycle import Logind
from .service import DEFAULT_DOWN_BUDGET, Service

log = logging.getLogger(__name__)

# A first start later than this after power-on is an install or a manual start, not a boot.
BOOT_WINDOW_SECONDS = 600.0
WATCHDOG_INTERVAL = 2.0
# Seconds for "TV off" when systemd stops the service because the machine is going down.
# The unit has TimeoutStopSec=20 (installer.py): this, the final disconnect and the rest of
# the clean-up fit into it with room to spare.
STOP_BUDGET = 8.0

RemoteFactory = Callable[[Any, config.Config], Awaitable[Remote]]
WifiFactory = Callable[[config.Config], WifiAssist | None]


def system_uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


async def _default_remote(bus: Any, cfg: config.Config) -> Remote:
    from .bluez import BleRemote

    remote = BleRemote(bus, cfg.bluetooth, state_path=paths.state_dir() / "bluetooth.json")
    await remote.start()
    return remote


def _default_wifi(cfg: config.Config) -> WifiAssist | None:
    try:
        from .atv import AtvAssist
    except ImportError as exc:
        log.info("Wi-Fi assist is not available (%s)", exc)
        return None
    return AtvAssist(paths.state_dir() / "atv")


class Daemon:
    def __init__(
        self,
        *,
        make_remote: RemoteFactory = _default_remote,
        make_wifi: WifiFactory = _default_wifi,
        uptime: Callable[[], float] = system_uptime,
        shutdown_kind: Callable[[], Awaitable[str | None]] = system.shutdown_kind,
        watchdog_interval: float = WATCHDOG_INTERVAL,
        run_command: system.Runner = system.run_command,
        orphan_check_delay: float = installer.ORPHAN_CHECK_DELAY,
    ):
        self._make_remote = make_remote
        self._make_wifi = make_wifi
        self._uptime = uptime
        self._shutdown_kind = shutdown_kind
        self._watchdog_interval = watchdog_interval
        self._run_command = run_command
        self._orphan_check_delay = orphan_check_delay
        self._stop = asyncio.Event()
        self._bus_lost = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self.service: Service | None = None
        self.logind: Logind | None = None
        self.ready = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> int:
        if socket_answers(paths.socket_path()):
            # Somebody ran "tcl-me-remo daemon" by hand while the service is up.  Stop before
            # anything is touched: a second instance would take the control socket away from
            # the first (and its file with it on the way out), and register a second remote.
            logsetup.setup()
            log.error("a tcl-me-remo service is already running (%s answers): not starting", paths.socket_path())
            return 1
        cfg_path = paths.config_path()
        load_failure: Exception | None = None
        try:
            cfg, problems, lasting = config.load_for_service(cfg_path)
        except Exception as exc:  # noqa: BLE001 - it promises not to raise; the service comes up regardless
            # Without this the service would exit, be restarted and exit again, with no
            # control socket through which the setting could ever be put right.
            load_failure = exc
            cfg = config.default_config()
            note = f"the settings could not be loaded ({type(exc).__name__}: {exc}); using defaults."
            try:
                note = f"{note} {config.set_aside(cfg_path)}"
            except Exception:  # noqa: BLE001
                pass
            problems = lasting = [note[:600]]
        ring = logsetup.setup(cfg.log_level)
        log.info("tcl-me-remo %s starting", __version__)
        if load_failure is not None:
            log.error("loading the settings failed", exc_info=load_failure)
        for problem in problems:
            log.warning("config: %s", problem)

        from . import dbus  # late import: keeps "tcl-me-remo --help" free of the D-Bus stack

        try:
            bus = await dbus.connect_system_bus()
        except Exception as exc:  # noqa: BLE001 - any failure here is fatal and must be explained
            log.error("cannot connect to the system D-Bus: %s", exc)
            return 1

        remote = server = wifi = None
        hooked: list[int] = []
        try:
            remote = await self._make_remote(bus, cfg)
            wifi = self._make_wifi(cfg)
            service = Service(
                cfg,
                remote=remote,
                wifi=wifi,
                save_config=lambda new_cfg: config.save(new_cfg, cfg_path),
                on_config_changed=lambda new_cfg: logsetup.set_level(new_cfg.log_level),
                recent_logs=ring.tail,
                config_problems=problems,
                lasting_config_problems=lasting,
                power_state_path=paths.state_dir() / "power.json",
            )
            self.service = service
            remote.on_paired = service.on_paired

            server = IpcServer(
                paths.socket_path(), service.rpc, group=os.environ.get("TCL_ME_REMO_SOCKET_GROUP", "wheel") or None
            )
            await server.start()

            self.logind = Logind(bus, on_sleep=self._on_sleep, on_shutdown=self._on_shutdown)
            await self.logind.start()

            loop = asyncio.get_running_loop()
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(signum, self._stop.set)
                    hooked.append(signum)
                except (NotImplementedError, RuntimeError, ValueError):  # not the main thread
                    pass

            self._spawn(self._watch_bus(bus))
            first_start = system.first_start_since_boot(paths.runtime_dir())
            if first_start and self._uptime() <= BOOT_WINDOW_SECONDS:
                self._spawn(service.handle_event("boot"))
            elif first_start:
                log.info("first start is long after power-on: not treating it as a boot")
            self._spawn(self._watchdog())
            self._spawn(self._orphan_check())

            system.sd_notify("READY=1")
            self.ready.set()
            await self._stop.wait()

            system.sd_notify("STOPPING=1")
            if self._bus_lost:
                # Nothing works without the system bus (no BlueZ, no logind).  Exit non-zero
                # so systemd starts a fresh process instead of leaving a deaf one running.
                return 1
            await self._going_down()
            return 0
        finally:
            for signum in hooked:
                asyncio.get_running_loop().remove_signal_handler(signum)
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.logind is not None:
                await self.logind.stop()
            if server is not None:
                await server.stop()
            if wifi is not None and hasattr(wifi, "close"):
                try:
                    # Drops a pairing connection that is still waiting for the code on the TV.
                    await wifi.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - shutting down regardless
                    log.debug("wifi.close() failed", exc_info=True)
            if remote is not None and hasattr(remote, "stop"):
                try:
                    await remote.stop()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - shutting down regardless
                    log.debug("remote.stop() failed", exc_info=True)
            bus.disconnect()
            log.info("stopped")

    # ------------------------------------------------------------------ event routes

    def _budget(self) -> float:
        return self.logind.down_budget if self.logind is not None else DEFAULT_DOWN_BUDGET

    async def _on_sleep(self, going_down: bool) -> None:
        assert self.service is not None
        if going_down:
            # Awaited: logind keeps the machine awake until this returns.
            await self.service.handle_event("suspend", budget=self._budget())
        else:
            # Not awaited: turning the TV on can take a minute, and the next suspend must
            # not wait for it.  But let the service take note of the resume before this
            # returns (one turn of the loop does it), so that a suspend reported right
            # behind it finds the bookkeeping in the order things happened.
            self._spawn(self.service.handle_event("resume"))
            await asyncio.sleep(0)

    async def _on_shutdown(self, going_down: bool, kind: str) -> None:
        assert self.service is not None
        if not going_down:
            self.service.cancel_shutdown()
            return
        if kind in ("shutdown", "reboot"):
            await self.service.handle_event(kind, budget=self._budget())
        # "unknown": this logind cannot tell a reboot from a power-off.  Do nothing yet; when
        # systemd stops us a moment later _going_down() can read it from the job queue.

    async def _going_down(self) -> None:
        """We are being stopped: if that is because the machine is going down, act on it."""
        assert self.service is not None
        try:
            kind = await self._shutdown_kind()
        except Exception:  # noqa: BLE001 - never let diagnostics block a shutdown
            log.debug("could not determine why we are stopping", exc_info=True)
            kind = None
        if kind is None:
            log.info("service stop requested (the system is not shutting down)")
            return
        log.info("the system is going down (%s)", kind)
        # Usually logind's route has dealt with this shutdown already and this is a
        # duplicate.  If it is the first word of it, or that attempt may be made again (see
        # Service._second_attempt_due), systemd's stop time-out is what limits us here.
        await self.service.handle_event(kind, budget=STOP_BUDGET)

    async def _watch_bus(self, bus: Any) -> None:
        try:
            await bus.wait_for_disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the library reports the reason by raising
            # The usual reason is an EOFError without text: the bus went away.
            reason = str(exc) or ("the bus closed it" if isinstance(exc, EOFError) else type(exc).__name__)
            log.error("lost the connection to the system D-Bus (%s): stopping, to be started afresh", reason)
        else:
            if self._stop.is_set():
                return
            log.error("the system D-Bus closed the connection: stopping, to be started afresh")
        self._bus_lost = True
        self._stop.set()

    async def _watchdog(self) -> None:
        assert self.service is not None
        while True:
            await asyncio.sleep(self._watchdog_interval)
            if self.service.slept_since_last_event():
                log.info("clock jump detected: the machine was suspended")
                self._spawn(self.service.handle_event("resume"))
            else:
                # A suspend or shutdown that was handled long ago, and here we still are.
                self.service.check_still_up()

    async def _orphan_check(self) -> None:
        """Some minutes after start-up: was the Decky plugin removed while nobody checked?

        The plugin's uninstall hook leaves a note and sets a timer for the check, and a
        reboot discards the timer.  If the note is (still) there, the installer is started
        outside this service to decide; see ``installer.uninstall_if_orphaned``.
        """
        await asyncio.sleep(self._orphan_check_delay)
        argv = installer.orphan_check_command(paths.state_dir())
        if argv is None:
            return
        log.info("the Decky plugin left a note that it was removed; checking whether it is gone for good")
        code, _ = await self._run_command(argv, 15.0)
        if code != 0:
            log.warning("could not start that check (systemd-run failed); it is made again after the next start")


def main() -> int:
    """Entry point for ``tcl-me-remo daemon``."""
    return asyncio.run(Daemon().run())
