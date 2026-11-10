"""Ending the link through the kernel: packet bytes, the choice of address, graceful failure."""

import errno

import pytest

from tcl_me_remo.bluez import mgmt, rawhci
from tcl_me_remo.bluez.mgmt import LinkCloser, MgmtError
from tcl_me_remo.bluez.rawhci import HciConnection

TV = "AA:BB:CC:DD:EE:FF"
RPA = "4A:11:22:33:44:55"


def reply(event, index, opcode, status, params=b""):
    body = opcode.to_bytes(2, "little") + bytes([status]) + params
    return event.to_bytes(2, "little") + index.to_bytes(2, "little") + len(body).to_bytes(2, "little") + body


# --------------------------------------------------------------------------- packet building


def test_disconnect_command_exact_bytes():
    # Header: command 0x0014, controller 0, seven bytes of parameters (all little endian).
    # Parameters: the address least significant byte first, then its type (1 = LE public).
    assert mgmt.disconnect_command(0, TV, mgmt.BDADDR_LE_PUBLIC) == bytes.fromhex("1400 0000 0700 ffeeddccbbaa 01")
    # hci1, a static random identity address.
    assert mgmt.disconnect_command(1, "c0:01:02:03:04:05", mgmt.BDADDR_LE_RANDOM) == bytes.fromhex(
        "1400 0100 0700 0504030201c0 02"
    )


@pytest.mark.parametrize(
    "dev_id, address, address_type",
    [
        (0, TV, 0),                    # BR/EDR: this service only ever has an LE link to end
        (0, TV, 3),
        (0xFFFF, TV, 1),               # "no controller" is not a controller
        (-1, TV, 1),
        (0, "AA:BB:CC:DD:EE", 1),
        (0, "AABBCCDDEEFF", 1),
        (0, "AA:BB:CC:DD:EE:GG", 1),
        (0, "", 1),
    ],
)
def test_disconnect_command_rejects_what_the_kernel_would_misread(dev_id, address, address_type):
    with pytest.raises(ValueError):
        mgmt.disconnect_command(dev_id, address, address_type)


def test_address_helpers():
    assert mgmt.parse_bdaddr(" aa:bb:cc:dd:ee:ff ") == bytes.fromhex("ffeeddccbbaa")
    assert rawhci.format_bdaddr(mgmt.parse_bdaddr(TV)) == TV  # the two byte orders agree
    assert mgmt.le_address_type("public") == 1 and mgmt.le_address_type("random") == 2
    for unknown in ("", "Public", "static", "bredr"):
        with pytest.raises(MgmtError, match="what kind of address"):
            mgmt.le_address_type(unknown)


def test_parse_reply():
    address = bytes.fromhex("ffeeddccbbaa01")
    # Command Complete: event 0x0001, controller 0, length 10, opcode 0x0014, status, parameters.
    done = bytes.fromhex("0100 0000 0a00 1400 00") + address
    assert done == reply(mgmt.EV_CMD_COMPLETE, 0, 0x0014, 0, address)
    assert mgmt.parse_reply(done, 0) == 0
    assert mgmt.parse_reply(reply(mgmt.EV_CMD_COMPLETE, 0, 0x0014, 0x02, address), 0) == 0x02
    # Command Status carries no parameters after the status.
    assert mgmt.parse_reply(bytes.fromhex("0200 0000 0300 1400 14"), 0) == 0x14

    assert mgmt.parse_reply(done, 1) is None                                              # another controller
    assert mgmt.parse_reply(reply(mgmt.EV_CMD_COMPLETE, 0, 0x0015, 0), 0) is None         # another command
    assert mgmt.parse_reply(bytes.fromhex("0c00 0000 0800") + address + b"\x02", 0) is None  # Device Disconnected
    assert mgmt.parse_reply(bytes.fromhex("0100 0000 0200 1400"), 0) is None              # no status byte
    assert mgmt.parse_reply(b"", 0) is None and mgmt.parse_reply(b"\x01\x00\x00", 0) is None


