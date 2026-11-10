"""One short session on the command port (TCP 6466).

The TV speaks first and the client only reacts: it echoes ``remote_configure`` with the
features it wants, echoes ``remote_set_active``, answers every ping, and takes
``remote_start`` as the sign that commands are accepted (its flag tells whether the TV is
on).  Then one command is sent.  Nothing is acknowledged, so the session lingers a moment,
still answering pings, to let the TV act before the connection is closed.

How a failure is reported matters to the caller, which retries only ``wifi_unreachable``:

- no connection, or no complete handshake in time: ``wifi_unreachable``;
- the TV dropping the connection as the command is written or during the linger: also
  ``wifi_unreachable``, because that is the only sign there can be that the command went
  nowhere (every command this is used for can safely be sent again);
- turned away before the TV said anything (TLS alert, reset or plain close: the ways a TV
  rejects a certificate it does not know), or a certificate other than the paired one:
  ``wifi_not_paired``;
- nonsense on the wire, or an error reported by the TV: ``wifi_error``;
- an address that cannot name a host: ``wifi_not_configured``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import ssl
from dataclasses import dataclass

from ..interfaces import TransportError
from . import certs, messages
from .channel import Channel, ChannelClosed, InvalidAddress
from .proto import ProtoError
from .util import clean_name, describe

log = logging.getLogger(__name__)

LINGER_SECONDS = 0.7
CLOSE_RESERVE = 0.25  # part of the time budget kept for closing the connection properly
# How long the TV gets to answer our TLS goodbye.  Its answer changes nothing (the work is
# done and the goodbye is sent), so a TV that does not bother must not cost the caller time.
CLOSE_SECONDS = 0.1
MAX_MESSAGES = 256  # a real session has about ten


@dataclass
class TvInfo:
    """What the TV said about itself during the handshake."""

    started: bool = False  # the TV is on (not in standby)
    model: str = ""
    vendor: str = ""
    features: int = 0  # the feature bits the TV offers


def _unreachable(message: str) -> TransportError:
    return TransportError("wifi_unreachable", message)


def _not_paired(host: str) -> TransportError:
    return TransportError("wifi_not_paired", f"the TV at {host} does not accept this machine; pair the Wi-Fi assist")


def _garbled(host: str, exc: Exception) -> TransportError:
    log.debug("bad data from %s: %s", host, exc)
    return TransportError("wifi_error", f"the TV at {host} sent data this program does not understand")


def connect_error(host: str, exc: BaseException) -> TransportError:
    """Classify a failure to connect or to complete the TLS handshake."""
    if isinstance(exc, TimeoutError):
        return _unreachable(f"the TV at {host} did not answer")
    if isinstance(exc, ssl.SSLError):
        log.debug("TLS handshake with %s failed: %s", host, exc)
        return _not_paired(host)
    if isinstance(exc, ConnectionRefusedError):
        return _unreachable(f"the TV at {host} refused the connection; it may be off or still starting")
    if isinstance(exc, ConnectionError):
        # The TCP connection existed and was cut during the TLS handshake.
        log.debug("connection to %s dropped during the TLS handshake: %r", host, exc)
        return _not_paired(host)
    return _unreachable(f"cannot reach the TV at {host} ({describe(exc)})")


def session_error(host: str, exc: BaseException, *, heard_from_tv: bool) -> TransportError:
    """Classify a failure after the TLS handshake and before the session is ready."""
    if isinstance(exc, TimeoutError):
        return _unreachable(f"the TV at {host} did not answer in time")
    if isinstance(exc, ProtoError):
        return _garbled(host, exc)
    if not heard_from_tv:
        # With TLS 1.3 our handshake completes before the TV has judged our certificate, so
        # its refusal only shows on the first read.
        log.debug("%s closed the connection before saying anything: %r", host, exc)
        return _not_paired(host)
    return _unreachable(f"the TV at {host} closed the connection")


async def oneshot(
    host: str,
    port: int,
    context: ssl.SSLContext,
    *,
    fingerprint: str,
    command: bytes | None = None,
    timeout: float,
    linger: float = LINGER_SECONDS,
) -> TvInfo:
    """Connect, shake hands, optionally send one command, close.

    ``fingerprint`` is the SHA-256 of the certificate the TV must present.  ``timeout``
    bounds the whole call, including the linger after a command and the close.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    work_deadline = deadline - min(CLOSE_RESERVE, timeout / 4)
    try:
        channel = await Channel.connect(host, port, context, deadline=work_deadline)
    except InvalidAddress:
        raise TransportError("wifi_not_configured", "the TV address is not valid") from None
    except OSError as exc:  # TimeoutError and TLS errors included
        raise connect_error(host, exc) from None
    try:
        if not hmac.compare_digest(certs.fingerprint(channel.peer_certificate), fingerprint):
            raise TransportError(
                "wifi_not_paired",
                f"the TV at {host} is not the one that was paired (its identity changed); pair the Wi-Fi assist again",
            )
        session = _Session(channel, host)
        await session.handshake(work_deadline)
        if command is not None:
            await session.send_command(command, work_deadline)
            await session.linger(min(loop.time() + linger, work_deadline))
        await channel.close(min(CLOSE_SECONDS, deadline - loop.time()))
    except BaseException:
        channel.abort()
        raise
    return session.info


