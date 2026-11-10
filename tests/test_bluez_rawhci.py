"""Direct delivery: packet bytes, connection-list parsing and graceful failure."""

import errno
import logging
import struct

import pytest

from tcl_me_remo import hid, keys
from tcl_me_remo.bluez import rawhci
from tcl_me_remo.bluez.rawhci import HciConnection, RawError, RawLink

TV = "AA:BB:CC:DD:EE:FF"


def conn_info(handle, address, link_type=0x80, out=0, state=1, link_mode=0) -> bytes:
    """One struct hci_conn_info as the kernel lays it out (bdaddr least significant byte first)."""
    bdaddr = bytes(reversed(bytes.fromhex(address.replace(":", ""))))
    return struct.pack("<H6sBBHI", handle, bdaddr, link_type, out, state, link_mode)


def conn_list(dev_id, *infos: bytes, room: int = 4) -> bytes:
    body = struct.pack("<HH", dev_id, len(infos)) + b"".join(infos)
    return body + bytes(16 * (room - len(infos)))


# --------------------------------------------------------------------------- packet building


def test_notification_packet_exact_bytes():
    # HOME = Consumer 0x0223 on report 2, value handle 0x010E, connection handle 0x0040.
    report_id, payload = hid.press_report(keys.resolve("HOME"))
    assert (report_id, payload) == (2, bytes.fromhex("23020000"))
    packet = rawhci.att_notification_packet(0x0040, 0x010E, payload)
    assert packet == bytes.fromhex(
        "02"            # HCI ACL data packet indicator
        "4000"          # connection handle 0x0040, PB = 00 (start, non-flushable), BC = 00
        "0b00"          # ACL data length: 11
        "0700"          # L2CAP length: 7
        "0400"          # channel 0x0004 (ATT)
        "1b"            # ATT Handle Value Notification
        "0e01"          # value handle 0x010E
        "23020000"      # the report
    )  # fmt: skip


def test_notification_packet_for_each_report_length():
    assert rawhci.att_notification_packet(0x0EFF, 0x0112, b"\x04").hex() == "02ff0e080004000400" + "1b1201" + "04"
    assert rawhci.att_notification_packet(1, 0x010A, b"\x28\x00").hex() == "020100090005000400" + "1b0a01" + "2800"
    # the release report is a notification like any other
    assert rawhci.att_notification_packet(1, 0x010A, b"\x00\x00")[-5:].hex() == "1b0a010000"


@pytest.mark.parametrize(
    "conn_handle, value_handle, value",
    [(0x0F00, 0x010A, b"\x00"), (-1, 0x010A, b"\x00"), (1, 0, b"\x00"), (1, 0x10000, b"\x00"), (1, 0x010A, bytes(21))],
)
def test_notification_packet_rejects_what_would_not_fit(conn_handle, value_handle, value):
    with pytest.raises(ValueError):
        rawhci.att_notification_packet(conn_handle, value_handle, value)


def test_credit_repair_commands():
    # packet type 0x01 (command), opcode little endian, no parameters
    assert rawhci.LE_READ_BUFFER_SIZE_COMMAND == bytes.fromhex("01022000")  # OGF 0x08, OCF 0x0002
    assert rawhci.OPCODE_LE_READ_BUFFER_SIZE == (0x08 << 10) | 0x0002
    assert rawhci.READ_BUFFER_SIZE_COMMAND == bytes.fromhex("01051000")     # OGF 0x04, OCF 0x0005
    assert rawhci.OPCODE_READ_BUFFER_SIZE == (0x04 << 10) | 0x0005


def test_command_complete_filter_is_a_struct_hci_ufilter():
    # { __u32 type_mask; __u32 event_mask[2]; __le16 opcode; } padded to 16 bytes:
    # bit 4 = HCI_EVENT_PKT, bit 14 = EVT_CMD_COMPLETE, opcode 0x2002.
    assert rawhci.command_complete_filter(0x2002) == bytes.fromhex("10000000" "00400000" "00000000" "0220" "0000")
    assert len(rawhci.command_complete_filter(0x1005)) == 16


