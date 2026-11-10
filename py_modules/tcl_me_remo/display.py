"""Display detection through sysfs, used to gate TV actions.

A handheld that is woken on the sofa must not switch the living-room TV on.  The gate looks
at the kernel's DRM connectors (``/sys/class/drm/card0-HDMI-A-1`` ...) which works the same
in Game Mode, Desktop Mode and before any session has started.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import DisplayConfig

# Connector type prefixes of panels that are built into the machine.
INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")
# Connector types that are not real displays.
IGNORED_PREFIXES = ("Virtual", "Writeback")

_CONNECTOR_RE = re.compile(r"^(card\d+)-(.+)$")


@dataclass(frozen=True)
class Connector:
    name: str        # e.g. "HDMI-A-1"
    card: str        # e.g. "card0"
    status: str      # "connected", "disconnected" or "unknown"
    internal: bool
    maker: str = ""  # EDID manufacturer (PNP) id, e.g. "TCL"
    model: str = ""  # EDID monitor name, e.g. "TCL TV"

    @property
    def connected(self) -> bool:
        return self.status == "connected"

    @property
    def label(self) -> str:
        ident = " ".join(part for part in (self.maker, self.model) if part)
        return f"{self.name} ({ident})" if ident else self.name


@dataclass(frozen=True)
class DisplayState:
    connectors: tuple[Connector, ...]

    @property
    def has_internal(self) -> bool:
        return any(c.internal for c in self.connectors)

    @property
    def external_connected(self) -> tuple[Connector, ...]:
        return tuple(c for c in self.connectors if c.connected and not c.internal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_internal": self.has_internal,
            "connectors": [dict(asdict(c), connected=c.connected, label=c.label) for c in self.connectors],
        }


@dataclass(frozen=True)
class Gate:
    ok: bool
    reason: str


def parse_edid(edid: bytes) -> tuple[str, str]:
    """Return ``(manufacturer_id, monitor_name)`` from a raw EDID blob ("" when absent)."""
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return "", ""
    word = (edid[8] << 8) | edid[9]
    letters = [(word >> shift) & 0x1F for shift in (10, 5, 0)]
    maker = "".join(chr(ord("A") + n - 1) for n in letters) if all(1 <= n <= 26 for n in letters) else ""
    model = ""
    for offset in (54, 72, 90, 108):
        block = edid[offset : offset + 18]
        if block[0:3] == b"\x00\x00\x00" and block[3] == 0xFC:
            raw = block[5:18].split(b"\n", 1)[0]
            model = raw.decode("ascii", "replace").strip()
            break
    return maker, model


def read_display_state(sys_root: Path | str = "/sys") -> DisplayState:
    """Enumerate DRM connectors.  Never raises: an unreadable sysfs yields no connectors."""
    base = Path(sys_root) / "class" / "drm"
    found: list[Connector] = []
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return DisplayState(connectors=())
    for entry in entries:
        match = _CONNECTOR_RE.match(entry.name)
        if not match:
            continue
        card, name = match.groups()
        if name.startswith(IGNORED_PREFIXES):
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            continue
        maker = model = ""
        if status == "connected":
            try:
                maker, model = parse_edid((entry / "edid").read_bytes())
            except OSError:
                pass
        found.append(
            Connector(
                name=name,
                card=card,
                status=status,
                internal=name.startswith(INTERNAL_PREFIXES),
                maker=maker,
                model=model,
            )
        )
    return DisplayState(connectors=tuple(found))


def evaluate(cfg: DisplayConfig, state: DisplayState) -> Gate:
    """Decide whether TV actions may run given the connected displays."""
    mode = cfg.require_external
    needle = cfg.edid_match.strip().lower()
    if mode == "no":
        return Gate(True, "display check is off")

    externals = state.external_connected
    if mode == "auto" and not state.has_internal and not needle:
        return Gate(True, "no built-in screen, so the TV is assumed to be the display")

    if not externals:
        if mode == "auto" and not state.has_internal:
            return Gate(False, f"no connected display matches {cfg.edid_match!r}")
        return Gate(False, "no external display is connected")

    if needle:
        for connector in externals:
            if needle in f"{connector.maker} {connector.model}".lower():
                return Gate(True, f"{connector.label} is connected")
        seen = ", ".join(c.label for c in externals)
        return Gate(False, f"no connected display matches {cfg.edid_match!r} (connected: {seen})")

    return Gate(True, f"{externals[0].label} is connected")


async def wait_for_gate(
    cfg: DisplayConfig,
    *,
    sys_root: Path | str = "/sys",
    poll: float = 0.5,
) -> Gate:
    """Evaluate the gate, retrying for up to ``cfg.wait_seconds`` while it is closed.

    Right after boot or resume the dock's display can take a few seconds to be detected.
    """
    deadline = time.monotonic() + max(0.0, cfg.wait_seconds)
    while True:
        gate = evaluate(cfg, read_display_state(sys_root))
        if gate.ok or time.monotonic() >= deadline:
            return gate
        await asyncio.sleep(poll)
