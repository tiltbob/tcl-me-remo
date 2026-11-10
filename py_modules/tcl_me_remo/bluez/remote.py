"""The emulated Bluetooth LE remote: BlueZ registration, pairing, readiness and key delivery.

``BleRemote`` implements :class:`tcl_me_remo.interfaces.Remote` on top of bluetoothd's D-Bus
API.  The parts live in sibling modules (GATT table, advertisement, agent, object mirror, raw
delivery); this module is the state machine that ties them together.

The facts that shape it (BlueZ 5.79+ and Android sources, see the research notes):

* **One registration for life.**  The GATT application is registered once per bluetoothd /
  adapter lifetime and never touched for configuration changes, because registering again
  throws away every subscription and moves nothing forward.
* **Calling the TV.**  A bonded Android TV connects by itself as soon as it sees our
  connectable advertisement.  We advertise while somebody waits for the TV (ensure_ready),
  in pairing mode, and - with ``reconnect_on_drop`` - in the background whenever the bonded
  TV is not connected, so that the link is already up when a key must go out in the few
  seconds before suspend.  Never otherwise: with that option off a TV in standby is not
  poked unless there is something to send, and a link we closed on purpose (disconnect(),
  idle time-out) stays closed until the next ensure_ready.
* **Readiness cannot be observed.**  bluetoothd tells us the link is up (``Device1.Connected``)
  but neither that it is encrypted nor that the TV's HID host finished setting up, so a
  settle delay stands in for both.
* **Subscriptions live in bluetoothd's memory.**  They survive disconnects and suspend, so a
  reconnect brings no new ``StartNotify`` and sending must not wait for one.  They are lost
  when bluetoothd restarts (every boot).  Android 14+ then subscribes again after the Service
  Changed indication; Android 11-13 never does, bluetoothd silently drops our notifications,
  and the only way to reach the TV short of pairing again is direct delivery (rawhci.py).
* **The first link after a registration is special.**  The Service Changed indication that
  bluetoothd sends on it makes Android 11-13 forget, for that connection, which
  notifications its HID host wanted.  Direct delivery therefore starts on the second link:
  the first one is dropped once on purpose ("bounce").
* **Device1.Disconnect() takes two seconds** (bluetoothd's ``DISCONNECT_TIMER``) and only
  answers once the link is down.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Coroutine

from .. import dbus, hid, keys, tvdata
from ..config import BluetoothConfig
from ..dbus import DBusError, MessageBus, Variant
from ..interfaces import BleState, PairedCallback, TransportError
from . import rawhci
from .adapter import ADAPTER, AGENT_MANAGER, BLUEZ, BLUEZ_ROOT, DEVICE, GATT_MANAGER, BluezWatcher
from .advert import Advertiser, AdvertSettings, Beacon
from .agent import AGENT_CAPABILITY, AGENT_PATH, PairingAgent
from .gatt import APP_ROOT, SERVICE_HANDLE, GattCharacteristic, HidApplication
from .rawhci import RawError, RawLink
from .store import StateStore

log = logging.getLogger(__name__)

# Timings.  Module-level so that tests can shorten them.
CALL_TIMEOUT = 5.0
REGISTER_TIMEOUT = 10.0
#: First and largest pause between attempts to register the GATT application.
REGISTER_RETRY = (1.0, 30.0)
#: After start-up or a bluetoothd restart the adapter may take a moment to appear; within
#: this time a missing adapter is waited for instead of reported.
ADAPTER_GRACE = 8.0
POWER_ON_WAIT = 5.0
#: Settle time once a StartNotify has been seen on the current link: by then the link is
#: encrypted and the TV is merely finishing its remaining CCC writes.
POST_SUBSCRIBE_SETTLE = 0.3
#: How long to wait for a re-subscription from a TV that is known not to re-subscribe.
KNOWN_SILENT_WAIT = 0.5
#: bluetoothd drops a link two seconds after Device1.Disconnect(); allow some slack.
DISCONNECT_WAIT = 3.5
#: A requested disconnect that has not happened after this long is considered failed.
CLOSING_GIVE_UP = 6.0
#: Pause before advertising again after bluetoothd released or refused the advertisement.
ADVERT_RETRY = 1.0
PAIRING_TICK = 1.0
REMOVE_DEVICE_TIMEOUT = 6.0

ERR_ALREADY_EXISTS = "org.bluez.Error.AlreadyExists"
ERR_DOES_NOT_EXIST = "org.bluez.Error.DoesNotExist"
ERR_NOT_READY = "org.bluez.Error.NotReady"
#: Replies that mean "there is nothing to disconnect / remove".
_GONE_ERRORS = (
    ERR_DOES_NOT_EXIST,
    "org.bluez.Error.NotConnected",
    "org.freedesktop.DBus.Error.UnknownObject",
    "org.freedesktop.DBus.Error.UnknownMethod",
)

NOT_SUBSCRIBED = (
    "the TV is connected but will not receive keys until it is paired again: it did not renew its "
    "subscription after Bluetooth restarted on this machine. Pair the TV again, or use the Wi-Fi assist"
)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, DBusError):
        name = str(exc.type).rsplit(".", 1)[-1]
        return f"{name}: {exc.text}" if exc.text else name
    if isinstance(exc, TimeoutError):
        return "BlueZ did not answer in time"
    return str(exc) or type(exc).__name__


class _Wakeup:
    """Lets any number of coroutines sleep until "something changed" or a timeout."""

    def __init__(self) -> None:
        self._waiters: set[asyncio.Future[None]] = set()

    def fire(self) -> None:
        waiters, self._waiters = self._waiters, set()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def wait(self, timeout: float) -> None:
        if timeout <= 0:
            await asyncio.sleep(0)
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await asyncio.wait([waiter], timeout=timeout)
        finally:
            self._waiters.discard(waiter)
            waiter.cancel()


@dataclass
class _PairingWindow:
    deadline: float
    adapter_path: str
    known: set[str]                       # devices that were already paired when the window opened
    prev_pairable: bool | None = None     # Adapter1.Pairable before we changed it (None: untouched)
    candidate: str = ""                   # the one device the agent said yes to
    touched: set[str] = field(default_factory=set)  # devices that read our HID attributes
    notified: bool = False                # a StartNotify arrived inside the window
    winner: str = ""
    aborted: str = ""
    closed: bool = False                  # no longer accepting anybody; clean-up may still be running
    cleanup: asyncio.Future[None] | None = None


@dataclass
class _Problem:
    code: str
    message: str
    transient: bool = False  # worth waiting for (registration under way, adapter still appearing)


class BleRemote:
    """See the module docstring.  All methods must be called from the event loop thread."""

    def __init__(
        self,
        bus: MessageBus,
        cfg: BluetoothConfig,
        *,
        on_paired: PairedCallback | None = None,
        state_path: Path | None = None,
        raw_link: RawLink | None = None,
    ):
        self._bus = bus
        self._cfg = dataclasses.replace(cfg)
        self.on_paired: PairedCallback | None = on_paired
        self._store = StateStore(state_path)
        self._raw = raw_link if raw_link is not None else RawLink()

        self._app: HidApplication | None = None
        self._watcher = BluezWatcher(bus, on_change=self._evaluate, on_owner_change=self._owner_changed)
        self._advertiser = Advertiser(bus, on_released=self._advert_released)
        self._beacon = Beacon(bus)
        self._agent = PairingAgent(self._agent_decision, on_release=self._agent_released)
        self._wakeup = _Wakeup()

        self._started = False
        self._stopped = False
        self._changed_at = time.monotonic()   # start-up / last time bluetoothd or the adapter went away
        self._adapter_path = ""
        self._powered = False
        self._registered = False
        self._pin_handle = True
        self._tv_path = ""
        self._connected = False
        self._connected_path = ""
        self._connected_at = 0.0
        self._links = 0                       # TV links seen since the current registration
        self._subscribed_at: float | None = None
        self._paired_this_registration = False
        self._closing_at: float | None = None  # we asked bluetoothd to drop the link
        self._parked = False                  # we closed the link on purpose: no background calling
        self._ready = False
        self._delivery = ""
        self._error = ""
        self._last_activity = 0.0
        self._advert_not_before = 0.0
        self._agent_registered = False

        self._pairing: _PairingWindow | None = None
        self._pairing_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Future[Any]] = set()

        self._op_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._abort = 0                       # bumped to make a running ensure_ready give up
        self._ensure_active = 0
        self._runs = 0
        self._last_outcome: bool | TransportError | None = None
        self.bounces = 0                      # diagnostics: how often the link was dropped on purpose

    # ================================================================== public: life cycle

    async def start(self) -> None:
        """Export our objects, mirror BlueZ and register the HID service if an adapter exists.

        Returns once the first registration attempt has finished (or right away when there is
        nothing to register with yet); a missing bluetoothd or adapter is not an error, the
        registration happens when they appear.
        """
        if self._started:
            return
        self._started, self._stopped = True, False
        # gatt_security is read here and nowhere else: flags are part of the registered table.
        self._app = HidApplication(self._cfg.gatt_security)
        self._app.set_callbacks(self._on_touch, self._on_notify)
        self._app.export(self._bus)
        self._advertiser.export()
        self._beacon.export()
        self._bus.export(AGENT_PATH, self._agent)
        self._changed_at = time.monotonic()
        await self._watcher.start()
        self._evaluate()

        deadline = time.monotonic() + REGISTER_TIMEOUT
        while self._adapter_path and not self._registered and not self._error and self._reconciling():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await self._wakeup.wait(remaining)

    async def stop(self) -> None:
        """Best-effort clean-up.  Never raises."""
        if not self._started:
            return
        self._stopped = True
        self._abort += 1
        self._wakeup.fire()
        window = self._pairing
        tasks = [self._pairing_task, self._reconnect_task, self._idle_task, self._reconcile_task]
        self._pairing_task = self._reconnect_task = self._idle_task = self._reconcile_task = None
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        if self._tasks:
            # Short clean-ups that outlived their cancelled owners: let them finish.
            await asyncio.wait(list(self._tasks), timeout=CALL_TIMEOUT)

        async def quietly(what: str, step: Awaitable[Any]) -> None:
            try:
                await step
            except Exception as exc:  # noqa: BLE001 - stopping regardless
                log.debug("stop: %s failed: %s", what, _describe(exc))

        if window is not None:
            await quietly("closing the pairing window", self._close_pairing(window))
        await quietly("stopping the advertisement", self._advertiser.stop())
        await quietly("stopping the wake beacon", self._beacon.stop())
        if self._agent_registered:
            await quietly("unregistering the agent", self._unregister_agent())
        if self._registered and self._adapter_path:
            await quietly(
                "unregistering the HID service",
                dbus.call(
                    self._bus, BLUEZ, self._adapter_path, GATT_MANAGER, "UnregisterApplication", "o", [APP_ROOT],
                    timeout=2.0,
                ),
            )  # fmt: skip
        await quietly("stopping the BlueZ mirror", self._watcher.stop())
        self._raw.detach()
        for what, undo in (
            ("agent", lambda: self._bus.unexport(AGENT_PATH, self._agent)),
            ("advertisement", self._advertiser.unexport),
            ("wake beacon", self._beacon.unexport),
            ("HID service", lambda: self._app.unexport(self._bus) if self._app is not None else None),
        ):
            try:
                undo()
            except Exception as exc:  # noqa: BLE001
                log.debug("stop: could not unexport the %s: %s", what, exc)
        self._registered = self._ready = self._connected = False
        self._delivery = ""
        self._started = False
        log.info("Bluetooth remote stopped")

    # ================================================================== public: Remote protocol

    @property
    def state(self) -> BleState:
        adapter = self._adapter_path
        props = self._watcher.interface(adapter, ADAPTER) if adapter else None
        window = self._pairing
        pairing = window is not None and not window.aborted
        return BleState(
            available=bool(adapter),
            adapter=adapter.rsplit("/", 1)[-1] if adapter else "",
            adapter_address=str((props or {}).get("Address", "")),
            powered=bool(adapter) and self._powered,
            registered=self._registered,
            advertising=self._advertiser.active,
            pairing=pairing,
            pairing_seconds_left=max(0, math.ceil(window.deadline - time.monotonic())) if pairing and window else 0,
            paired=bool(self._tv_path),
            tv_address=self._cfg.tv_address,
            tv_name=self._cfg.tv_name,
            connected=self._connected,
            ready=self._ready,
            subscribed=self._app.subscribed if self._app is not None else False,
            delivery=self._delivery if self._ready else "",
            error=self._error,
        )

    def apply_config(self, cfg: BluetoothConfig) -> None:
        """Adopt new settings.  Never touches the GATT registration."""
        old, self._cfg = self._cfg, dataclasses.replace(cfg)
        if old.gatt_security != cfg.gatt_security and self._started:
            log.info("gatt_security changed: takes effect when the service restarts")
        if old.adapter != cfg.adapter and self._started:
            log.info("adapter changed: takes effect when the service restarts")
        if self._started:
            self._evaluate()
            if self._ready and old.idle_disconnect_seconds != cfg.idle_disconnect_seconds:
                self._restart_idle_watch()
            if self._ready and self._delivery == "raw" and cfg.raw_notify == "off":
                # Direct delivery was just forbidden: keys can no longer reach the TV.
                self._ready, self._delivery = False, ""
                self._raw.detach()

    async def ensure_ready(self, timeout: float, *, wake: bool = False) -> bool:
        """See :meth:`tcl_me_remo.interfaces.Remote.ensure_ready`.

        ``wake=True`` says the caller is trying to turn the TV *on*: if a wake beacon is
        configured it is broadcast while we wait for the TV to connect.  It must not be set
        for anything else - waking a suspended TV in order to tell it to sleep would leave
        it on.
        """
        if self._ready:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        seen = self._runs
        if self._op_lock.locked():
            try:
                await asyncio.wait_for(self._op_lock.acquire(), max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                return False
        else:
            await self._op_lock.acquire()
        try:
            if self._runs != seen:
                # Somebody else just ran the whole sequence: its answer is ours, unless it
                # merely ran out of *its* time and we still have some.
                outcome = self._last_outcome
                if isinstance(outcome, TransportError):
                    raise TransportError(outcome.code, outcome.message)
                if outcome is True and self._ready:
                    return True
            try:
                result: bool | TransportError = await self._run_ensure(deadline, wake)
            except TransportError as exc:
                result = exc
            except (DBusError, TimeoutError, OSError) as exc:
                result = self._fail("bluetooth_error", f"Bluetooth problem: {_describe(exc)}")
            self._runs += 1
            self._last_outcome = result
            if isinstance(result, TransportError):
                raise result
            return result
        finally:
            self._op_lock.release()

    async def send_key(self, spec: str, hold: float) -> None:
        key = keys.resolve(spec)
        report_id, payload = hid.press_report(key)
        async with self._send_lock:
            if not self._ready:
                raise TransportError("not_connected", "the TV is not connected over Bluetooth")
            self._deliver(report_id, payload)
            released = False
            try:
                await asyncio.sleep(max(0.0, hold))
                if not self._ready:
                    # The TV tears its input device down with the link, which releases the key.
                    released = True
                    raise TransportError("not_connected", f"the Bluetooth link dropped while {key.spec} was held")
                self._deliver(report_id, hid.release_report(report_id))
                released = True
            finally:
                if not released:
                    # Cancelled or failed mid-press: a key left down would auto-repeat on the TV.
                    if self._ready:
                        try:
                            self._deliver(report_id, hid.release_report(report_id))
                            released = True
                        except TransportError:
                            pass
                    if not released and self._connected and self._closing_at is None:
                        # Closing the link makes the TV destroy its input device, keys and all.
                        log.warning("could not release %s; dropping the Bluetooth link to release it", key.spec)
                        self._spawn(self._drop_link_quietly())
            self._last_activity = time.monotonic()

    async def start_pairing(self, timeout: float) -> None:
        self._abort += 1  # a running ensure_ready gives way
        # No background calling while the old bond is removed and the window is set up.
        was_parked, self._parked = self._parked, True
        self._wakeup.fire()
        await self._cancel_task("_reconnect_task")
        async with self._op_lock:
            await self.cancel_pairing()
            try:
                await self._open_pairing(timeout)
            except TransportError:
                self._parked = was_parked
                self._call_in_background()
                raise
            except (DBusError, TimeoutError, OSError) as exc:
                self._parked = was_parked
                self._call_in_background()
                raise self._fail("bluetooth_error", f"could not open pairing mode: {_describe(exc)}") from None

    async def cancel_pairing(self) -> None:
        window, task = self._pairing, self._pairing_task
        self._pairing_task = None
        if window is not None:
            await self._close_pairing(window)
            log.info("pairing mode closed")
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def forget(self) -> None:
        self._abort += 1
        self._parked = True
        self._wakeup.fire()
        await self.cancel_pairing()
        await self._cancel_task("_reconnect_task")
        async with self._op_lock:
            address = self._cfg.tv_address
            await self._advertiser.stop()
            if address:
                self._closing_at = time.monotonic() if self._connected else None
                try:
                    removed = await self._remove_device(address)
                except (DBusError, TimeoutError, OSError) as exc:
                    self._closing_at = None
                    raise self._fail(
                        "bluetooth_error", f"could not remove the TV from BlueZ: {_describe(exc)}"
                    ) from None
                if not removed:
                    # BlueZ cannot be asked right now; remember to remove the bond later, or it
                    # would block the next pairing with the same TV.
                    self._store.add_stale(address)
                log.info("forgot TV %s", address)
            self._cfg = dataclasses.replace(self._cfg, tv_address="", tv_name="")
            self._store.clear_tv()
            self._evaluate()

    async def disconnect(self) -> None:
        self._abort += 1
        self._parked = True
        self._wakeup.fire()
        await self._cancel_task("_reconnect_task")
        await self._advertiser.stop()
        if not self._connected:
            return
        try:
            await self._request_disconnect(time.monotonic() + DISCONNECT_WAIT)
        except (DBusError, TimeoutError, OSError) as exc:
            raise self._fail("bluetooth_error", f"could not disconnect the TV: {_describe(exc)}") from None

    # ================================================================== BlueZ state tracking

    def _owner_changed(self, owner: str | None) -> None:
        self._changed_at = time.monotonic()
        if owner is None:
            self._agent_registered = False
        # The mirror is empty until the next snapshot: everything registered is gone.
        self._evaluate()

    def _select_adapter(self) -> str:
        usable = self._watcher.adapters()
        if self._adapter_path in usable:
            return self._adapter_path  # never hop adapters while the current one is alive
        wanted = self._cfg.adapter
        if wanted != "auto":
            path = f"{BLUEZ_ROOT}/{wanted}"
            return path if path in usable else ""
        tv = self._cfg.tv_address.upper()
        if tv:
            # The bond belongs to one adapter's address; prefer the adapter that holds it.
            for path in usable:
                for props in self._watcher.devices(path).values():
                    if str(props.get("Address", "")).upper() == tv and (props.get("Paired") or props.get("Bonded")):
                        return path
        return usable[0] if usable else ""

    def _find_tv(self) -> str:
        address = self._cfg.tv_address.upper()
        if not address or not self._adapter_path:
            return ""
        # Matched by the Address *property*: the object path of a TV that first connected from
        # a private address keeps that address until bluetoothd restarts.
        for path, props in sorted(self._watcher.devices(self._adapter_path).items()):
            if str(props.get("Address", "")).upper() == address and (props.get("Paired") or props.get("Bonded")):
                return path
        return ""

    def _evaluate(self) -> None:
        """Bring our view in line with the mirror.  Level-triggered and idempotent."""
        if not self._started or self._app is None:
            return
        adapter = self._select_adapter() if self._watcher.synced else ""
        if adapter != self._adapter_path:
            if self._adapter_path:
                self._adapter_lost()
            self._adapter_path = adapter
            if adapter:
                log.info("using Bluetooth adapter %s", adapter.rsplit("/", 1)[-1])
        if not adapter:
            self._wakeup.fire()
            return

        props = self._watcher.interface(adapter, ADAPTER) or {}
        powered = bool(props.get("Powered"))
        if powered != self._powered:
            self._powered = powered
            log.info("Bluetooth adapter is %s", "on" if powered else "off")
        if not self._registered or (powered and self._store.stale):
            self._kick_reconcile()

        self._tv_path = self._find_tv()
        connected_path = ""
        if self._tv_path and (self._watcher.interface(self._tv_path, DEVICE) or {}).get("Connected"):
            connected_path = self._tv_path
        if self._connected and connected_path != self._connected_path:
            self._link_down()
        if connected_path and not self._connected:
            self._link_up(connected_path)

        if self._pairing is not None:
            self._pairing_progress()
        self._call_in_background()
        self._wakeup.fire()

    def _adapter_lost(self) -> None:
        log.warning("Bluetooth adapter %s is gone", self._adapter_path.rsplit("/", 1)[-1])
        self._changed_at = time.monotonic()
        if self._connected:
            self._link_down(adapter_gone=True)
        self._registered = False
        self._powered = False
        self._tv_path = ""
        self._links = 0
        self._subscribed_at = None
        self._advertiser.forget()
        self._beacon.forget()
        self._raw.reset()
        if self._app is not None:
            # bluetoothd destroyed the adapter's GATT database and with it every subscription.
            self._app.reset_for_registration(SERVICE_HANDLE if self._pin_handle else 0)
        if self._pairing is not None:
            self._pairing.aborted = "Bluetooth went away"
        self._error = "the Bluetooth adapter is gone"

    def _link_up(self, path: str) -> None:
        self._connected, self._connected_path = True, path
        self._connected_at = time.monotonic()
        self._closing_at = None
        self._links += 1
        log.info("TV connected (%s), link %d since the HID service was registered", path, self._links)

    def _link_down(self, *, adapter_gone: bool = False) -> None:
        expected = self._closing_at is not None
        self._connected, self._connected_path = False, ""
        self._closing_at = None
        self._ready, self._delivery = False, ""
        log.info("TV disconnected%s", "" if expected or adapter_gone else " (not on our request)")
        if adapter_gone:
            self._raw.reset()
        else:
            try:
                self._raw.link_closed(rawhci.dev_id_from_adapter(self._adapter_path))
            except RawError:
                self._raw.reset()
        task, self._idle_task = self._idle_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    # ================================================================== GATT registration

    def _reconciling(self) -> bool:
        return self._reconcile_task is not None and not self._reconcile_task.done()

    def _kick_reconcile(self) -> None:
        if self._stopped or self._reconciling():
            return
        self._reconcile_task = asyncio.ensure_future(self._reconcile())

    async def _reconcile(self) -> None:
        """Register the HID service (retrying for as long as it takes) and tidy stale bonds."""
        delay = REGISTER_RETRY[0]
        while not self._stopped:
            adapter = self._adapter_path
            if not adapter:
                return
            try:
                if not self._registered:
                    await self._register_app(adapter)
                if self._registered and self._powered:
                    await self._remove_stale()
                return
            except (DBusError, TimeoutError, OSError) as exc:
                if adapter != self._adapter_path:
                    continue
                self._error = f"could not register the HID service with BlueZ: {_describe(exc)}"
                log.warning("%s; retrying in %gs", self._error, delay)
                self._wakeup.fire()
            await asyncio.sleep(delay)
            delay = min(delay * 2, REGISTER_RETRY[1])

    async def _register_call(self, adapter: str) -> None:
        try:
            await dbus.call(
                self._bus, BLUEZ, adapter, GATT_MANAGER, "RegisterApplication", "oa{sv}", [APP_ROOT, {}],
                timeout=REGISTER_TIMEOUT,
            )  # fmt: skip
        except DBusError as exc:
            if exc.type != ERR_ALREADY_EXISTS:
                raise
            # An earlier attempt whose reply we never saw.  The registration stands.
            log.info("the HID service was already registered")

    async def _register_app(self, adapter: str) -> None:
        assert self._app is not None
        owner = self._watcher.owner
        self._app.reset_for_registration(SERVICE_HANDLE if self._pin_handle else 0)
        self._subscribed_at = None
        try:
            await self._register_call(adapter)
        except DBusError as exc:
            if not self._pin_handle:
                raise
            # Most likely the pinned handle range is taken by another application.
            log.warning(
                "BlueZ refused the HID service at its fixed handle 0x%04X (%s); retrying with automatic handles",
                SERVICE_HANDLE,
                _describe(exc),
            )
            self._app.reset_for_registration(0)
            await self._register_call(adapter)  # if this fails too, the pin was not the problem
            self._pin_handle = False
        if adapter != self._adapter_path or owner != self._watcher.owner:
            return  # registered with a bluetoothd / adapter that has gone meanwhile
        self._registered = True
        self._links = 1 if self._connected else 0
        self._paired_this_registration = False
        if self._error.startswith("could not register") or self._error == "the Bluetooth adapter is gone":
            self._error = ""
        self._log_handles()
        self._call_in_background()
        self._wakeup.fire()

    def _log_handles(self) -> None:
        assert self._app is not None
        frozen = self._app.expected_handles(SERVICE_HANDLE)
        found, sources = {}, set()
        for report_id, char in self._app.reports.items():
            handle, source = self._app.report_value_handle(report_id)
            found[char.path] = handle
            sources.add(source)
        text = ", ".join(f"0x{handle:04X}" for handle in found.values())
        origin = "reported by BlueZ" if sources == {"bluez"} else "computed, BlueZ did not report them"
        log.info("HID service registered on %s; report value handles %s (%s)", self._adapter_path, text, origin)
        if any(frozen[path] != handle for path, handle in found.items()):
            log.warning(
                "the HID attribute handles differ from the fixed layout (%s expected); a TV that paired with the "
                "fixed layout will ignore keys until it is paired again, and automatic handles move at every restart",
                ", ".join(f"0x{frozen[path]:04X}" for path in found),
            )

    # ================================================================== advertising

    def _advert_released(self) -> None:
        self._advert_not_before = time.monotonic() + ADVERT_RETRY
        self._wakeup.fire()

    async def _advertise(self, *, discoverable: bool, fast: bool) -> None:
        """Make sure our advertisement is on air.  Raises DBusError / TimeoutError."""
        pause = self._advert_not_before - time.monotonic()
        if pause > 0 and not self._advertiser.active:
            await asyncio.sleep(pause)
        if not self._adapter_path or not self._registered:
            return  # never advertise without the HID service: the TV would find nothing to use
        settings = AdvertSettings(name=self._cfg.device_name, discoverable=discoverable, fast=fast)
        try:
            await self._advertiser.start(self._adapter_path, settings)
        except (DBusError, TimeoutError, OSError):
            self._advert_not_before = time.monotonic() + ADVERT_RETRY
            raise

    def _should_call(self) -> bool:
        """Whether the bonded TV should be called although nobody is waiting for it."""
        return bool(
            self._cfg.reconnect_on_drop
            and not self._parked
            and not self._stopped
            and self._registered
            and self._powered
            and self._tv_path
            and not self._connected
            and self._pairing is None
        )

    def _call_in_background(self) -> None:
        if self._ensure_active or not self._should_call():
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            log.info("calling the TV in the background until it connects")
            self._reconnect_task = asyncio.ensure_future(self._reconnect_in_background())

    async def _reconnect_in_background(self) -> None:
        try:
            while self._should_call():
                try:
                    await self._advertise(discoverable=False, fast=False)
                except (DBusError, TimeoutError, OSError) as exc:
                    log.warning("could not advertise for the TV to reconnect: %s", _describe(exc))
                    await asyncio.sleep(max(ADVERT_RETRY, 5.0))
                    continue
                await self._wakeup.wait(60.0)
        finally:
            if self._pairing is None and self._ensure_active == 0:
                await self._finish(self._advertiser.stop())

    async def _start_beacon(self) -> None:
        """Broadcast the configured wake beacon next to the connectable advertisement.

        Experimental and strictly optional: a failure is logged and otherwise ignored.
        """
        beacon = tvdata.wake_beacon(self._cfg.wake_beacon, self._cfg.wake_beacon_data, self._cfg.tv_address)
        if beacon is None or not self._adapter_path:
            return
        try:
            await self._beacon.start(self._adapter_path, *beacon)
        except (DBusError, TimeoutError, OSError) as exc:
            log.warning("could not broadcast the wake beacon: %s", _describe(exc))

    # ================================================================== readiness

    def _problem(self) -> _Problem | None:
        """Why the TV cannot be reached at all right now (None if it can)."""
        if self._stopped or not self._started:
            return _Problem("bluetooth_error", "the Bluetooth remote is not running")
        if self._watcher.owner is None:
            return _Problem("no_adapter", "the Bluetooth service (bluetoothd) is not running", transient=True)
        if not self._adapter_path:
            wanted = self._cfg.adapter
            text = "no Bluetooth adapter was found" if wanted == "auto" else f"Bluetooth adapter {wanted} was not found"
            return _Problem("no_adapter", text, transient=True)
        if not self._powered:
            return _Problem("bluetooth_off", "Bluetooth is turned off on this machine")
        if not self._registered:
            return _Problem(
                "bluetooth_error",
                self._error or "the HID service is not registered with BlueZ yet",
                transient=self._reconciling(),
            )
        if self._pairing is not None:
            if self._pairing.closed:
                return _Problem("not_paired", "pairing mode is closing", transient=True)
            return _Problem("not_paired", "pairing mode is open; finish or cancel pairing first")
        if not self._cfg.tv_address:
            return _Problem("not_paired", "no TV is paired yet")
        if not self._tv_path:
            return _Problem("not_paired", f"BlueZ no longer knows the TV {self._cfg.tv_address}; pair it again")
        return None

    def _fail(self, code: str, message: str) -> TransportError:
        self._error = message
        return TransportError(code, message)

    def _closing(self) -> bool:
        if self._closing_at is None or not self._connected:
            return False
        if time.monotonic() - self._closing_at > CLOSING_GIVE_UP:
            self._closing_at = None  # bluetoothd never dropped it; treat the link as alive
            return False
        return True

    def _resubscribe_wait(self) -> float:
        full = float(self._cfg.resubscribe_wait)
        if self._store.resubscribes(self._cfg.tv_address) is False:
            return min(full, KNOWN_SILENT_WAIT)
        return full

    def _note_silent(self) -> None:
        """Remember that this TV does not re-subscribe, so later boots need not wait as long."""
        assert self._app is not None
        if not self._app.subscribed and not self._paired_this_registration:
            self._store.set_resubscribes(self._cfg.tv_address, False)

    def _bluez_ready_at(self) -> float:
        settle = self._cfg.settle_ms / 1000.0
        if self._subscribed_at is not None and self._subscribed_at > self._connected_at:
            return self._subscribed_at + min(settle, POST_SUBSCRIBE_SETTLE)
        return self._connected_at + settle

    def _set_ready(self, delivery: str) -> None:
        self._ready, self._delivery = True, delivery
        self._error = ""
        self._last_activity = time.monotonic()
        log.info(
            "TV ready for keys (%s)", "through BlueZ" if delivery == "bluez" else "direct delivery, BlueZ bypassed"
        )
        self._restart_idle_watch()

    def _attach_raw(self) -> None:
        assert self._app is not None
        for report_id in self._app.reports:
            if not self._app.report_value_handle(report_id)[0]:
                raise self._fail(
                    "not_subscribed", f"{NOT_SUBSCRIBED} (direct delivery needs attribute handles BlueZ did not report)"
                )
        try:
            self._raw.attach(rawhci.dev_id_from_adapter(self._adapter_path), self._cfg.tv_address)
        except RawError as exc:
            raise self._fail("not_subscribed", f"{NOT_SUBSCRIBED} (direct delivery is not possible: {exc})") from None

    async def _power_on(self, deadline: float) -> None:
        log.info("turning the Bluetooth adapter on")
        try:
            await dbus.set_property(
                self._bus, BLUEZ, self._adapter_path, ADAPTER, "Powered", Variant("b", True), timeout=CALL_TIMEOUT
            )
        except (DBusError, TimeoutError, OSError) as exc:
            raise self._fail(
                "bluetooth_off", f"Bluetooth is turned off and could not be turned on: {_describe(exc)}"
            ) from None
        until = min(deadline, time.monotonic() + POWER_ON_WAIT)
        while not self._powered and self._adapter_path and time.monotonic() < until:
            await self._wakeup.wait(until - time.monotonic())

    async def _run_ensure(self, deadline: float, wake: bool = False) -> bool:
        """The readiness sequence.  Re-evaluates everything after every wait.

        Returns False when the deadline passes or somebody (disconnect, pairing, stop) called
        it off; raises TransportError when waiting is pointless.
        """
        assert self._app is not None
        generation = self._abort
        self._parked = False
        await self._cancel_task("_reconnect_task")
        tried_power = beacon_tried = False
        self._ensure_active += 1
        try:
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if generation != self._abort:
                    return False

                problem = self._problem()
                if problem is not None:
                    if problem.code == "bluetooth_off" and self._cfg.power_on_adapter and not tried_power:
                        tried_power = True
                        await self._power_on(deadline)
                        continue
                    grace = ADAPTER_GRACE - (now - self._changed_at) if problem.code == "no_adapter" else 1.0
                    if problem.transient and remaining > 0 and grace > 0:
                        await self._wakeup.wait(min(remaining, grace))
                        continue
                    raise self._fail(problem.code, problem.message)

                if self._closing():
                    # A link we asked bluetoothd to drop is not worth sending on.
                    if remaining <= 0:
                        return False
                    await self._wakeup.wait(min(remaining, 0.5))
                    continue

                if not self._connected:
                    if remaining <= 0:
                        self._error = "the TV did not connect over Bluetooth"
                        return False
                    await self._advertise(discoverable=False, fast=True)
                    if wake and not beacon_tried:
                        beacon_tried = True
                        await self._start_beacon()
                    await self._wakeup.wait(remaining)
                    continue

                if self._advertiser.active or self._beacon.active:
                    await self._advertiser.stop()
                    await self._beacon.stop()
                    continue

                since = now - self._connected_at
                settle = self._cfg.settle_ms / 1000.0
                mode = self._cfg.raw_notify
                if self._app.subscribed and mode != "always":
                    ready_at = self._bluez_ready_at()
                    if now >= ready_at:
                        self._raw.detach()
                        self._set_ready("bluez")
                        return True
                    wait = ready_at - now
                elif mode != "always" and since < self._resubscribe_wait():
                    # Android 14+ needs a few seconds to rediscover us and subscribe again.
                    wait = self._resubscribe_wait() - since
                elif mode == "off":
                    self._note_silent()
                    raise self._fail("not_subscribed", NOT_SUBSCRIBED)
                elif self._links < 2:
                    # First link since the registration: see the module docstring.
                    self._note_silent()
                    if remaining <= 0:
                        self._error = "the TV connected but was not ready for keys in time"
                        return False
                    log.info("the TV did not subscribe; reconnecting it once before using direct delivery")
                    self.bounces += 1
                    await self._request_disconnect(min(deadline, now + DISCONNECT_WAIT))
                    continue
                elif since < settle:
                    wait = settle - since
                else:
                    self._note_silent()
                    self._attach_raw()
                    self._set_ready("raw")
                    return True

                if remaining <= 0:
                    self._error = "the TV connected but was not ready for keys in time"
                    return False
                await self._wakeup.wait(min(wait, remaining))
        finally:
            self._ensure_active -= 1
            if self._beacon.active:
                await self._finish(self._beacon.stop())
            if self._advertiser.active and self._pairing is None:
                await self._finish(self._advertiser.stop())
            # Whoever called us off parked the link or opened pairing; otherwise the
            # background policy takes over from here.
            self._call_in_background()

    async def _request_disconnect(self, until: float) -> None:
        """Ask bluetoothd to drop the TV link and wait (until ``until``) for it to happen."""
        path = self._connected_path
        if not path:
            return
        self._closing_at = time.monotonic()
        self._ready, self._delivery = False, ""
        self._raw.detach()
        try:
            await dbus.call(
                self._bus, BLUEZ, path, DEVICE, "Disconnect", timeout=max(0.2, until - time.monotonic())
            )
        except TimeoutError:
            pass  # bluetoothd answers only once the link is down, about two seconds later
        except DBusError as exc:
            if self._connected and exc.type not in _GONE_ERRORS:
                self._closing_at = None
                raise
        while self._connected and time.monotonic() < until:
            await self._wakeup.wait(until - time.monotonic())

    async def _drop_link_quietly(self) -> None:
        try:
            await self._request_disconnect(time.monotonic() + DISCONNECT_WAIT)
        except (DBusError, TimeoutError, OSError) as exc:
            log.warning("could not drop the TV link: %s", _describe(exc))

    # ================================================================== key delivery

    def _deliver(self, report_id: int, payload: bytes) -> None:
        """Hand one input report to the TV through exactly one path."""
        assert self._app is not None
        char = self._app.reports[report_id]
        if self._delivery == "raw":
            handle, _source = self._app.report_value_handle(report_id)
            try:
                self._raw.notify(handle, payload)
            except RawError as exc:
                self._ready, self._delivery = False, ""
                raise self._fail("bluetooth_error", f"direct delivery to the TV failed: {exc}") from None
            char.value = bytes(payload)
            return
        try:
            char.notify(payload)
        except Exception as exc:  # noqa: BLE001 - the bus connection is gone
            raise self._fail("bluetooth_error", f"could not send the key to BlueZ: {exc}") from None

    def _restart_idle_watch(self) -> None:
        task, self._idle_task = self._idle_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if self._ready and self._cfg.idle_disconnect_seconds > 0 and not self._stopped:
            self._idle_task = asyncio.ensure_future(self._idle_watch())

    async def _idle_watch(self) -> None:
        while self._ready and self._connected:
            idle = self._cfg.idle_disconnect_seconds
            if idle <= 0:
                return
            due = self._last_activity + idle - time.monotonic()
            if due <= 0 and not self._send_lock.locked():
                log.info("no key for %gs: dropping the TV link", idle)
                self._parked = True  # the point is to free the radio, not to reconnect at once
                await self._drop_link_quietly()
                return
            await asyncio.sleep(max(due, 0.05))

    # ================================================================== GATT callbacks

    def _on_touch(self, char: GattCharacteristic, device: str) -> None:
        window = self._pairing
        if window is not None and device:
            window.touched.add(device)
            self._pairing_progress()

    def _on_notify(self, char: GattCharacteristic, enabled: bool) -> None:
        assert self._app is not None
        log.debug("%s %s", "StartNotify" if enabled else "StopNotify", char.path)
        if not enabled:
            if not self._app.subscribed:
                self._subscribed_at = None
                log.info("the TV's subscription to key reports ended")
            self._wakeup.fire()
            return
        if self._subscribed_at is None:
            self._subscribed_at = time.monotonic()
            log.info("the TV subscribed to key reports")
        window = self._pairing
        if window is not None:
            window.notified = True
            self._pairing_progress()
        elif not self._paired_this_registration and self._tv_path:
            # A subscription that is not part of pairing: this TV renews it by itself.
            self._store.set_resubscribes(self._cfg.tv_address, True)
        if self._delivery == "raw" and self._cfg.raw_notify != "always":
            # From here on bluetoothd forwards our notifications; using both paths would
            # deliver every key twice.
            log.info("switching key delivery from direct to BlueZ")
            self._delivery = "bluez"
            self._raw.detach()
        self._wakeup.fire()

    # ================================================================== pairing

    def _agent_released(self) -> None:
        self._agent_registered = False

    def _agent_decision(self, kind: str, device: str) -> bool:
        window = self._pairing
        if window is None or window.closed or window.aborted or time.monotonic() >= window.deadline:
            return False
        if window.winner:
            return device == window.winner
        if kind == "service":
            return bool(window.candidate) and device == window.candidate
        if device in window.known:
            return False  # bonded before the window opened: not the TV being paired now
        if not window.candidate:
            window.candidate = device
            return True
        return device == window.candidate

    def _pairing_progress(self) -> None:
        """Pairing is done when a newly bonded, connected device has used our HID service."""
        window = self._pairing
        if window is None or window.winner or window.closed or window.aborted:
            return
        eligible = [
            path
            for path, props in sorted(self._watcher.devices(window.adapter_path).items())
            if path not in window.known
            and (props.get("Paired") or props.get("Bonded"))
            and props.get("Connected")
            and (not window.candidate or path == window.candidate)
        ]
        winner = next((path for path in eligible if path in window.touched), "")
        if not winner and window.notified and len(eligible) == 1:
            winner = eligible[0]  # StartNotify does not say who subscribed; one candidate is enough
        if winner:
            window.winner = winner
            self._wakeup.fire()

    async def _register_agent(self) -> None:
        try:
            await dbus.call(
                self._bus, BLUEZ, BLUEZ_ROOT, AGENT_MANAGER, "RegisterAgent", "os", [AGENT_PATH, AGENT_CAPABILITY],
                timeout=CALL_TIMEOUT,
            )  # fmt: skip
        except DBusError as exc:
            if exc.type != ERR_ALREADY_EXISTS:
                raise
        self._agent_registered = True
        await self._request_default_agent()

    async def _request_default_agent(self) -> None:
        # Whoever calls this last wins, so the pairing loop repeats it.  A no-op in bluetoothd
        # when we already are the default agent.
        await dbus.call(
            self._bus, BLUEZ, BLUEZ_ROOT, AGENT_MANAGER, "RequestDefaultAgent", "o", [AGENT_PATH], timeout=CALL_TIMEOUT
        )

    async def _unregister_agent(self) -> None:
        self._agent_registered = False
        try:
            await dbus.call(
                self._bus, BLUEZ, BLUEZ_ROOT, AGENT_MANAGER, "UnregisterAgent", "o", [AGENT_PATH], timeout=CALL_TIMEOUT
            )
        except (DBusError, TimeoutError, OSError) as exc:
            log.debug("UnregisterAgent: %s", _describe(exc))

    async def _set_pairable(self, adapter: str, value: bool) -> None:
        await dbus.set_property(
            self._bus, BLUEZ, adapter, ADAPTER, "Pairable", Variant("b", value), timeout=CALL_TIMEOUT
        )

    async def _open_pairing(self, timeout: float) -> None:
        tried_power = False
        while True:
            problem = self._problem()
            if problem is None or problem.code == "not_paired":
                break
            if problem.code == "bluetooth_off" and self._cfg.power_on_adapter and not tried_power:
                tried_power = True
                await self._power_on(time.monotonic() + POWER_ON_WAIT)
                continue
            raise self._fail(problem.code, problem.message)

        adapter = self._adapter_path
        # A bond BlueZ still holds for the old TV would make it refuse the new pairing without
        # ever asking our agent (JustWorksRepairing = never), so it goes first.
        if self._cfg.tv_address:
            self._closing_at = time.monotonic() if self._connected else None
            await self._remove_device(self._cfg.tv_address)
        await self._remove_stale()

        props = self._watcher.interface(adapter, ADAPTER) or {}
        window = _PairingWindow(
            deadline=time.monotonic() + timeout,
            adapter_path=adapter,
            known={
                path
                for path, device in self._watcher.devices(adapter).items()
                if device.get("Paired") or device.get("Bonded")
            },
        )
        self._pairing = window
        try:
            await self._register_agent()
            if not props.get("Pairable", False):
                window.prev_pairable = False
                await self._set_pairable(adapter, True)
            await self._advertise(discoverable=True, fast=True)
        except BaseException:
            await self._close_pairing(window)
            raise
        self._error = ""
        log.info("pairing mode open for %gs as %r", timeout, self._cfg.device_name)
        self._pairing_task = asyncio.ensure_future(self._pairing_loop(window))
        self._wakeup.fire()

    async def _pairing_loop(self, window: _PairingWindow) -> None:
        try:
            while self._pairing is window and not window.closed:
                if window.aborted:
                    self._error = f"pairing was interrupted: {window.aborted}"
                    log.warning("%s", self._error)
                    return
                if window.winner:
                    await self._complete_pairing(window)
                    return
                remaining = window.deadline - time.monotonic()
                if remaining <= 0:
                    self._error = "pairing mode timed out before a TV paired"
                    log.info("%s", self._error)
                    return
                try:
                    await self._hold_pairing_conditions(window)
                except (DBusError, TimeoutError, OSError) as exc:
                    log.debug("pairing: could not re-assert pairing conditions: %s", _describe(exc))
                await self._wakeup.wait(min(remaining, PAIRING_TICK))
        finally:
            await self._close_pairing(window)

    async def _hold_pairing_conditions(self, window: _PairingWindow) -> None:
        """Other software (or a timeout) may take the default agent or pairability away."""
        if not self._agent_registered:
            await self._register_agent()
        else:
            await self._request_default_agent()
        props = self._watcher.interface(window.adapter_path, ADAPTER) or {}
        if not props.get("Pairable", True):
            await self._set_pairable(window.adapter_path, True)
        if not self._advertiser.active:
            await self._advertise(discoverable=True, fast=True)

    async def _complete_pairing(self, window: _PairingWindow) -> None:
        path = window.winner
        try:
            # Trusted: bluetoothd then lets the TV use our services without asking an agent.
            await dbus.set_property(self._bus, BLUEZ, path, DEVICE, "Trusted", Variant("b", True), timeout=CALL_TIMEOUT)
        except (DBusError, TimeoutError, OSError) as exc:
            log.warning("could not mark the TV as trusted: %s", _describe(exc))
        try:
            props = await dbus.get_all_properties(self._bus, BLUEZ, path, DEVICE, timeout=CALL_TIMEOUT)
        except (DBusError, TimeoutError, OSError) as exc:
            log.debug("could not re-read the TV's properties (%s); using the mirrored ones", _describe(exc))
            props = self._watcher.interface(path, DEVICE) or {}
        # The Address property is the identity address once the keys were exchanged.  The
        # object path may still carry the private address the TV first connected from.
        address = str(props.get("Address", "")).upper()
        if not address:
            self._error = "pairing finished but BlueZ did not report the TV's address"
            log.error("%s", self._error)
            return
        name = str(props.get("Name") or "")
        alias = str(props.get("Alias") or "")
        if not name and alias.replace("-", ":").upper() != address:
            name = alias
        name = name[:64]

        self._cfg = dataclasses.replace(self._cfg, tv_address=address, tv_name=name)
        self._paired_this_registration = True
        self._parked = False
        self._store.set_resubscribes(address, None)
        self._store.discard_stale(address)
        self._error = ""
        log.info("paired with %s (%s)", name or "a TV", address)
        callback = self.on_paired
        if callback is not None:
            try:
                await callback(address, name)
            except Exception:  # noqa: BLE001 - the bond exists whatever the listener does
                log.exception("the on_paired callback failed")
        await self._close_pairing(window)
        self._evaluate()

    async def _close_pairing(self, window: _PairingWindow) -> None:
        """End the pairing window and put everything back.  Idempotent, never raises.

        The clean-up runs in a task of its own so that it completes whoever asked for it and
        whatever happens to them; every caller waits for that same task.
        """
        if window.cleanup is None:
            window.closed = True
            window.cleanup = asyncio.ensure_future(self._restore_after_pairing(window))
            self._tasks.add(window.cleanup)
            window.cleanup.add_done_callback(self._tasks.discard)
        await asyncio.shield(window.cleanup)

    async def _restore_after_pairing(self, window: _PairingWindow) -> None:
        try:
            await self._advertiser.stop()
            if self._agent_registered:
                await self._unregister_agent()  # the previous default agent takes over again
            if window.prev_pairable is not None and window.adapter_path == self._adapter_path:
                try:
                    await self._set_pairable(window.adapter_path, window.prev_pairable)
                except (DBusError, TimeoutError, OSError) as exc:
                    log.debug("could not restore Pairable: %s", _describe(exc))
        finally:
            if self._pairing is window:
                self._pairing = None
            self._call_in_background()
            self._wakeup.fire()

    # ================================================================== bonds

    async def _remove_device(self, address: str) -> bool:
        """Remove the bond for ``address``.  True if it is gone, False if BlueZ cannot be asked now."""
        adapter = self._adapter_path
        if not adapter or self._watcher.owner is None:
            return False
        address = address.upper()
        paths = [
            path
            for path, props in self._watcher.devices(adapter).items()
            if str(props.get("Address", "")).upper() == address
        ]
        for path in paths:
            try:
                await dbus.call(
                    self._bus, BLUEZ, adapter, ADAPTER, "RemoveDevice", "o", [path], timeout=REMOVE_DEVICE_TIMEOUT
                )
                log.info("removed %s from BlueZ", path)
            except DBusError as exc:
                if exc.type == ERR_NOT_READY:
                    return False  # the adapter is off; BlueZ refuses to remove devices then
                if exc.type not in _GONE_ERRORS:
                    raise
            # bluetoothd announces the removal only after it answered (InterfacesRemoved is
            # sent from an idle callback).  Do not let the dead object linger in the mirror: a
            # TV that pairs again may get the very same object path.
            self._watcher.forget_object(path)
        if paths:
            self._evaluate()
        return True

    async def _remove_stale(self) -> None:
        for address in self._store.stale:
            if address == self._cfg.tv_address.upper():
                self._store.discard_stale(address)  # paired again since
                continue
            try:
                if await self._remove_device(address):
                    self._store.discard_stale(address)
            except (DBusError, TimeoutError, OSError) as exc:
                log.debug("could not remove the stale bond %s: %s", address, _describe(exc))

    # ================================================================== helpers

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run a short clean-up to completion even if the calling task is being cancelled."""
        task = asyncio.ensure_future(coro)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            raise

    async def _cancel_task(self, name: str) -> None:
        task: asyncio.Task[None] | None = getattr(self, name)
        setattr(self, name, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