def test_parse_command_complete_and_le_buffer_count():
    # As a raw socket delivers it: type 04, event 0E, length 07, ncmd 01, opcode 2002, status 00,
    # LE packet length 251, 8 LE buffers.
    event = bytes.fromhex("04" "0e" "07" "01" "0220" "00" "fb00" "08")
    params = rawhci.parse_command_complete(event, 0x2002)
    assert params == bytes.fromhex("00fb0008")
    assert rawhci.le_buffer_count(params) == 8
    assert rawhci.le_buffer_count(bytes.fromhex("00000000")) == 0  # one pool shared with classic
    assert rawhci.le_buffer_count(bytes.fromhex("0cfb0008")) is None  # status: command disallowed
    assert rawhci.le_buffer_count(b"\x00\xfb") is None

    assert rawhci.parse_command_complete(event, 0x1005) is None                       # another command's answer
    assert rawhci.parse_command_complete(bytes.fromhex("040f0400010220"), 0x2002) is None  # Command Status
    assert rawhci.parse_command_complete(bytes.fromhex("02410007"), 0x2002) is None   # ACL data
    assert rawhci.parse_command_complete(b"\x04\x0e", 0x2002) is None


# --------------------------------------------------------------------------- conn list


def test_conn_list_request_layout():
    buf = rawhci.conn_list_request(3, max_connections=2)
    assert len(buf) == 4 + 2 * 16
    assert bytes(buf[:4]) == bytes.fromhex("03000200")
    assert rawhci.HCIGETCONNLIST == (2 << 30) | (4 << 16) | (ord("H") << 8) | 212


def test_parse_conn_list_and_bdaddr_byte_order():
    raw = conn_list(
        0,
        conn_info(0x0041, TV, link_type=0x80, out=0, state=1, link_mode=0x0004),
        conn_info(0x0003, "00:1A:7D:DA:71:13", link_type=0x01, out=1, state=1),
    )
    # The first entry's address bytes on the wire are reversed.
    assert raw[6:12] == bytes.fromhex("ffeeddccbbaa")
    connections = rawhci.parse_conn_list(raw)
    assert connections == [
        HciConnection(0x0041, TV, 0x80, False, 1, 0x0004),
        HciConnection(0x0003, "00:1A:7D:DA:71:13", 0x01, True, 1, 0),
    ]
    assert connections[0].is_le and connections[0].is_up
    assert not connections[1].is_le


def test_parse_conn_list_trusts_neither_count_nor_length():
    assert rawhci.parse_conn_list(struct.pack("<HH", 0, 0)) == []
    # the kernel says 5 entries but the buffer holds one
    assert len(rawhci.parse_conn_list(struct.pack("<HH", 0, 5) + conn_info(1, TV))) == 1
    with pytest.raises(ValueError):
        rawhci.parse_conn_list(b"\x00")
    with pytest.raises(ValueError):
        rawhci.format_bdaddr(b"\x00" * 5)


def test_find_tv_link_by_address_case_insensitive():
    links = [
        HciConnection(0x0040, "11:11:11:11:11:11", 0x80, True, 1),      # a BLE gamepad we connected to
        HciConnection(0x0041, TV, 0x80, False, 1),
        HciConnection(0x0042, TV, 0x01, False, 1),                      # same address over classic: not it
    ]  # fmt: skip
    assert rawhci.find_tv_link(links, TV.lower()).handle == 0x0041


def test_find_tv_link_falls_back_to_the_single_incoming_le_link():
    links = [
        HciConnection(0x0040, "11:11:11:11:11:11", 0x80, True, 1),
        HciConnection(0x0041, "5A:00:00:00:00:01", 0x80, False, 1),     # still listed under a private address
        HciConnection(0x0042, "22:22:22:22:22:22", 0x80, False, 4),     # not connected (BT_LISTEN-ish state)
    ]  # fmt: skip
    assert rawhci.find_tv_link(links, TV).handle == 0x0041


def test_find_tv_link_refuses_to_guess():
    with pytest.raises(RawError, match="no Bluetooth LE link"):
        rawhci.find_tv_link([HciConnection(0x0040, "11:11:11:11:11:11", 0x80, True, 1)], TV)
    two = [
        HciConnection(0x0041, "5A:00:00:00:00:01", 0x80, False, 1),
        HciConnection(0x0042, "5A:00:00:00:00:02", 0x80, False, 1),
    ]
    with pytest.raises(RawError, match="2 devices"):
        rawhci.find_tv_link(two, TV)