class _Session:
    def __init__(self, channel: Channel, host: str):
        self._channel = channel
        self._host = host
        self._features = messages.WANTED_FEATURES
        self._messages = 0
        self._ready = False
        self.info = TvInfo()

    async def handshake(self, deadline: float) -> None:
        """React to the TV until it sends ``remote_start``."""
        while not self._ready:
            try:
                frame = await self._channel.receive(deadline)
                await self._handle(frame, deadline)
            except (TimeoutError, ProtoError, ChannelClosed, OSError) as exc:
                raise session_error(self._host, exc, heard_from_tv=self._messages > 0) from None

    async def send_command(self, command: bytes, deadline: float) -> None:
        try:
            await self._channel.send(command, deadline)
        except (TimeoutError, ChannelClosed, OSError) as exc:
            # ChannelClosed: the TV hung up at remote_start and the command was never written.
            raise session_error(self._host, exc, heard_from_tv=True) from None

    async def linger(self, until: float) -> None:
        """Keep the session alive until ``until`` so the TV can act on what was sent.

        A TV keeps a session for as long as the client does.  One that drops it now may have
        done so before it read the command (the write and the hang-up crossed), and nothing
        else would ever tell: the caller is asked to try again.
        """
        host = self._host
        try:
            while True:
                await self._handle(await self._channel.receive(until), until)
        except TimeoutError:
            pass
        except ProtoError as exc:
            # The command is out and the TV is still there; what it babbles afterwards changes nothing.
            log.debug("bad data from %s after the command: %s", self._host, exc)
        except (ChannelClosed, OSError) as exc:
            log.info("the TV at %s closed the connection right after the command; it may not have acted on it", host)
            log.debug("how the session with %s ended: %r", host, exc)
            raise _unreachable(f"the TV at {host} closed the connection as the command was sent") from None

    async def _handle(self, frame: bytes, deadline: float) -> None:
        self._messages += 1
        if self._messages > MAX_MESSAGES:
            raise TransportError("wifi_error", f"the TV at {self._host} keeps sending data instead of getting ready")
        message = messages.decode_remote(frame)
        if "remote_configure" in message:
            configure = message["remote_configure"]
            device = configure.get("device_info")
            if device is not None:
                self.info.model = clean_name(device["model"])
                self.info.vendor = clean_name(device["vendor"])
            self.info.features = configure["code1"]
            self._features = messages.WANTED_FEATURES & configure["code1"]
            log.debug("%s offers features %#x; using %#x", self._host, configure["code1"], self._features)
            await self._channel.send(messages.remote_configure(self._features), deadline)
        elif "remote_set_active" in message:
            await self._channel.send(messages.remote_set_active(self._features), deadline)
        elif "remote_ping_request" in message:
            await self._channel.send(messages.ping_response(message["remote_ping_request"]["val1"]), deadline)
        elif "remote_start" in message:
            self.info.started = message["remote_start"]["started"]
            self._ready = True
            log.debug("%s is ready (%s)", self._host, "on" if self.info.started else "in standby")
        elif "remote_error" in message:
            raise TransportError("wifi_error", f"the TV at {self._host} reported an error instead of doing as asked")
        else:
            log.debug("%s sent a message this client does not use (%d bytes)", self._host, len(frame))
