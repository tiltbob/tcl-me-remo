"""One TLS connection to the TV, carrying length-prefixed messages.

Used for both ports.  Every operation takes a deadline (event-loop time), so nothing here
can wait on the TV for ever.  Errors are raised as they are (TimeoutError, OSError and
its TLS and connection subclasses, :class:`ChannelClosed`, :class:`InvalidAddress`,
``proto.ProtoError``); what they mean differs between pairing and commands, so the callers
classify them.
"""

from __future__ import annotations

import asyncio
import collections
import ssl

from .proto import MAX_FRAME, FrameDecoder, encode_frame

CLOSE_SECONDS = 0.5  # how long an orderly TLS shutdown may take before the socket is dropped
_READ_SIZE = 4096


class ChannelClosed(Exception):
    """The TV closed the connection."""


class InvalidAddress(Exception):
    """The address cannot name a host at all, so no attempt to reach it can ever succeed."""


class Channel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, max_frame: int = MAX_FRAME):
        self._reader = reader
        self._writer = writer
        self._decoder = FrameDecoder(max_frame)
        self._frames: collections.deque[bytes] = collections.deque()
        self._closed = False
        ssl_object = writer.get_extra_info("ssl_object")
        # DER of the certificate the TV presented ("" if, against the TLS rules, it sent none).
        self.peer_certificate: bytes = (ssl_object.getpeercert(True) if ssl_object is not None else None) or b""

    @classmethod
    async def connect(cls, host: str, port: int, context: ssl.SSLContext, *, deadline: float) -> Channel:
        """Open the connection and complete the TLS handshake before ``deadline``."""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        try:
            async with asyncio.timeout_at(deadline):
                reader, writer = await asyncio.open_connection(
                    host,
                    port,
                    ssl=context,
                    # No server name indication: the TV's certificate names no host, and a
                    # connection by name then looks exactly like one by address.
                    server_hostname="",
                    # Our own deadline always fires first; these only replace asyncio's long defaults.
                    ssl_handshake_timeout=remaining + 5.0,
                    ssl_shutdown_timeout=CLOSE_SECONDS,
                )
        except ValueError:  # a host name the resolver will not even look up (empty label, too long)
            raise InvalidAddress(host) from None
        return cls(reader, writer)

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, message: bytes, deadline: float) -> None:
        """Write one message.  Raises ChannelClosed if the TV has closed the connection.

        The check matters: asyncio throws away what is written to a TLS connection whose
        other end has said goodbye, without any error, and nothing in the protocol would
        reveal the loss afterwards.
        """
        data = encode_frame(message)
        if self._closed or self._reader.at_eof() or self._writer.transport.is_closing():
            raise ChannelClosed
        self._writer.write(data)
        async with asyncio.timeout_at(deadline):
            await self._writer.drain()

    async def receive(self, deadline: float) -> bytes:
        """The next message.  Raises TimeoutError at ``deadline`` and ChannelClosed at end of stream."""
        while not self._frames:
            if deadline - asyncio.get_running_loop().time() <= 0:
                raise TimeoutError
            async with asyncio.timeout_at(deadline):
                data = await self._reader.read(_READ_SIZE)
            if not data:
                raise ChannelClosed
            self._frames.extend(self._decoder.feed(data))
        return self._frames.popleft()

    async def close(self, timeout: float = CLOSE_SECONDS) -> None:
        """Shut the connection down in an orderly way, or drop it if that takes too long."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            async with asyncio.timeout(max(timeout, 0.0)):
                await self._writer.wait_closed()
        except (TimeoutError, OSError):  # includes TLS and connection errors while closing
            self._writer.transport.abort()
        except BaseException:
            self._writer.transport.abort()
            raise

    def abort(self) -> None:
        """Drop the connection now.  Safe to call at any time, any number of times."""
        if not self._closed:
            self._closed = True
            self._writer.transport.abort()
