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

from . import __version__, config, logsetup, paths, system
from .interfaces import Remote, WifiAssist
from .ipc import IpcServer
from .lifecycle import Logind
from .service import DEFAULT_DOWN_BUDGET, Service

log = logging.getLogger(__name__)

# A first start later than this after power-on is an install or a manual start, not a boot.
BOOT_WINDOW_SECONDS = 600.0
WATCHDOG_INTERVAL = 2.0

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
    ):
        self._make_remote = make_remote
        self._make_wifi = make_wifi
        self._uptime = uptime
        self._shutdown_kind = shutdown_kind
        self._watchdog_interval = watchdog_interval
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
        cfg_path = paths.config_path()
        cfg, problems = config.load(cfg_path)
        ring = logsetup.setup(cfg.log_level)
        log.info("tcl-me-remo %s starting", __version__)
        for problem in problems:
            log.warning("config: %s", problem)

        from . import dbus  # late import: keeps "tcl-me-remo --help" free of the D-Bus stack

        try:
            bus = await dbus.connect_system_bus()
        except Exception as exc:  # noqa: BLE001 - any failure here is fatal and must be explained
            log.error("cannot connect to the system D-Bus: %s", exc)
            return 1

        remote = server = None
        hooked: list[int] = []
        try:
            remote = await self._make_remote(bus, cfg)
            service = Service(
                cfg,
                remote=remote,
                wifi=self._make_wifi(cfg),
                save_config=lambda new_cfg: config.save(new_cfg, cfg_path),
                on_config_changed=lambda new_cfg: logsetup.set_level(new_cfg.log_level),
                recent_logs=ring.tail,
                config_problems=problems,
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
            self._spawn(self.service.handle_event("resume"))

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
        await self.service.handle_event(kind, budget=self._budget())

    async def _watch_bus(self, bus: Any) -> None:
        try:
            await bus.wait_for_disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the library reports the reason by raising
            log.error("lost the connection to the system D-Bus: %s", exc)
        else:
            if self._stop.is_set():
                return
            log.error("the system D-Bus closed the connection")
        self._bus_lost = True
        self._stop.set()

    async def _watchdog(self) -> None:
        assert self.service is not None
        while True:
            await asyncio.sleep(self._watchdog_interval)
            if self.service.slept_since_last_event():
                log.info("clock jump detected: the machine was suspended")
                self._spawn(self.service.handle_event("resume"))


def main() -> int:
    """Entry point for ``tcl-me-remo daemon``."""
    return asyncio.run(Daemon().run())
