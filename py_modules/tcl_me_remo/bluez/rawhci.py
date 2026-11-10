"""Direct delivery: ATT notifications injected through a raw HCI socket.

UNVERIFIED ON HARDWARE.  Everything here was derived from kernel and Android source code;
it has never been run against a controller or a TV.  Treat it as a designed experiment.

Why this exists
    bluetoothd keeps a client's notification subscription (the CCC descriptor value) only in
    memory.  After a SteamOS reboot a bonded Android 11-13 TV reconnects and still believes
    it is subscribed - correctly, the specification obliges a bonded peripheral to remember -
    so it never writes the CCC again, and bluetoothd silently drops every key report
    (``send_notification_to_device``: ``if (!ccc || !(ccc->value & 0x0003)) return;``).
    Android 14+ re-subscribes after the Service Changed indication and does not need this.

What it does
    It writes the notification straight to the controller, bypassing bluetoothd's bookkeeping.
    One packet = HCI ACL header (connection handle, packet-boundary flag 00 = start of a
    non-flushable host packet, as the kernel uses on LE) + L2CAP header (length, channel
    0x0004 = ATT) + ATT Handle Value Notification (opcode 0x1B, value handle, report).  The
    controller encrypts it like any other data on the link.  The TV's GATT client does not
    know or care who composed the packet; it forwards the value to whoever registered for the
    handle, which its HID host did locally when it connected.

Why the kernel accepts it (``net/bluetooth/hci_sock.c``, identical in v6.1 ... v7.0)
    ``hci_sock_sendmsg`` on a socket bound to ``HCI_CHANNEL_RAW`` takes ACL data packets from
    a process with ``CAP_NET_RAW``, queues them on ``hdev->raw_q``, and ``hci_tx_work`` hands
    them to the driver unmodified.  bluetoothd keeps working alongside: this is the channel
    ``hcitool`` and ``hcidump`` use.  The connection handle comes from ``ioctl(HCIGETCONNLIST)``.

What it costs - read this before relying on it
    Packets sent this way bypass the kernel's flow control: nothing is counted when they go
    out, but the controller's "Number Of Completed Packets" event for them is still applied
    to the connection (``hci_num_comp_pkts_evt``).

    * Linux < 6.17: ``conn->sent -= count`` is unchecked and wraps below zero.  While the
      counter reads exactly -1 (one injected packet more than the kernel has in flight - in
      practice between a key's press and its release) the scheduler never picks that link
      (``hci_chan_sent``: ``if (conn->sent < min)`` with ``min = ~0``), so bluetoothd's own
      traffic to the TV pauses for the length of the key press.  Harmless, as long as the
      release always follows; ``RawLink.notify`` therefore never leaves a press unanswered
      without telling the caller.
      When the link closes, ``hci_conn_del`` adds the wrapped counter to the adapter-wide
      LE credit count (``hdev->le_cnt += conn->sent``), which *removes* one transmit credit
      per injected packet for every LE device, game controllers included, until the count is
      rebuilt.  ``RawLink.link_closed`` repairs that by sending "LE Read Buffer Size" (a read-only
      HCI command, opcode 0x2002), whose completion handler resets the count
      (``hci_cc_le_read_buffer_size``: ``hdev->le_cnt = hdev->le_pkts``).  Controllers that
      share one buffer pool between classic and LE (they report 0 LE buffers) are not
      repaired by this; there the missing credits come out of the classic pool.
    * Linux >= 6.17 clamps both places.  Nothing leaks, but the kernel logs a warning
      ("hcon ... sent 0 < count 1") for every injected packet.
    * A kernel L2CAP frame that the controller is still receiving in fragments could be
      interrupted by an injected packet.  Reports are sent on an idle link, well after
      connection set-up, and LE controllers with data length extension do not fragment at
      all, so this is unlikely; it would cost the TV one ATT response.

What it assumes
    * The value handles are the ones the TV cached when it paired (the GATT table is frozen
      and its service handle pinned, see gatt.py).
    * The TV has registered for the notifications on this connection.  On the first
      connection after bluetoothd registered our service that is not certain (the Service
      Changed indication makes Android 11-13 drop its registrations until the next
      connection), which is why remote.py reconnects once before using this path.
    * The process is root (or has CAP_NET_RAW) and the kernel has Bluetooth sockets.

Layout: packet building and parsing are pure functions; :class:`HciSocketBackend` is the only
code that touches the operating system, and tests replace it.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import logging
import os
import platform
import re
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger(__name__)

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_RAW = 0
#: _IOR('H', 212, int) from include/net/bluetooth/hci_sock.h
HCIGETCONNLIST = 0x800448D4

HCI_COMMAND_PKT = 0x01
HCI_ACLDATA_PKT = 0x02
#: hci_conn.type of an LE link (include/net/bluetooth/hci.h)
LE_LINK = 0x80
#: hci_conn.state values of a usable link (include/net/bluetooth/bluetooth.h)
BT_CONNECTED = 1
BT_CONFIG = 7

L2CAP_CID_ATT = 0x0004
ATT_OP_HANDLE_VALUE_NOTIFICATION = 0x1B
#: Every LE controller accepts 27 bytes of ACL payload; minus L2CAP (4) and ATT (3) headers.
MAX_VALUE_LENGTH = 20

#: HCI command packet: LE Read Buffer Size (OGF 0x08, OCF 0x0002), no parameters.
LE_READ_BUFFER_SIZE_COMMAND = bytes([HCI_COMMAND_PKT, 0x02, 0x20, 0x00])
#: First kernel release that clamps the per-connection and adapter-wide packet counters.
FIXED_KERNEL = (6, 17)

MAX_CONNECTIONS = 32
# struct hci_conn_list_req { __u16 dev_id; __u16 conn_num; struct hci_conn_info conn_info[]; }
_LIST_HEADER = struct.Struct("=HH")
# struct hci_conn_info { __u16 handle; bdaddr_t bdaddr; __u8 type; __u8 out; __u16 state; __u32 link_mode; }
_CONN_INFO = struct.Struct("=H6sBBHI")
# struct sockaddr_hci { sa_family_t hci_family; unsigned short hci_dev; unsigned short hci_channel; }
_SOCKADDR_HCI = struct.Struct("=HHH")


class RawError(Exception):
    """Direct delivery is not possible; the message says why in plain words."""


@dataclass(frozen=True)
class HciConnection:
    handle: int
    address: str       # "AA:BB:CC:DD:EE:FF"
    link_type: int
    outgoing: bool     # we initiated it (never the case for the TV, which connects to us)
    state: int
    link_mode: int = 0

    @property
    def is_le(self) -> bool:
        return self.link_type == LE_LINK

    @property
    def is_up(self) -> bool:
        return self.state in (BT_CONNECTED, BT_CONFIG)


# --------------------------------------------------------------------------- pure functions


def format_bdaddr(raw: bytes) -> str:
    """``bdaddr_t`` stores the address least significant byte first; print it the usual way."""
    if len(raw) != 6:
        raise ValueError("a Bluetooth address has six bytes")
    return ":".join(f"{byte:02X}" for byte in reversed(raw))


def conn_list_request(dev_id: int, max_connections: int = MAX_CONNECTIONS) -> bytearray:
    """Buffer for ``ioctl(HCIGETCONNLIST)``: the request header plus room for the answer."""
    buf = bytearray(_LIST_HEADER.size + max_connections * _CONN_INFO.size)
    _LIST_HEADER.pack_into(buf, 0, dev_id, max_connections)
    return buf


def parse_conn_list(buf: bytes) -> list[HciConnection]:
    """Decode the buffer the kernel filled in for ``HCIGETCONNLIST``."""
    if len(buf) < _LIST_HEADER.size:
        raise ValueError("connection list is shorter than its header")
    _dev_id, count = _LIST_HEADER.unpack_from(buf, 0)
    available = (len(buf) - _LIST_HEADER.size) // _CONN_INFO.size
    connections = []
    for index in range(min(count, available)):
        handle, bdaddr, link_type, out, state, link_mode = _CONN_INFO.unpack_from(
            buf, _LIST_HEADER.size + index * _CONN_INFO.size
        )
        connections.append(HciConnection(handle, format_bdaddr(bdaddr), link_type, bool(out), state, link_mode))
    return connections


def find_tv_link(connections: list[HciConnection], tv_address: str) -> HciConnection:
    """Pick the LE link that belongs to the TV, or raise :class:`RawError`.

    The kernel lists the identity address once it has resolved the TV's private address
    (``hci_conn.dst`` is rewritten when the IRK matches).  Should it still show a private
    address, the TV is nevertheless the peer that connected *to us*, and if there is exactly
    one such LE link it is unambiguous.
    """
    links = [conn for conn in connections if conn.is_le and conn.is_up and conn.handle <= 0x0EFF]
    wanted = tv_address.strip().upper()
    for conn in links:
        if conn.address.upper() == wanted:
            return conn
    incoming = [conn for conn in links if not conn.outgoing]
    if len(incoming) == 1:
        log.info("raw: no link is listed under %s; using the only incoming LE link (%s)", wanted, incoming[0].address)
        return incoming[0]
    if not incoming:
        raise RawError("the controller reports no Bluetooth LE link from the TV")
    raise RawError(
        f"{len(incoming)} devices are connected to this machine over Bluetooth LE and none is listed as the TV"
    )


def att_notification_packet(conn_handle: int, value_handle: int, value: bytes) -> bytes:
    """One complete HCI packet carrying an ATT Handle Value Notification."""
    if not 0 <= conn_handle <= 0x0EFF:
        raise ValueError(f"invalid connection handle 0x{conn_handle:04X}")
    if not 1 <= value_handle <= 0xFFFF:
        raise ValueError(f"invalid attribute handle 0x{value_handle:04X}")
    if len(value) > MAX_VALUE_LENGTH:
        raise ValueError(f"a report of {len(value)} bytes does not fit an unfragmented LE packet")
    att = struct.pack("<BH", ATT_OP_HANDLE_VALUE_NOTIFICATION, value_handle) + bytes(value)
    l2cap = struct.pack("<HH", len(att), L2CAP_CID_ATT) + att
    # Packet-boundary flag 0b00 (bits 12-13) and broadcast flag 0b00 (bits 14-15): the handle as is.
    return struct.pack("<BHH", HCI_ACLDATA_PKT, conn_handle, len(l2cap)) + l2cap


def dev_id_from_adapter(adapter: str) -> int:
    """``"/org/bluez/hci0"`` or ``"hci0"`` -> 0."""
    match = re.search(r"hci(\d+)$", adapter)
    if not match:
        raise RawError(f"cannot tell the controller number from {adapter!r}")
    return int(match.group(1))


def kernel_needs_credit_repair(release: str | None = None) -> bool:
    """True for kernels that leak LE transmit credits after injected packets (see module docs)."""
    match = re.match(r"(\d+)\.(\d+)", release if release is not None else platform.release())
    if not match:
        return True  # unknown: the repair is harmless on a fixed kernel
    return (int(match.group(1)), int(match.group(2))) < FIXED_KERNEL


# --------------------------------------------------------------------------- OS access


class RawBackend(Protocol):
    """The few system calls direct delivery needs.  All methods raise ``OSError`` on failure."""

    def open(self, dev_id: int) -> None: ...

    def connections(self) -> list[HciConnection]: ...

    def send(self, packet: bytes) -> None: ...

    def close(self) -> None: ...


class HciSocketBackend:
    """A raw HCI socket bound to one controller.

    The socket's event filter is left at its default (everything filtered out), so nothing is
    ever queued for us to read.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._dev_id = -1

    def open(self, dev_id: int) -> None:
        self.close()
        # The numeric family works even when Python was built without Bluetooth support;
        # only address parsing in bind() needs that support.
        sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
        try:
            if hasattr(socket, "AF_BLUETOOTH"):
                sock.bind((dev_id,))
            else:
                self._bind_with_libc(sock.fileno(), dev_id)
        except BaseException:
            sock.close()
            raise
        self._sock, self._dev_id = sock, dev_id

    @staticmethod
    def _bind_with_libc(fd: int, dev_id: int) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        address = _SOCKADDR_HCI.pack(AF_BLUETOOTH, dev_id, HCI_CHANNEL_RAW)
        if libc.bind(fd, address, len(address)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def _need(self) -> socket.socket:
        if self._sock is None:
            raise OSError(errno.EBADF, "the HCI socket is not open")
        return self._sock

    def connections(self) -> list[HciConnection]:
        buf = conn_list_request(self._dev_id)
        fcntl.ioctl(self._need().fileno(), HCIGETCONNLIST, buf, True)
        return parse_conn_list(bytes(buf))

    def send(self, packet: bytes) -> None:
        # Tiny packets on a datagram-like socket: this never blocks in practice, and
        # MSG_DONTWAIT makes sure it cannot stall the event loop if it ever would.
        sent = self._need().send(packet, socket.MSG_DONTWAIT)
        if sent != len(packet):
            raise OSError(errno.EIO, f"short write to the HCI socket ({sent} of {len(packet)} bytes)")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def _explain(exc: OSError) -> str:
    if exc.errno in (errno.EPERM, errno.EACCES):
        return "the service lacks permission for raw Bluetooth access (it must run as root)"
    if exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
        return "this kernel has no Bluetooth socket support"
    if exc.errno == errno.ENODEV:
        return "the Bluetooth controller is gone"
    if exc.errno == errno.ENETDOWN:
        return "the Bluetooth controller is switched off"
    return exc.strerror or str(exc)


class RawLink:
    """Direct delivery to one TV connection: attach, notify, detach, and settle the bill."""

    def __init__(
        self,
        backend_factory: Callable[[], RawBackend] = HciSocketBackend,
        *,
        repair_credits: bool | None = None,
    ):
        self._backend_factory = backend_factory
        self._repair = kernel_needs_credit_repair() if repair_credits is None else repair_credits
        self._backend: RawBackend | None = None
        self.connection: HciConnection | None = None
        self.injected = 0  # packets injected on the current TV link (kept across detach/attach)

    @property
    def attached(self) -> bool:
        return self.connection is not None

    def attach(self, dev_id: int, tv_address: str) -> HciConnection:
        """Open the controller and find the TV's link.  Raises :class:`RawError`."""
        self.detach()
        backend = self._backend_factory()
        try:
            backend.open(dev_id)
            connection = find_tv_link(backend.connections(), tv_address)
        except OSError as exc:
            self._close(backend)
            raise RawError(_explain(exc)) from None
        except RawError:
            self._close(backend)
            raise
        self._backend, self.connection = backend, connection
        log.info("raw: attached to hci%d, connection handle 0x%04X (%s)", dev_id, connection.handle, connection.address)
        return connection

    def notify(self, value_handle: int, value: bytes) -> None:
        """Send one notification.  Raises :class:`RawError` if it could not be handed over."""
        if self._backend is None or self.connection is None:
            raise RawError("direct delivery is not attached to a connection")
        packet = att_notification_packet(self.connection.handle, value_handle, value)
        try:
            self._backend.send(packet)
        except OSError as exc:
            raise RawError(_explain(exc)) from None
        self.injected += 1

    def detach(self) -> None:
        """Stop using the connection (delivery moved to bluetoothd, or the link is going).

        Never raises.  The count of injected packets is kept: the kernel's bookkeeping can
        only be repaired once the link is closed, see :meth:`link_closed`.
        """
        backend, self._backend = self._backend, None
        self.connection = None
        if backend is not None:
            self._close(backend)

    def link_closed(self, dev_id: int) -> None:
        """The TV link is down: undo what the injected packets did to the kernel's counters.

        Never raises.  Must only be called after the link has closed - the repair resets the
        adapter's LE credit count, which is wrong while injected packets are still in flight,
        and pointless before ``hci_conn_del`` has subtracted them.
        """
        self.detach()
        injected, self.injected = self.injected, 0
        if not injected or not self._repair:
            return
        backend = self._backend_factory()
        try:
            backend.open(dev_id)
            backend.send(LE_READ_BUFFER_SIZE_COMMAND)
            log.debug("raw: asked the kernel to rebuild its LE credit count after %d injected packets", injected)
        except OSError as exc:
            log.warning("raw: could not rebuild the kernel's LE credit count: %s", _explain(exc))
        finally:
            self._close(backend)

    def reset(self) -> None:
        """The controller itself went away; its counters start from scratch when it returns."""
        self.detach()
        self.injected = 0

    @staticmethod
    def _close(backend: RawBackend) -> None:
        try:
            backend.close()
        except OSError:
            pass