def test_dev_id_and_kernel_version_helpers():
    assert rawhci.dev_id_from_adapter("/org/bluez/hci0") == 0
    assert rawhci.dev_id_from_adapter("hci12") == 12
    with pytest.raises(RawError):
        rawhci.dev_id_from_adapter("/org/bluez")
    assert rawhci.kernel_needs_credit_repair("6.11.11-valve12-1-neptune-611")
    assert rawhci.kernel_needs_credit_repair("6.16.4-arch1-1")
    assert not rawhci.kernel_needs_credit_repair("6.17.0")
    assert not rawhci.kernel_needs_credit_repair("7.0.1-1")
    assert rawhci.kernel_needs_credit_repair("weird")


@pytest.mark.parametrize(
    "release, version, free",
    [
        ("7.2.7-valve3-1-neptune", (7, 2), True),
        ("6.17.0", (6, 17), True),
        ("6.17.2-arch1-1", (6, 17), True),
        ("6.100.1", (6, 100), True),                   # compared as numbers, not as text
        ("6.16.12-200.fc42.x86_64", (6, 16), False),
        ("6.11.11-valve19", (6, 11), False),
        ("6.1.52-valve16-1-neptune-61", (6, 1), False),
        ("5.17.3", (5, 17), False),                    # the major version counts first
        ("garbage", None, False),
        ("", None, False),
        ("v6.17", None, False),
        ("6", None, False),
    ],
)
def test_direct_delivery_is_free_only_on_kernels_known_to_clamp(release, version, free):
    """What ``raw_notify = auto`` rests on: unknown counts as old."""
    assert rawhci.kernel_version(release) == version
    assert rawhci.direct_delivery_is_free(release) is free
    assert rawhci.kernel_needs_credit_repair(release) is not free


def test_kernel_helpers_default_to_the_running_kernel(monkeypatch):
    monkeypatch.setattr(rawhci.platform, "release", lambda: "6.18.44-fc-v338")
    assert rawhci.kernel_version() == (6, 18) and rawhci.direct_delivery_is_free()
    monkeypatch.setattr(rawhci.platform, "release", lambda: "6.11.11-valve19")
    assert not rawhci.direct_delivery_is_free() and rawhci.kernel_needs_credit_repair()


# --------------------------------------------------------------------------- RawLink with a fake backend


class Backend:
    """``le_buffers``: what the controller answers to LE Read Buffer Size (None: nothing)."""

    def __init__(self, log, *, connections=(), open_error=None, send_error=None, le_buffers=8, status=0):
        self.log, self._connections, self._open_error, self.send_error = log, list(connections), open_error, send_error
        self._le_buffers, self._status, self._watched, self._answered = le_buffers, status, None, False

    def open(self, dev_id):
        if self._open_error:
            raise self._open_error
        self.log.append(("open", dev_id))

    def connections(self):
        return self._connections

    def send(self, packet):
        if self.send_error:
            raise self.send_error
        self.log.append(("send", packet.hex()))

    def watch(self, opcode):
        self._watched = opcode
        self.log.append(("watch", hex(opcode)))

    def reply(self, opcode):
        asked = ("send", rawhci.LE_READ_BUFFER_SIZE_COMMAND.hex()) in self.log
        if self._le_buffers is None or self._answered or not asked or self._watched != opcode:
            return None
        self._answered = True
        return bytes([self._status, 0xFB, 0x00, self._le_buffers])

    def close(self):
        self.log.append(("close",))


@pytest.fixture(autouse=True)
def quick_replies(monkeypatch):
    monkeypatch.setattr(rawhci, "REPLY_WAIT", 0.05)
    monkeypatch.setattr(rawhci, "REPLY_POLL", 0.005)


