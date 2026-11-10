"""The framed TLS channel of the Wi-Fi assist, with a stand-in for the connection."""

import asyncio
import ssl
import time

import pytest

from tcl_me_remo.atv.channel import Channel, ChannelClosed, InvalidAddress
from tcl_me_remo.atv.proto import ProtoError


class StubTransport:
    def __init__(self):
        self.aborted = 0
        self.closing = False

    def abort(self):
        self.aborted += 1

    def is_closing(self):
        return self.closing


class StubWriter:
    """A stream writer whose peer can be made to stall or fail."""

    def __init__(self):
        self.transport = StubTransport()
        self.written = b""
        self.closed = False
        self.drain_blocks = False
        self.close_blocks = False
        self.close_error: Exception | None = None

    def get_extra_info(self, name):
        return None

    def write(self, data):
        self.written += data

    async def drain(self):
        if self.drain_blocks:
            await asyncio.Event().wait()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        if self.close_error is not None:
            raise self.close_error
        if self.close_blocks:
            await asyncio.Event().wait()


def make_channel(**kwargs):
    reader, writer = asyncio.StreamReader(), StubWriter()
    return Channel(reader, writer, **kwargs), reader, writer


def soon(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


async def test_receive_reassembles_frames_and_keeps_extra_ones():
    channel, reader, _ = make_channel()
    assert channel.peer_certificate == b""
    reader.feed_data(b"\x03ab")
    task = asyncio.create_task(channel.receive(soon(1.0)))
    await asyncio.sleep(0.01)
    assert not task.done()
    reader.feed_data(b"c\x01x\x00\x02y")
    assert await task == b"abc"
    assert await channel.receive(soon(1.0)) == b"x"
    assert await channel.receive(soon(1.0)) == b""
    with pytest.raises(TimeoutError):
        await channel.receive(soon(0.05))  # "\x02y" is half a frame
    reader.feed_data(b"z")
    assert await channel.receive(soon(1.0)) == b"yz"


async def test_receive_deadline_and_end_of_stream():
    channel, reader, _ = make_channel()
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        await channel.receive(soon(0.1))
    assert 0.08 <= time.monotonic() - began < 0.3
    with pytest.raises(TimeoutError):
        await channel.receive(soon(-1.0))  # already too late: not even a look
    reader.feed_data(b"\x05ab")
    reader.feed_eof()
    with pytest.raises(ChannelClosed):
        await channel.receive(soon(1.0))  # the stream ended inside a frame


async def test_receive_passes_on_connection_errors_and_refuses_big_frames():
    channel, reader, _ = make_channel()
    reader.set_exception(ConnectionResetError("reset"))
    with pytest.raises(ConnectionResetError):
        await channel.receive(soon(1.0))

    channel, reader, _ = make_channel(max_frame=100)
    reader.feed_data(b"\x65")  # 101 bytes announced
    with pytest.raises(ProtoError):
        await channel.receive(soon(1.0))


async def test_cancelled_receive_loses_nothing():
    channel, reader, _ = make_channel()
    reader.feed_data(b"\x04ab")
    task = asyncio.create_task(channel.receive(soon(5.0)))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    reader.feed_data(b"cd")
    assert await channel.receive(soon(1.0)) == b"abcd"


async def test_send_frames_the_message_and_honours_the_deadline():
    channel, _, writer = make_channel()
    await channel.send(b"abc", soon(1.0))
    assert writer.written == b"\x03abc"
    writer.drain_blocks = True  # the TV stopped reading
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        await channel.send(b"x" * 200, soon(0.1))
    assert time.monotonic() - began < 0.3
    with pytest.raises(ProtoError):
        await channel.send(b"x" * 100_000, soon(1.0))


async def test_send_refuses_a_connection_the_tv_already_closed():
    """asyncio drops such a write without a word; the caller must not think it went out."""
    channel, reader, writer = make_channel()
    reader.feed_eof()
    with pytest.raises(ChannelClosed):
        await channel.send(b"abc", soon(1.0))
    assert writer.written == b""

    channel, _, writer = make_channel()
    writer.transport.closing = True  # the TLS layer has begun to shut down
    with pytest.raises(ChannelClosed):
        await channel.send(b"abc", soon(1.0))
    assert writer.written == b""

    channel, _, writer = make_channel()
    channel.abort()
    with pytest.raises(ChannelClosed):
        await channel.send(b"abc", soon(1.0))
    assert writer.written == b""


async def test_orderly_close():
    channel, _, writer = make_channel()
    await channel.close()
    assert writer.closed and writer.transport.aborted == 0 and channel.closed
    await channel.close()
    channel.abort()  # both harmless afterwards
    assert writer.transport.aborted == 0


async def test_close_that_stalls_is_cut_short():
    channel, _, writer = make_channel()
    writer.close_blocks = True
    began = time.monotonic()
    await channel.close(0.1)
    assert 0.08 <= time.monotonic() - began < 0.3
    assert writer.transport.aborted == 1

    channel, _, writer = make_channel()
    writer.close_blocks = True
    await channel.close(-5.0)  # no time left at all
    assert writer.transport.aborted == 1


async def test_close_survives_connection_errors():
    channel, _, writer = make_channel()
    writer.close_error = ConnectionResetError("gone")
    await channel.close()
    assert writer.transport.aborted == 1


async def test_cancelled_close_drops_the_connection():
    channel, _, writer = make_channel()
    writer.close_blocks = True
    task = asyncio.create_task(channel.close(5.0))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.transport.aborted == 1 and channel.closed


async def test_abort_is_idempotent():
    channel, _, writer = make_channel()
    channel.abort()
    channel.abort()
    await channel.close()
    assert writer.transport.aborted == 1 and not writer.closed


@pytest.mark.parametrize("host", ["a..b", ".local", "x" * 64 + ".example"])
async def test_connect_names_an_address_that_cannot_be_looked_up(host):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(InvalidAddress):
        await Channel.connect(host, 6466, context, deadline=soon(1.0))


async def test_connect_with_no_time_left_does_not_touch_the_network():
    with pytest.raises(TimeoutError):
        await Channel.connect("192.0.2.1", 6466, None, deadline=soon(-1.0))