def test_statuses_are_put_in_plain_words():
    assert "no such link" in mgmt.describe_status(0x02)
    assert "another disconnect" in mgmt.describe_status(0x0A)
    assert "no longer has such a link" in mgmt.describe_status(0x0E)
    assert "must run as root" in mgmt.describe_status(0x14)
    assert mgmt.describe_status(0x7F) == "the kernel answered with status 0x7F"


# --------------------------------------------------------------------------- which link


def link(address, *, handle=0x0041, out=False, link_type=0x80, state=1):
    return HciConnection(handle, address, link_type, out, state)


def test_target_is_bluez_address_and_type_when_the_kernel_lists_the_tv_under_it():
    assert mgmt.disconnect_target(TV.lower(), "public", [link(TV)]) == (TV, mgmt.BDADDR_LE_PUBLIC)
    # A static random identity address: the type matters, the kernel looks links up by both.
    assert mgmt.disconnect_target(TV, "random", [link(TV)]) == (TV, mgmt.BDADDR_LE_RANDOM)
    # Other links (a gamepad we connected to, a classic headset) do not confuse it.
    others = [link("11:11:11:11:11:11", handle=1, out=True), link("22:22:22:22:22:22", handle=2, link_type=0x01)]
    assert mgmt.disconnect_target(TV, "public", [*others, link(TV)]) == (TV, mgmt.BDADDR_LE_PUBLIC)


def test_target_follows_the_kernel_when_it_knows_the_link_under_a_private_address():
    # BlueZ says "public" about the identity address; the address the kernel has is the one
    # the TV used on the air, and a private address is a random address.
    assert mgmt.disconnect_target(TV, "public", [link(RPA)]) == (RPA, mgmt.BDADDR_LE_RANDOM)


def test_a_link_listed_under_the_tv_s_address_rules_every_other_link_out():
    """The kernel lists links in every state.  A TV link that is on its way down (BT_DISCONN = 8:
    bluetoothd's own Disconnect has just gone out) must not make the only *other* incoming
    link look like the TV's: ending somebody else's link cannot be taken back."""
    phone = link("5B:00:00:00:00:01", handle=2)  # a resolvable private address, a real candidate otherwise
    for state in (8, 9, 2):  # BT_DISCONN, BT_CLOSED, BT_OPEN
        with pytest.raises(MgmtError, match="already closing the TV's link"):
            mgmt.disconnect_target(TV, "public", [link(TV, state=state), phone])
        with pytest.raises(MgmtError, match="already closing the TV's link"):
            mgmt.disconnect_target(TV, "public", [phone, link(TV, state=state)])
    # ... and while the TV's own link is up, it is the one, whoever else is connected.
    assert mgmt.disconnect_target(TV, "public", [phone, link(TV)]) == (TV, mgmt.BDADDR_LE_PUBLIC)
    assert mgmt.disconnect_target(TV, "public", [link(TV, state=7), phone]) == (TV, mgmt.BDADDR_LE_PUBLIC)  # BT_CONFIG


def test_another_address_is_only_taken_for_the_tv_when_nothing_else_can_be_meant():
    assert mgmt.is_resolvable_private(RPA) and mgmt.is_resolvable_private("7f:ff:ff:ff:ff:ff")
    for other in ("C0:01:02:03:04:05", "3A:11:22:33:44:55", "80:11:22:33:44:55", "00:1A:7D:DA:71:13", "nonsense"):
        assert not mgmt.is_resolvable_private(other)  # static random, non-resolvable, public-looking

    # The only incoming link, but not under an address a TV's privacy would produce.
    for other in ("C0:01:02:03:04:05", "3A:11:22:33:44:55", "00:1A:7D:DA:71:13"):
        with pytest.raises(MgmtError, match="neither the TV's address nor a private address"):
            mgmt.disconnect_target(TV, "public", [link(other)])
    # A second incoming link in any state leaves a doubt, also one that is closing: it might
    # be the TV's, and then the one that is up is not.
    with pytest.raises(MgmtError, match="none is listed as the TV"):
        mgmt.disconnect_target(TV, "public", [link(RPA), link("5B:00:00:00:00:01", handle=2, state=8)])
    # The single candidate has to be up itself.
    with pytest.raises(MgmtError, match="closing already"):
        mgmt.disconnect_target(TV, "public", [link(RPA, state=8)])
    # Links we opened (a gamepad) and classic links are nobody's candidates and no cause for doubt.
    others = [link("5B:00:00:00:00:09", handle=1, out=True), link("22:22:22:22:22:22", handle=2, link_type=0x01)]
    assert mgmt.disconnect_target(TV, "public", [*others, link(RPA)]) == (RPA, mgmt.BDADDR_LE_RANDOM)
    with pytest.raises(MgmtError, match="no Bluetooth LE link from the TV"):
        mgmt.disconnect_target(TV, "public", others)


