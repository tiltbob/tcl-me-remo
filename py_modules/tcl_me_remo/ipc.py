"""Control channel between the service and its clients (CLI, Decky plugin backend).

Newline-delimited JSON over a Unix stream socket.  One request per line:

    {"id": 1, "method": "status", "params": {}}

and one reply per request:

    {"id": 1, "ok": true, "result": {...}}
    {"id": 1, "ok": false, "error": {"code": "not_paired", "message": "..."}}

Only the standard library is used, and the blocking client (:func:`call_sync`) avoids asyncio
entirely so it can be imported from any Python the caller happens to run in.
"""

from __future__ import annotations

import asyncio
import grp
import json
import logging
import os
import socket
import stat
from pathlib import Path
from typing import Any, Awaitable, Callable

MAX_LINE = 1 << 20  # 1 MiB per message

log = logging.getLogger(__name__)

Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class RpcError(Exception):
    """An error that travels over the control channel with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class ServiceUnavailable(RpcError):
    """The service socket could not be reached (service stopped or not installed)."""

    def __init__(self, message: str):
        super().__init__("unavailable", message)


def _reply_ok(req_id: Any, result: Any) -> bytes:
    return (json.dumps({"id": req_id, "ok": True, "result": result}, separators=(",", ":")) + "\n").encode()


def _reply_err(req_id: Any, code: str, message: str) -> bytes:
    payload = {"id": req_id, "ok": False, "error": {"code": code, "message": message}}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


class IpcServer:
    """Serves ``handler(method, params)`` on a Unix socket."""

    def __init__(self, path: Path | str, handler: Handler, *, mode: int = 0o660, group: str | None = "wheel"):
        self.path = Path(path)
        self._handler = handler
        self._mode = mode
        self._group = group
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_stale_socket()
        old_umask = os.umask(0o177)  # the socket is born private; widened deliberately below
        try:
            self._server = await asyncio.start_unix_server(self._on_client, path=str(self.path), limit=MAX_LINE)
        finally:
            os.umask(old_umask)
        mode = self._mode
        if self._group:
            try:
                os.chown(self.path, -1, grp.getgrnam(self._group).gr_gid)
            except (KeyError, PermissionError, OSError) as exc:
                log.debug("control socket stays owner-only (%s)", exc)
                mode &= 0o700
        os.chmod(self.path, mode)

    def _remove_stale_socket(self) -> None:
        try:
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()  # stop accepting; does not touch established connections
        # Cancel in-flight clients *before* wait_closed(): since Python 3.12 wait_closed()
        # blocks until every connection is gone, so the other order deadlocks.
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        try:
            self.path.unlink()
        except OSError:
            pass

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    writer.write(_reply_err(None, "bad_request", "message too large"))
                    await writer.drain()
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                writer.write(await self._dispatch(line))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if task is not None:
                self._clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError, OSError):
                pass

    async def _dispatch(self, line: bytes) -> bytes:
        req_id: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            req_id = request.get("id")
            method = request.get("method")
            params = request.get("params")
            if params is None:
                params = {}
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("request needs a string 'method' and an object 'params'")
        except ValueError as exc:
            return _reply_err(req_id, "bad_request", str(exc))
        try:
            result = await self._handler(method, params)
            return _reply_ok(req_id, result)
        except RpcError as exc:
            return _reply_err(req_id, exc.code, exc.message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the connection
            log.exception("control request %r failed", method)
            return _reply_err(req_id, "internal", f"{type(exc).__name__}: {exc}")


def _decode_reply(line: bytes) -> Any:
    if not line:
        raise ServiceUnavailable("the service closed the connection without answering")
    try:
        reply = json.loads(line)
    except ValueError as exc:
        raise RpcError("bad_reply", f"unreadable reply from the service: {exc}") from None
    if not isinstance(reply, dict):
        raise RpcError("bad_reply", "unreadable reply from the service")
    if reply.get("ok"):
        return reply.get("result")
    error = reply.get("error") or {}
    raise RpcError(str(error.get("code", "error")), str(error.get("message", "unknown error")))


def _encode_request(method: str, params: dict[str, Any] | None) -> bytes:
    return (json.dumps({"id": 1, "method": method, "params": params or {}}, separators=(",", ":")) + "\n").encode()


async def call(path: Path | str, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
    """Asynchronous client: one request, one reply."""

    async def exchange() -> Any:
        try:
            reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_LINE)
        except (FileNotFoundError, ConnectionError, PermissionError) as exc:
            raise ServiceUnavailable(f"cannot reach the tcl-me-remo service at {path}: {exc}") from None
        try:
            writer.write(_encode_request(method, params))
            await writer.drain()
            return _decode_reply(await reader.readline())
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except asyncio.TimeoutError:  # noqa: UP041 - also correct on Python 3.10 (Decky's interpreter)
        raise RpcError("timeout", f"the service did not answer {method!r} within {timeout:g}s") from None


def call_sync(path: Path | str, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
    """Blocking client: one request, one reply.  Uses only the ``socket`` module."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionError, PermissionError) as exc:
            raise ServiceUnavailable(f"cannot reach the tcl-me-remo service at {path}: {exc}") from None
        try:
            sock.sendall(_encode_request(method, params))
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if b"\n" in chunk:
                    break
                if size > MAX_LINE:
                    raise RpcError("bad_reply", "reply from the service is too large")
        except TimeoutError:
            raise RpcError("timeout", f"the service did not answer {method!r} within {timeout:g}s") from None
        return _decode_reply(b"".join(chunks).split(b"\n", 1)[0])
    finally:
        sock.close()
