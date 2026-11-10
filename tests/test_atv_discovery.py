"""mDNS discovery of the Wi-Fi assist: the DNS parser against hand-built packets, and the
browse against a responder on the loopback interface (no multicast, no real network)."""

import asyncio
import random
import struct
import time

import pytest

from tcl_me_remo.atv import AtvAssist, discovery
from tcl_me_remo.atv.discovery import TYPE_A, TYPE_PTR, TYPE_SRV, TYPE_TXT, devices_from, parse_response, read_name

SERVICE = ("_androidtvremote2", "_tcp", "local")

# A response as a TV sends it, written out byte by byte.  Offsets are noted because the
# names are compressed: a pointer is 0xC0 followed by the offset of the name it continues with.
RESPONSE = (
    bytes.fromhex("0000 8400 0000 0001 0000 0003")  # response, authoritative; 1 answer, 3 additional
    # 12: PTR _androidtvremote2._tcp.local -> <instance>
    + b"\x11_androidtvremote2"  # 12
    + b"\x04_tcp"  # 30
    + b"\x05local\x00"  # 35
    + bytes.fromhex("000c 0001 00000078 0011")  # PTR, IN, TTL 120, 17 bytes of data
    + b"\x0eLiving Room TV\xc0\x0c"  # 52: the instance name, continued at 12
    # 69: SRV for the instance (the name at 52), class IN with the cache-flush bit
    + bytes.fromhex("c034 0021 8001 00000078 000f")
    + bytes.fromhex("0000 0000 1942")  # priority, weight, port 6466
    + b"\x06tclc7k\xc0\x23"  # 87: target tclc7k.local ("local" is at 35)
    # 96: TXT for the instance
    + bytes.fromhex("c034 0010 8001 00000078 0015")
    + b"\x14bt=AA:BB:CC:DD:EE:FF"
    # 129: A for tclc7k.local (the name at 87)
    + bytes.fromhex("c057 0001 8001 00000078 0004 c0a80132")
)


def name(*labels: str, pointer: int | None = None) -> bytes:
    out = b"".join(bytes([len(label.encode())]) + label.encode() for label in labels)
    return out + (struct.pack("!H", 0xC000 | pointer) if pointer is not None else b"\x00")


def record(owner: bytes, rtype: int, data: bytes, *, rclass: int = 1) -> bytes:
    return owner + struct.pack("!HHIH", rtype, rclass, 120, len(data)) + data


def message(*records: bytes, flags: int = 0x8400, questions: bytes = b"", question_count: int = 0) -> bytes:
    return struct.pack("!HHHHHH", 0, flags, question_count, len(records), 0, 0) + questions + b"".join(records)


def announcement(instance: str, address: bytes, *, port: int = 6466, host: str = "tv") -> bytes:
    """PTR + SRV + A without compression."""
    full = name(instance, *SERVICE)
    target = name(host, "local")
    return message(
        record(name(*SERVICE), TYPE_PTR, full),
        record(full, TYPE_SRV, struct.pack("!HHH", 0, 0, port) + target),
        record(target, TYPE_A, address),
    )


# ---------------------------------------------------------------------------- packets


def test_query_bytes():
    expected = "000000000001000000000000115f616e64726f6964747672656d6f746532045f746370056c6f63616c00000c8001"
    assert discovery.build_query(unicast_reply=True) == bytes.fromhex(expected)
    assert discovery.build_query(unicast_reply=False) == bytes.fromhex(expected[:-4] + "0001")


def test_parse_a_compressed_response():
    assert len(RESPONSE) == 145
    records = {rec.rtype: rec for rec in parse_response(RESPONSE)}
    assert records[TYPE_PTR].name == SERVICE
    assert records[TYPE_PTR].data == ("Living Room TV", *SERVICE)
    assert records[TYPE_SRV].name == ("Living Room TV", *SERVICE)
    assert records[TYPE_SRV].data == (6466, ("tclc7k", "local"))
    assert records[TYPE_TXT].data == {"bt": "AA:BB:CC:DD:EE:FF"}
    assert records[TYPE_A].name == ("tclc7k", "local") and records[TYPE_A].data == "192.168.1.50"


