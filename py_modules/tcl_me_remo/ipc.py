"""Control channel between the service and its clients (CLI, Decky plugin backend).

Newline-delimited JSON over a Unix stream socket.  One request per line:

    {"id": 1, "method": "status", "params": {}}

and one reply per request:

    {"id": 1, "ok": true, "result": {...}}
    {"id": 1, "ok": false, "error": {"code": "not_paired", "message": "..."}}

Only the standard library is used, and the blocking client (:func:`call_sync`) avoids asyncio
entirely so it can be imported from any Python the caller happens to run in.

The server runs in the service's only event loop and as root, and whoever may open the socket
may send it anything.  So it bounds what a client can make it hold: the length of a line, the
number of clients at once, and the time a connection may sit without a complete request.
Whatever arrives gets one reply; nothing a client sends ends the connection handler with an
exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
from pathlib import Path
from typing import Any, Awaitable, Callable

MAX_LINE = 1 << 20  # 1 MiB per message
# Clients served at once.  The CLI, the plugin and the systemd hook each open a connection
# per request and close it; this is far more than they ever have open together.
MAX_CLIENTS = 32
# Seconds a connection may take to deliver a complete request line.  It bounds the wait for
# a request only: a request that is being handled takes as long as it takes (turning the TV
# on can take minutes), and the connection is not timed while it is.
IDLE_SECONDS = 30.0
# An over-long request is read to its end and thrown away, so that the answer ("too large")
# reaches a client that is still busy sending; beyond this much the connection is closed.
MAX_DISCARD = 8 * MAX_LINE

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
    """The service socket could not be reached (service stopped or not installed).

    ``denied`` is set when the socket is there but this user may not open it.
    """

    def __init__(self, message: str, *, denied: bool = False):
        super().__init__("unavailable", message)
        self.denied = denied


class SocketInUse(RuntimeError):
    """Another process is serving the control socket: this one must not take it over."""


def socket_answers(path: Path | str) -> bool:
    """True if something is listening on the Unix socket at ``path`` (blocking, quick).

    A socket file left behind by a process that died refuses the connection; a live one
    accepts it, or has no room for it right now.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError):
        return False
    except (BlockingIOError, TimeoutError, PermissionError):
        return True  # full, slow or not ours to open: somebody is there
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _no_constant(name: str) -> Any:
    # json.loads accepts NaN, Infinity and -Infinity, which are not JSON and are never a
    # sensible value of anything here.  (1e999 still becomes an infinity without passing
    # through this, so every handler checks its numbers itself.)
    raise ValueError(f"{name} is not valid JSON: numbers must be finite")


def _reply_ok(req_id: Any, result: Any) -> bytes:
    return (json.dumps({"id": req_id, "ok": True, "result": result}, separators=(",", ":")) + "\n").encode()


def _reply_err(req_id: Any, code: str, message: str) -> bytes:
    payload = {"id": req_id, "ok": False, "error": {"code": code, "message": message}}
    try:
        text = json.dumps(payload, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):  # an id that parsed but cannot be written back
        payload["id"] = None
        text = json.dumps(payload, separators=(",", ":"))
    return (text + "\n").encode()