async def test_raw_link_attach_notify_and_settle_after_the_link_closed():
    log = []
    link = RawLink(lambda: Backend(log, connections=[HciConnection(0x0041, TV, 0x80, False, 1)]), repair_credits=True)
    assert link.attach(0, TV).handle == 0x0041 and link.attached
    link.notify(0x0112, b"\x04")
    link.notify(0x0112, b"\x00")
    assert link.injected == 2

    link.detach()  # delivery moved to BlueZ while the link stays up: nothing to repair yet
    assert not link.attached and link.injected == 2
    assert log == [
        ("open", 0),
        ("send", "024100080004000400" + "1b1201" + "04"),
        ("send", "024100080004000400" + "1b1201" + "00"),
        ("close",),
    ]

    assert link.owes
    log.clear()
    await link.settle(0)  # now the kernel has folded our packets into its LE credit count
    # The controller has LE buffers of its own (8): rebuilding the LE count is all it takes.
    assert log == [("open", 0), ("watch", "0x2002"), ("send", "01022000"), ("close",)]
    assert link.injected == 0 and not link.owes
    log.clear()
    await link.settle(0)  # nothing owed: nothing sent
    assert log == []


async def test_settle_repairs_the_shared_pool_of_a_controller_without_le_buffers():
    """Such a controller reports 0 LE buffers; the kernel then took the credits from acl_cnt."""
    log = []
    link = RawLink(
        lambda: Backend(log, connections=[HciConnection(0x0041, TV, 0x80, False, 1)], le_buffers=0),
        repair_credits=True,
    )
    link.attach(0, TV)
    link.notify(0x010E, bytes(4))
    log.clear()
    await link.settle(0)
    # The socket of the link is closed; on a new one: LE Read Buffer Size (whose answer says
    # "shared"), then Read Buffer Size, which resets acl_cnt.
    assert log == [
        ("close",), ("open", 0), ("watch", "0x2002"), ("send", "01022000"), ("send", "01051000"), ("close",)
    ]  # fmt: skip
    assert not link.owes


@pytest.mark.parametrize(
    "backend_args, reason", [({"le_buffers": None}, "did not arrive"), ({"status": 0x0C}, "failure")]
)
async def test_settle_does_not_guess_when_the_controller_does_not_say(backend_args, reason, caplog):
    """Resetting the classic count needlessly would over-credit a pool that carries audio."""
    log = []
    link = RawLink(
        lambda: Backend(log, connections=[HciConnection(1, TV, 0x80, False, 1)], **backend_args), repair_credits=True
    )
    link.attach(0, TV)
    link.notify(0x010A, bytes(8))
    log.clear()
    await link.settle(0)
    assert log == [("close",), ("open", 0), ("watch", "0x2002"), ("send", "01022000"), ("close",)]
    assert reason in caplog.text and not link.owes


async def test_settle_never_raises(caplog):
    caplog.set_level(logging.DEBUG)
    log = []
    backend = Backend(log, connections=[HciConnection(1, TV, 0x80, False, 1)])
    link = RawLink(lambda: backend, repair_credits=True)
    link.attach(0, TV)
    link.notify(0x010A, bytes(8))
    # The controller was unplugged or switched off: its counters start afresh, nothing to repair.
    backend.send_error = OSError(errno.ENETDOWN, "Network is down")
    await link.settle(0)
    assert not link.owes and "could not rebuild" not in caplog.text and "nothing to rebuild" in caplog.text
    assert log[-1] == ("close",)

    link.attach(0, TV)
    backend.send_error = None
    link.notify(0x010A, bytes(8))
    backend.send_error = PermissionError(errno.EPERM, "Operation not permitted")
    await link.settle(0)
    assert not link.owes and "could not rebuild the kernel's credit count" in caplog.text


async def test_raw_link_does_not_repair_on_fixed_kernels_or_untouched_links():
    log = []
    link = RawLink(lambda: Backend(log, connections=[HciConnection(1, TV, 0x80, False, 1)]), repair_credits=False)
    assert not link.repairs
    link.attach(0, TV)
    link.notify(0x010A, b"\x00\x00")
    assert not link.owes
    await link.settle(0)
    assert ("send", "01022000") not in log and ("watch", "0x2002") not in log

    log.clear()
    link = RawLink(lambda: Backend(log, connections=[HciConnection(1, TV, 0x80, False, 1)]), repair_credits=True)
    assert link.repairs
    link.attach(0, TV)
    await link.settle(0)  # attached but never used
    assert log == [("open", 0), ("close",)]


