"""Ending the TV link at once: the kernel's management command "Disconnect".

UNVERIFIED ON HARDWARE.  Like rawhci.py this was derived from kernel and BlueZ source code
and has never been run against a controller.

Why this exists
    ``Device1.Disconnect()`` does not drop a link, it starts bluetoothd's two second
    ``DISCONNECT_TIMER`` (src/device.c ``device_request_disconnect``) and drops the link when
    that fires.  Before suspend and shutdown the service has no two seconds: the machine goes
    down with the link still up and the *kernel* ends it, with reason 0x15 "remote device
    terminated connection due to power off" (``hci_suspend_sync`` / ``hci_power_off_sync``:
    ``hci_disconnect_all_sync(hdev, HCI_ERROR_REMOTE_POWER_OFF)``).  Several Android versions'
    HID host re-arm their background connection to a remote only for certain reasons; 0x13
    "remote user terminated connection", which real remotes send, is among them in Android
    13 and 14, 0x15 is not (research notes, correction C2; Android 16 re-arms for any
    reason, Android 11 and 12 for a time-out only).  Whether a TV calls back after the
    machine wakes may therefore depend on which of the two it was told.

What it does
    It sends the one command bluetoothd itself sends when its timer fires (src/adapter.c
    ``btd_adapter_disconnect_device``: ``mgmt_send(adapter->mgmt, MGMT_OP_DISCONNECT, ...)``),
    without the timer.  The kernel's handler (net/bluetooth/mgmt.c ``disconnect``) passes
    ``HCI_ERROR_REMOTE_USER_TERM`` - reason 0x13 - in every version read (v6.1, v6.6: ``err =
    hci_disconnect(conn, HCI_ERROR_REMOTE_USER_TERM);`` v6.11, v6.17, master:
    ``hci_abort_conn(conn, HCI_ERROR_REMOTE_USER_TERM);`` from ``disconnect_sync``).

    A management packet is a six byte header (command code, controller index, parameter
    length; little endian) followed by the parameters, here the peer's address (six bytes,
    least significant first) and its address type (1 = LE public, 2 = LE random) - BlueZ
    doc/mgmt-api.txt, "Packet Structures" and "Disconnect Command" (code 0x0014).  It is
    written to an HCI socket bound to ``HCI_DEV_NONE`` on ``HCI_CHANNEL_CONTROL``
    (hci_sock.c ``hci_sock_bind``: any other device index is ``-EINVAL``); a process with
    ``CAP_NET_ADMIN`` gets a trusted socket, which Disconnect requires (``hci_mgmt_cmd``:
    otherwise ``MGMT_STATUS_PERMISSION_DENIED``).

Living next to bluetoothd
    Any number of control sockets may be open; ``btmgmt`` works next to a running bluetoothd
    the same way.  The "Device Disconnected" event goes to every control socket
    (``mgmt_device_disconnected`` -> ``mgmt_event``), and bluetoothd handles it whoever asked
    for the disconnect, exactly as it handles a link the TV closed (src/adapter.c
    ``disconnected_callback`` -> ``dev_disconnected`` -> ``adapter_remove_connection``).  So
    ``Device1.Connected`` turns false as usual and nothing in remote.py needs to know how the
    link ended.

    Two things to know.  The first command for a controller on a control socket marks the
    controller as managed (``mgmt_init_hdev``), which bluetoothd has long done; remote.py
    only ever comes here for a link bluetoothd reported, so that is never our doing.  And
    before Linux 6.11 only one Disconnect may be pending per controller (``pending_find(
    MGMT_OP_DISCONNECT, hdev)`` -> ``MGMT_STATUS_BUSY``): while ours waits for the
    controller's Disconnection Complete, normally a few tens of milliseconds, a disconnect
    bluetoothd wants for another device is refused, and the other way round.  A refusal on
    our side makes remote.py fall back to ``Device1.Disconnect()``.

Which link
    The kernel looks the link up by address *and* address type (``hci_conn_hash_lookup_le(
    hdev, &cp->addr.bdaddr, le_addr_type(cp->addr.type))``), under the address it tracks the
    connection by: the identity address once it knows the peer's IRK, which is always the
    case for a bonded TV that bluetoothd reports as connected.  bluetoothd's
    ``Device1.Address`` / ``AddressType`` are that pair (it uses them for its own Disconnect),
    so they are what we send.  The kernel's own connection list (rawhci.py,
    ``HCIGETCONNLIST``) is consulted first: should it list the TV's link under another
    address, that address is used instead.  Such an address is the one the TV used on the
    air and not its identity, hence a private address, and private addresses are random
    addresses by definition (Core specification, Vol 6, Part B, 1.3.2).

Layout: packet building and parsing are pure functions; :class:`MgmtSocketBackend` is the only
code that touches the operating system, and tests replace it.
"""

