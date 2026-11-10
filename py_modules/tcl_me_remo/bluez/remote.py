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
  idle time-out) is "parked": it stays closed until the next ensure_ready, or until the
  service says the machine has woken up again (resume_link).  The idle time-out applies to
  every link that carries no key, also to one that background calling brought up.
* **Readiness cannot be observed.**  bluetoothd tells us the link is up (``Device1.Connected``)
  but neither that it is encrypted nor that the TV's HID host finished setting up, so a
  settle delay stands in for both.
* **Subscriptions live in bluetoothd's memory.**  They survive disconnects and suspend, so a
  reconnect brings no new ``StartNotify`` and sending must not wait for one.  They are lost
  when bluetoothd restarts (every boot).  Android 14+ then subscribes again after the Service
  Changed indication; Android 11-13 never does, bluetoothd silently drops our notifications,
  and the only way to reach the TV short of pairing again is direct delivery (rawhci.py).
  That path disturbs the kernel's packet accounting before Linux 6.17, so by default
  (``raw_notify = auto``) it is used only from that version on; see ``_direct_delivery``.
* **There are three of them.**  Each report characteristic has a subscription of its own, and
  a host writes them one after another (Android: in table order, each once the last was
  answered).  bluetoothd forwards a report only if the one of *its* characteristic has
  arrived and drops it otherwise, as silently as when there is none.  So a report is never
  handed to bluetoothd for a characteristic that is not subscribed (``_deliver``), and the
  link is ready "through BlueZ" only once all three are.  While only some have arrived the
  others are waited for like the first; when that time is up the missing reports travel by
  direct delivery where it is allowed, and where it is not, a key in such a report fails
  with ``not_subscribed`` while the keys of the other reports are sent.
* **The first link after a registration is special.**  The Service Changed indication that
  bluetoothd sends on it makes Android 11-13 forget, for that connection, which
  notifications its HID host wanted.  Direct delivery therefore starts on the second link:
  the first one is dropped once on purpose ("bounce").
