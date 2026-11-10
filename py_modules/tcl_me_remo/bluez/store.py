"""Small persistent memory of the Bluetooth remote (one JSON file).

It holds what cannot be learnt again cheaply after a reboot:

* whether the paired TV wrote its notification subscription again the last time bluetoothd
  had lost it (Android 14+ does, Android 11-13 does not) - this decides how long the next
  boot waits before falling back to direct delivery;
* addresses of TVs whose bond BlueZ still holds although we no longer want it (the user
  asked to forget the TV while BlueZ could not be reached, or a pairing window ended after
  the TV had bonded but before it was adopted), so that the bond is removed later instead of
  silently blocking the next pairing (``JustWorksRepairing = never`` rejects pairing with a
  device BlueZ still knows);
* addresses of TVs that bonded in a pairing window which is still open ("pending"): should
  the service die before the window completes, the next start treats them as stale;
* whether packets were injected through the raw HCI socket whose effect on the kernel's
  counters has not been repaired yet (rawhci.py), together with the boot it happened in.

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
        self._pending: list[str] = []
        self._raw_debt = ""   # boot id the unsettled injected packets belong to, "" if none
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
        for name in ("stale", "pending"):
            items = data.get(name)
            if isinstance(items, list):
                setattr(self, f"_{name}", [item.upper() for item in items if isinstance(item, str)])
        debt = data.get("raw_debt")
        self._raw_debt = debt if isinstance(debt, str) else ""

    def _save(self) -> None:
        if self._path is None:
            return
        payload: dict[str, Any] = {
            "version": VERSION,
            "tv": self._tv,
            "resubscribes": self._resubscribes,
            "stale": self._stale,
            "pending": self._pending,
            "raw_debt": self._raw_debt,
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

    @property
    def tv(self) -> str:
        """Address of the TV the hint belongs to: the TV this service last paired with or used."""
        return self._tv

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

    # ------------------------------------------------------------------ bonds of an unfinished pairing

    @property
    def pending(self) -> list[str]:
        return list(self._pending)

    def add_pending(self, address: str) -> None:
        address = address.upper()
        if address and address not in self._pending:
            self._pending.append(address)
            self._save()

    def discard_pending(self, address: str) -> None:
        address = address.upper()
        if address in self._pending:
            self._pending.remove(address)
            self._save()

    def pending_to_stale(self) -> None:
        """Whatever bonded in a window that never completed is a bond to remove."""
        if not self._pending:
            return
        for address in self._pending:
            if address not in self._stale:
                self._stale.append(address)
        self._pending = []
        self._save()

    # ------------------------------------------------------------------ raw delivery debt

    def raw_debt(self, boot: str) -> bool:
        """Whether injected packets from the boot ``boot`` are still to be settled."""
        return bool(self._raw_debt) and self._raw_debt == (boot or "unknown")

    def set_raw_debt(self, boot: str) -> None:
        boot = boot or "unknown"
        if self._raw_debt != boot:
            self._raw_debt = boot
            self._save()

    def clear_raw_debt(self) -> None:
        if self._raw_debt:
            self._raw_debt = ""
            self._save()