async def test_a_debt_from_an_earlier_run_is_settled_like_our_own():
    log = []
    link = RawLink(lambda: Backend(log), repair_credits=True)
    assert not link.owes
    link.assume_debt()
    assert link.owes
    await link.settle(0)
    assert ("send", "01022000") in log and not link.owes
    # On a kernel that needs no repair there is nothing to assume either.
    fixed = RawLink(lambda: Backend(log), repair_credits=False)
    fixed.assume_debt()
    assert not fixed.owes


@pytest.mark.parametrize(
    "error, text",
    [
        (PermissionError(errno.EPERM, "Operation not permitted"), "must run as root"),
        (OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol"), "no Bluetooth socket support"),
        (OSError(errno.ENODEV, "No such device"), "controller is gone"),
    ],
)
def test_raw_link_explains_why_it_cannot_attach(error, text):
    log = []
    link = RawLink(lambda: Backend(log, open_error=error), repair_credits=True)
    with pytest.raises(RawError, match=text):
        link.attach(0, TV)
    assert not link.attached and log == [("close",)]


def test_raw_link_attach_without_a_tv_link_closes_the_socket():
    log = []
    link = RawLink(lambda: Backend(log, connections=[]), repair_credits=True)
    with pytest.raises(RawError, match="no Bluetooth LE link"):
        link.attach(0, TV)
    assert log == [("open", 0), ("close",)]


def test_raw_link_send_failure_is_a_raw_error_and_not_counted():
    log = []
    backend = Backend(log, connections=[HciConnection(1, TV, 0x80, False, 1)])
    link = RawLink(lambda: backend, repair_credits=True)
    link.attach(0, TV)
    backend.send_error = OSError(errno.ENETDOWN, "Network is down")
    with pytest.raises(RawError, match="switched off"):
        link.notify(0x010A, b"\x00\x00")
    assert link.injected == 0
    with pytest.raises(RawError, match="not attached"):
        RawLink(lambda: backend).notify(0x010A, b"\x00\x00")


async def test_real_backend_fails_cleanly_without_bluetooth_sockets():
    """In CI there is no Bluetooth stack; on a real machine without root this is EPERM."""
    link = RawLink(repair_credits=True)
    try:
        link.attach(0, TV)
    except RawError as exc:
        assert str(exc)
    else:  # pragma: no cover - only on a machine with a controller and a connected TV
        link.detach()
    assert not link.attached
    link.assume_debt()
    await link.settle(0)  # never raises
    assert not link.owes


def test_boot_id_names_the_running_kernel(monkeypatch, tmp_path):
    path = tmp_path / "boot_id"
    path.write_text("2a8bdd28-b770-4695-a089-8d49ad00f77c\n")
    monkeypatch.setattr(rawhci, "BOOT_ID_PATH", str(path))
    assert rawhci.boot_id() == "2a8bdd28-b770-4695-a089-8d49ad00f77c"
    monkeypatch.setattr(rawhci, "BOOT_ID_PATH", str(tmp_path / "missing"))
    assert rawhci.boot_id() == ""


# --------------------------------------------------------------------------- the real backend's system calls


class FakeSocket:
    instances: list["FakeSocket"] = []

    def __init__(self, family, kind, proto):
        self.args = (family, kind, proto)
        self.bound = None
        self.sent = []
        self.closed = False
        FakeSocket.instances.append(self)

    def bind(self, address):
        self.bound = address

    def fileno(self):
        return 77

    def send(self, data, flags):
        self.sent.append((bytes(data), flags))
        return len(data)

    def setsockopt(self, level, name, value):
        self.options = getattr(self, "options", []) + [(level, name, bytes(value))]

    def recv(self, size, flags):
        self.recv_args = (size, flags)
        incoming = getattr(self, "incoming", [])
        if not incoming:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        return incoming.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch):
    FakeSocket.instances = []
    monkeypatch.setattr(rawhci.socket, "socket", FakeSocket)
    return FakeSocket.instances


