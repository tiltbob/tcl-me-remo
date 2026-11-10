"""Find Google TVs on the local network: a one-shot mDNS browse without any library.

A query for ``_androidtvremote2._tcp.local`` goes to the mDNS multicast group and the
answers are collected for a fixed time.  Two sockets are used at once, because either can
be blocked by a host firewall:

- an ordinary socket on a free port, asking for unicast replies (always possible);
- a socket sharing port 5353 and joined to the multicast group, which hears the answers
  TVs send to everybody (best effort: it needs the port to be shareable).

If neither hears anything and ``avahi-browse`` is installed, its answer is used.

Nothing here raises for network reasons, and packets are parsed defensively: anything
malformed is dropped as a whole, and what one packet can cost is bounded (parsing happens on
the event loop, and anybody on the network can answer).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..system import run_command
from .util import clean_name

log = logging.getLogger(__name__)

SERVICE = "_androidtvremote2._tcp"
SERVICE_LABELS = ("_androidtvremote2", "_tcp", "local")
DEFAULT_PORT = 6466
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353

TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV = 1, 12, 16, 33
_CLASS_IN = 1
_FLAG_RESPONSE = 0x8000
_UNICAST_REPLY = 0x8000  # in the question's class field: "answer to me directly"

MAX_PACKET = 9000
MAX_PACKETS = 256  # per browse; a busy network must not make us collect without end
MAX_DEVICES = 32
MAX_RECORDS = 64  # looked at per packet; a TV's whole answer is under ten
MAX_QUESTIONS = 4  # a response carries none, or the one it answers
_MAX_NAME_BYTES = 255
_MAX_POINTERS = 16
_RESEND_SECONDS = 1.0

Name = tuple[str, ...]


@dataclass(frozen=True)
class Record:
    """One resource record of interest.  ``data`` depends on ``rtype``:

    PTR: the target name; SRV: ``(port, target name)``; A: the dotted address;
    TXT: a dict of its ``key=value`` strings.
    """

    name: Name
    rtype: int
    data: Any


@dataclass
class _Answers:
    """Everything heard during one browse, keyed by lower-cased names."""

    instances: dict[Name, tuple[str, str]] = field(default_factory=dict)  # -> (display name, sender)
    services: dict[Name, tuple[int, Name]] = field(default_factory=dict)  # -> (port, host name)
    addresses: dict[Name, list[str]] = field(default_factory=dict)
    packets: int = 0

    def add(self, packet: bytes, sender: str) -> None:
        self.packets += 1
        if self.packets > MAX_PACKETS:
            return
        try:
            records = parse_response(packet)
        except ValueError:
            return
        for record in records:
            key = _fold(record.name)
            if record.rtype == TYPE_PTR and key == SERVICE_LABELS:
                target: Name = record.data
                if len(target) == len(SERVICE_LABELS) + 1 and _fold(target[1:]) == SERVICE_LABELS:
                    self.instances.setdefault(_fold(target), (target[0], sender))
            elif record.rtype == TYPE_SRV:
                self.services[key] = (record.data[0], _fold(record.data[1]))
            elif record.rtype == TYPE_A:
                known = self.addresses.setdefault(key, [])
                if record.data not in known and len(known) < 8:
                    known.append(record.data)

    def devices(self) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for instance, (label, sender) in self.instances.items():
            port, host_name = self.services.get(instance, (DEFAULT_PORT, ()))
            addresses = self.addresses.get(host_name, [])
            # A TV answers for itself, so the sender is its address.  Some devices list
            # extra addresses that belong to something else; the sender wins over those.
            host = sender if sender in addresses or not addresses else addresses[0]
            name = clean_name(label) or host
            found.setdefault(host, {"name": name, "host": host, "port": port if 0 < port < 65536 else DEFAULT_PORT})
        return sorted(found.values(), key=lambda device: (device["name"].lower(), device["host"]))[:MAX_DEVICES]


def _fold(name: Name) -> Name:
    return tuple(label.lower() for label in name)


def devices_from(packets: Iterable[tuple[bytes, str]]) -> list[dict[str, Any]]:
    """The TVs described by received ``(packet, sender address)`` pairs."""
    answers = _Answers()
    for packet, sender in packets:
        answers.add(packet, sender)
    return answers.devices()


# ---------------------------------------------------------------------------- DNS packets


def encode_name(labels: Name) -> bytes:
    out = bytearray()
    for label in labels:
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise ValueError("bad DNS label")
        out.append(len(raw))
        out += raw
    return bytes(out) + b"\x00"


def build_query(*, unicast_reply: bool) -> bytes:
    """A standard query: PTR ``_androidtvremote2._tcp.local``, class IN."""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    question_class = _CLASS_IN | (_UNICAST_REPLY if unicast_reply else 0)
    return header + encode_name(SERVICE_LABELS) + struct.pack("!HH", TYPE_PTR, question_class)


def read_name(packet: bytes, pos: int, seen: dict[int, tuple[Name, int]] | None = None) -> tuple[Name, int]:
    """Read a possibly compressed name; returns ``(labels, position after it)``.

    ``seen`` (one dict per packet) remembers the names found where compression pointers
    lead.  Without it a small packet could have one long name decoded again for each of
    its many records.
    """
    labels: list[str] = []
    after = -1  # where reading continues once the first pointer was followed
    pointers = 0
    size = 0
    # Where names start (here, and wherever a pointer leads), with the labels and bytes read before each.
    followed = [(pos, 0, 0)]
    while True:
        if pos >= len(packet):
            raise ValueError("name runs past the end of the packet")
        length = packet[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(packet):
                raise ValueError("truncated compression pointer")
            target = (length & 0x3F) << 8 | packet[pos + 1]
            pointers += 1
            if target >= pos or pointers > _MAX_POINTERS:
                raise ValueError("compression pointer does not point backwards")
            if after < 0:
                after = pos + 2
            known = seen.get(target) if seen is not None else None
            if known is not None:
                labels.extend(known[0])
                size += known[1]
                if size > _MAX_NAME_BYTES:
                    raise ValueError("name is too long")
                break
            followed.append((target, len(labels), size))
            pos = target
            continue
        if length & 0xC0 or pos + 1 + length > len(packet):
            raise ValueError("bad label")
        size += 1 + length
        if size > _MAX_NAME_BYTES:
            raise ValueError("name is too long")
        labels.append(packet[pos + 1 : pos + 1 + length].decode("utf-8", "replace"))
        pos += 1 + length
    if seen is not None:
        for start, count, before in followed:
            seen[start] = (tuple(labels[count:]), size - before)
    return tuple(labels), (after if after >= 0 else pos)


def parse_response(packet: bytes) -> list[Record]:
    """The PTR, SRV, TXT and A records of an mDNS response.

    Returns nothing for a query (other hosts' questions carry records too, but those do not
    come from the device they describe).  Raises ValueError if the packet is malformed.
    Only the first ``MAX_RECORDS`` records are looked at.
    """
    if len(packet) < 12 or len(packet) > MAX_PACKET:
        raise ValueError("not a DNS packet")
    _, flags, questions, answers, authorities, additionals = struct.unpack_from("!HHHHHH", packet, 0)
    if not flags & _FLAG_RESPONSE:
        return []
    if questions > MAX_QUESTIONS:
        raise ValueError("too many questions for a response")
    seen: dict[int, tuple[Name, int]] = {}
    pos = 12
    for _ in range(questions):
        _, pos = read_name(packet, pos, seen)
        pos += 4
    records: list[Record] = []
    for _ in range(min(answers + authorities + additionals, MAX_RECORDS)):
        name, pos = read_name(packet, pos, seen)
        if pos + 10 > len(packet):
            raise ValueError("truncated record")
        rtype, rclass, _, length = struct.unpack_from("!HHIH", packet, pos)
        start, end = pos + 10, pos + 10 + length
        if end > len(packet):
            raise ValueError("record runs past the end of the packet")
        pos = end
        if rclass & 0x7FFF != _CLASS_IN:  # the top bit is mDNS's cache-flush flag
            continue
        if rtype == TYPE_PTR:
            target, _ = read_name(packet, start, seen)
            records.append(Record(name, rtype, target))
        elif rtype == TYPE_SRV:
            if length < 7:
                raise ValueError("short SRV record")
            target, _ = read_name(packet, start + 6, seen)
            records.append(Record(name, rtype, (struct.unpack_from("!H", packet, start + 4)[0], target)))
        elif rtype == TYPE_A:
            if length != 4:
                raise ValueError("bad A record")
            records.append(Record(name, rtype, socket.inet_ntoa(packet[start:end])))
        elif rtype == TYPE_TXT:
            records.append(Record(name, rtype, _parse_txt(packet[start:end])))
    return records


def _parse_txt(data: bytes) -> dict[str, str]:
    entries: dict[str, str] = {}
    pos = 0
    while pos < len(data):
        length = data[pos]
        item = data[pos + 1 : pos + 1 + length]
        if len(item) != length:
            raise ValueError("truncated TXT record")
        pos += 1 + length
        key, _, value = item.partition(b"=")
        if key:
            entries.setdefault(key.decode("utf-8", "replace").lower(), value.decode("utf-8", "replace"))
    return entries


# ---------------------------------------------------------------------------- browsing


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, answers: _Answers):
        self._answers = answers

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._answers.add(data, str(addr[0]))

    def error_received(self, exc: Exception) -> None:
        log.debug("mDNS socket: %s", exc)


def _query_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def _shared_socket(group: str, port: int) -> socket.socket:
    """A socket on the mDNS port itself, next to whatever resolver the system runs."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", port))
        membership = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


