"""Pairing with the TV (TCP 6467).

Three request/reply rounds make the TV display a six character code.  The connection then
stays open while the user reads it, because the code is tied to this very TLS session; the
secret derived from the code is sent on it and the TV, on accepting, trusts our certificate
from then on.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
from typing import Any

from ..interfaces import TransportError
from . import certs, messages
from .channel import Channel, ChannelClosed, InvalidAddress
from .proto import ProtoError
from .util import clean_name, describe

log = logging.getLogger(__name__)

_MAX_SKIPPED = 8  # repeated messages and messages of unknown kinds tolerated while waiting for one reply
_MAX_IDLE_MESSAGES = 64  # messages tolerated while the user reads the code (a TV sends none)
_MESSAGE_KINDS = ("pairing_request_ack", "options", "configuration_ack", "secret_ack")  # in the order they arrive


class CodeError(ValueError):
    """The code as typed cannot be the one on the TV screen."""


def minimal_bytes(value: int) -> bytes:
    """Big-endian without leading zero bytes, the form the key numbers are hashed in."""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def pairing_secret(client: tuple[int, int], server: tuple[int, int], code: str) -> bytes:
    """The 32 bytes that prove we saw the code.

    ``client`` and ``server`` are the RSA ``(modulus, exponent)`` of the two certificates of
    the pairing connection.  The code's last two bytes are a nonce chosen by the TV; its
    first byte is the first byte of the result, which lets a typo be caught here, before
    the TV is asked (it allows one answer per code).
    """
    text = "".join(code.split()).upper()
    if len(text) != messages.CODE_LENGTH or any(char not in "0123456789ABCDEF" for char in text):
        raise CodeError("the code is six characters long and uses only the digits 0-9 and the letters A-F")
    typed = bytes.fromhex(text)
    digest = hashlib.sha256()
    for number in (*client, *server):
        digest.update(minimal_bytes(number))
    digest.update(typed[1:])
    secret = digest.digest()
    if secret[0] != typed[0]:
        raise CodeError("that is not the code on the TV screen; check it and type it again")
    return secret


def _failed(message: str) -> TransportError:
    return TransportError("pairing_failed", message)


class PairingSession:
    """A pairing connection on which the TV is showing its code."""

    def __init__(self, channel: Channel, host: str, client: tuple[int, int], server: tuple[int, int]):
        self._channel = channel
        self._client = client
        self._server = server
        self.host = host
        self.server_name = ""

    @classmethod
    async def start(
        cls, host: str, port: int, identity: certs.Identity, *, client_name: str, timeout: float
    ) -> PairingSession:
        """Connect and run the exchange up to the point where the TV displays the code."""
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            channel = await Channel.connect(host, port, identity.context, deadline=deadline)
        except InvalidAddress:
            raise TransportError("wifi_not_configured", "the TV address is not valid") from None
        except TimeoutError:
            raise TransportError(
                "wifi_unreachable", f"the TV at {host} did not answer; check that it is on and on the same network"
            ) from None
        except ssl.SSLError as exc:
            log.debug("TLS handshake with %s for pairing failed: %s", host, exc)
            raise _failed(f"the TV at {host} refused the secure connection") from None
        except ConnectionRefusedError:
            raise TransportError(
                "wifi_unreachable", f"the TV at {host} refused the connection; check that it is on and is a Google TV"
            ) from None
        except OSError as exc:
            raise TransportError("wifi_unreachable", f"cannot reach the TV at {host} ({describe(exc)})") from None
        try:
            try:
                client = certs.rsa_public_numbers(identity.certificate)
                server = certs.rsa_public_numbers(channel.peer_certificate)
            except certs.CertError as exc:
                log.debug("certificate of %s: %s", host, exc)
                raise _failed("the TV presented a certificate this program cannot pair with") from None
            session = cls(channel, host, client, server)
            ack = await session._exchange(messages.pairing_request(client_name), "pairing_request_ack", deadline)
            session.server_name = clean_name(ack["server_name"])
            await session._exchange(messages.options(), "options", deadline)
            await session._exchange(messages.configuration(), "configuration_ack", deadline)
        except BaseException:
            channel.abort()
            raise
        return session

    @property
    def usable(self) -> bool:
        """False once the connection is gone; a new pairing must be started then."""
        return not self._channel.closed

    @property
    def tv_name(self) -> str:
        return self.server_name or certs.tv_name(self._channel.peer_certificate)

    @property
    def tv_fingerprint(self) -> str:
        return certs.fingerprint(self._channel.peer_certificate)

    async def wait_while_idle(self, deadline: float) -> str:
        """Watch the connection while the user reads the code.

        Returns the reason once the session is over (the TV dropped it, or ``deadline``
        passed).  Cancel the call to go on using the session.
        """
        try:
            for _ in range(_MAX_IDLE_MESSAGES):
                reply = messages.decode_pairing(await self._channel.receive(deadline))
                if reply["status"] != messages.STATUS_OK:
                    return "the TV ended pairing"
            return "the TV kept sending data instead of waiting for the code"
        except TimeoutError:
            return "no code was entered in time"
        except ProtoError:
            return "the TV sent data this program does not understand"
        except (ChannelClosed, OSError):
            return "the TV closed the connection"

    async def finish(self, code: str, *, timeout: float) -> None:
        """Send the secret for ``code``.  Raises ``pairing_failed`` unless the TV accepts it.

        A code that cannot be right is refused without contacting the TV and leaves the
        session usable for another try; any other failure ends it.
        """
        try:
            secret = pairing_secret(self._client, self._server, code)
        except CodeError as exc:
            raise _failed(str(exc)) from None
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            ack = await self._exchange(messages.secret(secret), "secret_ack", deadline)
        except BaseException:
            self._channel.abort()
            raise
        if ack["secret"] and ack["secret"] != secret:
            log.debug("the TV's acknowledgement does not echo the secret (ignored)")
        await self._channel.close()

    async def close(self) -> None:
        await self._channel.close()

    def abort(self) -> None:
        self._channel.abort()

    async def _exchange(self, message: bytes, expect: str, deadline: float) -> dict[str, Any]:
        """Send one message and return the body of the reply of kind ``expect``.

        A reply that belongs to an earlier round (a TV repeating an acknowledgement) is
        skipped like a message of an unknown kind; one of a later round is a failure.
        """
        answering_code = expect == "secret_ack"
        later_kinds = _MESSAGE_KINDS[_MESSAGE_KINDS.index(expect) + 1 :]
        try:
            await self._channel.send(message, deadline)
            for _ in range(_MAX_SKIPPED):
                reply = messages.decode_pairing(await self._channel.receive(deadline))
                if reply["status"] != messages.STATUS_OK:
                    log.debug("pairing with %s: status %d instead of %s", self.host, reply["status"], expect)
                    if answering_code:
                        raise _failed("the TV did not accept the code; start pairing again for a new code")
                    raise _failed(f"the TV refused to pair (status {reply['status']})")
                if expect in reply:
                    return reply[expect]
                if any(kind in reply for kind in later_kinds):
                    raise _failed("the TV answered out of turn; start pairing again")
                log.debug("pairing with %s: skipped a repeated message or one of an unknown kind", self.host)
            raise _failed("the TV sent too many messages this program does not understand")
        except TimeoutError:
            raise _failed("the TV did not answer in time; start pairing again") from None
        except ProtoError as exc:
            log.debug("pairing with %s: %s", self.host, exc)
            raise _failed("the TV sent data this program does not understand") from None
        except (ChannelClosed, OSError):
            if answering_code:
                raise _failed("the TV closed the connection without accepting the code; start pairing again") from None
            raise _failed("the TV closed the connection; start pairing again") from None