from __future__ import annotations

import errno
import logging
import socket
import struct
from typing import Callable, Protocol

from . import rawhci
from .rawhci import HciConnection, RawBackend, RawError

log = logging.getLogger(__name__)

#: include/net/bluetooth/hci_sock.h
HCI_CHANNEL_CONTROL = 3
HCI_DEV_NONE = 0xFFFF

OP_DISCONNECT = 0x0014
EV_CMD_COMPLETE = 0x0001
EV_CMD_STATUS = 0x0002

BDADDR_LE_PUBLIC = 0x01
BDADDR_LE_RANDOM = 0x02

STATUS_SUCCESS = 0x00
#: The statuses Disconnect can answer with, in plain words (doc/mgmt-api.txt "Error Codes").
_STATUS_TEXT = {
    0x01: "the kernel does not know the command",
    0x02: "the kernel has no such link",
    0x03: "the kernel could not do it",
    0x0A: "another disconnect is under way on this controller",
    0x0D: "the kernel rejected the address",
    # What kernels from 6.11 on answer when the link is not there: they look for it in a work
    # item after accepting the command, and -ENOTCONN maps to "disconnected", not 0x02.
    0x0E: "the kernel no longer has such a link",
    0x0F: "the Bluetooth controller is switched off",
    0x11: "the kernel has no such Bluetooth controller",
    0x14: "the service lacks permission to manage Bluetooth (it must run as root)",
}

# struct mgmt_hdr { __le16 opcode; __le16 index; __le16 len; }
_HEADER = struct.Struct("<HHH")
#: Largest packet read at once; the replies looked for are 9 and 16 bytes long.
MAX_PACKET_SIZE = 512
#: Packets looked at per poll before giving the event loop its turn back.
MAX_PACKETS_PER_POLL = 32


class MgmtError(Exception):
    """The link cannot be ended this way; the message says why in plain words."""


# --------------------------------------------------------------------------- pure functions


def parse_bdaddr(address: str) -> bytes:
    """``"AA:BB:CC:DD:EE:FF"`` -> ``bdaddr_t`` (least significant byte first)."""
    parts = address.strip().split(":")
    if len(parts) != 6 or any(len(part) != 2 for part in parts):
        raise ValueError(f"not a Bluetooth address: {address!r}")
    return bytes(int(part, 16) for part in reversed(parts))


def le_address_type(bluez_type: str) -> int:
    """``Device1.AddressType`` (``"public"`` / ``"random"``) as a management address type."""
    try:
        return {"public": BDADDR_LE_PUBLIC, "random": BDADDR_LE_RANDOM}[bluez_type]
    except KeyError:
        raise MgmtError(f"BlueZ did not say what kind of address the TV has ({bluez_type!r})") from None


def disconnect_command(dev_id: int, address: str, address_type: int) -> bytes:
    """The complete management packet for "Disconnect" (reason 0x13 is the kernel's choice)."""
    if not 0 <= dev_id < HCI_DEV_NONE:
        raise ValueError(f"invalid controller index {dev_id}")
    if address_type not in (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM):
        raise ValueError(f"not a Bluetooth LE address type: {address_type}")
    params = parse_bdaddr(address) + bytes([address_type])
    return _HEADER.pack(OP_DISCONNECT, dev_id, len(params)) + params


def parse_reply(packet: bytes, dev_id: int, opcode: int = OP_DISCONNECT) -> int | None:
    """Status from the Command Complete / Command Status event answering ``opcode``, else None.

    Both events start their parameters with the command code and a status byte.  Everything
    else a control socket receives (other sockets' events, other controllers) is None.
    """
    if len(packet) < _HEADER.size + 3:
        return None
    event, index, length = _HEADER.unpack_from(packet, 0)
    if event not in (EV_CMD_COMPLETE, EV_CMD_STATUS) or index != dev_id or length < 3:
        return None
    if int.from_bytes(packet[6:8], "little") != opcode:
        return None
    return packet[8]


def describe_status(status: int) -> str:
    return _STATUS_TEXT.get(status, f"the kernel answered with status 0x{status:02X}")


def disconnect_target(
    tv_address: str, bluez_type: str, connections: list[HciConnection] | None
) -> tuple[str, int]:
    """The (address, management address type) the kernel knows the TV's link by.

    ``connections`` is the kernel's connection list, or None if it could not be read; then
    bluetoothd's pair is all there is, which is also what bluetoothd itself would send.
    Raises :class:`MgmtError` rather than pick a link that might belong to something else.
    """
    wanted = tv_address.strip().upper()
    if connections is not None:
        try:
            link = rawhci.find_tv_link(connections, wanted)
        except RawError as exc:
            raise MgmtError(str(exc)) from None
        if link.address.upper() != wanted:
            # Listed under the address the TV used on the air: a private, hence random, one.
            return link.address.upper(), BDADDR_LE_RANDOM
    return wanted, le_address_type(bluez_type)


