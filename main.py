"""Decky Loader backend for tcl-me-remo.

Deliberately thin.  The work is done by a system service that keeps running when Decky or
Steam restarts; this file only

  * installs / repairs / updates that service when the plugin loads, and
  * forwards the panel's requests to the service's control socket.

It uses nothing beyond asyncio and json and runs the installer with the system Python, so it
does not depend on which standard-library modules Decky's bundled interpreter happens to ship.
"""

import asyncio
import json
import os

import decky

PLUGIN_DIR = decky.DECKY_PLUGIN_DIR
SOCKET_PATH = "/run/tcl-me-remo/control.sock"
SYSTEM_PYTHON = "/usr/bin/python3"
MAX_REPLY = 1 << 20

# Runs the installer module from a given py_modules directory.  -I: no user site-packages,
# no environment influence; this runs as root.
INSTALLER_SNIPPET = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from tcl_me_remo import installer; sys.exit(installer.main(sys.argv[2:]))"
)

# How long the panel waits for each kind of request.  Actions that talk to the TV can take a
# while (connect, wait for the TV to start, play a macro).
TIMEOUTS = {
    "tv_on": 200.0,
    "switch_input": 200.0,
    "run_macro": 200.0,
    "event": 240.0,
    "connect": 150.0,
    "tv_off": 45.0,
    "send_key": 30.0,
    "wifi_discover": 20.0,
    "wifi_test": 20.0,
    "wifi_pair_start": 30.0,
    "wifi_pair_finish": 30.0,
}
DEFAULT_TIMEOUT = 15.0
# Seconds after an uninstall hook before checking whether the plugin is really gone.
ORPHAN_CHECK_DELAY = 180
# Where the installer puts the service's code (first choice, then its fallback).
INSTALLED_MODULES = ("/opt/tcl-me-remo/py_modules", "/var/lib/tcl-me-remo/app/py_modules")