async def browse(timeout: float, *, target: tuple[str, int] = (MDNS_GROUP, MDNS_PORT)) -> list[dict[str, Any]]:
    """Ask once a second for ``timeout`` seconds and return what answered."""
    loop = asyncio.get_running_loop()
    answers = _Answers()
    senders: list[tuple[asyncio.DatagramTransport, bytes]] = []
    try:
        makers = [(_query_socket, True)]
        if target == (MDNS_GROUP, MDNS_PORT):
            makers.append((lambda: _shared_socket(*target), False))
        for make, unicast_reply in makers:
            try:
                sock = make()
            except OSError as exc:
                log.debug("mDNS socket unavailable: %s", exc)
                continue
            try:
                transport, _ = await loop.create_datagram_endpoint(lambda: _Listener(answers), sock=sock)
            except BaseException:
                sock.close()
                raise
            senders.append((transport, build_query(unicast_reply=unicast_reply)))
        deadline = loop.time() + timeout
        while senders and (remaining := deadline - loop.time()) > 0:
            for transport, query in senders:
                transport.sendto(query, target)
            await asyncio.sleep(min(_RESEND_SECONDS, remaining))
    finally:
        for transport, _ in senders:
            transport.close()
    return answers.devices()


def parse_avahi(output: str) -> list[dict[str, Any]]:
    """Devices from ``avahi-browse --resolve --parsable`` output (its ``=`` lines, IPv4 only)."""
    found: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        fields = line.split(";", 9)
        if len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4" or fields[4] != SERVICE:
            continue
        host = fields[7]
        try:
            socket.inet_aton(host)
            port = int(fields[8])
        except (OSError, ValueError):
            continue
        name = clean_name(_avahi_unescape(fields[3])) or host
        found.setdefault(host, {"name": name, "host": host, "port": port if 0 < port < 65536 else DEFAULT_PORT})
    return sorted(found.values(), key=lambda device: (device["name"].lower(), device["host"]))[:MAX_DEVICES]


