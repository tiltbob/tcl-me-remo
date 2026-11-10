import asyncio
import errno
import json
import logging
import os
import socket
import stat
from pathlib import Path

import pytest

from tcl_me_remo import installer, ipc


async def handler(method, params):
    if method == "echo":
        return params
    if method == "boom":
        raise ipc.RpcError("not_paired", "pair the TV first")
    if method == "bug":
        raise RuntimeError("oops")
    if method == "slow":
        await asyncio.sleep(5)
    if method == "nap":
        await asyncio.sleep(params["seconds"])
        return "rested"
    if method == "huge":
        return "x" * (ipc.MAX_LINE + 10)
    raise ipc.RpcError("unknown_method", f"no such method {method}")


@pytest.fixture
async def server(tmp_path):
    srv = ipc.IpcServer(tmp_path / "run" / "control.sock", handler, group=None)
    await srv.start()
    yield srv
    await srv.stop()


async def test_round_trip_async_client(server):
    assert await ipc.call(server.path, "echo", {"a": [1, 2, {"b": None}]}) == {"a": [1, 2, {"b": None}]}


async def test_errors_carry_codes(server):
    with pytest.raises(ipc.RpcError) as err:
        await ipc.call(server.path, "boom")
    assert (err.value.code, err.value.message) == ("not_paired", "pair the TV first")
    with pytest.raises(ipc.RpcError) as err:
        await ipc.call(server.path, "bug")
    assert err.value.code == "internal" and "oops" in err.value.message
    # the server survives a handler bug
    assert await ipc.call(server.path, "echo", {"x": 1}) == {"x": 1}


async def test_timeout(server):
    with pytest.raises(ipc.RpcError) as err:
        await ipc.call(server.path, "slow", timeout=0.1)
    assert err.value.code == "timeout"


async def test_sync_client_from_thread(server):
    result = await asyncio.to_thread(ipc.call_sync, server.path, "echo", {"big": "x" * 200_000})
    assert len(result["big"]) == 200_000
    with pytest.raises(ipc.RpcError) as err:
        await asyncio.to_thread(ipc.call_sync, server.path, "boom")
    assert err.value.code == "not_paired"
    with pytest.raises(ipc.RpcError) as err:
        await asyncio.to_thread(lambda: ipc.call_sync(server.path, "slow", timeout=0.1))
    assert err.value.code == "timeout"


async def test_unavailable(tmp_path):
    with pytest.raises(ipc.ServiceUnavailable):
        await ipc.call(tmp_path / "nope.sock", "status")
    with pytest.raises(ipc.ServiceUnavailable):
        ipc.call_sync(tmp_path / "nope.sock", "status")


async def test_malformed_requests_get_error_replies_and_connection_survives(server):
    reader, writer = await asyncio.open_unix_connection(str(server.path))
    for bad in (b"not json\n", b"[1,2]\n", b'{"id": 7, "method": 5}\n', b'{"id": 8, "method": "echo", "params": []}\n'):
        writer.write(bad)
        await writer.drain()
        reply = json.loads(await reader.readline())
        assert reply["ok"] is False and reply["error"]["code"] == "bad_request"
    writer.write(b'\n{"id": 9, "method": "echo", "params": {"k": 1}}\n')
    await writer.drain()
    assert json.loads(await reader.readline()) == {"id": 9, "ok": True, "result": {"k": 1}}
    writer.close()
    await writer.wait_closed()


async def test_socket_permissions_and_cleanup(tmp_path):
    path = tmp_path / "control.sock"
    srv = ipc.IpcServer(path, handler, mode=0o660, group=None)
    await srv.start()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o660
    await srv.stop()
    assert not path.exists()

    # an unknown group must not widen access beyond the owner
    srv = ipc.IpcServer(path, handler, mode=0o660, group="no-such-group-xyz")
    await srv.start()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    await srv.stop()

    # a socket file that a process which died left behind is replaced
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()
    assert path.exists() and not ipc.socket_answers(path)
    srv = ipc.IpcServer(path, handler, group=None)
    await srv.start()
    assert await ipc.call(path, "echo", {"v": 2}) == {"v": 2}
    await srv.stop()
    assert not path.exists()


# --------------------------------------------------------------------------- one service per socket