def test_devices_from_a_response():
    expected = [{"name": "Living Room TV", "host": "192.168.1.50", "port": 6466}]
    assert devices_from([(RESPONSE, "192.168.1.50")]) == expected
    assert devices_from([(RESPONSE, "192.168.1.50")] * 3) == expected  # answers repeat; devices do not


def test_every_truncation_is_rejected_as_a_whole():
    for cut in range(len(RESPONSE)):
        with pytest.raises(ValueError):
            parse_response(RESPONSE[:cut])
        assert devices_from([(RESPONSE[:cut], "192.168.1.50")]) == []


def test_sender_address_is_preferred_over_listed_addresses():
    both = message(
        record(name(*SERVICE), TYPE_PTR, name("TV", *SERVICE)),
        record(name("TV", *SERVICE), TYPE_SRV, struct.pack("!HHH", 0, 0, 6466) + name("tv", "local")),
        record(name("tv", "local"), TYPE_A, bytes([10, 0, 0, 9])),  # somebody else's, listed first
        record(name("tv", "local"), TYPE_A, bytes([192, 168, 1, 50])),
    )
    assert devices_from([(both, "192.168.1.50")])[0]["host"] == "192.168.1.50"
    # Relayed by another host: fall back on what the TV lists.
    assert devices_from([(both, "192.168.1.1")])[0]["host"] == "10.0.0.9"


def test_answer_without_srv_and_address_uses_the_sender_and_default_port():
    bare = message(record(name(*SERVICE), TYPE_PTR, name("Bare TV", *SERVICE)))
    assert devices_from([(bare, "192.168.1.60")]) == [{"name": "Bare TV", "host": "192.168.1.60", "port": 6466}]


def test_records_spread_over_packets_and_odd_ports():
    ptr = message(record(name(*SERVICE), TYPE_PTR, name("Split", *SERVICE)))
    rest = message(
        record(name("split", *SERVICE), TYPE_SRV, struct.pack("!HHH", 0, 0, 7000) + name("H", "local")),
        record(name("h", "LOCAL"), TYPE_A, bytes([192, 168, 1, 61])),
    )
    assert devices_from([(ptr, "192.168.1.61"), (rest, "192.168.1.61")]) == [
        {"name": "Split", "host": "192.168.1.61", "port": 7000}
    ]
    zero_port = announcement("Zero", bytes([192, 168, 1, 62]), port=0)
    assert devices_from([(zero_port, "192.168.1.62")])[0]["port"] == 6466


def test_names_compare_without_case_and_keep_dots_and_unicode():
    instance = name("Küche.TV 2", "_androidtvremote2", "_tcp", "LOCAL")
    shouting = message(record(name("_AndroidTVRemote2", "_TCP", "Local"), TYPE_PTR, instance))
    assert devices_from([(shouting, "192.168.1.70")]) == [{"name": "Küche.TV 2", "host": "192.168.1.70", "port": 6466}]
    hostile = message(record(name(*SERVICE), TYPE_PTR, name("TV\x1b[2J\n" + "x" * 50, *SERVICE)))
    shown = devices_from([(hostile, "192.168.1.71")])[0]["name"]
    assert shown.startswith("TV [2J x") and shown.isprintable()


def test_several_devices_are_sorted_and_deduplicated_by_address():
    packets = [
        (announcement("Wohnzimmer", bytes([192, 168, 1, 52])), "192.168.1.52"),
        (announcement("bedroom", bytes([192, 168, 1, 51])), "192.168.1.51"),
        (announcement("Wohnzimmer (2)", bytes([192, 168, 1, 52])), "192.168.1.52"),  # same TV, renamed
    ]
    assert devices_from(packets) == [
        {"name": "bedroom", "host": "192.168.1.51", "port": 6466},
        {"name": "Wohnzimmer", "host": "192.168.1.52", "port": 6466},
    ]


