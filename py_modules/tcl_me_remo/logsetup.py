"""Logging: journald-friendly stderr output plus an in-memory tail for the UI."""

from __future__ import annotations

import logging
import sys
import time
from collections import deque

RING_SIZE = 1000
# Longest line the ring keeps.  The ring is sent to clients (the "logs" request), so what it
# holds is bounded in both directions whatever ends up in a log message.
RING_LINE_CHARS = 500


class RingHandler(logging.Handler):
    """Keeps the most recent log lines so the Decky panel can show them without journalctl."""

    def __init__(self, capacity: int = RING_SIZE):
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            line = f"{stamp} {record.levelname:<7} {record.getMessage()}"
            if len(line) > RING_LINE_CHARS:
                line = line[: RING_LINE_CHARS - 3] + "..."
            self._lines.append(line)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def tail(self, count: int) -> list[str]:
        lines = list(self._lines)
        return lines[-count:] if count > 0 else []


def setup(level: str = "INFO") -> RingHandler:
    """Configure root logging once and return the ring buffer handler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler(sys.stderr)
    # journald adds its own timestamps; keep the line short.
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    ring = RingHandler()
    root.addHandler(stream)
    root.addHandler(ring)
    set_level(level)
    return ring


def set_level(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))