async def test_a_second_server_does_not_take_a_socket_that_answers(server):
    """``tcl-me-remo daemon`` typed by hand while the service runs must leave it reachable."""
    assert ipc.socket_answers(server.path)
    second = ipc.IpcServer(server.path, handler, group=None)
    with pytest.raises(ipc.SocketInUse):
        await second.start()
    assert await ipc.call(server.path, "echo", {"still": "the first"}) == {"still": "the first"}
    await second.stop()  # it never had the socket, so it must not remove the file either
    assert server.path.exists()
    assert await ipc.call(server.path, "echo", {"v": 1}) == {"v": 1}


async def test_stop_removes_only_the_socket_file_it_made(tmp_path):
    path = tmp_path / "control.sock"
    srv = ipc.IpcServer(path, handler, group=None)
    await srv.start()
    # Somebody else's socket has taken the place of ours since (another instance, say).
    path.unlink()
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other.bind(str(path))
    other.listen(1)
    try:
        await srv.stop()
        assert path.exists() and ipc.socket_answers(path)
    finally:
        other.close()


# --------------------------------------------------------------------------- hostile and broken input


async def _raw(server, payload, *, limit=1 << 16):
    reader, writer = await asyncio.open_unix_connection(str(server.path), limit=limit)
    writer.write(payload)
    await writer.drain()
    return reader, writer


async def test_every_request_gets_one_reply_however_it_is_nested(server, caplog):
    deep_list = b"[" * 100_000 + b"\n"
    deep_params = b'{"id": 3, "method": "echo", "params": ' + b'{"a":' * 50_000 + b"1" + b"}" * 50_000 + b"}\n"
    deep_id = b'{"id": ' + b"[" * 50_000 + b"]" * 50_000 + b', "method": "echo"}\n'
    with caplog.at_level(logging.WARNING):
        reader, writer = await _raw(server, b"")
        for bad in (deep_list, deep_params, deep_id):
            writer.write(bad)
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 10))
            assert reply["ok"] is False and reply["error"]["code"] == "bad_request", reply
            assert reply["error"]["message"] == "the request is nested too deeply"
        # Below the service's own bound the parser decides, and how deep it goes differs
        # between Python versions (3.14 parses what 3.13 refuses).  Either way: one reply.
        middling = b'{"id": ' + b"[" * 1500 + b"]" * 1500 + b', "method": "echo"}\n'
        assert middling.count(b"[") + middling.count(b"{") < ipc.MAX_BRACKETS
        writer.write(middling)
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 10))
        assert reply["ok"] is True or reply["error"]["code"] == "bad_request", reply
        # ... and the connection is still good for the next request
        writer.write(b'{"id": 4, "method": "echo", "params": {"k": 1}}\n')
        await writer.drain()
        assert json.loads(await reader.readline()) == {"id": 4, "ok": True, "result": {"k": 1}}
        writer.close()
        await writer.wait_closed()
    assert not [r for r in caplog.records if "Unhandled" in r.getMessage() or r.exc_info]


async def test_numbers_that_are_not_json_are_refused(server):
    for constant in (b"NaN", b"Infinity", b"-Infinity"):
        reader, writer = await _raw(server, b'{"id": 1, "method": "echo", "params": {"budget": ' + constant + b"}}\n")
        reply = json.loads(await reader.readline())
        assert reply["ok"] is False and reply["error"]["code"] == "bad_request"
        assert "finite" in reply["error"]["message"]
        writer.close()
        await writer.wait_closed()


async def test_an_oversized_request_is_answered_and_the_connection_stays_usable(server):
    # A client that sends everything before it reads anything, like the CLI and the plugin:
    # the server has to take the line off its hands for the answer to arrive.
    with pytest.raises(ipc.RpcError) as err:
        await asyncio.to_thread(ipc.call_sync, server.path, "echo", {"big": "x" * (2 * ipc.MAX_LINE)})
    assert (err.value.code, err.value.message) == ("bad_request", "message too large")

    reader, writer = await _raw(server, b"y" * (ipc.MAX_LINE + 100) + b'\n{"id": 5, "method": "echo"}\n')
    assert json.loads(await reader.readline())["error"]["message"] == "message too large"
    assert json.loads(await reader.readline()) == {"id": 5, "ok": True, "result": {}}
    writer.close()
    await writer.wait_closed()