class IpcServer:
    """Serves ``handler(method, params)`` on a Unix socket."""

    def __init__(
        self,
        path: Path | str,
        handler: Handler,
        *,
        mode: int = 0o660,
        group: str | None = "wheel",
        max_clients: int = MAX_CLIENTS,
        idle_timeout: float = IDLE_SECONDS,
    ):
        self.path = Path(path)
        self._handler = handler
        self._mode = mode
        self._group = group
        self._max_clients = max_clients
        self._idle_timeout = idle_timeout
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task[None]] = set()
        self._bound: tuple[int, int] | None = None  # (device, inode) of the socket file we made
        self._refusing = False                      # the "too many clients" warning has been logged

    async def start(self) -> None:
        """Bind and listen.  Raises :class:`SocketInUse` if another process serves the path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_stale_socket()
        old_umask = os.umask(0o177)  # the socket is born private; widened deliberately below
        try:
            self._server = await asyncio.start_unix_server(self._on_client, path=str(self.path), limit=MAX_LINE)
        finally:
            os.umask(old_umask)
        try:
            found = self.path.lstat()
            self._bound = (found.st_dev, found.st_ino)
        except OSError:
            self._bound = None
        mode = self._mode
        if self._group:
            try:
                import grp  # server side only: keeps the client usable on minimal interpreters

                os.chown(self.path, -1, grp.getgrnam(self._group).gr_gid)
            except (ImportError, KeyError, PermissionError, OSError) as exc:
                log.warning(
                    "the control socket could not be given to the group %r (%s): only its owner can use it",
                    self._group,
                    exc,
                )
                mode &= 0o700
        os.chmod(self.path, mode)

    def _remove_stale_socket(self) -> None:
        """Remove a socket file that a dead process left behind, and only that.

        A socket that still answers belongs to a running service (somebody started a second
        one by hand): taking the path would cut every client off from it, and it would be
        left unreachable when this process ends.
        """
        try:
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                return
        except FileNotFoundError:
            return
        if socket_answers(self.path):
            raise SocketInUse(f"another process is serving {self.path}")
        try:
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
        # Remove the socket file if it is still the one made in start(): never one that
        # another process has put there since, and none at all if start() did not get that far.
        bound, self._bound = self._bound, None
        try:
            found = self.path.lstat()
            if bound == (found.st_dev, found.st_ino):
                self.path.unlink()
        except OSError:
            pass

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        refused = len(self._clients) >= self._max_clients
        if task is not None and not refused:
            self._clients.add(task)
        try:
            if refused:
                if not self._refusing:
                    self._refusing = True
                    log.warning("%d control clients are connected: turning further ones away", len(self._clients))
                writer.write(_reply_err(None, "busy", "too many clients are connected to the service; try again"))
                await writer.drain()
                return
            while True:
                try:
                    line = await asyncio.wait_for(self._read_request(reader), self._idle_timeout)
                except asyncio.TimeoutError:  # noqa: UP041
                    log.debug("closing a control connection that sent no request for %gs", self._idle_timeout)
                    break
                except _Flood:
                    writer.write(_reply_err(None, "bad_request", "message too large"))
                    await writer.drain()
                    break
                if line is None:
                    writer.write(_reply_err(None, "bad_request", "message too large"))
                    await writer.drain()
                    continue
                if not line:
                    break
                if not line.strip():
                    continue
                # Not under the idle limit: a handler takes as long as it takes.
                writer.write(await self._dispatch(line))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001 - nothing a client does may end here with a traceback per request
            log.warning("control connection dropped", exc_info=True)
        finally:
            if task is not None and not refused:
                self._clients.discard(task)
                self._refusing = False
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError, OSError):
                pass

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> bytes | None:
        """The next request line; ``b""`` at the end of the stream.

        ``None`` stands for a line longer than MAX_LINE: it has been read to its end and
        thrown away, so the stream is at the start of the next request and the client, which
        may still have been sending, is able to read the answer.  Raises ``_Flood`` when the
        line does not end within MAX_DISCARD.
        """
        try:
            return await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            return exc.partial  # the stream ended; a last line without a line break still counts
        except asyncio.LimitOverrunError as exc:
            pending = exc.consumed
        discarded = 0
        while True:
            await reader.readexactly(pending)
            discarded += pending
            if discarded > MAX_DISCARD:
                raise _Flood
            try:
                await reader.readuntil(b"\n")
                return None
            except asyncio.IncompleteReadError:
                return None
            except asyncio.LimitOverrunError as exc:
                pending = exc.consumed

    async def _dispatch(self, line: bytes) -> bytes:
        req_id: Any = None
        try:
            request = json.loads(line, parse_constant=_no_constant)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            req_id = request.get("id")
            method = request.get("method")
            params = request.get("params")
            if params is None:
                params = {}
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("request needs a string 'method' and an object 'params'")
        except RecursionError:
            return _reply_err(None, "bad_request", "the request is nested too deeply")
        except ValueError as exc:
            return _reply_err(req_id, "bad_request", str(exc))
        try:
            result = await self._handler(method, params)
            reply = _reply_ok(req_id, result)
        except RpcError as exc:
            return _reply_err(req_id, exc.code, exc.message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the connection
            log.exception("control request %.60r failed", method)
            return _reply_err(req_id, "internal", f"{type(exc).__name__}: {str(exc)[:300]}")
        if len(reply) > MAX_LINE:
            # No client could read it (they apply the same limit), and each would fail in
            # its own way.  The handlers bound what they return; this is the last resort.
            log.error("the reply to %.60r is %d bytes, more than a message may have", method, len(reply))
            return _reply_err(req_id, "internal", "the reply is too large to send")
        return reply


class _Flood(Exception):
    """A request line went on and on (see IpcServer._read_request)."""


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


def _unreachable(path: Path | str, exc: OSError) -> ServiceUnavailable:
    """Why a connection to the socket at ``path`` could not be opened."""
    if isinstance(exc, PermissionError):
        return ServiceUnavailable(f"not allowed to use the tcl-me-remo service at {path}: {exc}", denied=True)
    if isinstance(exc, BlockingIOError):
        # What a Unix socket says when the listener has no room for another connection.
        return ServiceUnavailable(f"the tcl-me-remo service at {path} is not taking connections (busy or stuck)")
    return ServiceUnavailable(f"cannot reach the tcl-me-remo service at {path}: {exc}")


async def call(path: Path | str, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
    """Asynchronous client: one request, one reply.  Every failure is an :class:`RpcError`."""

    async def exchange() -> Any:
        try:
            reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_LINE)
        except OSError as exc:
            raise _unreachable(path, exc) from None
        try:
            writer.write(_encode_request(method, params))
            try:
                await writer.drain()
            except ConnectionError:
                pass  # the service may have said why before it closed: read what there is
            try:
                line = await reader.readline()
            except ValueError:  # what readline() makes of a line over the limit
                raise RpcError("bad_reply", "reply from the service is too large") from None
            return _decode_reply(line)
        except OSError as exc:
            raise ServiceUnavailable(f"the connection to the tcl-me-remo service broke: {exc}") from None
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
    """Blocking client: one request, one reply.  Uses only the ``socket`` module.

    Every failure is an :class:`RpcError`: ``unavailable`` when the service cannot be reached
    or the connection breaks, ``timeout``, ``bad_reply``, or the error the service answered with.
    """
    request = _encode_request(method, params)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            sock.settimeout(timeout)
        except (ValueError, OverflowError, TypeError):
            raise RpcError("bad_request", f"{timeout!r} is not a usable time-out") from None
        try:
            sock.connect(str(path))
        except TimeoutError:
            raise RpcError("timeout", f"the service did not accept the connection within {timeout:g}s") from None
        except OSError as exc:
            raise _unreachable(path, exc) from None
        try:
            try:
                sock.sendall(request)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the service may have said why before it closed: read what there is
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
        except OSError as exc:
            if not chunks:
                raise ServiceUnavailable(f"the connection to the tcl-me-remo service broke: {exc}") from None
        return _decode_reply(b"".join(chunks).split(b"\n", 1)[0])
    finally:
        sock.close()