def _avahi_unescape(text: str) -> str:
    r"""Undo avahi's escaping: ``\032`` is a decimal byte value, ``\.`` a literal character."""
    raw = text.encode("utf-8")
    out = bytearray()
    pos = 0
    while pos < len(raw):
        if raw[pos : pos + 1] == b"\\" and raw[pos + 1 : pos + 4].isdigit() and len(raw[pos + 1 : pos + 4]) == 3:
            out.append(int(raw[pos + 1 : pos + 4]) & 0xFF)
            pos += 4
        elif raw[pos : pos + 1] == b"\\" and pos + 1 < len(raw):
            out.append(raw[pos + 1])
            pos += 2
        else:
            out.append(raw[pos])
            pos += 1
    return out.decode("utf-8", "replace")


async def discover(
    timeout: float, *, target: tuple[str, int] = (MDNS_GROUP, MDNS_PORT), avahi: str | None = "avahi-browse"
) -> list[dict[str, Any]]:
    """``[{"name", "host", "port"}, ...]`` for the TVs found within ``timeout`` seconds.

    Never raises for network or tool problems; returns what could be found, possibly nothing.
    """
    timeout = min(max(float(timeout), 0.1), 30.0)

    async def from_sockets() -> list[dict[str, Any]]:
        try:
            return await browse(timeout, target=target)
        except (OSError, ValueError) as exc:
            log.debug("mDNS browse failed: %s", exc)
            return []

    async def from_avahi() -> list[dict[str, Any]]:
        if not avahi:
            return []
        argv = [avahi, "--resolve", "--terminate", "--parsable", "--no-db-lookup", SERVICE]
        code, output = await run_command(argv, timeout)
        return parse_avahi(output) if code == 0 else []

    # Both run at once so the fallback costs no extra time; avahi answers from its cache.
    direct, fallback = await asyncio.gather(from_sockets(), from_avahi())
    devices = direct or fallback
    log.debug("discovery found %d TV(s)", len(devices))
    return devices
