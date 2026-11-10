"""Small persistent memory of the Bluetooth remote (one JSON file).

It holds what cannot be learnt again cheaply after a reboot:

* whether the paired TV wrote its notification subscription again the last time bluetoothd
  had lost it (Android 14+ does, Android 11-13 does not) - this decides how long the next
  boot waits before falling back to direct delivery;
* addresses of TVs the user asked to forget while BlueZ could not be reached, so that the
  stale bond is removed later instead of silently blocking a new pairing
  (``JustWorksRepairing = never`` rejects pairing with a device BlueZ still knows).

Reads tolerate a missing or damaged file; writes are atomic and never raise.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1


class StateStore:
    def __init__(self, path: Path | None):
        self._path = Path(path) if path is not None else None
        self._tv = ""
        self._resubscribes: bool | None = None
        self._stale: list[str] = []
        self._load()

    # ------------------------------------------------------------------ file

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self._path, exc)
            return
        if not isinstance(data, dict):
            log.warning("ignoring malformed state file %s", self._path)
            return
        tv = data.get("tv")
        self._tv = tv.upper() if isinstance(tv, str) else ""
        hint = data.get("resubscribes")
        self._resubscribes = hint if isinstance(hint, bool) else None
        stale = data.get("stale")
        if isinstance(stale, list):
            self._stale = [item.upper() for item in stale if isinstance(item, str)]

    def _save(self) -> None:
        if self._path is None:
            return
        payload: dict[str, Any] = {
            "version": VERSION,
            "tv": self._tv,
            "resubscribes": self._resubscribes,
            "stale": self._stale,
        }
        tmp = ""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=self._path.name + ".", suffix=".tmp", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("could not save %s: %s", self._path, exc)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------ re-subscription hint

    def resubscribes(self, tv_address: str) -> bool | None:
        """True / False as observed last time for this TV, None if never observed."""
        if not tv_address or tv_address.upper() != self._tv:
            return None
        return self._resubscribes

    def set_resubscribes(self, tv_address: str, value: bool | None) -> None:
        address = tv_address.upper()
        if not address or (address == self._tv and value == self._resubscribes):
            return
        self._tv, self._resubscribes = address, value
        self._save()

    def clear_tv(self) -> None:
        if self._tv or self._resubscribes is not None:
            self._tv, self._resubscribes = "", None
            self._save()

    # ------------------------------------------------------------------ bonds still to remove

    @property
    def stale(self) -> list[str]:
        return list(self._stale)

    def add_stale(self, address: str) -> None:
        address = address.upper()
        if address and address not in self._stale:
            self._stale.append(address)
            self._save()

    def discard_stale(self, address: str) -> None:
        address = address.upper()
        if address in self._stale:
            self._stale.remove(address)
            self._save()