# --------------------------------------------------------------------------- OS access


class MgmtBackend(Protocol):
    """A management control socket.  All methods raise ``OSError`` on failure."""

    def open(self) -> None: ...

    def send(self, packet: bytes) -> None: ...

    def receive(self) -> bytes | None:
        """One packet that has arrived, or None; never waits."""

    def close(self) -> None: ...


class MgmtSocketBackend:
    """An HCI socket on the control channel (``HCI_DEV_NONE``, ``HCI_CHANNEL_CONTROL``)."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None

    def open(self) -> None:
        self.close()
        self._sock = rawhci.open_hci_socket(HCI_DEV_NONE, HCI_CHANNEL_CONTROL)

    def _need(self) -> socket.socket:
        if self._sock is None:
            raise OSError(errno.EBADF, "the management socket is not open")
        return self._sock

    def send(self, packet: bytes) -> None:
        # The kernel runs the command inside this call and never blocks on the controller.
        sent = self._need().send(packet, socket.MSG_DONTWAIT)
        if sent != len(packet):
            raise OSError(errno.EIO, f"short write to the management socket ({sent} of {len(packet)} bytes)")

    def receive(self) -> bytes | None:
        try:
            packet = self._need().recv(MAX_PACKET_SIZE, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            return None
        return packet or None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class LinkCloser:
    """Asks the kernel to end the TV's link now, and tells what the kernel said about it.

    ``request`` hands the command over and returns at once; ``status`` is polled afterwards
    (it never waits) and ``close`` gives the socket back.  One request at a time.
    """

    def __init__(
        self,
        backend_factory: Callable[[], MgmtBackend] = MgmtSocketBackend,
        raw_backend_factory: Callable[[], RawBackend] = rawhci.HciSocketBackend,
    ):
        self._backend_factory = backend_factory
        self._raw_backend_factory = raw_backend_factory
        self._backend: MgmtBackend | None = None
        self._dev_id = -1

    @property
    def pending(self) -> bool:
        return self._backend is not None

    def _kernel_connections(self, dev_id: int) -> list[HciConnection] | None:
        backend = self._raw_backend_factory()
        try:
            backend.open(dev_id)
            return backend.connections()
        except OSError as exc:
            log.debug("mgmt: could not read the kernel's connection list (%s)", rawhci.explain_os_error(exc))
            return None
        finally:
            try:
                backend.close()
            except OSError:
                pass

    def request(self, dev_id: int, tv_address: str, bluez_type: str) -> tuple[str, int]:
        """Hand "Disconnect" for the TV's link to the kernel.  Raises :class:`MgmtError`.

        Returns the address and address type that were named.  Does not wait for anything:
        when this returns the kernel has the command, and has either refused it (raised here
        if the refusal is already readable) or is carrying it out.
        """
        self.close()
        address, address_type = disconnect_target(tv_address, bluez_type, self._kernel_connections(dev_id))
        try:
            packet = disconnect_command(dev_id, address, address_type)
        except ValueError as exc:
            raise MgmtError(str(exc)) from None
        backend = self._backend_factory()
        try:
            backend.open()
            backend.send(packet)
        except OSError as exc:
            self._close(backend)
            raise MgmtError(rawhci.explain_os_error(exc)) from None
        self._backend, self._dev_id = backend, dev_id
        status = self.status()
        if status not in (None, STATUS_SUCCESS):
            self.close()
            raise MgmtError(describe_status(status))
        log.info(
            "mgmt: asked the kernel to end the link to %s (%s address) on hci%d",
            address,
            "public" if address_type == BDADDR_LE_PUBLIC else "random",
            dev_id,
        )
        return address, address_type

    def status(self) -> int | None:
        """The kernel's answer to the pending request if it has arrived (0 = the link is down)."""
        if self._backend is None:
            return None
        try:
            for _ in range(MAX_PACKETS_PER_POLL):
                packet = self._backend.receive()
                if packet is None:
                    return None
                status = parse_reply(packet, self._dev_id)
                if status is not None:
                    return status
        except OSError as exc:
            log.debug("mgmt: could not read the kernel's answer (%s)", rawhci.explain_os_error(exc))
        return None

    def close(self) -> None:
        """Never raises.  A request the kernel has accepted is carried out regardless."""
        backend, self._backend = self._backend, None
        if backend is not None:
            self._close(backend)

    @staticmethod
    def _close(backend: MgmtBackend) -> None:
        try:
            backend.close()
        except OSError:
            pass
