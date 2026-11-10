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
      When the link closes, ``hci_conn_del`` adds the wrapped counter to an adapter-wide
      credit count, which *removes* one transmit credit per injected packet until the count
      is rebuilt.  Which count depends on the controller (``if (hdev->le_pkts) hdev->le_cnt
      += conn->sent; else hdev->acl_cnt += conn->sent;``):

      - a controller with LE buffers of its own loses LE credits (every LE device, game
        controllers included);
      - a controller that shares one buffer pool between classic and LE (it reports 0 LE
        buffers) loses credits of that shared pool, which also carries headsets and classic
        controllers.

      A count that lands exactly on zero with nothing in flight stalls all transmission of
      that pool until the adapter is reset; one that would go below zero wraps and is clamped
      back to full by the next completion event.  ``RawLink.settle`` repairs the count after
      the link has closed: it sends "LE Read Buffer Size" (read-only, opcode 0x2002), whose
      completion handler resets the LE count (``hci_cc_le_read_buffer_size``: ``hdev->le_cnt
      = hdev->le_pkts``), reads the controller's answer, and if that says "0 LE buffers" also
      sends "Read Buffer Size" (opcode 0x1005; ``hci_cc_read_buffer_size``: ``hdev->acl_cnt =
      hdev->acl_pkts``).  The second command is sent only when needed because resetting a
      count while the kernel has packets of its own in flight (music to a headset) briefly
      over-credits it.  The debt outlives this process: remote.py records it on disk when the
      first packet is injected and settles it at the next start if the service stopped while
      the link was still up.
    * Linux >= 6.17 (from 6.17-rc4) clamps both places.  Nothing leaks, but the kernel logs
      a warning ("hcon ... sent 0 < count 1") for every injected packet.
    * The first of the two clamps was backported: stable kernels from 6.16.5, 6.12.45,
      6.6.104, 6.1.150 and 5.15.191 on keep the counter at zero and log the same warning, so
      nothing is lost there either.  "Linux < 6.17" above therefore means mainline, and the
      stable releases before those.  A vendor kernel's version does not say which it is
      (upstream 6.11.11, which SteamOS 3.7 builds on, is unpatched), so the rule that decides
      (kernel.py) goes by 6.17 alone and the repair runs on everything older; on a patched
      kernel it resets a count that was never wrong.

    * A kernel L2CAP frame that the controller is still receiving in fragments could be
      interrupted by an injected packet.  Reports are sent on an idle link, well after
      connection set-up, and LE controllers with data length extension do not fragment at
      all, so this is unlikely; it would cost the TV one ATT response.

    The repair is derived from source like everything else here, and what it has to prevent
    (a stalled transmit queue for the user's game controllers) is worse than what this path
    cures.  remote.py therefore uses the path of its own accord only where no repair is
    needed (``bluetooth.raw_notify = auto``, :func:`direct_delivery_is_free`); on older
    kernels the user has to ask for it (``on``).

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

import asyncio
import ctypes
import errno
import fcntl
import logging
import os
import re
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Protocol

# The kernel rule lives in kernel.py (the doctor needs it without the rest of this package);
# the names stay available here, where the facts behind them are written down.
from .kernel import (  # noqa: F401 - re-exported
    FIXED_KERNEL,
    direct_delivery_is_free,
    kernel_needs_credit_repair,
    kernel_version,
)

log = logging.getLogger(__name__)

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_RAW = 0
#: _IOR('H', 212, int) from include/net/bluetooth/hci_sock.h
HCIGETCONNLIST = 0x800448D4

#: Socket option level and name for the event filter of a raw HCI socket (lib/bluetooth.h, hci_sock.h).
SOL_HCI = 0
HCI_FILTER = 2

HCI_COMMAND_PKT = 0x01
HCI_ACLDATA_PKT = 0x02
HCI_EVENT_PKT = 0x04
EVT_CMD_COMPLETE = 0x0E
#: hci_conn.type of an LE link (include/net/bluetooth/hci.h)
LE_LINK = 0x80
#: hci_conn.state values of a usable link (include/net/bluetooth/bluetooth.h)
BT_CONNECTED = 1
BT_CONFIG = 7
#: ... and of one whose HCI Disconnect has been sent and not yet completed.
BT_DISCONN = 8

L2CAP_CID_ATT = 0x0004
ATT_OP_HANDLE_VALUE_NOTIFICATION = 0x1B
#: Every LE controller accepts 27 bytes of ACL payload; minus L2CAP (4) and ATT (3) headers.
MAX_VALUE_LENGTH = 20

#: HCI command packet: LE Read Buffer Size (OGF 0x08, OCF 0x0002), no parameters.
OPCODE_LE_READ_BUFFER_SIZE = 0x2002
LE_READ_BUFFER_SIZE_COMMAND = bytes([HCI_COMMAND_PKT, 0x02, 0x20, 0x00])
#: HCI command packet: Read Buffer Size (OGF 0x04, OCF 0x0005), no parameters.
OPCODE_READ_BUFFER_SIZE = 0x1005
READ_BUFFER_SIZE_COMMAND = bytes([HCI_COMMAND_PKT, 0x05, 0x10, 0x00])
#: How long the controller's answer to LE Read Buffer Size is waited for, and how often it is looked for.
REPLY_WAIT = 0.5
REPLY_POLL = 0.02

MAX_CONNECTIONS = 32
# struct hci_conn_list_req { __u16 dev_id; __u16 conn_num; struct hci_conn_info conn_info[]; }
_LIST_HEADER = struct.Struct("=HH")
# struct hci_conn_info { __u16 handle; bdaddr_t bdaddr; __u8 type; __u8 out; __u16 state; __u32 link_mode; }
_CONN_INFO = struct.Struct("=H6sBBHI")
# struct sockaddr_hci { sa_family_t hci_family; unsigned short hci_dev; unsigned short hci_channel; }
_SOCKADDR_HCI = struct.Struct("=HHH")
# struct hci_ufilter { __u32 type_mask; __u32 event_mask[2]; __le16 opcode; } - 16 bytes with padding,
# and the kernel refuses a shorter option value.
_FILTER_MASKS = struct.Struct("=III")
#: Largest HCI event: packet type, event code, length byte, 255 bytes of parameters.
MAX_EVENT_SIZE = 258
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


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


def command_complete_filter(opcode: int) -> bytes:
    """Socket filter that lets through only the Command Complete event of ``opcode``.

    ``is_filtered_packet`` in hci_sock.c: bit ``packet type`` of ``type_mask``, bit ``event
    code`` of ``event_mask``, and - for Command Complete - an exact opcode match.
    """
    masks = _FILTER_MASKS.pack(1 << HCI_EVENT_PKT, 1 << EVT_CMD_COMPLETE, 0)
    return masks + struct.pack("<H", opcode) + b"\x00\x00"


def parse_command_complete(packet: bytes, opcode: int) -> bytes | None:
    """Return parameters (status first) if ``packet`` is the Command Complete event of ``opcode``.

    As read from a raw HCI socket: packet type, event code, parameter length, number of
    commands the controller accepts, opcode (little endian), return parameters.
    """
    if len(packet) < 6 or packet[0] != HCI_EVENT_PKT or packet[1] != EVT_CMD_COMPLETE:
        return None
    if int.from_bytes(packet[4:6], "little") != opcode:
        return None
    return bytes(packet[6:])


def le_buffer_count(params: bytes) -> int | None:
    """Number of LE data buffers from the return parameters of LE Read Buffer Size.

    ``struct hci_rp_le_read_buffer_size { status; le_mtu (2 bytes); le_max_pkt; }``.  None if
    the command failed or the answer is too short.  Zero means the controller has one pool
    for classic and LE data.
    """
    if len(params) < 4 or params[0] != 0:
        return None
    return params[3]


def boot_id() -> str:
    """Identifies the running kernel instance; its packet counters do not outlive it."""
    try:
        with open(BOOT_ID_PATH, encoding="ascii") as handle:
            return handle.read().strip()
    except (OSError, ValueError):
        return ""


def dev_id_from_adapter(adapter: str) -> int:
    """``"/org/bluez/hci0"`` or ``"hci0"`` -> 0."""
    match = re.search(r"hci(\d+)$", adapter)
    if not match:
        raise RawError(f"cannot tell the controller number from {adapter!r}")
    return int(match.group(1))


# --------------------------------------------------------------------------- OS access


def open_hci_socket(dev_id: int, channel: int) -> socket.socket:
    """An HCI socket bound to ``dev_id`` on ``channel``.  Raises ``OSError``.

    The numeric address family works even when Python was built without Bluetooth support;
    only bind() needs that support to build the address.  Python's own bind() can moreover
    only name the raw channel (before 3.14), so every other channel, and every Python without
    Bluetooth support, binds through libc with a ``struct sockaddr_hci`` packed here.
    """
    sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    try:
        if channel == HCI_CHANNEL_RAW and hasattr(socket, "AF_BLUETOOTH"):
            sock.bind((dev_id,))
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            address = _SOCKADDR_HCI.pack(AF_BLUETOOTH, dev_id, channel)
            if libc.bind(sock.fileno(), address, len(address)) != 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code))
    except BaseException:
        sock.close()
        raise
    return sock