def test_target_without_the_kernel_list_is_what_bluetoothd_itself_would_send():
    assert mgmt.disconnect_target(TV, "public", None) == (TV, mgmt.BDADDR_LE_PUBLIC)
    with pytest.raises(MgmtError, match="what kind of address"):
        mgmt.disconnect_target(TV, "", None)


def test_target_refuses_to_guess():
    with pytest.raises(MgmtError, match="no Bluetooth LE link from the TV"):
        mgmt.disconnect_target(TV, "public", [])
    with pytest.raises(MgmtError, match="none is listed as the TV"):
        mgmt.disconnect_target(TV, "public", [link(RPA), link("5B:00:00:00:00:01", handle=2)])
    # A link that is already on its way down (BT_DISCONN = 8) is not there to be ended again.
    with pytest.raises(MgmtError):
        mgmt.disconnect_target(TV, "public", [link(TV, state=8)])
    # The kernel's list wins over a missing AddressType only when it names another address.
    with pytest.raises(MgmtError, match="what kind of address"):
        mgmt.disconnect_target(TV, "", [link(TV)])
    assert mgmt.disconnect_target(TV, "", [link(RPA)]) == (RPA, mgmt.BDADDR_LE_RANDOM)


# --------------------------------------------------------------------------- LinkCloser with fake backends


class Control:
    def __init__(self, log, *, open_error=None, send_error=None, answers=()):
        self.log, self._open_error, self._send_error = log, open_error, send_error
        self.incoming = list(answers)

    def open(self):
        if self._open_error:
            raise self._open_error
        self.log.append(("open",))

    def send(self, packet):
        if self._send_error:
            raise self._send_error
        self.log.append(("send", packet.hex()))

    def receive(self):
        return self.incoming.pop(0) if self.incoming else None

    def close(self):
        self.log.append(("close",))


class Raw:
    def __init__(self, log, connections=(), error=None):
        self.log, self._connections, self._error = log, list(connections), error

    def open(self, dev_id):
        self.log.append(("raw open", dev_id))
        if self._error:
            raise self._error

    def connections(self):
        return self._connections

    def close(self):
        self.log.append(("raw close",))


def closer(log, control=None, *, connections=None, raw_error=None):
    control = control if control is not None else Control(log)
    connections = [link(TV)] if connections is None else connections
    return LinkCloser(lambda: control, lambda: Raw(log, connections, raw_error)), control


def test_request_reads_the_kernel_list_then_sends_one_command_and_keeps_listening():
    log = []
    link_closer, control = closer(log)
    assert link_closer.request(0, TV, "public") == (TV, 1)
    assert log == [("raw open", 0), ("raw close",), ("open",), ("send", "140000000700ffeeddccbbaa01")]
    assert link_closer.pending and link_closer.status() is None  # carried out, not answered yet

    # Events for everybody arrive on a control socket; only our command's answer counts.
    control.incoming += [
        bytes.fromhex("0c00 0000 0800 ffeeddccbbaa 01 02"),                            # Device Disconnected
        reply(mgmt.EV_CMD_COMPLETE, 1, mgmt.OP_DISCONNECT, 0x02),                      # another controller
        reply(mgmt.EV_CMD_COMPLETE, 0, mgmt.OP_DISCONNECT, 0, bytes.fromhex("ffeeddccbbaa01")),
    ]
    assert link_closer.status() == 0
    link_closer.close()
    link_closer.close()  # idempotent
    assert log[-1] == ("close",) and log.count(("close",)) == 1 and not link_closer.pending
    assert link_closer.status() is None


