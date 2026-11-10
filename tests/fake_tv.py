"""An in-process Google TV for the Wi-Fi assist tests.

Two TLS servers on 127.0.0.1 with ephemeral ports stand in for the pairing port (6467) and
the command port (6466).  The fake behaves as the protocol descriptions say a TV does: it
demands a client certificate, shows a pairing code derived from both certificates, accepts
commands only from clients it paired with, speaks first on the command port and pings.

It deliberately shares no code with the client under test.  The wire helpers below use the
raw field numbers, and RSA key numbers come from the ``openssl`` tool, so a mistake in the
client's tables or in its certificate parsing cannot cancel itself out.

Failure modes are attributes to set before connecting; see ``FakeTv.__init__``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import socket
import ssl
import struct
import subprocess
from pathlib import Path
from typing import Any, AsyncIterator

from tcl_me_remo.atv import AtvAssist

SUPPORTED_FEATURES = 639  # what a current TV advertises

# ---------------------------------------------------------------------------- wire helpers


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def field(number: int, value: int | bytes | str) -> bytes:
    """One protobuf field: a varint for an int, length-delimited otherwise."""
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    raw = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(raw)) + raw


def frame(message: bytes) -> bytes:
    return varint(len(message)) + message


def parse(data: bytes) -> dict[int, list[Any]]:
    """One level of a protobuf message: ``{field number: [values]}`` (ints and bytes)."""
    out: dict[int, list[Any]] = {}
    pos = 0

    def read_varint() -> int:
        nonlocal pos
        value = shift = 0
        while True:
            byte = data[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    while pos < len(data):
        key = read_varint()
        if key & 7 == 0:
            value: Any = read_varint()
        elif key & 7 == 2:
            length = read_varint()
            value = data[pos : pos + length]
            pos += length
        else:
            raise ValueError(f"wire type {key & 7}")
        out.setdefault(key >> 3, []).append(value)
    return out


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    length = shift = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return await reader.readexactly(length) if length else b""


# ---------------------------------------------------------------------------- certificates

_certificates: dict[str, tuple[str, str]] = {}  # subject -> (certificate PEM, key PEM), made once per run


def make_certificate(directory: Path, stem: str, subject: str, *, elliptic: bool = False) -> tuple[Path, Path]:
    """Write a self-signed certificate (RSA unless ``elliptic``) and its key; returns their paths."""
    cert_path, key_path = directory / f"{stem}.pem", directory / f"{stem}.key"
    if subject not in _certificates:
        key_type = ["ec", "-pkeyopt", "ec_paramgen_curve:prime256v1"] if elliptic else ["rsa:2048"]
        argv = ["openssl", "req", "-x509", "-newkey", *key_type, "-nodes", "-sha256", "-days", "30"]
        subprocess.run(
            argv + ["-subj", subject, "-keyout", str(key_path), "-out", str(cert_path)], check=True, capture_output=True
        )
        _certificates[subject] = (cert_path.read_text(), key_path.read_text())
    cert_pem, key_pem = _certificates[subject]
    cert_path.write_text(cert_pem)
    key_path.write_text(key_pem)
    return cert_path, key_path


def openssl_rsa_numbers(certificate_der: bytes) -> tuple[int, int]:
    """(modulus, exponent) of a certificate as the openssl tool reports them."""
    done = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-noout", "-modulus", "-text"],
        input=certificate_der,
        check=True,
        capture_output=True,
    )
    text = done.stdout.decode()
    modulus = re.search(r"^Modulus=([0-9A-Fa-f]+)$", text, re.MULTILINE)
    exponent = re.search(r"Exponent: (\d+)", text)
    assert modulus and exponent, text
    return int(modulus.group(1), 16), int(exponent.group(1))


def minimal(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def expected_secret(client_der: bytes, server_der: bytes, nonce: bytes) -> bytes:
    """The secret a TV expects for its two-byte nonce."""
    digest = hashlib.sha256()
    for certificate in (client_der, server_der):
        modulus, exponent = openssl_rsa_numbers(certificate)
        digest.update(minimal(modulus))
        digest.update(minimal(exponent))
    digest.update(nonce)
    return digest.digest()


def fingerprint(certificate_der: bytes) -> str:
    return hashlib.sha256(certificate_der).hexdigest()


def free_port() -> int:
    """A port on which nothing listens (connections are refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------- the TV


class FakeTv:
    host = "127.0.0.1"

    def __init__(self, directory: Path, *, name: str = "Fake TV", elliptic: bool = False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.model = "Fake Model"
        self.vendor = "FakeCo"
        subject = f"/CN=atvremote\\/fake\\/fake\\/{name}\\/AA:BB:CC:DD:EE:FF"
        # ``elliptic``: a certificate with an EC key, which the pairing protocol cannot use.
        self.cert_path, self.key_path = make_certificate(self.directory, "tv", subject, elliptic=elliptic)

        # Trust.  Python's TLS cannot accept "any" client certificate, so the files a client
        # will present are named here and read at each connection (the client may not have
        # created them yet when the fake starts).
        self.client_certs: list[Path] = []
        self.paired: set[str] = set()  # fingerprints of the client certificates paired so far

        # What the TV saw.
        self.pairing_requests: list[tuple[str, str]] = []  # (service name, client name)
        self.code = ""  # the code "on screen"
        self.bad_secrets = 0
        self.configures: list[dict[int, list[Any]]] = []  # raw remote_configure bodies
        self.actives: list[int] = []
        self.keys: list[tuple[int, int]] = []  # (key code, direction)
        self.links: list[str] = []
        self.pongs: list[int] = []
        self.pings_sent = 0
        self.server_names: list[str | None] = []  # the TLS server name of each connection attempt
        self.remote_connections = 0  # command sessions that passed TLS
        self.endings: list[str] = []  # how each of them ended: "eof" or the exception type

        # Behaviour on the command port.
        self.started = True  # False: the TV is in standby
        self.supported = SUPPORTED_FEATURES
        self.reject = "tls"  # how an unpaired client is turned away: "tls", "eof" or "reset"
        self.silent = False  # complete TLS, then say nothing
        self.delay = 0.0  # seconds before the first message
        self.garbage = False  # first message is not valid protobuf
        self.oversize = False  # first message claims to be 16 MiB long
        self.close_after_configure = False  # hang up after the first message
        self.never_start = False  # complete the handshake except for remote_start
        self.error_on = ""  # send remote_error: "handshake" (in place of remote_start) or "command"
        self.ping_interval = 0.0  # > 0: ping whenever the client was quiet for this long
        self.ping_first = False  # ping (value 0) before remote_start
        self.dribble = False  # write one byte at a time
        self.flood = False  # after the first message, send pings without end
        self.close_after_command = False  # hang up as soon as a key or link arrived
        # Say remote_start, then drop the connection without reading anything more: "close"
        # (orderly) or "reset".  ``drop_delay`` seconds pass in between, so that a command
        # sent in answer to remote_start is already on its way when the connection goes.
        self.drop_at_start = ""
        self.drop_delay = 0.0
        self.deaf = ""  # "start" or "command": from then on read nothing, not even the TLS goodbye

        # Behaviour on the pairing port.  ``pair_fault`` happens in place of the reply at
        # ``pair_fault_at``: "request", "options", "configuration" or "secret".
        self.pair_fault = ""  # "drop", "status", "silent", "garbage" or "wrong_kind"
        self.pair_fault_at = ""
        self.pair_noise = 0  # precede every reply with this many messages of a kind no client knows
        self.pair_repeat = ""  # the stage whose reply is sent twice
        self.pair_wrong_echo = False  # acknowledge the secret without echoing it correctly
        self.nonce: bytes | None = None  # fixed nonce; random when None

        self.pair_port = 0
        self.remote_port = 0
        self._servers: list[asyncio.Server] = []
        self._extra_servers: list[asyncio.Server] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._tls_max: ssl.TLSVersion | None = None
        self.errors: list[BaseException] = []  # bugs in the fake or protocol violations by the client
        self._bases: list[ssl.SSLContext] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> FakeTv:
        for handler, remote in ((self._pairing, False), (self._remote, True)):
            base = self._context(remote)
            base.sni_callback = self._swap_context(remote)
            self._bases.append(base)
            server = await asyncio.start_server(self._tracked(handler), self.host, 0, ssl=base)
            self._servers.append(server)
        self.pair_port, self.remote_port = (server.sockets[0].getsockname()[1] for server in self._servers)
        return self

    async def stop(self) -> None:
        for server in self._servers + self._extra_servers:
            server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        for _ in range(100):
            if not self._writers:
                break
            await asyncio.sleep(0.01)

    async def close_remote_port(self) -> None:
        """Stop listening on the command port, as a TV whose network is not up yet."""
        self._servers[1].close()
        await asyncio.sleep(0)

    async def open_remote_port(self) -> None:
        """Listen on the same command port again."""
        self._servers[1] = await asyncio.start_server(
            self._tracked(self._remote), self.host, self.remote_port, ssl=self._bases[1]
        )

    async def idle(self, timeout: float = 2.0) -> None:
        """Wait until no client is connected."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._writers:
            assert asyncio.get_running_loop().time() < deadline, "a client connection is still open"
            await asyncio.sleep(0.005)

    @property
    def open_connections(self) -> int:
        return len(self._writers)

    def limit_tls(self, version: ssl.TLSVersion) -> None:
        """Behave like an older TV that tops out at this TLS version."""
        self._tls_max = version
        for base in self._bases:
            base.maximum_version = version

    def change_certificate(self) -> None:
        """Start presenting a new certificate, as after a factory reset."""
        subject = f"/CN=atvremote\\/fake\\/fake\\/{self.name} (reset)\\/AA:BB:CC:DD:EE:FF"
        self.cert_path, self.key_path = make_certificate(self.directory, "tv-reset", subject)

    def hang_up(self) -> None:
        """Drop every connection, as when the user dismisses the pairing dialog on the TV."""
        for writer in list(self._writers):
            writer.close()

    async def push(self, message: bytes) -> None:
        """Send a message, out of turn, on every open connection."""
        for writer in list(self._writers):
            await self._send(writer, message)

    @property
    def certificate_der(self) -> bytes:
        return ssl.PEM_cert_to_DER_cert(self.cert_path.read_text())

    async def start_alert_port(self) -> int:
        """A port that answers a TLS ClientHello with a fatal ``certificate_unknown`` alert."""

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.read(4096)
                writer.write(bytes.fromhex("15 03 03 00 02 02 2e"))
                await writer.drain()
            finally:
                writer.close()

        return await self._extra_port(handle)

    async def start_mute_port(self) -> int:
        """A port that accepts TCP connections and never says a word (no TLS)."""

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while await reader.read(4096):
                    pass
            finally:
                writer.close()

        return await self._extra_port(handle)

    async def _extra_port(self, handler: Any) -> int:
        server = await asyncio.start_server(self._tracked(handler), self.host, 0)
        self._extra_servers.append(server)
        return server.sockets[0].getsockname()[1]

    # ------------------------------------------------------------------ TLS

    def _context(self, remote: bool) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_path, self.key_path)
        context.verify_mode = ssl.CERT_REQUIRED
        if self._tls_max is not None:
            context.maximum_version = self._tls_max
        for path in self.client_certs:
            if not path.exists():
                continue
            pem = path.read_text()
            known = fingerprint(ssl.PEM_cert_to_DER_cert(pem)) in self.paired
            if remote and self.reject == "tls" and not known:
                continue  # the TLS layer itself refuses this client
            context.load_verify_locations(cadata=pem)
        return context

    def _swap_context(self, remote: bool) -> Any:
        def on_client_hello(ssl_object: Any, server_name: str | None, base: ssl.SSLContext) -> None:
            self.server_names.append(server_name)
            ssl_object.context = self._context(remote)

        return on_client_hello

    def _tracked(self, handler: Any) -> Any:
        async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self._writers.add(writer)
            try:
                await handler(reader, writer)
            except (asyncio.IncompleteReadError, OSError):  # the client left; includes TLS errors
                pass
            except Exception as exc:  # noqa: BLE001 - reported by the fixture, not lost in a log
                self.errors.append(exc)
            finally:
                writer.close()
                self._writers.discard(writer)

        return run

    async def _send(self, writer: asyncio.StreamWriter, message: bytes) -> None:
        data = frame(message)
        if self.dribble:
            for index in range(len(data)):
                writer.write(data[index : index + 1])
                await writer.drain()
                await asyncio.sleep(0.001)
        else:
            writer.write(data)
            await writer.drain()

    @staticmethod
    async def _until_client_leaves(reader: asyncio.StreamReader) -> None:
        while await reader.read(4096):
            pass

    # ------------------------------------------------------------------ pairing port

    async def _pairing(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client_der = writer.get_extra_info("ssl_object").getpeercert(True)
        ok = field(1, 2) + field(2, 200)

        async def reply(stage: str, message: bytes) -> bool:
            """Send the normal reply, or the configured fault.  False ends the session."""
            for _ in range(self.pair_noise):
                await self._send(writer, ok + field(99, b"future"))
            if self.pair_fault_at != stage:
                await self._send(writer, message)
                if self.pair_repeat == stage:
                    await self._send(writer, message)
                return True
            if self.pair_fault == "status":
                await self._send(writer, field(1, 2) + field(2, 400))
            elif self.pair_fault == "garbage":
                await self._send(writer, b"\x0a\x7f\x01")  # a field longer than the message
            elif self.pair_fault == "wrong_kind":
                await self._send(writer, ok + field(41, b""))
            elif self.pair_fault == "silent":
                await self._until_client_leaves(reader)
            return False  # "drop": just hang up

        request = parse(await read_frame(reader))
        body = parse(request[10][0])
        self.pairing_requests.append((body[1][0].decode(), body[2][0].decode()))
        assert request[1] == [2] and request[2] == [200], request
        if not await reply("request", ok + field(11, field(1, self.name))):
            return

        options = parse(parse(await read_frame(reader))[20][0])
        assert parse(options[1][0]) == {1: [3], 2: [6]} and options[3] == [1], options  # hexadecimal, 6; input role
        if not await reply("options", ok + field(20, field(2, field(1, 3) + field(2, 6)) + field(3, 1))):
            return

        configuration = parse(parse(await read_frame(reader))[30][0])
        assert parse(configuration[1][0]) == {1: [3], 2: [6]} and configuration[2] == [1], configuration
        nonce = self.nonce if self.nonce is not None else os.urandom(2)
        wanted = await asyncio.to_thread(expected_secret, client_der, self.certificate_der, nonce)
        self.code = f"{wanted[0]:02X}{nonce.hex().upper()}"
        if not await reply("configuration", ok + field(31, b"")):
            return

        secret = parse(parse(await read_frame(reader))[40][0])[1][0]
        if secret != wanted:
            self.bad_secrets += 1
            await self._send(writer, field(1, 2) + field(2, 402))
            return
        echo = b"not the secret" if self.pair_wrong_echo else wanted
        if not await reply("secret", ok + field(41, field(1, echo))):
            return
        self.paired.add(fingerprint(client_der))
        self.code = ""
        await self._until_client_leaves(reader)

    # ------------------------------------------------------------------ command port

    async def _remote(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.remote_connections += 1
        ending = "eof"
        try:
            await self._remote_session(reader, writer)
        except asyncio.IncompleteReadError as exc:
            ending = "eof" if not exc.partial else "truncated"
        except OSError as exc:
            ending = type(exc).__name__
        finally:
            self.endings.append(ending)

    async def _remote_session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = fingerprint(writer.get_extra_info("ssl_object").getpeercert(True))
        if client not in self.paired:
            if self.reject == "reset":
                sock = writer.get_extra_info("socket")
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.transport.abort()
            return
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.silent:
            return await self._until_client_leaves(reader)
        if self.garbage:
            await self._send(writer, b"\x0a\x7f\x01")
            return await self._until_client_leaves(reader)
        if self.oversize:
            writer.write(varint(16 * 1024 * 1024) + b"\x00" * 4096)
            await writer.drain()
            return await self._until_client_leaves(reader)

        device = field(1, self.model) + field(2, self.vendor) + field(3, 1) + field(4, "12")
        device += field(5, "com.google.android.tv.remote.service") + field(6, "6.0.0")
        await self._send(writer, field(1, field(1, self.supported) + field(2, device)))
        if self.close_after_configure:
            return
        if self.flood:
            while True:
                await self._send(writer, field(8, field(1, 1)))
                await asyncio.sleep(0)

        first_configure = first_active = True
        while True:
            try:
                data = await asyncio.wait_for(read_frame(reader), self.ping_interval or None)
            except TimeoutError:
                self.pings_sent += 1
                await self._send(writer, field(8, field(1, self.pings_sent) + field(2, 7)))
                continue
            message = parse(data)
            if 1 in message:
                self.configures.append(parse(message[1][0]))
                if first_configure:
                    first_configure = False
                    await self._send(writer, field(1, field(1, self.supported)))  # the short second configure
                    await self._send(writer, field(2, b""))  # remote_set_active {}
            elif 2 in message:
                self.actives.append(parse(message[2][0]).get(1, [0])[0])
                if first_active:
                    first_active = False
                    if await self._after_set_active(writer):
                        return
            elif 9 in message:
                self.pongs.append(parse(message[9][0]).get(1, [0])[0])
            elif 10 in message:
                key = parse(message[10][0])
                self.keys.append((key[1][0], key[2][0]))
                await self._after_command(writer)
            elif 90 in message:
                self.links.append(parse(message[90][0])[1][0].decode())
                await self._after_command(writer)

    async def _after_set_active(self, writer: asyncio.StreamWriter) -> bool:
        """What follows the handshake.  True: the TV dropped the connection."""
        if self.ping_first:
            await self._send(writer, field(8, b""))  # proto3: value 0 is left off the wire
        # Things a client must skip: a message kind it does not know, fixed-width fields,
        # and a message long enough to need a two-byte length prefix.
        await self._send(writer, field(77, 5) + varint(78 << 3 | 5) + b"\x01\x02\x03\x04")
        await self._send(writer, varint(79 << 3 | 1) + b"\x00" * 8)
        await self._send(writer, field(20, field(1, field(12, "com.example." + "x" * 200))))
        if self.never_start:
            return False
        if self.error_on == "handshake":
            await self._send(writer, field(3, field(1, 1)))
            return False
        await self._send(writer, field(40, field(1, 1) if self.started else b""))
        if self.drop_at_start:
            writer.transport.pause_reading()  # whatever the client sends now is never looked at
            await asyncio.sleep(self.drop_delay)
            if self.drop_at_start == "reset":
                sock = writer.get_extra_info("socket")
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.transport.abort()
            else:
                writer.close()
            return True
        await self._send(writer, field(50, field(6, 100) + field(7, 12)))  # volume report
        if self.deaf == "start":
            writer.transport.pause_reading()
        return False

    async def _after_command(self, writer: asyncio.StreamWriter) -> None:
        if self.error_on == "command":
            await self._send(writer, field(3, field(1, 1) + field(2, b"\x52\x00")))
        if self.close_after_command:
            writer.close()
        if self.deaf == "command":
            writer.transport.pause_reading()


# ---------------------------------------------------------------------------- test helpers

CLIENT_KEY, CLIENT_CERT, RECORD = "client.key", "client.pem", "pairing.json"  # the assist's files


@contextlib.asynccontextmanager
async def running_tv(directory: Path, **kwargs: Any) -> AsyncIterator[FakeTv]:
    tv = FakeTv(directory, **kwargs)
    await tv.start()
    try:
        yield tv
    finally:
        await tv.stop()
    assert not tv.errors, tv.errors


def new_assist(tv: FakeTv, state_dir: Path, **kwargs: Any) -> AtvAssist:
    """An assist pointed at the fake, with the waits a real TV needs shortened."""
    kwargs.setdefault("pair_port", tv.pair_port)
    kwargs.setdefault("remote_port", tv.remote_port)
    assist = AtvAssist(state_dir, **kwargs)
    assist.linger_seconds = 0.05
    assist.settle_seconds = 0.0
    if state_dir / CLIENT_CERT not in tv.client_certs:
        tv.client_certs.append(state_dir / CLIENT_CERT)
    return assist


def seed_identity(state_dir: Path) -> None:
    """Give the assist a ready-made key and certificate, to spare a test the key generation."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert_path, key_path = make_certificate(state_dir, "seed", "/CN=tcl-me-remo")
    key_path.chmod(0o600)
    key_path.rename(state_dir / CLIENT_KEY)
    cert_path.rename(state_dir / CLIENT_CERT)


async def pair(assist: AtvAssist, tv: FakeTv) -> None:
    await assist.start_pairing(tv.host)
    await assist.finish_pairing(tv.code)
    assert assist.status() == {"paired": True, "pairing": False, "pairing_host": ""}