def test_other_services_and_stray_records_are_ignored():
    cast = ("_googlecast", "_tcp", "local")
    packets = [
        message(record(name(*cast), TYPE_PTR, name("Chromecast", *cast))),
        message(record(name(*SERVICE), TYPE_PTR, name("elsewhere", "local"))),  # not an instance of the service
        message(record(name(*SERVICE), TYPE_PTR, name("a", "b", *SERVICE))),
        message(record(name("old", "_androidtvremote", "_tcp", "local"), TYPE_SRV, bytes(6) + name("x", "local"))),
        message(record(name(*SERVICE), TYPE_PTR, name("Chaos", *SERVICE), rclass=3)),  # not class IN
        message(record(name(*SERVICE), 28, bytes(16))),  # AAAA: IPv4 only
    ]
    assert devices_from([(packet, "192.168.1.80") for packet in packets]) == []


def test_queries_from_other_hosts_are_not_answers():
    """A browsing phone repeats the answers it knows in its question; it is not the TV."""
    question = name(*SERVICE) + struct.pack("!HH", TYPE_PTR, 1)
    known_answer = record(name(*SERVICE), TYPE_PTR, name("Living Room TV", *SERVICE))
    query = message(known_answer, flags=0x0000, questions=question, question_count=1)
    assert parse_response(query) == []
    assert devices_from([(query, "192.168.1.23")]) == []
    as_response = message(known_answer, flags=0x8400, questions=question, question_count=1)
    assert devices_from([(as_response, "192.168.1.50")])[0]["name"] == "Living Room TV"


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        b"\x00" * 11,
        message(b"\xc0\x0c" + struct.pack("!HHIH", TYPE_PTR, 1, 0, 0)),  # a name that points at itself
        message(b"\xc0\x40" + struct.pack("!HHIH", TYPE_PTR, 1, 0, 0)),  # a pointer forwards
        message(b"\xc0"),  # half a pointer
        message(b"\x45abc"),  # label with reserved bits
        message(b"\x3fshort"),  # label longer than the packet
        message(record(name(*SERVICE), TYPE_A, b"\x01\x02\x03")),  # address of three bytes
        message(record(name(*SERVICE), TYPE_SRV, b"\x00\x00")),  # SRV too short
        message(record(name(*SERVICE), TYPE_TXT, b"\x09short")),  # TXT string past its record
        message(name(*SERVICE) + struct.pack("!HHIH", TYPE_PTR, 1, 0, 500)),  # data past the packet
        struct.pack("!HHHHHH", 0, 0x8400, 0, 65535, 65535, 65535),  # promises records it does not have
        struct.pack("!HHHHHH", 0, 0x8400, 3, 0, 0, 0),  # promises questions it does not have
        message(questions=(name(*SERVICE) + struct.pack("!HH", TYPE_PTR, 1)) * 5, question_count=5),  # too many
        b"\x00" * 9001,
    ],
)
def test_malformed_packets_raise_value_error_and_yield_nothing(packet):
    with pytest.raises(ValueError):
        parse_response(packet)
    assert devices_from([(packet, "192.168.1.90")]) == []


def test_name_reader_limits():
    looping = b"\x01a\xc0\x00"  # "a" then back to the start, for ever
    with pytest.raises(ValueError):
        read_name(looping, 0)
    long_name = b"".join(b"\x3f" + b"x" * 63 for _ in range(5)) + b"\x00"  # 320 bytes; the limit is 255
    with pytest.raises(ValueError, match="too long"):
        read_name(long_name, 0)
    chained = name("local") + name("tv", pointer=0) + name("my", pointer=7)
    assert read_name(chained, 12) == (("my", "tv", "local"), 17)
    with pytest.raises(ValueError):
        discovery.encode_name(("x" * 64,))


