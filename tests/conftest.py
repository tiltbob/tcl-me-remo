import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def _private_bus_holder():
    return {}


@pytest.fixture
def kill_private_bus(private_bus, _private_bus_holder):
    """Returns a function that kills the private bus daemon (to test losing the system bus)."""

    def kill():
        proc = _private_bus_holder["proc"]
        proc.kill()
        proc.wait(timeout=5)

    return kill


@pytest.fixture
def private_bus(tmp_path, monkeypatch, _private_bus_holder):
    """A throw-away dbus-daemon standing in for the system bus.

    Yields its address and points DBUS_SYSTEM_BUS_ADDRESS at it, so code that connects to
    "the system bus" lands here.  Fake BlueZ / logind services can claim their well-known
    names on it because the session-bus policy lets anyone own any name.
    """
    socket_path = tmp_path / "bus.sock"
    proc = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1", f"--address=unix:path={socket_path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    _private_bus_holder["proc"] = proc
    try:
        address = proc.stdout.readline().decode().strip()
        if not address:
            pytest.skip("dbus-daemon is not available")
        monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", address)
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
        yield address
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.stdout:
            proc.stdout.close()
    assert os.environ.get("DBUS_SYSTEM_BUS_ADDRESS") == address