async def test_a_line_that_never_ends_is_cut_off(server, monkeypatch):
    monkeypatch.setattr(ipc, "MAX_DISCARD", 2 * ipc.MAX_LINE)
    reader, writer = await asyncio.open_unix_connection(str(server.path))

    async def pour():
        try:
            for _ in range(64):  # 16 MiB if nobody stops it
                writer.write(b"z" * (1 << 18))
                await writer.drain()
        except ConnectionError:
            pass

    pouring = asyncio.create_task(pour())
    try:
        rest = await asyncio.wait_for(reader.read(), 20)  # until the server closes
    except ConnectionError:
        rest = b""  # closed with our data unread: the answer may be lost, the connection is gone
    await pouring
    writer.close()
    assert rest == b"" or json.loads(rest)["error"]["code"] == "bad_request"
    assert await ipc.call(server.path, "echo", {"v": 1}) == {"v": 1}


async def test_a_reply_too_large_to_read_becomes_an_error_reply(server):
    for client in (ipc.call, lambda *a: asyncio.to_thread(ipc.call_sync, *a)):
        with pytest.raises(ipc.RpcError) as err:
            await client(server.path, "huge")
        assert err.value.code == "internal" and "too large" in err.value.message


# --------------------------------------------------------------------------- how many, how long


async def test_the_number_of_clients_is_limited_and_the_newest_is_told(tmp_path):
    srv = ipc.IpcServer(tmp_path / "control.sock", handler, group=None, max_clients=3)
    await srv.start()
    held = []
    try:
        for n in range(3):
            reader, writer = await _raw(srv, b'{"id": %d, "method": "echo"}\n' % n)
            assert json.loads(await reader.readline())["ok"] is True  # served, and still connected
            held.append((reader, writer))

        # One more: told why, and closed, without having to send anything first.
        reader, writer = await asyncio.open_unix_connection(str(srv.path))
        reply = json.loads(await asyncio.wait_for(reader.readline(), 10))
        assert reply["ok"] is False and reply["error"]["code"] == "busy" and "too many" in reply["error"]["message"]
        assert await asyncio.wait_for(reader.read(), 10) == b""
        writer.close()
        # The clients of this project see it as an error with that code, not as a broken pipe.
        with pytest.raises(ipc.RpcError) as err:
            await asyncio.to_thread(ipc.call_sync, srv.path, "echo")
        assert err.value.code == "busy"

        # Those that are connected are not disturbed, and a place that is given up is free again.
        reader, writer = held.pop()
        writer.write(b'{"id": 9, "method": "echo", "params": {"k": 1}}\n')
        await writer.drain()
        assert json.loads(await reader.readline())["result"] == {"k": 1}
        writer.close()
        await writer.wait_closed()
        for _ in range(200):
            try:
                assert await ipc.call(srv.path, "echo", {"v": 2}) == {"v": 2}
                break
            except ipc.RpcError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("the place of a client that left was not given to the next one")
    finally:
        for _, writer in held:
            writer.close()
        await srv.stop()


async def test_a_connection_that_sends_no_request_is_closed_and_a_running_request_is_not(tmp_path):
    # The limit is short for a test and still long next to a hiccup of the machine: a request
    # has to arrive within it, and a test must not depend on doing anything in less.
    idle = 2.0
    srv = ipc.IpcServer(tmp_path / "control.sock", handler, group=None, idle_timeout=idle)
    await srv.start()
    try:

        async def is_closed(start):
            reader, writer = await _raw(srv, start)
            try:
                # Without the limit this wait never ends, and it is this time-out that fails.
                return await asyncio.wait_for(reader.read(), 30) == b""
            finally:
                writer.close()

        # Nothing at all, and half a request: both closed.
        assert await asyncio.gather(is_closed(b""), is_closed(b'{"id": 1, "method": "ec')) == [True, True]

        # A request that takes longer than the limit is answered all the same: the limit is
        # on the wait for a request, never on the work.  (Were it on the work, the
        # connection would be closed first and there would be no line to read.)
        request = b'{"id": 2, "method": "nap", "params": {"seconds": %g}}\n' % (1.5 * idle)
        reader, writer = await _raw(srv, request)
        assert json.loads(await asyncio.wait_for(reader.readline(), 30)) == {"id": 2, "ok": True, "result": "rested"}
        writer.close()
        await writer.wait_closed()
    finally:
        await srv.stop()