def test_request_names_the_address_the_kernel_knows():
    log = []
    link_closer, _control = closer(log, connections=[link(RPA)])
    assert link_closer.request(2, TV, "public") == (RPA, 2)
    assert ("send", "14000200070055443322114a02") in log


def test_request_works_without_the_kernel_list():
    log = []
    link_closer, _control = closer(log, raw_error=PermissionError(errno.EPERM, "Operation not permitted"))
    assert link_closer.request(0, TV, "random") == (TV, 2)
    assert log == [("raw open", 0), ("raw close",), ("open",), ("send", "140000000700ffeeddccbbaa02")]


@pytest.mark.parametrize(
    "status, text",
    [(0x02, "no such link"), (0x0A, "another disconnect"), (0x0F, "switched off"), (0x14, "must run as root")],
)
def test_a_refusal_that_is_already_there_is_raised_at_once(status, text):
    log = []
    control = Control(log, answers=[reply(mgmt.EV_CMD_COMPLETE, 0, mgmt.OP_DISCONNECT, status)])
    link_closer, _ = closer(log, control)
    with pytest.raises(MgmtError, match=text):
        link_closer.request(0, TV, "public")
    assert log[-1] == ("close",) and not link_closer.pending


@pytest.mark.parametrize(
    "kwargs, text",
    [
        ({"open_error": OSError(errno.EAFNOSUPPORT, "Address family not supported")}, "no Bluetooth socket support"),
        ({"open_error": PermissionError(errno.EPERM, "Operation not permitted")}, "must run as root"),
        ({"send_error": OSError(errno.EINVAL, "Invalid argument")}, "Invalid argument"),
    ],
)
def test_request_explains_why_the_kernel_cannot_be_asked(kwargs, text):
    log = []
    link_closer, _ = closer(log, Control(log, **kwargs))
    with pytest.raises(MgmtError, match=text):
        link_closer.request(0, TV, "public")
    assert log[-1] == ("close",) and not link_closer.pending


def test_request_does_not_touch_the_control_socket_when_the_link_is_not_clear():
    log = []
    link_closer, _ = closer(log, connections=[])
    with pytest.raises(MgmtError, match="no Bluetooth LE link"):
        link_closer.request(0, TV, "public")
    assert log == [("raw open", 0), ("raw close",)]


def test_status_survives_a_socket_that_fails_and_a_flood_of_events():
    log = []
    control = Control(log)
    link_closer, _ = closer(log, control)
    link_closer.request(0, TV, "public")
    control.incoming = [bytes.fromhex("0600 0000 0400 01000000")] * 100   # New Settings, again and again
    assert link_closer.status() is None and len(control.incoming) == 100 - mgmt.MAX_PACKETS_PER_POLL

    def broken():
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

    control.receive = broken
    assert link_closer.status() is None  # never raises


def test_real_control_socket_opens_or_fails_cleanly():
    """In CI there is no Bluetooth stack; the caller then takes the bluetoothd way.

    Nothing is ever written here: on a machine that has a TV connected this test must not be
    what disconnects it.
    """
    backend = mgmt.MgmtSocketBackend()
    try:
        backend.open()
    except OSError as exc:
        assert rawhci.explain_os_error(exc)
    else:  # pragma: no cover - only where the kernel has Bluetooth sockets
        backend.receive()
    finally:
        backend.close()

    log = []
    link_closer = LinkCloser(raw_backend_factory=lambda: Raw(log, connections=[]))
    with pytest.raises(MgmtError, match="no Bluetooth LE link"):
        link_closer.request(0, TV, "public")
    assert not link_closer.pending


