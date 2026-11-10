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
number of clients at once, the time a connection may sit without a complete request or
without taking the replies it was sent, and the length of what is echoed or quoted back.
Whatever arrives gets one reply; nothing a client sends ends the connection handler with an
exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import socket
import stat
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import aio

MAX_LINE = 1 << 20  # 1 MiB per message
# Clients served at once.  The CLI, the plugin and the systemd hook each open a connection
# per request and close it; this is far more than they ever have open together.
MAX_CLIENTS = 32
# Seconds a connection may take to deliver a complete request line.  It bounds the wait for
# a request only: a request that is being handled takes as long as it takes (turning the TV
# on can take minutes), and the connection is not timed while it is.
IDLE_SECONDS = 30.0
# Seconds a connection that is being closed has to take what was written to it, and seconds
# a client that was turned away has to send the request it came with (the server waits for
# it, so that the client's write does not meet a closed socket and lose the answer).
CLOSE_SECONDS = 5.0
REFUSAL_LINGER = 1.0
# An error message is for a person; what a client sent is quoted cut short, never at length.
MAX_ERROR_CHARS = 2000
# The id of a request is echoed back; this many bytes of JSON are far more than a number or
# a name takes.
MAX_ID_BYTES = 1024
# An over-long request is read to its end and thrown away, so that the answer ("too large")
# reaches a client that is still busy sending; beyond this much the connection is closed.
MAX_DISCARD = 8 * MAX_LINE
#: Most opening brackets and braces a request may contain.  It bounds the nesting before the
#: parser sees the line: how deep json.loads goes before it gives up depends on the Python
#: version and on the stack the process happens to have (3.14 parses tens of thousands of
#: levels on some machines and none of them on others), and whatever parses can be echoed
#: back in the reply.  Real requests have a dozen; the count includes brackets inside text,
#: of which a macro of the greatest allowed length can have about a thousand.
MAX_BRACKETS = 2048

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
    # sensible value of anything here.
    raise ValueError(f"{name} is not valid JSON: numbers must be finite")


def _finite(text: str) -> float:
    # 1e999 is JSON, and Python reads it as an infinity: the same thing by another name.  It
    # is refused wherever it stands in a request (the id, which is echoed, included), since
    # no reply that holds one is JSON.  The handlers check the numbers they take all the
    # same: they have other callers.
    value = float(text)
    if math.isinf(value):
        raise ValueError(f"{text[:20]} is too large a number: numbers must be finite")
    return value


def _reply_ok(req_id: Any, result: Any) -> bytes:
    return (json.dumps({"id": req_id, "ok": True, "result": result}, separators=(",", ":")) + "\n").encode()


