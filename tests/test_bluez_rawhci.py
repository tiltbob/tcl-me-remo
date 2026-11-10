"""Direct delivery: packet bytes, connection-list parsing and graceful failure."""

import errno
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


def test_credit_repair_command_is_le_read_buffer_size():
    # packet type 0x01 (command), opcode 0x2002 little endian, no parameters
    assert rawhci.LE_READ_BUFFER_SIZE_COMMAND == bytes.fromhex("01022000")


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
    two = [HciConnection(0x0041, "5A:00:00:00:00:01", 0x80, False, 1), HciConnection(0x0042, "5A:00:00:00:00:02", 0x80, False, 1)]
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


# --------------------------------------------------------------------------- RawLink with a fake backend


class Backend:
    def __init__(self, log, *, connections=(), open_error=None, send_error=None):
        self.log, self._connections, self._open_error, self.send_error = log, list(connections), open_error, send_error

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

    def close(self):
        self.log.append(("close",))


def test_raw_link_attach_notify_and_settle_after_the_link_closed():
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

    log.clear()
    link.link_closed(0)  # now the kernel has folded our packets into its LE credit count
    assert log == [("open", 0), ("send", "01022000"), ("close",)]
    assert link.injected == 0
    log.clear()
    link.link_closed(0)  # nothing owed: nothing sent
    assert log == []


def test_raw_link_does_not_repair_on_fixed_kernels_or_untouched_links():
    log = []
    link = RawLink(lambda: Backend(log, connections=[HciConnection(1, TV, 0x80, False, 1)]), repair_credits=False)
    link.attach(0, TV)
    link.notify(0x010A, b"\x00\x00")
    link.link_closed(0)
    assert ("send", "01022000") not in log

    log.clear()
    link = RawLink(lambda: Backend(log, connections=[HciConnection(1, TV, 0x80, False, 1)]), repair_credits=True)
    link.attach(0, TV)
    link.link_closed(0)  # attached but never used
    assert log == [("open", 0), ("close",)]


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


def test_real_backend_fails_cleanly_without_bluetooth_sockets():
    """In CI there is no Bluetooth stack; on a real machine without root this is EPERM."""
    link = RawLink(repair_credits=True)
    try:
        link.attach(0, TV)
    except RawError as exc:
        assert str(exc)
    else:  # pragma: no cover - only on a machine with a controller and a connected TV
        link.detach()
    assert not link.attached
    link.link_closed(0)  # never raises