def test_the_unit_leaves_room_for_more_files_than_clients():
    """The cap on clients is what keeps the service inside its file descriptors; the unit
    must not leave it at systemd's default of 1024 minus whatever else is open."""
    unit = installer.render_units(installer.Layout(), Path("/opt/tcl-me-remo"))[installer.SERVICE]
    limit = int(unit.split("LimitNOFILE=")[1].split()[0])
    assert limit >= 4096 > 8 * ipc.MAX_CLIENTS
    assert ipc.MAX_CLIENTS == 32 and 5 <= ipc.IDLE_SECONDS <= 120


# --------------------------------------------------------------------------- the clients


def _full_listener(path):
    """A socket that listens and has no room left: what a stuck service looks like from outside."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(0)
    waiting = []
    for _ in range(64):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.setblocking(False)
        try:
            client.connect(str(path))
        except BlockingIOError:
            client.close()
            return listener, waiting
        waiting.append(client)
    raise AssertionError("the listener's queue never filled up")


async def test_a_service_that_takes_no_connections_is_unavailable_not_a_traceback(tmp_path):
    path = tmp_path / "stuck.sock"
    listener, waiting = _full_listener(path)
    try:
        with pytest.raises(ipc.RpcError) as err:
            ipc.call_sync(path, "event", {"name": "suspend"}, timeout=0.5)
        assert err.value.code in ("unavailable", "timeout")
        with pytest.raises(ipc.RpcError) as err:
            await ipc.call(path, "status", timeout=0.5)
        assert err.value.code in ("unavailable", "timeout")
        assert ipc.socket_answers(path)  # and it is not mistaken for a socket nobody serves
    finally:
        for sock in (listener, *waiting):
            sock.close()


async def test_broken_connections_and_replies_are_rpc_errors(tmp_path):
    path = tmp_path / "odd.sock"
    behaviour = {"mode": "hang up"}

    async def on_client(reader, writer):
        if behaviour["mode"] == "flood":
            writer.write(b"x" * (ipc.MAX_LINE + (1 << 17)))  # a "reply" with no end
        elif behaviour["mode"] == "half":
            await reader.readline()
            writer.write(b'{"id": 1, "ok": tr')
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    odd = await asyncio.start_unix_server(on_client, path=str(path))
    try:
        for mode, codes in (("hang up", ("unavailable",)), ("flood", ("bad_reply",)), ("half", ("bad_reply",))):
            behaviour["mode"] = mode
            for client in (ipc.call, lambda *a: asyncio.to_thread(ipc.call_sync, *a)):
                with pytest.raises(ipc.RpcError) as err:
                    await client(path, "status")
                assert err.value.code in codes, (mode, err.value.code, err.value.message)
    finally:
        odd.close()
        await odd.wait_closed()


def test_not_being_allowed_is_told_apart_from_not_running(tmp_path, monkeypatch):
    class Denied(socket.socket):
        def connect(self, address):
            raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(ipc.socket, "socket", Denied)
    with pytest.raises(ipc.ServiceUnavailable) as err:
        ipc.call_sync(tmp_path / "control.sock", "status")
    assert err.value.denied and err.value.code == "unavailable"
    monkeypatch.undo()
    with pytest.raises(ipc.ServiceUnavailable) as err:
        ipc.call_sync(tmp_path / "nope.sock", "status")
    assert not err.value.denied


def test_a_time_out_that_is_no_number_is_an_error_not_a_crash(tmp_path):
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ipc.RpcError):
            ipc.call_sync(tmp_path / "nope.sock", "status", timeout=bad)


async def test_the_group_that_could_not_be_set_is_reported(tmp_path, caplog):
    srv = ipc.IpcServer(tmp_path / "control.sock", handler, mode=0o660, group="no-such-group-xyz")
    with caplog.at_level(logging.WARNING, logger="tcl_me_remo.ipc"):
        await srv.start()
    await srv.stop()
    assert any("no-such-group-xyz" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