def test_backend_with_python_bluetooth_support(fake_socket, monkeypatch):
    monkeypatch.setattr(rawhci.socket, "AF_BLUETOOTH", 31, raising=False)
    seen = {}

    def ioctl(fd, request, buf, mutate):
        seen.update(fd=fd, request=request, mutate=mutate, asked=bytes(buf[:4]))
        answer = conn_list(1, conn_info(0x0041, TV))
        buf[: len(answer)] = answer
        return 0

    monkeypatch.setattr(rawhci.fcntl, "ioctl", ioctl)
    backend = rawhci.HciSocketBackend()
    backend.open(1)
    sock = fake_socket[0]
    assert sock.args == (31, rawhci.socket.SOCK_RAW, 1)  # AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI
    assert sock.bound == (1,)                             # hci1, raw channel

    assert backend.connections() == [HciConnection(0x0041, TV, 0x80, False, 1, 0)]
    assert seen == {"fd": 77, "request": 0x800448D4, "mutate": True, "asked": bytes.fromhex("01002000")}

    backend.send(b"\x02\x41\x00")
    assert sock.sent == [(b"\x02\x41\x00", rawhci.socket.MSG_DONTWAIT)]
    backend.close()
    assert sock.closed
    with pytest.raises(OSError):
        backend.send(b"\x02")


def test_backend_binds_through_libc_when_python_has_no_bluetooth_support(fake_socket, monkeypatch):
    monkeypatch.delattr(rawhci.socket, "AF_BLUETOOTH", raising=False)
    calls = []

    class Libc:
        def bind(self, fd, address, length):
            calls.append((fd, bytes(address), length))
            return 0 if len(calls) == 1 else -1

    monkeypatch.setattr(rawhci.ctypes, "CDLL", lambda name, use_errno: Libc())
    monkeypatch.setattr(rawhci.ctypes, "get_errno", lambda: errno.ENODEV)
    backend = rawhci.HciSocketBackend()
    backend.open(2)
    # struct sockaddr_hci { family = AF_BLUETOOTH (31), dev = 2, channel = HCI_CHANNEL_RAW (0) }
    assert calls == [(77, bytes.fromhex("1f00" "0200" "0000"), 6)]
    assert fake_socket[0].bound is None and not fake_socket[0].closed

    with pytest.raises(OSError) as err:
        backend.open(3)  # bind() fails: the socket must not leak
    assert err.value.errno == errno.ENODEV
    assert fake_socket[0].closed and fake_socket[1].closed


def test_backend_short_write_is_an_error(fake_socket, monkeypatch):
    monkeypatch.setattr(rawhci.socket, "AF_BLUETOOTH", 31, raising=False)
    monkeypatch.setattr(FakeSocket, "send", lambda self, data, flags: 1)
    backend = rawhci.HciSocketBackend()
    backend.open(0)
    with pytest.raises(OSError, match="short write"):
        backend.send(b"\x02\x41\x00")


def test_backend_reads_a_command_reply_without_waiting(fake_socket, monkeypatch):
    monkeypatch.setattr(rawhci.socket, "AF_BLUETOOTH", 31, raising=False)
    backend = rawhci.HciSocketBackend()
    backend.open(0)
    sock = fake_socket[0]
    backend.watch(rawhci.OPCODE_LE_READ_BUFFER_SIZE)
    # setsockopt(SOL_HCI = 0, HCI_FILTER = 2, struct hci_ufilter)
    assert sock.options == [(0, 2, bytes.fromhex("10000000004000000000000002200000"))]

    assert backend.reply(rawhci.OPCODE_LE_READ_BUFFER_SIZE) is None  # nothing there yet: no blocking
    assert sock.recv_args == (258, rawhci.socket.MSG_DONTWAIT)
    sock.incoming = [
        bytes.fromhex("040e0401051000"),          # somebody else's Command Complete: skipped
        bytes.fromhex("040e0701022000fb0000"),    # ours: status 0, 251 bytes, 0 LE buffers
    ]
    params = backend.reply(rawhci.OPCODE_LE_READ_BUFFER_SIZE)
    assert params == bytes.fromhex("00fb0000") and rawhci.le_buffer_count(params) == 0
    assert backend.reply(rawhci.OPCODE_LE_READ_BUFFER_SIZE) is None
    backend.close()
    with pytest.raises(OSError):
        backend.watch(rawhci.OPCODE_LE_READ_BUFFER_SIZE)
