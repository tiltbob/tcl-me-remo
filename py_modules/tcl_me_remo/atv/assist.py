"""AtvAssist: the Wi-Fi assist as the service sees it (``interfaces.WifiAssist``).

Commands are one-shot: each opens a connection, completes the handshake, does its work and
closes.  There is no background connection and no retrying here; the service decides when
to try again, based on the error code.

Pairing is the one thing with state: ``start_pairing`` leaves a connection open while the
user reads the code from the TV, for at most ``hold_seconds``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from ..interfaces import TransportError
from . import certs, discovery, messages, remote
from .pairing import PairingSession
from .store import PairingRecord, Store
from .util import describe

log = logging.getLogger(__name__)

PAIRING_START_SECONDS = 15.0  # connect and the three rounds before the code appears
PAIRING_HOLD_SECONDS = 120.0  # how long the TV's code may wait for the user
PAIRING_FINISH_SECONDS = 10.0  # the TV answers the secret by itself, no human involved
SETTLE_SECONDS = 1.0  # a TV needs a moment after pairing before it accepts commands

_MAX_HOST = 253
_MAX_LINK = 2048
_MAX_KEYCODE = 0xFFFF


class AtvAssist:
    def __init__(
        self,
        state_dir: Path,
        *,
        client_name: str = "tcl-me-remo",
        pair_port: int = 6467,
        remote_port: int = 6466,
        openssl: str = "openssl",
    ):
        self._store = Store(Path(state_dir))
        self._client_name = client_name
        self._pair_port = pair_port
        self._remote_port = remote_port
        self._openssl = openssl

        # Timings; attributes so that tests (and a hardware session) can adjust them.
        self.start_seconds = PAIRING_START_SECONDS
        self.hold_seconds = PAIRING_HOLD_SECONDS
        self.finish_seconds = PAIRING_FINISH_SECONDS
        self.settle_seconds = SETTLE_SECONDS
        self.linger_seconds = remote.LINGER_SECONDS

        self._identity: certs.Identity | None = None  # loaded on first use
        self._record: PairingRecord | None = None
        self._has_identity = False
        self._session: PairingSession | None = None
        self._hold_task: asyncio.Task[None] | None = None
        self._hold_deadline = 0.0
        self._settle_until = 0.0
        self._pairing_lock = asyncio.Lock()
        try:
            self._has_identity = self._store.has_identity()
            self._record = self._store.read_record()
        except OSError as exc:
            log.warning("cannot read the Wi-Fi assist state in %s (%s)", self._store.directory, exc)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        session = self._session
        return {
            "paired": self._has_identity and self._record is not None,
            "pairing": session is not None,
            "pairing_host": session.host if session is not None else "",
        }

    async def discover(self, timeout: float) -> list[dict[str, Any]]:
        try:
            return await discovery.discover(timeout)
        except Exception as exc:  # noqa: BLE001 - discovery is a convenience and must never fail a request
            log.debug("discovery failed: %r", exc)
            return []

    # ------------------------------------------------------------------ pairing

    async def start_pairing(self, host: str) -> None:
        host = _checked_host(host)
        async with self._pairing_lock:
            await self._drop_session()
            identity = await self._identity_for_pairing()
            session = await PairingSession.start(
                host, self._pair_port, identity, client_name=self._client_name, timeout=self.start_seconds
            )
            self._session = session
            self._hold_deadline = asyncio.get_running_loop().time() + self.hold_seconds
            self._hold_task = asyncio.create_task(self._hold(session))
            log.info("pairing with the TV at %s: it now shows a code", host)

    async def finish_pairing(self, code: str) -> None:
        async with self._pairing_lock:
            session = self._session
            await self._stop_holding()
            if session is None or self._session is not session:
                raise TransportError("pairing_failed", "pairing is not in progress; start pairing first")
            try:
                await session.finish(str(code), timeout=self.finish_seconds)
            except BaseException as exc:
                if session.usable and isinstance(exc, TransportError):
                    self._hold_task = asyncio.create_task(self._hold(session))  # the user may try again
                else:
                    session.abort()
                    self._session = None
                raise
            self._session = None
            record = PairingRecord(host=session.host, fingerprint=session.tv_fingerprint, name=session.tv_name)
            try:
                await asyncio.to_thread(self._store.write_record, record)
            except OSError as exc:
                raise TransportError(
                    "wifi_error", f"the TV accepted the code but the pairing could not be saved ({describe(exc)})"
                ) from None
            self._record = record
            self._settle_until = asyncio.get_running_loop().time() + self.settle_seconds
            log.info("paired with %s at %s over Wi-Fi", record.name or "the TV", record.host)

    async def cancel_pairing(self) -> None:
        async with self._pairing_lock:
            await self._drop_session()

    async def unpair(self) -> None:
        async with self._pairing_lock:
            await self._drop_session()
            self._identity = None
            self._record = None
            self._has_identity = False
            try:
                await asyncio.to_thread(self._store.remove)
            except OSError as exc:
                raise TransportError("wifi_error", f"could not remove the Wi-Fi pairing ({describe(exc)})") from None
            log.info("Wi-Fi pairing removed")

    async def close(self) -> None:
        """Drop a pending pairing connection.  Nothing else is kept open."""
        await self.cancel_pairing()

    async def _identity_for_pairing(self) -> certs.Identity:
        """Our identity, created now if there is none.  An existing one is always kept."""
        try:
            created = await asyncio.to_thread(
                self._store.ensure_identity, common_name=self._client_name, openssl=self._openssl
            )
        except certs.OpensslMissing:
            raise TransportError(
                "wifi_error", f"the '{self._openssl}' program is needed to pair over Wi-Fi and was not found"
            ) from None
        except certs.CertError as exc:
            raise TransportError("wifi_error", f"could not create the key for the Wi-Fi assist: {exc}") from None
        except OSError as exc:
            raise TransportError("wifi_error", f"cannot write to {self._store.directory} ({describe(exc)})") from None
        if created:
            self._identity = None
            self._record = None
            log.info("created a new key and certificate for the Wi-Fi assist in %s", self._store.directory)
        self._has_identity = True
        try:
            return await self._load_identity()
        except FileNotFoundError:
            raise TransportError("wifi_error", "the key of the Wi-Fi assist disappeared; try again") from None

    async def _load_identity(self) -> certs.Identity:
        if self._identity is None:
            try:
                self._identity = await asyncio.to_thread(self._store.load_identity)
            except certs.CertError as exc:
                # Not replaced automatically: only the user can tell whether that is wanted.
                log.warning("the Wi-Fi assist identity in %s is unusable: %s", self._store.directory, exc)
                raise TransportError(
                    "wifi_error",
                    "the saved key of the Wi-Fi assist cannot be used; remove the Wi-Fi pairing, then pair again",
                ) from None
        return self._identity

    async def _hold(self, session: PairingSession) -> None:
        """Keep a pairing session until it is used, cancelled, dropped by the TV or too old."""
        reason = await session.wait_while_idle(self._hold_deadline)
        if self._session is session:
            self._session = None
            self._hold_task = None
            log.info("pairing with the TV at %s ended: %s", session.host, reason)
        await session.close()

    async def _stop_holding(self) -> None:
        """Take the held session over from its watcher, for the caller to use or close."""
        task, self._hold_task = self._hold_task, None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait([task])
        except BaseException:
            # The caller was cancelled before it had the session.  Nothing watches it any
            # more, so it must not stay open (and reported as pending) without a time limit.
            session, self._session = self._session, None
            if session is not None:
                session.abort()
            raise

    async def _drop_session(self) -> None:
        session = self._session
        await self._stop_holding()
        self._session = None
        if session is not None:
            await session.close()
            log.info("pairing with the TV at %s cancelled", session.host)

    # ------------------------------------------------------------------ commands

    async def probe(self, host: str, *, timeout: float) -> dict[str, Any]:
        info = await self._oneshot(host, None, timeout)
        record = self._record
        return {"on": info.started, "name": info.model or info.vendor or (record.name if record else "")}

    async def send_key(self, host: str, keycode: int, *, timeout: float) -> None:
        if isinstance(keycode, bool) or not isinstance(keycode, int) or not 0 < keycode <= _MAX_KEYCODE:
            raise TransportError("wifi_error", f"{keycode!r} is not an Android key code")
        info = await self._oneshot(host, messages.key_inject(keycode, messages.DIRECTION_SHORT), timeout)
        if not info.features & messages.FEATURE_KEY:
            log.warning("the TV at %s does not offer key presses over Wi-Fi; key %d may be ignored", host, keycode)
        log.info("sent key %d to the TV at %s over Wi-Fi", keycode, host)

    async def launch_app_link(self, host: str, link: str, *, timeout: float) -> None:
        if not isinstance(link, str) or not link.strip() or len(link.encode("utf-8")) > _MAX_LINK:
            raise TransportError("wifi_error", "the app link is empty or too long")
        info = await self._oneshot(host, messages.app_link(link.strip()), timeout)
        if not info.features & messages.FEATURE_APP_LINK:
            log.warning("the TV at %s does not offer app links over Wi-Fi; the link may be ignored", host)
        log.info("asked the TV at %s to open %s", host, link.strip())

    async def _oneshot(self, host: str, command: bytes | None, timeout: float) -> remote.TvInfo:
        loop = asyncio.get_running_loop()
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
            raise TransportError("wifi_error", "the time limit for the TV is not a number")
        deadline = loop.time() + max(float(timeout), 0.0)
        host = _checked_host(host)
        record = self._record
        if record is None or not self._has_identity:
            raise TransportError("wifi_not_paired", "the Wi-Fi assist is not paired with the TV")
        try:
            identity = await self._load_identity()
        except FileNotFoundError:
            self._has_identity = False
            raise TransportError("wifi_not_paired", "the Wi-Fi assist is not paired with the TV") from None
        settle = min(self._settle_until, deadline) - loop.time()
        if settle > 0:
            await asyncio.sleep(settle)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TransportError("wifi_unreachable", f"the TV at {host} could not be asked in the time allowed")
        return await remote.oneshot(
            host,
            self._remote_port,
            identity.context,
            fingerprint=record.fingerprint,
            command=command,
            timeout=remaining,
            linger=self.linger_seconds,
        )


def _checked_host(host: str) -> str:
    text = host.strip() if isinstance(host, str) else ""
    if not text:
        raise TransportError("wifi_not_configured", "no TV address is set for the Wi-Fi assist")
    if len(text) > _MAX_HOST or any(char.isspace() or not char.isprintable() for char in text):
        raise TransportError("wifi_not_configured", "the TV address is not valid")
    return text