# --------------------------------------------------------------------------- the real backend's system calls


class FakeSocket:
    instances: list["FakeSocket"] = []

    def __init__(self, family, kind, proto):
        self.args = (family, kind, proto)
        self.bound = None
        self.sent = []
        self.incoming = []
        self.closed = False
        FakeSocket.instances.append(self)

    def bind(self, address):
        self.bound = address

    def fileno(self):
        return 55

    def send(self, data, flags):
        self.sent.append((bytes(data), flags))
        return len(data)

    def recv(self, size, flags):
        self.recv_args = (size, flags)
        if not self.incoming:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        return self.incoming.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch):
    FakeSocket.instances = []
    monkeypatch.setattr(rawhci.socket, "socket", FakeSocket)
    return FakeSocket.instances


@pytest.mark.parametrize("python_has_bluetooth", [True, False])
def test_control_socket_is_bound_through_libc_whatever_python_supports(fake_socket, monkeypatch, python_has_bluetooth):
    if python_has_bluetooth:
        monkeypatch.setattr(rawhci.socket, "AF_BLUETOOTH", 31, raising=False)
    else:
        monkeypatch.delattr(rawhci.socket, "AF_BLUETOOTH", raising=False)
    calls = []

    class Libc:
        def bind(self, fd, address, length):
            calls.append((fd, bytes(address), length))
            return 0

    monkeypatch.setattr(rawhci.ctypes, "CDLL", lambda name, use_errno: Libc())
    backend = mgmt.MgmtSocketBackend()
    backend.open()
    sock = fake_socket[0]
    assert sock.args == (31, rawhci.socket.SOCK_RAW, 1)  # AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI
    # struct sockaddr_hci { family = AF_BLUETOOTH (31), dev = HCI_DEV_NONE, channel = HCI_CHANNEL_CONTROL (3) }
    assert calls == [(55, bytes.fromhex("1f00" "ffff" "0300"), 6)]
    assert sock.bound is None  # Python's own bind() cannot name a channel

    packet = mgmt.disconnect_command(0, TV, 1)
    backend.send(packet)
    assert sock.sent == [(packet, rawhci.socket.MSG_DONTWAIT)]

    assert backend.receive() is None  # nothing there: no blocking
    assert sock.recv_args == (mgmt.MAX_PACKET_SIZE, rawhci.socket.MSG_DONTWAIT)
    sock.incoming = [b"\x01\x00\x00\x00\x03\x00\x14\x00\x00", b""]
    assert backend.receive() == b"\x01\x00\x00\x00\x03\x00\x14\x00\x00"
    assert backend.receive() is None

    backend.close()
    backend.close()
    assert sock.closed
    with pytest.raises(OSError):
        backend.send(packet)
    with pytest.raises(OSError):
        backend.receive()


def test_control_socket_bind_failure_does_not_leak_the_socket(fake_socket, monkeypatch):
    class Libc:
        def bind(self, fd, address, length):
            return -1

    monkeypatch.setattr(rawhci.ctypes, "CDLL", lambda name, use_errno: Libc())
    monkeypatch.setattr(rawhci.ctypes, "get_errno", lambda: errno.EINVAL)
    backend = mgmt.MgmtSocketBackend()
    with pytest.raises(OSError) as err:
        backend.open()
    assert err.value.errno == errno.EINVAL and fake_socket[0].closed


def test_control_socket_short_write_is_an_error(fake_socket, monkeypatch):
    monkeypatch.setattr(rawhci.ctypes, "CDLL", lambda name, use_errno: type("Libc", (), {"bind": lambda *a: 0})())
    monkeypatch.setattr(FakeSocket, "send", lambda self, data, flags: 5)
    backend = mgmt.MgmtSocketBackend()
    backend.open()
    with pytest.raises(OSError, match="short write"):
        backend.send(mgmt.disconnect_command(0, TV, 1))