def test_names_reached_through_pointers_are_remembered_per_packet():
    """The same results with and without the memory, including when the length limit is hit."""
    # 0: local   7: tv -> 0   12: my -> 7   17: your -> 7
    packet = name("local") + name("tv", pointer=0) + name("my", pointer=7) + name("your", pointer=7)
    seen: dict = {}
    assert read_name(packet, 12, seen) == (("my", "tv", "local"), 17) == read_name(packet, 12)
    assert seen == {12: (("my", "tv", "local"), 12), 7: (("tv", "local"), 9), 0: (("local",), 6)}
    assert read_name(packet, 17, seen) == (("your", "tv", "local"), 24) == read_name(packet, 17)
    assert read_name(packet, 7, seen) == (("tv", "local"), 12) == read_name(packet, 7)

    # A remembered name still counts towards the length limit of the name that continues with it.
    long_tail = b"".join(b"\x3f" + b"x" * 63 for _ in range(3)) + b"\x00"  # 0: 192 bytes of labels
    packet = long_tail + b"\x01y\xc0\x00" + b"\x3f" + b"z" * 63 + b"\xc0\x00"  # 193: short   197: too long
    seen = {}
    assert read_name(packet, 193, seen)[0] == ("y", "x" * 63, "x" * 63, "x" * 63)
    assert seen[0] == (("x" * 63,) * 3, 192)
    for memory in (seen, None):
        with pytest.raises(ValueError, match="too long"):
            read_name(packet, 197, memory)

    # Nothing is remembered about a name that turned out to be malformed.
    seen = {}
    with pytest.raises(ValueError):
        read_name(b"\x01a\x45" + b"\x01b\xc0\x00", 3, seen)
    assert seen == {}


def test_a_packet_made_to_be_expensive_is_cut_short():
    """Hundreds of tiny records that each point at one long name: about the worst a single
    packet can ask of the parser (which runs on the event loop).  Only the first MAX_RECORDS
    are looked at, and the long name is decoded once."""
    long_name = b"".join(b"\x01a" for _ in range(126)) + b"\x00"  # 253 bytes, 126 labels, at offset 12
    cheap = b"\xc0\x0c" + struct.pack("!HHIH", TYPE_PTR, 1, 120, 2) + b"\xc0\x0c"
    packet = message(record(long_name, TYPE_PTR, b"\xc0\x0c"), *[cheap] * 600)
    assert len(packet) < discovery.MAX_PACKET

    slices = 0

    class Counting(bytes):
        def __getitem__(self, key):
            nonlocal slices
            slices += isinstance(key, slice)
            return bytes.__getitem__(self, key)

    began = time.perf_counter()
    records = parse_response(Counting(packet))
    elapsed = time.perf_counter() - began
    assert len(records) == discovery.MAX_RECORDS == 64
    assert all(rec.name == ("a",) * 126 == rec.data for rec in records)
    assert slices == 126  # each label of the long name was cut out of the packet exactly once
    assert elapsed < 0.02  # more than 40 ms before

    answers = discovery._Answers()
    began = time.perf_counter()
    for _ in range(discovery.MAX_PACKETS + 50):
        answers.add(packet, "192.168.1.66")
    assert time.perf_counter() - began < 2.0  # a whole browse under fire: 12 s before
    assert answers.devices() == []


def test_records_up_to_the_limit_are_used_and_later_ones_ignored():
    padding = [record(name("other", "local"), TYPE_A, bytes([10, 0, 0, index])) for index in range(1, 62)]
    full = name("Den TV", *SERVICE)
    packet = message(
        record(name(*SERVICE), TYPE_PTR, full),
        *padding,  # 61 records about something else
        record(full, TYPE_SRV, struct.pack("!HHH", 0, 0, 7000) + name("den", "local")),
        record(name("den", "local"), TYPE_A, bytes([192, 168, 1, 77])),  # the 64th record
        record(name("den", "local"), TYPE_A, bytes([10, 9, 9, 9])),  # one too many
        b"\xc0",  # and whatever follows is not even read
    )
    packet = packet[:6] + struct.pack("!H", 200) + packet[8:]  # the header promises far more
    records = parse_response(packet)
    assert len(records) == 64 and records[-1].data == "192.168.1.77"
    assert devices_from([(packet, "192.168.1.200")]) == [{"name": "Den TV", "host": "192.168.1.77", "port": 7000}]


