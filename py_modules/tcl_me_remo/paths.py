"""Filesystem locations, overridable through the environment (used by tests and packaging)."""

from __future__ import annotations

import os
from pathlib import Path


def config_path() -> Path:
    # Not under /etc: SteamOS wipes everything there except a fixed keep list on OS updates,
    # while /var is carried over.
    return Path(os.environ.get("TCL_ME_REMO_CONFIG", "/var/lib/tcl-me-remo/config.json"))


def state_dir() -> Path:
    """Persistent private state (Wi-Fi pairing certificate, last-known TV state)."""
    return Path(os.environ.get("TCL_ME_REMO_STATE_DIR", "/var/lib/tcl-me-remo"))


def runtime_dir() -> Path:
    """Volatile state that must not survive a reboot (control socket, boot marker)."""
    return Path(os.environ.get("TCL_ME_REMO_RUNTIME_DIR", "/run/tcl-me-remo"))


def socket_path() -> Path:
    return Path(os.environ.get("TCL_ME_REMO_SOCKET", str(runtime_dir() / "control.sock")))