def _clean_env():
    """Environment for system tools: without the loader's bundled library path."""
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    env.pop("LD_LIBRARY_PATH", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    return env


async def _run(argv, timeout):
    """Run a command; returns (exit_code, stdout, stderr).  Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_clean_env(),
        )
    except OSError as exc:
        return -1, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"timed out after {timeout:g}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _installer(*args, timeout=120.0):
    """Run the bundled installer; returns its JSON result, or {"error": ...}."""
    argv = [SYSTEM_PYTHON, "-I", "-c", INSTALLER_SNIPPET, os.path.join(PLUGIN_DIR, "py_modules"), "--json", *args]
    code, out, err = await _run(argv, timeout)
    lines = [line for line in out.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1])
        if isinstance(result, dict):
            return result
    except (IndexError, ValueError):
        pass
    detail = (err or out).strip().splitlines()
    return {"error": detail[-1] if detail else f"the installer failed (exit code {code})"}


async def _socket_call(method, params, timeout):
    """One request to the service's control socket; raises OSError / ValueError on failure."""
    reader, writer = await asyncio.open_unix_connection(SOCKET_PATH, limit=MAX_REPLY)
    try:
        request = {"id": 1, "method": method, "params": params or {}}
        writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    if not line:
        raise ConnectionError("the service closed the connection without answering")
    reply = json.loads(line)
    if not isinstance(reply, dict):
        raise ValueError("unreadable reply from the service")
    return reply


def _installed_modules():
    """The py_modules directory of the installed service (not the plugin's own copy)."""
    for path in INSTALLED_MODULES:
        if os.path.isdir(os.path.join(path, "tcl_me_remo")):
            return path
    return INSTALLED_MODULES[0]


def _failure(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


class Plugin:
    def __init__(self):
        # Set here, not in _main: the panel can call in before _main has run.
        self._install_lock = asyncio.Lock()
        self._install_error = ""
        self._startup = None

    async def _main(self):
        decky.logger.info("tcl-me-remo plugin loaded from %s", PLUGIN_DIR)
        # Do not hold up the loader: install or update the service in the background.
        self._startup = asyncio.ensure_future(self._ensure_service())

    async def _unload(self):
        # The service keeps running without the plugin; that is the point of having one.
        decky.logger.info("tcl-me-remo plugin unloaded (the service keeps running)")

    async def _uninstall(self):
        # Decky calls this for updates as well as removals, and kills us 5 seconds after asking
        # us to stop.  So nothing is removed here: a timer checks later whether the plugin is
        # really gone and only then removes the service.  Settings and the TV pairing are kept
        # either way.
        modules = _installed_modules()
        argv = [
            "systemd-run",
            f"--on-active={ORPHAN_CHECK_DELAY}",
            "--collect",
            "--quiet",
            "--description=tcl-me-remo: remove the service if the plugin is gone",
            SYSTEM_PYTHON, "-I", "-c", INSTALLER_SNIPPET, modules,
            "uninstall-if-orphaned", PLUGIN_DIR,
        ]  # fmt: skip
        code, _out, err = await _run(argv, 3.0)
        if code != 0:
            decky.logger.warning("could not schedule the clean-up check: %s", err.strip())

    async def _ensure_service(self):
        async with self._install_lock:
            result = await _installer("install", "--user-home", decky.DECKY_USER_HOME)
        if "error" in result:
            self._install_error = str(result["error"])
            decky.logger.error("service install failed: %s", self._install_error)
        else:
            self._install_error = ""
            for line in result.get("log", []):
                decky.logger.info("service: %s", line)
        return result

    # ------------------------------------------------------------------ called by the panel

    async def rpc(self, method: str, params: dict | None = None) -> dict:
        """Forward one request to the service.  Always returns {"ok": ...}; never raises."""
        if not isinstance(method, str) or not isinstance(params or {}, dict):
            return _failure("bad_request", "method must be text and params an object")
        timeout = TIMEOUTS.get(method, DEFAULT_TIMEOUT)
        try:
            reply = await asyncio.wait_for(_socket_call(method, params, timeout), timeout)
        except TimeoutError:
            return _failure("timeout", f"the service did not answer '{method}' within {timeout:g}s")
        except (OSError, ValueError) as exc:
            return _failure("unavailable", f"cannot reach the tcl-me-remo service: {exc}")
        if reply.get("ok"):
            return {"ok": True, "result": reply.get("result")}
        error = reply.get("error") or {}
        return _failure(str(error.get("code", "error")), str(error.get("message", "unknown error")))

    async def service_info(self) -> dict:
        """What is installed and whether it runs (see ServiceInfo in src/types.ts)."""
        state = await _installer("status", timeout=20.0)
        if "error" in state:
            state = {
                "installed": False,
                "active": False,
                "enabled": False,
                "installed_version": "",
                "bundled_version": "",
                "needs_update": False,
                "problems": [str(state["error"])],
            }
        if self._install_error:
            state.setdefault("problems", []).insert(0, f"install failed: {self._install_error}")
        return state

    async def install_service(self) -> dict:
        """Install, update or repair the service."""
        result = await self._ensure_service()
        if "error" in result:
            return _failure("install_failed", str(result["error"]))
        return {"ok": True, "result": result}

    async def remove_service(self, purge: bool = False) -> dict:
        """Stop and remove the service; with purge also its settings and state."""
        async with self._install_lock:
            result = await _installer("uninstall", *(["--purge"] if purge else []))
        if "error" in result:
            return _failure("uninstall_failed", str(result["error"]))
        return {"ok": True, "result": result}

    async def restart_service(self) -> dict:
        code, out, err = await _run(["systemctl", "restart", "tcl-me-remo.service"], 60.0)
        if code != 0:
            return _failure("restart_failed", (err or out).strip() or "systemctl restart failed")
        return {"ok": True, "result": {}}