class RawBackend(Protocol):
    """The few system calls direct delivery needs.  All methods raise ``OSError`` on failure."""

    def open(self, dev_id: int) -> None: ...

    def connections(self) -> list[HciConnection]: ...

    def send(self, packet: bytes) -> None: ...

    def watch(self, opcode: int) -> None:
        """From now on keep the controller's Command Complete events for ``opcode``."""

    def reply(self, opcode: int) -> bytes | None:
        """Return parameters of a Command Complete for ``opcode`` that has arrived; never waits."""

    def close(self) -> None: ...


class HciSocketBackend:
    """A raw HCI socket bound to one controller.

    The socket's event filter is left at its default (everything filtered out) unless
    :meth:`watch` asks for one command's answer, so nothing else is ever queued for us.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._dev_id = -1

    def open(self, dev_id: int) -> None:
        self.close()
        self._sock, self._dev_id = open_hci_socket(dev_id, HCI_CHANNEL_RAW), dev_id

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

    def watch(self, opcode: int) -> None:
        self._need().setsockopt(SOL_HCI, HCI_FILTER, command_complete_filter(opcode))

    def reply(self, opcode: int) -> bytes | None:
        sock = self._need()
        while True:
            try:
                packet = sock.recv(MAX_EVENT_SIZE, socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError):
                return None
            if not packet:
                return None
            params = parse_command_complete(packet, opcode)
            if params is not None:
                return params

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def explain_os_error(exc: OSError) -> str:
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
        #: Packets injected whose effect on the kernel's counters has not been settled yet.
        #: Kept across detach/attach and across TV links until :meth:`settle` ran.
        self.injected = 0

    @property
    def attached(self) -> bool:
        return self.connection is not None

    @property
    def repairs(self) -> bool:
        """Whether this kernel needs its counters repaired after injected packets."""
        return self._repair

    @property
    def owes(self) -> bool:
        """Injected packets have left the kernel's credit count in need of a repair."""
        return self._repair and self.injected > 0

    def assume_debt(self) -> None:
        """An earlier run of the service injected packets and could not settle (it was stopped
        while the TV link was up)."""
        self.injected = max(self.injected, 1)

    def attach(self, dev_id: int, tv_address: str) -> HciConnection:
        """Open the controller and find the TV's link.  Raises :class:`RawError`."""
        self.detach()
        backend = self._backend_factory()
        try:
            backend.open(dev_id)
            connection = find_tv_link(backend.connections(), tv_address)
        except OSError as exc:
            self._close(backend)
            raise RawError(explain_os_error(exc)) from None
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
            raise RawError(explain_os_error(exc)) from None
        self.injected += 1

    def detach(self) -> None:
        """Stop using the connection (delivery moved to bluetoothd, or the link is going).

        Never raises.  The count of injected packets is kept: the kernel's bookkeeping can
        only be repaired once the link is closed, see :meth:`settle`.
        """
        backend, self._backend = self._backend, None
        self.connection = None
        if backend is not None:
            self._close(backend)

    async def settle(self, dev_id: int) -> None:
        """Undo what the injected packets did to the kernel's counters.  Never raises.

        Must only be called once the TV link has closed (bluetoothd reports that after the
        kernel deleted the connection): the repair resets an adapter-wide credit count, which
        is wrong while injected packets are still in flight and pointless before
        ``hci_conn_del`` has subtracted them.  Takes up to :data:`REPLY_WAIT` because the
        controller's answer decides whether a second command is needed (module docstring);
        it never blocks the event loop.
        """
        self.detach()
        owed, self.injected = self.injected, 0
        if not owed or not self._repair:
            return
        backend = self._backend_factory()
        try:
            backend.open(dev_id)
            backend.watch(OPCODE_LE_READ_BUFFER_SIZE)
            backend.send(LE_READ_BUFFER_SIZE_COMMAND)
            params, waited = None, 0.0
            while params is None and waited < REPLY_WAIT:
                await asyncio.sleep(REPLY_POLL)
                waited += REPLY_POLL
                params = backend.reply(OPCODE_LE_READ_BUFFER_SIZE)
            buffers = le_buffer_count(params) if params is not None else None
            if buffers is None:
                log.warning(
                    "raw: asked the kernel to rebuild its LE credit count after %d injected packets, but the "
                    "controller's answer %s; if it shares its buffers between classic and LE, credits stay lost "
                    "until Bluetooth is switched off and on",
                    owed,
                    "did not arrive" if params is None else "reported a failure",
                )
            elif buffers == 0:
                # One pool for classic and LE: the credits were taken from the classic count.
                backend.send(READ_BUFFER_SIZE_COMMAND)
                log.debug("raw: rebuilt the kernel's shared credit count after %d injected packets", owed)
            else:
                log.debug("raw: rebuilt the kernel's LE credit count after %d injected packets", owed)
        except OSError as exc:
            if exc.errno in (errno.ENODEV, errno.ENETDOWN):
                # The controller is gone or off: its counters start from scratch when it is back.
                log.debug("raw: nothing to rebuild, %s", explain_os_error(exc))
            else:
                log.warning("raw: could not rebuild the kernel's credit count: %s", explain_os_error(exc))
        finally:
            self._close(backend)

    @staticmethod
    def _close(backend: RawBackend) -> None:
        try:
            backend.close()
        except OSError:
            pass