def test_random_damage_never_raises_anything_but_value_error():
    rng = random.Random(42)
    for _ in range(3000):
        damaged = bytearray(RESPONSE)
        for _ in range(rng.randrange(1, 5)):
            damaged[rng.randrange(len(damaged))] = rng.randrange(256)
        try:
            parse_response(bytes(damaged))
        except ValueError:
            pass
        assert isinstance(devices_from([(bytes(damaged), "192.168.1.50")]), list)
    for _ in range(1000):
        junk = rng.randbytes(rng.randrange(0, 80))
        assert isinstance(devices_from([(junk, "192.168.1.50")]), list)


def test_a_flood_of_packets_is_bounded():
    packets = []
    for index in range(1000):
        address = bytes([10, 0, index // 250, index % 250 + 1])
        packet = announcement(f"TV {index:04}", address, host=f"tv{index}")
        packets.append((packet, ".".join(str(part) for part in address)))
    found = devices_from(packets)
    assert len(found) == discovery.MAX_DEVICES
    assert found[0] == {"name": "TV 0000", "host": "10.0.0.1", "port": 6466}
    assert devices_from(packets[: discovery.MAX_PACKETS])[-1] == found[-1]  # later packets were not even parsed


# ---------------------------------------------------------------------------- avahi-browse output

AVAHI_OUTPUT = """\
+;wlan0;IPv4;Living\\032Room\\032TV;_androidtvremote2._tcp;local
+;wlan0;IPv6;Living\\032Room\\032TV;_androidtvremote2._tcp;local
=;wlan0;IPv6;Living\\032Room\\032TV;_androidtvremote2._tcp;local;Android.local;fe80::1;6466;"bt=AA:BB:CC:DD:EE:FF"
=;wlan0;IPv4;Living\\032Room\\032TV;_androidtvremote2._tcp;local;Android.local;192.168.1.50;6466;"bt=AA:BB:CC:DD:EE:FF"
=;eth0;IPv4;Living\\032Room\\032TV;_androidtvremote2._tcp;local;Android.local;192.168.1.50;6466;"bt=AA:BB;x"
=;wlan0;IPv4;K\\195\\188che\\.TV\\059\\0322;_androidtvremote2._tcp;local;k.local;192.168.1.51;6467;
=;wlan0;IPv4;Chromecast;_googlecast._tcp;local;c.local;192.168.1.52;8009;
=;wlan0;IPv4;Broken;_androidtvremote2._tcp;local;b.local;not-an-address;6466;
=;wlan0;IPv4;Broken;_androidtvremote2._tcp;local;b.local;192.168.1.53;port;
=;short
garbage line
"""


def test_parse_avahi_output():
    assert discovery.parse_avahi(AVAHI_OUTPUT) == [
        {"name": "Küche.TV; 2", "host": "192.168.1.51", "port": 6467},
        {"name": "Living Room TV", "host": "192.168.1.50", "port": 6466},
    ]
    assert discovery.parse_avahi("") == []


# ---------------------------------------------------------------------------- browsing


class Responder(asyncio.DatagramProtocol):
    """Stands in for the multicast group on a loopback port: answers every query."""

    def __init__(self, *answers: bytes):
        self.answers = answers
        self.queries: list[bytes] = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.queries.append(data)
        for answer in self.answers:
            self.transport.sendto(answer, addr)


async def responder(*answers: bytes):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(lambda: Responder(*answers), local_addr=("127.0.0.1", 0))
    return transport, protocol, transport.get_extra_info("sockname")[:2]


async def test_discover_asks_and_collects_answers():
    junk = b"\xff" * 40
    transport, protocol, address = await responder(junk, announcement("Loopback TV", bytes([127, 0, 0, 1])), RESPONSE)
    try:
        began = time.monotonic()
        found = await discovery.discover(0.25, target=address, avahi=None)
        elapsed = time.monotonic() - began
    finally:
        transport.close()
    assert found == [
        {"name": "Living Room TV", "host": "192.168.1.50", "port": 6466},
        {"name": "Loopback TV", "host": "127.0.0.1", "port": 6466},
    ]
    assert 0.2 <= elapsed < 0.6
    assert protocol.queries and set(protocol.queries) == {discovery.build_query(unicast_reply=True)}


async def test_discover_repeats_its_question(monkeypatch):
    assert discovery._RESEND_SECONDS == 1.0
    monkeypatch.setattr(discovery, "_RESEND_SECONDS", 0.1)
    transport, protocol, address = await responder()
    try:
        assert await discovery.discover(0.25, target=address, avahi=None) == []
    finally:
        transport.close()
    assert len(protocol.queries) == 3


async def test_discover_returns_nothing_quickly_when_nobody_answers():
    transport, _, address = await responder()
    transport.close()  # now a port nobody listens on
    began = time.monotonic()
    assert await discovery.discover(0.2, target=address, avahi=None) == []
    # A zero timeout is raised to the minimum, and a missing avahi-browse is not an error.
    assert await discovery.discover(0.0, target=address, avahi="/nonexistent/avahi-browse") == []
    assert time.monotonic() - began < 0.7
    assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []


async def test_discover_falls_back_to_avahi_only_when_sockets_found_nothing(tmp_path):
    tool = tmp_path / "avahi-browse"
    tool.write_text("#!/bin/sh\ncat <<'EOF'\n" + AVAHI_OUTPUT + "EOF\n")
    tool.chmod(0o755)
    transport, _, address = await responder()
    try:
        found = await discovery.discover(0.2, target=address, avahi=str(tool))
    finally:
        transport.close()
    assert [device["name"] for device in found] == ["Küche.TV; 2", "Living Room TV"]

    transport, _, address = await responder(announcement("Loopback TV", bytes([127, 0, 0, 1])))
    try:
        found = await discovery.discover(0.2, target=address, avahi=str(tool))
    finally:
        transport.close()
    assert [device["name"] for device in found] == ["Loopback TV"]

    failing = tmp_path / "failing"
    line = "=;wlan0;IPv4;X;_androidtvremote2._tcp;local;x.local;192.168.1.9;6466;"
    failing.write_text(f"#!/bin/sh\necho '{line}'\nexit 1\n")
    failing.chmod(0o755)
    transport, _, address = await responder()
    try:
        assert await discovery.discover(0.2, target=address, avahi=str(failing)) == []
    finally:
        transport.close()


async def test_discover_never_raises(monkeypatch, tmp_path):
    def no_sockets():
        raise OSError("Network is down")

    monkeypatch.setattr(discovery, "_query_socket", no_sockets)
    assert await discovery.discover(0.2, target=("127.0.0.1", 9), avahi=None) == []

    async def broken(timeout, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(discovery, "discover", broken)
    assert await AtvAssist(tmp_path / "atv").discover(0.2) == []


async def test_assist_discover_passes_results_through(monkeypatch, tmp_path):
    seen = []

    async def fake(timeout, **kwargs):
        seen.append(timeout)
        return [{"name": "Living Room TV", "host": "192.168.1.50", "port": 6466}]

    monkeypatch.setattr(discovery, "discover", fake)
    assert await AtvAssist(tmp_path / "atv").discover(3.0) == [
        {"name": "Living Room TV", "host": "192.168.1.50", "port": 6466}
    ]
    assert seen == [3.0]


async def test_cancelling_discover_closes_its_socket():
    transport, protocol, address = await responder()
    try:
        task = asyncio.create_task(discovery.discover(5.0, target=address, avahi=None))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        transport.close()
    assert len(protocol.queries) == 1
