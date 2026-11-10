import asyncio
import json
import os
import stat

import pytest

from tcl_me_remo import ipc


async def handler(method, params):
    if method == "echo":
        return params
    if method == "boom":
        raise ipc.RpcError("not_paired", "pair the TV first")
    if method == "bug":
        raise RuntimeError("oops")
    if method == "slow":
        await asyncio.sleep(5)
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

    # a stale socket file from a crashed run is replaced; a regular file is not clobbered silently
    srv = ipc.IpcServer(path, handler, group=None)
    await srv.start()
    srv2 = ipc.IpcServer(path, handler, group=None)
    await srv2.start()
    assert await ipc.call(path, "echo", {"v": 2}) == {"v": 2}
    await srv2.stop()
    await srv.stop()