* **Device1.Disconnect() takes two seconds** (bluetoothd's ``DISCONNECT_TIMER``) and only
  answers once the link is down.  That is too long for the moments before suspend, so with
  ``reconnect_on_drop`` the bounce is done in the background as soon as it is known to be
  needed, not when the first key is due.  For the same reason a key that is down is released
  before a link is given up on purpose, and the link is ended through the kernel instead
  (mgmt.py) when the machine is about to sleep: ``disconnect(fast=True)``.
* **A bond is refused a second time.**  bluetoothd rejects pairing with a device it still
  holds a bond for, without asking any agent (``JustWorksRepairing = never``).  Every bond
  this service caused and no longer wants is therefore removed: the old TV's before a new
  pairing, and that of a TV which bonded in a pairing window that ended before the TV was
  adopted (store.py keeps the list across restarts).
* **Callers bring their own deadline.**  ensure_ready's timeout bounds everything it does,
  D-Bus calls included; near suspend it is about three seconds.  Loops that wait for BlueZ
  to change never sleep through a change that arrived while they were talking to bluetoothd
  (see ``_Wakeup``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import platform
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
from .mgmt import LinkCloser, MgmtError
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
#: Settle time once the last StartNotify has been seen on the current link: by then the link
#: is encrypted and the TV's HID host is merely finishing its set-up.
POST_SUBSCRIBE_SETTLE = 0.3
#: How long to wait for a re-subscription from a TV that is known not to re-subscribe, or
#: for the rest from one that stopped after some of its subscriptions (see _resubscribe_wait).
KNOWN_SILENT_WAIT = 0.5
#: bluetoothd drops a link two seconds after Device1.Disconnect(); allow some slack.
DISCONNECT_WAIT = 3.5
#: A requested disconnect that has not happened after this long is considered failed.
CLOSING_GIVE_UP = 6.0
#: disconnect(fast=True): a report handed over less than this long ago is given time to go out
#: before the kernel is told to end the link (the controller may drop what it still holds).
REPORT_FLUSH = 0.15
#: disconnect(fast=True): how long the link is watched after the kernel has the request.  The
#: request stands whatever happens; this only lets the caller see the result when it is quick.
FAST_DISCONNECT_WAIT = 0.3
FAST_DISCONNECT_POLL = 0.02
#: Pause before advertising again after bluetoothd released or refused the advertisement.
ADVERT_RETRY = 1.0
PAIRING_TICK = 1.0
REMOVE_DEVICE_TIMEOUT = 6.0
#: With less of its budget left than this, a waiting ensure_ready no longer goes on air: the
#: TV could not answer in time, and the request would only have to be taken back.
MIN_ADVERT_BUDGET = 0.05
#: Shortest time a D-Bus call is given when a caller's budget is nearly used up.
MIN_CALL_TIMEOUT = 0.05
#: How long ensure_ready waits for bluetoothd to confirm that the advertisement is off air
#: before it returns anyway (the request is on its way by then).
CLEANUP_GRACE = 0.25

ERR_ALREADY_EXISTS = "org.bluez.Error.AlreadyExists"
ERR_FAILED = "org.bluez.Error.Failed"
#: bluetoothd's only reply when the requested handle range cannot be had (gatt-database.c,
#: client_ready_cb: "Failed to create entry in database").
PIN_REFUSED_TEXT = "create entry in database"
ERR_DOES_NOT_EXIST = "org.bluez.Error.DoesNotExist"
ERR_NOT_READY = "org.bluez.Error.NotReady"
#: Replies that mean "there is nothing to disconnect / remove".
_GONE_ERRORS = (
    ERR_DOES_NOT_EXIST,
    "org.bluez.Error.NotConnected",
    "org.freedesktop.DBus.Error.UnknownObject",
    "org.freedesktop.DBus.Error.UnknownMethod",
)

CLASSIC_VISIBLE = (
    "this machine is also discoverable over classic Bluetooth (is another Bluetooth pairing screen open?). "
    "A TV may then try to pair the wrong way and give up; close other pairing screens and try again"
)

NOT_SUBSCRIBED = (
    "the TV is connected but will not receive keys until it is paired again: it did not renew its "
    "subscription after Bluetooth restarted on this machine. Pair the TV again, or use the Wi-Fi assist"
)
#: The same situation when direct delivery would be allowed but the kernel is too old for it to
#: be used without being asked.  ``{kernel}`` is the running kernel's release.
NOT_SUBSCRIBED_OLD_KERNEL = (
    "the TV is connected but will not receive keys: it did not renew its subscription after Bluetooth "
    "restarted on this machine. Direct delivery could reach it, but is not used automatically on this "
    "kernel ({kernel}; before Linux 6.17 it can disturb other Bluetooth devices). Set Direct delivery "
    "(bluetooth.raw_notify) to 'on' to use it anyway, or pair the TV again, or use the Wi-Fi assist"
)
#: Appended to either when the TV renewed some of its three subscriptions and the key that
#: was to be sent travels in a report whose subscription is missing.
NOT_SUBSCRIBED_THIS_KEY = " (it renewed its subscription for some keys only, and this key is not among them)"

#: What send_key answers when the key it holds was released underneath it (_release_held):
#: an error code and the beginning of the sentence, "... while HOME was held".  Only the first
#: is about the link; after the other two it is up as before.
HOLD_LINK_CLOSED = ("not_connected", "the Bluetooth link was closed")
HOLD_SUBSCRIPTION_ENDED = ("not_subscribed", "the TV ended a subscription to key reports")
HOLD_DELIVERY_CHANGED = ("not_connected", "key delivery was changed in the settings")


#: Longest TV name that is kept (``bluetooth.tv_name`` allows as much).
MAX_TV_NAME = 64


def _clean_name(text: str, limit: int = MAX_TV_NAME) -> str:
    """Make the name a device gave itself safe to store, show and log.

    It is whatever the device that bonds in the pairing window says it is called; with a line
    break in it, one log line would look like two.  Line breaks, other control characters and
    anything else that is not printable become spaces, runs of white space are collapsed and
    the result is cut to ``limit`` characters.  (The Wi-Fi assist does the same to names from
    the network; this transport does not import it.)
    """
    printable = "".join(char if char.isprintable() else " " for char in text[: limit * 4])
    return " ".join(printable.split())[:limit].strip()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, DBusError):
        name = str(exc.type).rsplit(".", 1)[-1]
        return f"{name}: {exc.text}" if exc.text else name
    if isinstance(exc, TimeoutError):
        return "BlueZ did not answer in time"
    return str(exc) or type(exc).__name__


class _Wakeup:
    """Lets any number of coroutines sleep until "something changed" or a timeout.

    ``fire()`` alone would only reach coroutines that are already waiting.  A loop that looks
    at the state, awaits something else (a D-Bus call) and only then waits would sleep through
    a change that came in between.  Such loops read ``generation`` *before* they look at the
    state and hand it to ``wait``, which then returns at once if anything fired since.
    """

    def __init__(self) -> None:
        self._waiters: set[asyncio.Future[None]] = set()
        self.generation = 0

    def fire(self) -> None:
        self.generation += 1
        waiters, self._waiters = self._waiters, set()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def wait(self, timeout: float, seen: int | None = None) -> None:
        if timeout <= 0 or (seen is not None and seen != self.generation):
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
    bonded: dict[str, str] = field(default_factory=dict)  # path -> Address of devices that bonded in this window
    adopted: str = ""                     # address of the TV this window ended up storing
    classic_visible: bool = False         # the adapter itself was discoverable at some point
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
    #: Whether it becomes ``state.error``.  "Pairing mode is open" does not: it is an answer to
    #: one caller, true only while the window is, and it would hide what the window has to say.
    lasting: bool = True


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
        link_closer: LinkCloser | None = None,
        kernel_release: str | None = None,
    ):
        self._bus = bus
        self._cfg = dataclasses.replace(cfg)
        self.on_paired: PairedCallback | None = on_paired
        self._store = StateStore(state_path)
        #: The running kernel's release, e.g. "6.11.11-valve19-1-neptune-611".  It decides whether
        #: direct delivery is used without being asked for, and whether its traces need a repair.
        self._kernel = kernel_release if kernel_release is not None else platform.release()
        self._raw_is_free = rawhci.direct_delivery_is_free(self._kernel)
        self._raw = raw_link if raw_link is not None else RawLink(repair_credits=not self._raw_is_free)
        self._closer = link_closer if link_closer is not None else LinkCloser()

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
        self._tv_path = ""
        self._connected = False
        self._connected_path = ""
        self._connected_at = 0.0
        self._links = 0                       # TV links seen since the current registration
        self._subscribed_at: float | None = None   # the last StartNotify (None: no subscription at all)
        #: Some, not all, report characteristics are subscribed and the time allowed for the
        #: others has been waited out once since the last StartNotify: see _resubscribe_wait.
        self._partial_waited = False
        self._paired_this_registration = False
        self._closing_at: float | None = None  # we asked bluetoothd to drop the link
        self._parked = False                  # we closed the link on purpose: no background calling
        self._ready = False
        self._delivery = ""
        self._error = ""
        self._pairing_note = ""               # how the last pairing window ended / what is wrong with the open one
        self._held: int | None = None         # report id of the key that is down right now
        self._hold_ended = HOLD_LINK_CLOSED   # why that key was released before its time was up
        self._last_activity = 0.0
        self._last_report_at = 0.0            # when the last input report was handed over
        self._advert_not_before = 0.0
        self._agent_registered = False

        self._pairing: _PairingWindow | None = None
        self._pair_cancel = 0                 # bumped by every request to end pairing mode
        self._pair_opening = 0                # start_pairing calls under way
        self._pairing_task: asyncio.Task[None] | None = None
        self._settle_task: asyncio.Future[Any] | None = None
        self._boot = rawhci.boot_id()
        self._debt_noted = False              # the state file says raw delivery left a debt in this boot
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
        self._parked = False
        # A TV that bonded in a pairing window which never completed (the service died first)
        # must not keep its bond: BlueZ would refuse its next pairing attempt.
        self._store.pending_to_stale()
        if self._raw.repairs and self._store.raw_debt(self._boot):
            # An earlier run injected packets and stopped while the TV link was still up.
            self._raw.assume_debt()
            self._debt_noted = True
        else:
            self._store.clear_raw_debt()
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
            seen = self._wakeup.generation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await self._wakeup.wait(remaining, seen)

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
        # A debt the raw path left with the kernel stays on record (state file): it can only be
        # settled once the TV link is down, which the next start takes care of.
        self._raw.detach()
        self._closer.close()
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
        # Back to a blank slate: a later start() learns everything again from a fresh snapshot.
        self._registered = self._ready = self._connected = self._powered = False
        self._delivery = ""
        self._adapter_path = self._tv_path = self._connected_path = ""
        self._links = 0
        self._closing_at = self._subscribed_at = None
        self._partial_waited = False
        self._agent_registered = False
        self._held = None
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
            # bluetoothd may keep (and we then mirror) the subscription of a TV that has been
            # forgotten; without a TV there is nobody whose subscription this could be.
            subscribed=bool(self._tv_path) and self._app is not None and self._app.subscribed,
            delivery=self._delivery if self._ready else "",
            error=self._error or self._pairing_note,
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
            if old.idle_disconnect_seconds != cfg.idle_disconnect_seconds:
                self._restart_idle_watch()
            if self._ready and self._delivery == "raw" and self._direct_delivery() == "off":
                # Direct delivery was just forbidden: keys can no longer reach the TV.
                self._release_held(HOLD_DELIVERY_CHANGED)
                self._ready, self._delivery = False, ""
                self._raw.detach()
            elif (
                self._ready
                and self._delivery == "bluez"
                and self._app is not None
                and not self._app.fully_subscribed
                and self._direct_delivery() != "off"
            ):
                # Direct delivery was just allowed, and some reports have no other way to the
                # TV: the next ensure_ready sets it up for them.
                self._release_held(HOLD_DELIVERY_CHANGED)
                self._ready, self._delivery = False, ""

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
        # Always against the deadline: a lock that reads "free" may be on its way to another
        # waiter (asyncio.Lock hands over through the event loop), behind whom we would queue.
        # An uncontended acquire does not wait and so cannot time out.
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                await self._op_lock.acquire()
        except TimeoutError:
            return False
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
            self._held, self._hold_ended = report_id, HOLD_LINK_CLOSED
            try:
                await asyncio.sleep(max(0.0, hold))
                if self._held is None:
                    # We gave the link up ourselves meanwhile (disconnect, forget, pairing), or
                    # it stopped being ready for keys while it stayed up, and the key was
                    # released on the way out, see _release_held.
                    code, what = self._hold_ended
                    raise TransportError(code, f"{what} while {key.spec} was held")
                if not self._ready:
                    # The TV tears its input device down with the link, which releases the key.
                    self._held = None
                    raise TransportError("not_connected", f"the Bluetooth link dropped while {key.spec} was held")
                self._deliver(report_id, hid.release_report(report_id))
                self._held = None
            finally:
                if self._held is not None:
                    # Cancelled or failed mid-press: a key left down would auto-repeat on the TV.
                    self._held = None
                    released = False
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

    def _release_held(self, ended: tuple[str, str] = HOLD_LINK_CLOSED) -> None:
        """Release the key that is down, if any, before the link stops being usable.

        bluetoothd keeps a link up for two more seconds after Device1.Disconnect(); a key
        left down for that long would auto-repeat on the TV.  The same goes for a link that
        stays up and is merely not ready for keys any more: ``ended`` is what send_key then
        answers (HOLD_...).
        """
        report_id, self._held = self._held, None
        if report_id is None:
            return
        self._hold_ended = ended
        if not self._ready:
            return
        if self._delivery != "raw" and self._app is not None and not self._app.reports[report_id].notifying:
            # The TV has just ended the subscription of this very report: bluetoothd would
            # drop the release (_deliver refuses it), and the TV no longer wants to hear of it.
            return
        try:
            self._deliver(report_id, hid.release_report(report_id))
        except TransportError:
            pass

    async def start_pairing(self, timeout: float) -> None:
        # cancel_pairing() and forget() from here on call this request off, at whatever point
        # of the set-up it has arrived (they bump the counter and then wait for us).
        token = self._pair_cancel
        self._pair_opening += 1  # also keeps the background policy off the air meanwhile
        self._abort += 1  # a running ensure_ready gives way
        self._wakeup.fire()
        try:
            await self._cancel_task("_reconnect_task")
            async with self._op_lock:
                await self._end_pairing()  # a window that is still open is replaced
                try:
                    await self._open_pairing(timeout, token)
                except TransportError:
                    raise
                except (DBusError, TimeoutError, OSError) as exc:
                    raise self._fail("bluetooth_error", f"could not open pairing mode: {_describe(exc)}") from None
        finally:
            self._pair_opening -= 1
            self._call_in_background()

    async def cancel_pairing(self) -> None:
        self._pair_cancel += 1
        self._wakeup.fire()
        if self._pairing is None and not self._pair_opening:
            return
        # Serialised with start_pairing: a window that is being set up sees the request after
        # its current step and backs out; we return once everything has been put back.
        async with self._op_lock:
            if await self._end_pairing():
                log.info("pairing mode closed")

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
                self._release_held()
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
            self._pairing_note = ""
            self._evaluate()

    async def disconnect(self, *, fast: bool = False) -> None:
        """See :meth:`tcl_me_remo.interfaces.Remote.disconnect`.

        ``fast`` ends the link through the kernel (mgmt.py): at once instead of after
        bluetoothd's two seconds, and with the same reason code.  The request is made before
        anything here waits for bluetoothd, and is not taken back if the caller stops waiting.
        Whenever that way is not open the usual one is taken.
        """
        self._abort += 1
        self._parked = True
        self._wakeup.fire()
        asked_kernel = False
        try:
            if fast and self._connected:
                asked_kernel = await self._kernel_disconnect()
            await self._cancel_task("_reconnect_task")
            await self._advertiser.stop()
            if asked_kernel and await self._kernel_disconnect_took():
                return
        finally:
            self._closer.close()
        if not self._connected:
            return
        try:
            await self._request_disconnect(time.monotonic() + DISCONNECT_WAIT)
        except (DBusError, TimeoutError, OSError) as exc:
            raise self._fail("bluetooth_error", f"could not disconnect the TV: {_describe(exc)}") from None

    def resume_link(self) -> None:
        """See :meth:`tcl_me_remo.interfaces.Remote.resume_link`.  Never raises."""
        try:
            if not self._started or self._stopped or not self._parked:
                return
            # With idle_disconnect_seconds the link this brings up is closed (and parked) again
            # by the idle time-out like any other link that carries no key, see _idle_watch.
            self._parked = False
            if self._keeping_link():
                log.info("the machine is awake again: calling the TV in the background")
            self._call_in_background()
        except Exception:  # noqa: BLE001 - a courtesy call on the way up must never fail
            log.exception("could not resume background calling")

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
            if not powered and self._pairing is None:
                self._advertiser.discard()
                self._beacon.discard()
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
        self._settle_if_due()
        self._call_in_background()
        self._wakeup.fire()

    def _settle_if_due(self) -> None:
        """Repair the kernel's packet accounting once the link that carried injected packets is gone."""
        if self._connected or self._stopped or not self._adapter_path or not self._powered:
            return
        if not (self._raw.owes or self._debt_noted):
            return
        if self._settle_task is None or self._settle_task.done():
            self._settle_task = self._spawn(self._settle(self._adapter_path))

    async def _settle(self, adapter: str) -> None:
        try:
            dev_id = rawhci.dev_id_from_adapter(adapter)
        except RawError:
            return
        await self._raw.settle(dev_id)
        if not self._raw.owes and not self._raw.attached:
            # Unless the TV is back already and direct delivery is in use on the new link:
            # then the record stays for that link.
            self._debt_noted = False
            self._store.clear_raw_debt()

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
        self._partial_waited = False
        self._advertiser.forget()
        self._beacon.forget()
        if self._app is not None:
            # bluetoothd destroyed the adapter's GATT database and with it every subscription.
            # The next registration asks for the fixed handles again, whatever happened to the last.
            self._app.reset_for_registration(SERVICE_HANDLE)
        if self._pairing is not None:
            self._pairing.aborted = "Bluetooth went away"
        self._error = "the Bluetooth adapter is gone"

    def _link_up(self, path: str) -> None:
        self._connected, self._connected_path = True, path
        self._connected_at = time.monotonic()
        self._closing_at = None
        self._links += 1
        log.info("TV connected (%s), link %d since the HID service was registered", path, self._links)
        self._restart_idle_watch()

    def _link_down(self, *, adapter_gone: bool = False) -> None:
        expected = self._closing_at is not None
        self._connected, self._connected_path = False, ""
        self._closing_at = None
        self._ready, self._delivery = False, ""
        log.info("TV disconnected%s", "" if expected or adapter_gone else " (not on our request)")
        # What raw delivery owes the kernel is kept, also when bluetoothd went away with the
        # link possibly still up in the kernel: it is settled once the TV is known to be gone.
        self._raw.detach()
        if not adapter_gone:
            self._settle_if_due()
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
        # Every registration asks for the fixed handles first.  Giving them up is costly (a TV
        # that paired with them ignores keys sent on any others, and automatic handles move at
        # every restart), so it happens only on the one answer that means "this range is
        # taken" and is not remembered: any other error is retried by _reconcile, pin included.
        self._app.reset_for_registration(SERVICE_HANDLE)
        self._subscribed_at = None
        self._partial_waited = False
        try:
            await self._register_call(adapter)
        except DBusError as exc:
            if exc.type != ERR_FAILED or PIN_REFUSED_TEXT not in (exc.text or "").lower():
                raise
            log.warning(
                "BlueZ refused the HID service at its fixed handle 0x%04X (%s); retrying with automatic handles",
                SERVICE_HANDLE,
                _describe(exc),
            )
            self._app.reset_for_registration(0)
            await self._register_call(adapter)
        if adapter != self._adapter_path or owner != self._watcher.owner:
            return  # registered with a bluetoothd / adapter that has gone meanwhile
        self._registered = True
        self._links = 1 if self._connected else 0
        self._paired_this_registration = False
        self._error = ""  # whatever went wrong before, this is a fresh start
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

    def _advert_pause(self) -> float:
        """Seconds to stay off air after bluetoothd released or refused the advertisement.

        Callers wait this out on the wake-up, not asleep: the TV may well have answered the
        last advertising event and connect meanwhile.
        """
        if self._advertiser.active:
            return 0.0
        return max(0.0, self._advert_not_before - time.monotonic())

    def _call_budget(self, deadline: float) -> float:
        """Timeout for one D-Bus call made on behalf of a caller whose time ends at ``deadline``."""
        return min(CALL_TIMEOUT, max(MIN_CALL_TIMEOUT, deadline - time.monotonic()))

    async def _advertise(self, *, discoverable: bool, fast: bool, timeout: float = CALL_TIMEOUT) -> None:
        """Make sure our advertisement is on air.  Raises DBusError / TimeoutError."""
        if not self._adapter_path or not self._registered:
            return  # never advertise without the HID service: the TV would find nothing to use
        settings = AdvertSettings(name=self._cfg.device_name, discoverable=discoverable, fast=fast)
        try:
            await self._advertiser.start(self._adapter_path, settings, timeout=timeout)
        except (DBusError, TimeoutError, OSError):
            self._advert_not_before = time.monotonic() + ADVERT_RETRY
            raise

    def _keeping_link(self) -> bool:
        """Whether the background policy (``reconnect_on_drop``) currently applies."""
        return bool(
            self._cfg.reconnect_on_drop
            and not self._parked
            and not self._stopped
            and self._registered
            and self._powered
            and self._tv_path
            and self._pairing is None
            and not self._pair_opening
        )

    def _call_in_background(self) -> None:
        if self._ensure_active or not self._keeping_link():
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.ensure_future(self._keep_link())

    def _first_link(self) -> bool:
        """True on the one link that carried the Service Changed indication (module docstring).

        A TV that paired during this registration never got that indication: it is sent to
        devices that were bonded when the service was registered.
        """
        return self._links < 2 and not self._paired_this_registration

    def _needs_bounce(self) -> bool:
        """The TV will need direct delivery, which cannot work on the current link.

        That is also so when only some of its subscriptions are missing: going by Android's
        source, a host that wrote them on this link listens here to the reports whose write
        succeeded, and to all of them from the next link on.
        """
        assert self._app is not None
        mode = self._direct_delivery()
        return (
            self._connected
            and self._first_link()
            and not self._ready
            and mode != "off"
            and (mode == "always" or not self._app.fully_subscribed)
        )

    async def _keep_link(self) -> None:
        """Background policy: call the TV while it is away, and get the bounce out of the way.

        The second part matters before suspend: the service then has a few seconds to deliver
        the off key, which is not enough for dropping the link and waiting for the TV to
        come back.  Doing that as soon as it is known to be necessary leaves a link that
        ensure_ready can declare ready at once.
        """
        calling = False
        try:
            while self._keeping_link():
                seen = self._wakeup.generation  # before looking at anything, see _Wakeup
                now = time.monotonic()
                if not self._connected:
                    if not calling:
                        log.info("calling the TV in the background until it connects")
                        calling = True
                    pause = self._advert_pause()
                    if pause > 0:
                        await self._wakeup.wait(pause, seen)
                        continue
                    try:
                        await self._advertise(discoverable=False, fast=False)
                    except (DBusError, TimeoutError, OSError) as exc:
                        log.warning("could not advertise for the TV to reconnect: %s", _describe(exc))
                        await asyncio.sleep(max(ADVERT_RETRY, 5.0))
                        continue
                    await self._wakeup.wait(60.0, seen)
                    continue
                calling = False
                if self._advertiser.active:
                    await self._advertiser.stop()
                    continue
                if self._closing():
                    await self._wakeup.wait(0.5, seen)
                    continue
                if self._needs_bounce():
                    due = self._connected_at + max(self._resubscribe_wait(), self._cfg.settle_ms / 1000.0)
                    if now < due:
                        await self._wakeup.wait(due - now, seen)
                        continue
                    self._note_silent()
                    self._note_partial()
                    log.info(
                        "the TV did not subscribe%s; reconnecting it once so that direct delivery is ready",
                        " to every key report" if self._app is not None and self._app.subscribed else "",
                    )
                    self.bounces += 1
                    try:
                        await self._request_disconnect(now + DISCONNECT_WAIT)
                    except (DBusError, TimeoutError, OSError) as exc:
                        log.warning("could not reconnect the TV: %s", _describe(exc))
                        await asyncio.sleep(max(ADVERT_RETRY, 5.0))
                    continue
                await self._wakeup.wait(60.0, seen)
        finally:
            # A waiting ensure_ready or an opening pairing window takes the advertisement over.
            if self._pairing is None and self._ensure_active == 0:
                await self._finish(self._advertiser.stop())
            if self._reconnect_task is asyncio.current_task():
                # Not called off but run out: the policy stopped applying (the link was parked,
                # the option switched off).  It may apply again by now - resume_link() a moment
                # after the idle time-out, say - and whoever noticed found this task still
                # winding down and started none.
                self._reconnect_task = None
                self._call_in_background()

    async def _start_beacon(self, timeout: float = CALL_TIMEOUT) -> None:
        """Broadcast the configured wake beacon next to the connectable advertisement.

        Experimental and strictly optional: a failure is logged and otherwise ignored.
        """
        beacon = tvdata.wake_beacon(self._cfg.wake_beacon, self._cfg.wake_beacon_data, self._cfg.tv_address)
        if beacon is None or not self._adapter_path:
            return
        try:
            await self._beacon.start(self._adapter_path, *beacon, timeout=timeout)
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
                return _Problem("not_paired", "pairing mode is closing", transient=True, lasting=False)
            return _Problem("not_paired", "pairing mode is open; finish or cancel pairing first", lasting=False)
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

    def _direct_delivery(self) -> str:
        """What ``bluetooth.raw_notify`` comes to on this kernel: ``off``, ``needed`` or ``always``.

        ``needed`` is "when the TV has not subscribed".  ``auto`` means that only where the
        injected packets leave no trace (Linux 6.17 and later, see rawhci.py): on an older
        kernel the repair this module does afterwards is derived from source and unverified,
        and what it would have to prevent - a stalled transmit queue for the user's game
        controllers - is far worse, and far harder to trace back to us, than a TV that did
        not turn on.  ``on`` is the user saying they want it anyway.
        """
        mode = self._cfg.raw_notify
        if mode == "always":
            return "always"
        if mode == "on" or (mode == "auto" and self._raw_is_free):
            return "needed"
        return "off"

    def _not_subscribed(self, *, this_key: bool = False) -> str:
        """The ``not_subscribed`` message; ``this_key`` when other keys do reach the TV."""
        suffix = NOT_SUBSCRIBED_THIS_KEY if this_key else ""
        if self._cfg.raw_notify == "auto" and not self._raw_is_free:
            if rawhci.kernel_version(self._kernel) is None:
                kernel = f"its version, {self._kernel!r}, could not be read"
            else:
                kernel = f"Linux {self._kernel}"
            return NOT_SUBSCRIBED_OLD_KERNEL.format(kernel=kernel) + suffix
        return NOT_SUBSCRIBED + suffix

    def _resubscribe_wait(self) -> float:
        """How long after the link came up the TV's subscriptions are waited for.

        The configured time, for the first of them and - a host writes them one after another -
        for the rest.  Shorter in two cases where nothing more is to be expected: a TV that
        is on record as never re-subscribing, and one that stopped after some of its
        subscriptions and was given the whole time once already (the link went down between
        its writes, say; on later links it writes none).  The second is not kept across
        registrations and ends with the next subscription that does arrive.
        """
        full = float(self._cfg.resubscribe_wait)
        if self._store.resubscribes(self._cfg.tv_address) is False or self._partial_waited:
            return min(full, KNOWN_SILENT_WAIT)
        return full

    def _note_silent(self) -> None:
        """Remember that this TV does not re-subscribe, so later boots need not wait as long.

        Only a TV that renewed none of its subscriptions: one that renewed some does
        re-subscribe, and is given its full time again after the next boot.
        """
        assert self._app is not None
        if not self._app.subscribed and not self._paired_this_registration:
            self._store.set_resubscribes(self._cfg.tv_address, False)

    def _note_partial(self) -> None:
        """The time allowed for the TV's subscriptions is over and only some have arrived."""
        assert self._app is not None
        if not self._app.subscribed or self._app.fully_subscribed or self._partial_waited:
            return
        if self._direct_delivery() == "always":
            return  # nothing was waited for, and nothing is handed to bluetoothd
        self._partial_waited = True
        missing = [char.path.rsplit("/", 1)[-1] for char in self._app.reports.values() if not char.notifying]
        log.warning(
            "the TV subscribed to some key reports only (missing: %s); BlueZ would drop the keys of the others",
            ", ".join(missing),
        )

    def _bluez_ready_at(self) -> float:
        settle = self._cfg.settle_ms / 1000.0
        if self._subscribed_at is not None and self._subscribed_at > self._connected_at:
            return self._subscribed_at + min(settle, POST_SUBSCRIBE_SETTLE)
        return self._connected_at + settle

    def _set_ready(self, delivery: str) -> None:
        self._ready, self._delivery = True, delivery
        self._error = self._pairing_note = ""
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
        if self._raw.repairs and not self._debt_noted:
            # On record before the first packet goes out: if the service stops while this link
            # is up, the next start still knows that the kernel's counters need a repair.
            self._debt_noted = True
            self._store.set_raw_debt(self._boot)

    async def _power_on(self, deadline: float) -> None:
        log.info("turning the Bluetooth adapter on")
        try:
            await dbus.set_property(
                self._bus, BLUEZ, self._adapter_path, ADAPTER, "Powered", Variant("b", True),
                timeout=self._call_budget(deadline),
            )  # fmt: skip
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
        it off; raises TransportError when waiting is pointless.  ``deadline`` bounds every
        wait and every D-Bus call in here.
        """
        assert self._app is not None
        generation = self._abort
        self._parked = False
        # Counted before the first await: from here on the background policy must stay out, or
        # a BlueZ signal during the hand-over would start a second caller that fights us over
        # the advertisement (ours is the fast one).
        self._ensure_active += 1
        tried_power = beacon_tried = False
        try:
            await self._cancel_task("_reconnect_task")
            while True:
                seen = self._wakeup.generation  # before looking at anything, see _Wakeup
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
                        await self._wakeup.wait(min(remaining, grace), seen)
                        continue
                    if not problem.lasting:
                        raise TransportError(problem.code, problem.message)
                    raise self._fail(problem.code, problem.message)

                if self._closing():
                    # A link we asked bluetoothd to drop is not worth sending on.
                    if remaining <= 0:
                        return False
                    await self._wakeup.wait(min(remaining, 0.5), seen)
                    continue

                if not self._connected:
                    if remaining <= 0:
                        self._error = "the TV did not connect over Bluetooth"
                        return False
                    pause = self._advert_pause()
                    if pause > 0 or remaining < MIN_ADVERT_BUDGET:
                        # Not yet (bluetoothd has just released or refused the advertisement) or
                        # not any more (too late for the TV to answer).  A TV that answered the
                        # last advertising event may still connect meanwhile.
                        await self._wakeup.wait(min(pause, remaining) if pause > 0 else remaining, seen)
                        continue
                    try:
                        await self._advertise(discoverable=False, fast=True, timeout=min(CALL_TIMEOUT, remaining))
                    except TimeoutError:
                        if remaining >= CALL_TIMEOUT:
                            raise  # bluetoothd had all the time a call ever gets
                        self._error = "Bluetooth was too slow to call the TV in the time allowed"
                        return False
                    if wake and not beacon_tried:
                        beacon_tried = True
                        await self._start_beacon(self._call_budget(deadline))
                    await self._wakeup.wait(deadline - time.monotonic(), seen)
                    continue

                if self._advertiser.active or self._beacon.active:
                    await self._advertiser.stop(timeout=self._call_budget(deadline))
                    await self._beacon.stop(timeout=self._call_budget(deadline))
                    continue

                since = now - self._connected_at
                settle = self._cfg.settle_ms / 1000.0
                mode = self._direct_delivery()
                if self._app.fully_subscribed and mode != "always":
                    ready_at = self._bluez_ready_at()
                    if now >= ready_at:
                        self._raw.detach()
                        self._set_ready("bluez")
                        return True
                    wait = ready_at - now
                elif mode != "always" and since < self._resubscribe_wait():
                    # Android 14+ needs a few seconds to rediscover us and subscribe again, and
                    # writes its three subscriptions one after another: bluetoothd forwards a
                    # report only once its own has arrived, so some is not enough.
                    wait = self._resubscribe_wait() - since
                elif mode == "off" and not self._app.subscribed:
                    # The link stays as it is (no bounce: nothing would use the second link),
                    # and what was learnt about the TV is kept all the same - the next boot
                    # then reports this after a moment instead of after the full wait.
                    self._note_silent()
                    raise self._fail("not_subscribed", self._not_subscribed())
                elif mode == "off":
                    # Some reports are subscribed and nothing can be done for the others: the
                    # keys of the first kind are sent, each of the others fails with
                    # not_subscribed when it is due (_deliver).
                    self._note_partial()
                    ready_at = self._bluez_ready_at()
                    if now >= ready_at:
                        self._set_ready("bluez")
                        return True
                    wait = ready_at - now
                elif since < settle:
                    # Also before a bounce: the Service Changed indication must have reached the
                    # TV on this link, or it would simply arrive on the next one.
                    wait = settle - since
                elif self._first_link():
                    # First link since the registration: see the module docstring.
                    self._note_silent()
                    self._note_partial()
                    if remaining <= 0:
                        self._error = "the TV connected but was not ready for keys in time"
                        return False
                    log.info(
                        "the TV did not subscribe%s; reconnecting it once before using direct delivery",
                        " to every key report" if self._app.subscribed else "",
                    )
                    self.bounces += 1
                    await self._request_disconnect(min(deadline, now + DISCONNECT_WAIT))
                    continue
                else:
                    self._note_silent()
                    self._note_partial()
                    self._attach_raw()
                    self._set_ready("raw")
                    return True

                if remaining <= 0:
                    self._error = "the TV connected but was not ready for keys in time"
                    return False
                await self._wakeup.wait(min(wait, remaining), seen)
        finally:
            await self._leave_ensure()

    async def _leave_ensure(self) -> None:
        """Take down what ensure_ready put on air and hand over to the background policy."""
        self._last_activity = time.monotonic()  # the idle time-out counts from the end of the wait
        if not (self._beacon.active or self._advertiser.active):
            self._ensure_active -= 1
            self._call_in_background()
            return
        # In a task of its own: it completes whatever happens to our caller (a second
        # cancellation included), and a slow bluetoothd does not keep the caller much longer.
        cleanup = self._spawn(self._ensure_cleanup())
        await asyncio.wait([cleanup], timeout=CLEANUP_GRACE)

    async def _ensure_cleanup(self) -> None:
        try:
            # Only if nobody else waits for the TV by now: after a cancellation this may run
            # while the next ensure_ready is already on air with the same advertisement.
            if self._ensure_active == 1 and self._beacon.active:
                await self._beacon.stop()
            if self._ensure_active == 1 and self._advertiser.active and self._pairing is None:
                await self._advertiser.stop()
        finally:
            # Counted until here so that the background policy cannot go on air in between and
            # have its advertisement taken down by us.  Whoever called us off parked the link
            # or opened pairing; otherwise that policy takes over now.
            self._ensure_active -= 1
            self._call_in_background()

    async def _request_disconnect(self, until: float) -> None:
        """Ask bluetoothd to drop the TV link and wait (until ``until``) for it to happen."""
        path = self._connected_path
        if not path:
            return
        self._release_held()
        self._closing_at = time.monotonic()
        self._ready, self._delivery = False, ""
        self._raw.detach()
        try:
            await dbus.call(
                self._bus, BLUEZ, path, DEVICE, "Disconnect", timeout=max(MIN_CALL_TIMEOUT, until - time.monotonic())
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

    async def _kernel_disconnect(self) -> bool:
        """Have the kernel end the TV link now (mgmt.py).  True if it has taken the request.

        Never raises anything but a cancellation, and even then the request is made first:
        a link the kernel ends by itself at suspend is ended with the wrong reason.
        """
        path = self._connected_path
        self._release_held()
        # From here on the link is on its way out, as in _request_disconnect: nothing more is
        # sent on it and a waiting ensure_ready does not take it for a link to use.
        self._closing_at = time.monotonic()
        self._ready, self._delivery = False, ""
        self._raw.detach()
        # A report handed to bluetoothd or the controller a moment ago - the release above,
        # the key-up of SLEEP when the caller ran out of time - must be on the air before the
        # link goes, or the TV never sees the key come up (it sleeps on key-up).
        pause = self._last_report_at + REPORT_FLUSH - time.monotonic()
        try:
            if pause > 0:
                await asyncio.sleep(pause)
        finally:
            asked = self._ask_kernel_to_disconnect(path)
        return asked

    def _ask_kernel_to_disconnect(self, path: str) -> bool:
        if not self._connected or self._connected_path != path or not self._adapter_path:
            return False
        props = self._watcher.interface(path, DEVICE) or {}
        try:
            self._closer.request(
                rawhci.dev_id_from_adapter(self._adapter_path), self._cfg.tv_address, str(props.get("AddressType", ""))
            )
        except (MgmtError, RawError) as exc:
            log.info("could not end the TV link through the kernel (%s); asking BlueZ, which takes two seconds", exc)
            return False
        return True

    async def _kernel_disconnect_took(self) -> bool:
        """Watch a link the kernel was asked to end.  False if the kernel refused after all."""
        until = time.monotonic() + FAST_DISCONNECT_WAIT
        while self._connected:
            status = self._closer.status()
            if status == 0:
                break  # down in the kernel; bluetoothd's word follows
            if status is not None:
                log.info("the kernel did not end the TV link (status 0x%02X); asking BlueZ instead", status)
                self._closing_at = None
                return False
            remaining = until - time.monotonic()
            if remaining <= 0:
                break  # the kernel has the request; no need to watch it being carried out
            await self._wakeup.wait(min(FAST_DISCONNECT_POLL, remaining))
        return True

    # ================================================================== key delivery

    def _deliver(self, report_id: int, payload: bytes) -> None:
        """Hand one input report to the TV through exactly one path.

        The path is chosen per report: while direct delivery is in use, a report whose
        characteristic the TV has subscribed to meanwhile goes through bluetoothd, the others
        stay direct until their subscriptions (three separate writes by the TV) have arrived.

        Never to bluetoothd for a characteristic without a subscription: it would drop the
        report without a word and the key would count as sent.  Where direct delivery is not
        in use such a key fails with ``not_subscribed``; the link stays ready, because the
        keys of the subscribed reports do arrive.
        """
        assert self._app is not None
        char = self._app.reports[report_id]
        if self._delivery != "raw" and not char.notifying:
            raise self._fail("not_subscribed", self._not_subscribed(this_key=True))
        self._last_report_at = time.monotonic()
        if self._delivery == "raw" and (self._cfg.raw_notify == "always" or not char.notifying):
            handle, _source = self._app.report_value_handle(report_id)
            try:
                self._raw.notify(handle, payload)
            except (RawError, ValueError) as exc:
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
        if self._connected and self._cfg.idle_disconnect_seconds > 0 and not self._stopped:
            self._idle_task = asyncio.ensure_future(self._idle_watch())

    def _link_in_use(self) -> bool:
        """Somebody is sending on the link, waiting for it, pairing over it or closing it."""
        return bool(
            self._ensure_active
            or self._send_lock.locked()
            or self._pairing is not None
            or self._pair_opening
            or self._closing()
        )

    async def _idle_watch(self) -> None:
        """``idle_disconnect_seconds``: drop a link that has carried no key for that long.

        That goes for every link, however it came up: one that was used, and equally one the
        TV opened because it was called in the background (after boot, a wake or a drop) or
        while pairing, on which nothing was sent at all.  The time counts from the last key,
        or from the moment the link came up or was last waited for.
        """
        while self._connected:
            idle = self._cfg.idle_disconnect_seconds
            if idle <= 0:
                return
            now = time.monotonic()
            if self._link_in_use():
                self._last_activity = now
            due = max(self._last_activity, self._connected_at) + idle - now
            if due <= 0:
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
                self._partial_waited = False
                log.info("the TV's subscription to key reports ended")
            if self._ready and self._delivery == "bluez":
                # bluetoothd drops this report from now on (all of ours, once the last
                # subscription is gone).  Not ready any more, so that the next ensure_ready
                # looks again (direct delivery, or an honest error) instead of sending keys
                # into the void.  A key that is down comes up first: its own report still
                # gets through unless it is the one this is about, and send_key, which finds
                # the link not ready when the time is up, would leave it down on the TV.
                self._release_held(HOLD_SUBSCRIPTION_ENDED)
                self._ready, self._delivery = False, ""
            self._wakeup.fire()
            return
        # The last one counts (_bluez_ready_at), and whatever was concluded from the others
        # staying away is open again.
        self._subscribed_at = time.monotonic()
        self._partial_waited = False
        if self._app.fully_subscribed:
            log.info("the TV subscribed to key reports")
            if self._ready:
                self._error = ""  # "this key is not among them" (_deliver) no longer holds
        window = self._pairing
        if window is not None:
            window.notified = True
            self._pairing_progress()
        elif not self._paired_this_registration and self._tv_path:
            # A subscription that is not part of pairing: this TV renews it by itself.
            self._store.set_resubscribes(self._cfg.tv_address, True)
        if self._delivery == "raw" and self._cfg.raw_notify != "always":
            # From here on bluetoothd forwards this report (see _deliver, which never uses both
            # paths for one report).  Direct delivery is given up once all three are subscribed.
            if all(report.notifying for report in self._app.reports.values()):
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
        devices = self._watcher.devices(window.adapter_path)
        for path, props in devices.items():
            # Keep track of the bonds this window is responsible for (the device our agent said
            # yes to, or a new one that uses our HID service).  If the window ends without
            # adopting the TV its bond must go again, or BlueZ would refuse that TV's next
            # attempt without asking anybody (JustWorksRepairing = never).
            if path in window.known or not (props.get("Paired") or props.get("Bonded")):
                continue
            address = str(props.get("Address", "")).upper()
            if address and (path == window.candidate or path in window.touched) and window.bonded.get(path) != address:
                window.bonded[path] = address
                self._store.add_pending(address)
        eligible = [
            path
            for path, props in sorted(devices.items())
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
        # Assumed registered from the moment the request leaves: if we are cancelled before the
        # answer, the clean-up must still unregister what bluetoothd may have accepted.
        self._agent_registered = True
        try:
            await dbus.call(
                self._bus, BLUEZ, BLUEZ_ROOT, AGENT_MANAGER, "RegisterAgent", "os", [AGENT_PATH, AGENT_CAPABILITY],
                timeout=CALL_TIMEOUT,
            )  # fmt: skip
        except DBusError as exc:
            if exc.type != ERR_ALREADY_EXISTS:
                self._agent_registered = False
                raise
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

    async def _end_pairing(self) -> bool:
        """Close the pairing window, if there is one, and wait until everything is put back.

        The caller holds ``_op_lock``.  The loop goes first: stopped at whatever it is doing,
        it cannot re-assert the agent role, Pairable or the advertisement after the clean-up.
        A loop that is just storing a TV that paired is left to finish instead.
        """
        window, task = self._pairing, self._pairing_task
        self._pairing_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            if window is None or not window.winner:
                task.cancel()
            await asyncio.wait([task])
        if window is not None:
            await self._close_pairing(window)
        return window is not None

    def _check_classic_visibility(self, window: _PairingWindow) -> None:
        """Warn while the adapter itself is discoverable; never change that setting.

        bluetoothd derives our advertisement's Flags from the adapter (advertising.c
        set_flags): only while the adapter is not discoverable do they say "BR/EDR not
        supported".  Without that, and with classic inquiry scan on, a TV may pair over classic
        Bluetooth, find no HID service there and give up.
        """
        props = self._watcher.interface(window.adapter_path, ADAPTER) or {}
        if props.get("Discoverable"):
            if not window.classic_visible:
                log.warning("pairing: %s", CLASSIC_VISIBLE)
            window.classic_visible = True
            self._pairing_note = CLASSIC_VISIBLE
        elif self._pairing_note == CLASSIC_VISIBLE:
            self._pairing_note = ""

    async def _open_pairing(self, timeout: float, token: int) -> None:
        def called_off() -> bool:
            return token != self._pair_cancel or self._stopped

        tried_power = False
        while True:
            if called_off():
                return
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
        # ever asking our agent (JustWorksRepairing = never), so it goes first.  That includes
        # the TV this service used last if the configuration no longer names it (settings
        # reset or edited by hand): nothing else would ever remove that bond.
        self._release_held()
        for address in dict.fromkeys((self._cfg.tv_address.upper(), self._store.tv)):
            if address and not called_off():
                self._closing_at = time.monotonic() if self._connected else None
                await self._remove_device(address)
        await self._remove_stale()
        if called_off():
            return  # the user changed their mind while the old bond was being removed

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
        self._pairing_note = ""
        try:
            await self._register_agent()
            if not called_off() and not props.get("Pairable", False):
                window.prev_pairable = False
                await self._set_pairable(adapter, True)
            if not called_off():
                await self._advertise(discoverable=True, fast=True)
        except BaseException:
            await self._undo_pairing(window)
            raise
        if called_off() or window.closed:
            await self._undo_pairing(window)
            return
        self._error = ""
        self._check_classic_visibility(window)
        log.info("pairing mode open for %gs as %r", timeout, self._cfg.device_name)
        self._pairing_task = asyncio.ensure_future(self._pairing_loop(window))
        self._wakeup.fire()

    async def _undo_pairing(self, window: _PairingWindow) -> None:
        """Back out of a window that was still being set up."""
        await self._close_pairing(window)
        # Once more, directly: somebody else (stop) may have run the clean-up before our last
        # step, which would then be left standing.  Every part of it is safe to repeat.
        await self._restore_after_pairing(window)

    async def _pairing_loop(self, window: _PairingWindow) -> None:
        try:
            while self._pairing is window and not window.closed:
                seen = self._wakeup.generation  # before looking at anything, see _Wakeup
                if window.aborted:
                    self._pairing_note = f"pairing was interrupted: {window.aborted}"
                    log.warning("%s", self._pairing_note)
                    return
                if window.winner:
                    await self._complete_pairing(window)
                    return
                remaining = window.deadline - time.monotonic()
                if remaining <= 0:
                    self._pairing_note = "pairing mode timed out before a TV paired"
                    if window.classic_visible:
                        self._pairing_note += f" ({CLASSIC_VISIBLE})"
                    log.info("%s", self._pairing_note)
                    return
                try:
                    await self._hold_pairing_conditions(window)
                except (DBusError, TimeoutError, OSError) as exc:
                    log.debug("pairing: could not re-assert pairing conditions: %s", _describe(exc))
                await self._wakeup.wait(min(remaining, PAIRING_TICK), seen)
        finally:
            await self._close_pairing(window)

    async def _hold_pairing_conditions(self, window: _PairingWindow) -> None:
        """Other software (or a timeout) may take the default agent or pairability away."""
        self._check_classic_visibility(window)
        if not self._agent_registered:
            await self._register_agent()
        else:
            await self._request_default_agent()
        props = self._watcher.interface(window.adapter_path, ADAPTER) or {}
        if not window.closed and not props.get("Pairable", True):
            await self._set_pairable(window.adapter_path, True)
        if not window.closed and not self._advertiser.active and self._advert_pause() <= 0:
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
            self._pairing_note = "pairing finished but BlueZ did not report the TV's address"
            log.error("%s", self._pairing_note)
            return
        # Cleaned here, where it comes in: this is what the settings, the state and every log
        # line (ours and the service's) get.
        name = _clean_name(str(props.get("Name") or ""))
        alias = str(props.get("Alias") or "")
        if not name and alias.replace("-", ":").upper() != address:
            name = _clean_name(alias)

        self._cfg = dataclasses.replace(self._cfg, tv_address=address, tv_name=name)
        window.adopted = address
        self._paired_this_registration = True
        self._parked = False
        self._store.set_resubscribes(address, None)
        self._store.discard_stale(address)
        self._store.discard_pending(address)
        self._error = self._pairing_note = ""
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
            prev_pairable, window.prev_pairable = window.prev_pairable, None
            if prev_pairable is not None and window.adapter_path == self._adapter_path:
                try:
                    await self._set_pairable(window.adapter_path, prev_pairable)
                except (DBusError, TimeoutError, OSError) as exc:
                    log.debug("could not restore Pairable: %s", _describe(exc))
            await self._drop_unadopted_bonds(window)
        finally:
            if self._pairing is window:
                self._pairing = None
            self._last_activity = time.monotonic()  # for the idle time-out, pairing was use
            if self._pairing_note == CLASSIC_VISIBLE:
                # That was a warning about the open window.  How a window ended is a note of
                # its own, and a window the user closed leaves none.
                self._pairing_note = ""
            self._call_in_background()
            self._wakeup.fire()

    async def _drop_unadopted_bonds(self, window: _PairingWindow) -> None:
        """Remove the bond of a TV that paired in this window without being adopted.

        Such a bond is known to nobody: the settings do not name the TV, so neither the next
        pairing nor forget() would remove it, while BlueZ refuses that TV's next attempt
        (JustWorksRepairing = never).  Done while the window still counts as open so that a
        new window cannot mistake the dying device for one that was bonded all along.
        """
        leftovers = [address for address in dict.fromkeys(window.bonded.values()) if address != window.adopted]
        window.bonded.clear()
        for address in leftovers:
            try:
                removed = await self._remove_device(address)
            except (DBusError, TimeoutError, OSError) as exc:
                log.debug("could not remove the unfinished bond with %s: %s", address, _describe(exc))
                removed = False
            if removed:
                log.info("removed the bond of %s, which paired without completing", address)
            else:
                self._store.add_stale(address)  # BlueZ cannot be asked now; _remove_stale will
            self._store.discard_pending(address)

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

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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
