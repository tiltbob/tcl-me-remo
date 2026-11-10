"""Facts about specific TVs that cannot be derived, only collected.

Everything here comes from owners' reports (mostly Home Assistant users), not from TCL, and
none of it has been verified by this project on real hardware.  Treat each entry as a
starting point to test from the UI, and prefer adding a new entry over changing a reported one.
"""

from __future__ import annotations

from dataclasses import dataclass

_PASSTHROUGH = "content://android.media.tv/passthrough/"


@dataclass(frozen=True)
class LinkPreset:
    """App links that make a TV show a given HDMI input (for the Wi-Fi assist)."""

    name: str
    title: str
    description: str
    links: dict[int, str]  # HDMI port number -> app link


def _tcl(service: str, ids: dict[int, str]) -> dict[int, str]:
    # "/" inside the TV-input id must stay percent-encoded.
    return {port: f"{_PASSTHROUGH}com.tcl.tvinput%2F{service}%2F{hw}" for port, hw in ids.items()}


_REALTEK_IDS = {1: "HW1413744128", 2: "HW1413744384", 3: "HW1413744640", 4: "HW1413745664"}

LINK_PRESETS: dict[str, LinkPreset] = {
    preset.name: preset
    for preset in (
        LinkPreset(
            name="tcl-mediatek",
            title="TCL, MediaTek models",
            description="Reported for C805, C849, C855, C7K and 98P74; the likely match for QM6K/QM7/QM8 and Q7.",
            links=_tcl(".TvPassThroughService", {1: "HW15", 2: "HW16", 3: "HW17", 4: "HW18"}),
        ),
        LinkPreset(
            name="tcl-realtek",
            title="TCL, Realtek models",
            description="Reported for C825 and C645; the likely match for Q5 and Q6 (Q550G, Q650G).",
            links=_tcl(".passthroughinput.TvPassThroughService", _REALTEK_IDS),
        ),
        LinkPreset(
            name="tcl-realtek-alt",
            title="TCL, Realtek models (older naming)",
            description="Reported for C635 (HDMI 1 and 2 confirmed; 3 and 4 follow the same pattern).",
            links=_tcl(".passthrough.TvPassThroughService", _REALTEK_IDS),
        ),
    )
}


def link_preset_catalog() -> list[dict[str, object]]:
    return [
        {
            "name": preset.name,
            "title": preset.title,
            "description": preset.description,
            "links": {str(port): link for port, link in preset.links.items()},
        }
        for preset in LINK_PRESETS.values()
    ]


# --------------------------------------------------------------------------- wake beacon
#
# MediaTek TV platforms arm a filter in the Bluetooth controller before suspending and wake
# when an advertisement with matching manufacturer data arrives.  MediaTek's reference driver
# (btmtk, "WoBLE by BLE RC") uses this default: company id 0x0046, one ignored byte, the TV's
# own Bluetooth address, four ignored bytes, then the ASCII text "CRKTM".  A product can
# replace the pattern; what TCL ships is not public.

MEDIATEK_COMPANY_ID = 0x0046


def mediatek_wake_data(tv_address: str) -> bytes:
    """Manufacturer data (without the company id) matching MediaTek's default wake filter."""
    octets = bytes(int(part, 16) for part in tv_address.split(":"))
    if len(octets) != 6:
        raise ValueError(f"not a Bluetooth address: {tv_address!r}")
    # The driver copies the address as the controller reports it: least significant byte first.
    return b"\x00" + octets[::-1] + b"\x00\x00\x00\x00" + b"CRKTM"


def wake_beacon(mode: str, custom_hex: str, tv_address: str) -> tuple[int, bytes] | None:
    """``(company_id, data)`` to broadcast for the configured wake beacon, or None for none."""
    if mode == "mediatek" and tv_address:
        return MEDIATEK_COMPANY_ID, mediatek_wake_data(tv_address)
    if mode == "custom" and custom_hex:
        raw = bytes.fromhex(custom_hex)
        return int.from_bytes(raw[:2], "little"), raw[2:]
    return None
