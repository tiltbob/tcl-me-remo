"""Small helpers shared by the Wi-Fi assist modules."""

from __future__ import annotations

MAX_NAME = 64


def clean_name(text: str, limit: int = MAX_NAME) -> str:
    """Make a name that came from the network safe to log and display.

    Control and other non-printable characters become spaces, runs of white space are
    collapsed, and the result is cut to ``limit`` characters.
    """
    printable = "".join(char if char.isprintable() else " " for char in text[: limit * 4])
    return " ".join(printable.split())[:limit].strip()


def describe(exc: BaseException) -> str:
    """A short reason for an operating system error, without paths, addresses or reprs."""
    reason = getattr(exc, "strerror", None)
    if isinstance(reason, str) and reason:
        return reason[:1].lower() + reason[1:]
    return "connection lost" if isinstance(exc, ConnectionError) else "system error"