def _reply_err(req_id: Any, code: str, message: str) -> bytes:
    if len(message) > MAX_ERROR_CHARS:
        message = message[: MAX_ERROR_CHARS - 3] + "..."
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
        refusal_linger: float = REFUSAL_LINGER,
    ):
        self.path = Path(path)
        self._handler = handler
        self._mode = mode
        self._group = group
        self._max_clients = max_clients
        self._idle_timeout = idle_timeout
        self._refusal_linger = refusal_linger
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task[None]] = set()
        self._turned_away: set[asyncio.Task[None]] = set()  # being told "busy"; not served
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
        clients = [*self._clients, *self._turned_away]
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
        if task is not None:
            (self._turned_away if refused else self._clients).add(task)
        flush = True  # False: nobody is taking what is written, so it is dropped with the connection
        try:
            if refused:
                if not self._refusing:
                    self._refusing = True
                    log.warning("%d control clients are connected: turning further ones away", len(self._clients))
                writer.write(_reply_err(None, "busy", "too many clients are connected to the service; try again"))
                await self._send(writer)
                # The client is about to send its request, or has.  Closing before it did
                # makes its write fail, and a client that then stops (asyncio's do: the
                # Decky bridge) never reads the line above.  So take its request off its
                # hands first, or its goodbye; not for long, and not for a crowd.
                if len(self._turned_away) <= self._max_clients:
                    try:
                        await aio.wait_for(reader.readline(), self._refusal_linger)
                    except (asyncio.TimeoutError, ValueError):  # noqa: UP041 - nothing came / a line over the limit
                        pass
                return
            while True:
                try:
                    line = await aio.wait_for(self._read_request(reader), self._idle_timeout)
                except asyncio.TimeoutError:  # noqa: UP041
                    log.debug("closing a control connection that sent no request for %gs", self._idle_timeout)
                    break
                except _Flood:
                    writer.write(_reply_err(None, "bad_request", "message too large"))
                    await self._send(writer)
                    break
                if line is None:
                    writer.write(_reply_err(None, "bad_request", "message too large"))
                    await self._send(writer)
                    continue
                if not line:
                    break
                if not line.strip():
                    continue
                # Not under the idle limit: a handler takes as long as it takes.
                writer.write(await self._dispatch(line))
                await self._send(writer)
        except _NotReading:
            log.debug("closing a control connection that did not take its replies for %gs", self._idle_timeout)
            flush = False
        except asyncio.CancelledError:
            flush = False  # the server is stopping
        except ConnectionError:
            pass
        except Exception:  # noqa: BLE001 - nothing a client does may end here with a traceback per request
            log.warning("control connection dropped", exc_info=True)
        finally:
            # The place is given up when the connection is really gone, so that the number
            # of clients is the number of open connections.
            await self._close(writer, flush=flush)
            if task is not None:
                self._turned_away.discard(task)
                if not refused:
                    self._clients.discard(task)
                    self._refusing = False

    async def _send(self, writer: asyncio.StreamWriter) -> None:
        """Wait until what was written is on its way.  Raises ``_NotReading`` when the client
        has not taken it within the idle limit.

        A client that sends requests and reads no answers would otherwise sit here for ever,
        on one of the few places there are.  Like the limit on the wait for a request this
        bounds the client and not the work: the handler has returned by now.
        """
        try:
            await aio.wait_for(writer.drain(), self._idle_timeout)
        except asyncio.TimeoutError:  # noqa: UP041
            raise _NotReading from None

    @staticmethod
    async def _close(writer: asyncio.StreamWriter, *, flush: bool) -> None:
        """Close the connection; never raises and never waits for long.

        A closing connection first delivers what is still to be written, and waits for a
        client that does not read as long as that client likes.  After CLOSE_SECONDS, or at
        once without ``flush``, the rest is dropped.
        """
        transport = writer.transport
        try:
            if flush:
                writer.close()
                await aio.wait_for(writer.wait_closed(), CLOSE_SECONDS)
            else:
                transport.abort()
        except (asyncio.TimeoutError, asyncio.CancelledError):  # noqa: UP041
            transport.abort()
        except Exception:  # noqa: BLE001 - the connection broke while closing: it is closed
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
        if line.count(b"[") + line.count(b"{") > MAX_BRACKETS:
            return _reply_err(None, "bad_request", "the request is nested too deeply")
        try:
            request = json.loads(line, parse_constant=_no_constant, parse_float=_finite)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            req_id = request.get("id")
            if not _echoable(req_id):
                req_id = None
                raise ValueError(f"the id of a request may take at most {MAX_ID_BYTES} bytes of JSON")
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
            # (The id and the message both have an upper length, so this one fits.)
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


def _echoable(req_id: Any) -> bool:
    """Whether ``req_id`` can go back in a reply: it must be short, and JSON again."""
    try:
        return len(json.dumps(req_id, allow_nan=False)) <= MAX_ID_BYTES
    except (ValueError, TypeError, RecursionError):
        return False


class _Flood(Exception):
    """A request line went on and on (see IpcServer._read_request)."""


class _NotReading(Exception):
    """A client did not take what was written to it (see IpcServer._send)."""


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
        return await aio.wait_for(exchange(), timeout)
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
        chunks: list[bytes] = []
        size = 0
        try:
            try:
                sock.sendall(request)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the service may have said why before it closed: read what there is
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
